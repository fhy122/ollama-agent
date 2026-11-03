"""Web UI 冒烟测试（离线，不依赖 Ollama）。

验证：静态页、健康检查、配置读改、上传、NDJSON 流事件协议、发射器接口。
"""
from __future__ import annotations

import base64
import io
import json
import os
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# 测试不要碰真实的 ~/.ollama-agent/sessions
os.environ.setdefault("OLLAMA_AGENT_SESSIONS", tempfile.mkdtemp(prefix="oa-sessions-"))

from agent.config import AgentConfig
from webui.stream import StreamEmitter


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return r.status, r.read()


def _post(url, obj):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read()


def main():
    # ---- StreamEmitter 协议 ----
    buf = io.BytesIO()
    em = StreamEmitter(buf)
    em.step(1)
    em.text("你好")
    em.think("想想")
    em.tool_call("read_file", {"path": "a.txt"})
    em.tool_result("read_file", "内容", 1.23, images=["abc"])
    em.confirm("c7", 'delete_file({"path":"x"})')
    em.stats({"prompt_eval_count": 10, "eval_count": 5, "total_duration": 2e9})
    em.notice("提示")
    lines = [json.loads(x) for x in buf.getvalue().decode("utf-8").strip().split("\n")]
    types = [x["type"] for x in lines]
    assert types == ["step", "text", "think", "tool_call", "tool_result",
                     "confirm", "stats", "notice"], types
    assert lines[3]["args"] == {"path": "a.txt"}
    assert lines[4]["images"] == ["abc"]
    assert lines[5]["id"] == "c7" and "delete_file" in lines[5]["question"]
    print("PASS: StreamEmitter NDJSON 协议（8 类事件，含 confirm）")

    # ---- 起服务（随机端口） ----
    import socket
    from http.server import ThreadingHTTPServer
    from webui.server import WebUIApp, make_handler

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    app = WebUIApp(AgentConfig(use_color=False, warm=False), port=port)
    app.start_hotkey = lambda: False
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"

    try:
        # 静态页
        st, body = _get(base + "/")
        assert st == 200 and b"Ollama Agent" in body and b"app.js" in body
        st, js = _get(base + "/static/app.js")
        assert st == 200 and b"sendMessage" in js
        st, css = _get(base + "/static/style.css")
        assert st == 200 and b"--accent" in css
        print("PASS: 静态页 / app.js / style.css")

        # 健康检查 + 配置
        st, body = _get(base + "/api/health")
        assert json.loads(body)["ok"] is True
        st, body = _get(base + "/api/config")
        cfg = json.loads(body)
        assert "model" in cfg and "think" in cfg and "tools" in cfg and cfg["think"] == "off"
        assert len(cfg["tools"]) >= 8, cfg["tools"]
        print(f"PASS: /api/health + /api/config（think={cfg['think']}，tools={len(cfg['tools'])}）")

        # 配置热更新（不打 Ollama）
        st, body = _post(base + "/api/config", {"think": "on", "max_steps": 6, "temperature": 0.5})
        cfg2 = json.loads(body)
        assert cfg2["think"] == "on" and cfg2["max_steps"] == 6 and cfg2["temperature"] == 0.5
        st, body = _post(base + "/api/config", {"think": "off", "max_steps": 12, "temperature": 0.7})
        print("PASS: /api/config 热更新（think/max_steps/temperature）")

        # 上传
        payload = base64.b64encode("hello ui".encode("utf-8")).decode("ascii")
        st, body = _post(base + "/api/upload", {"name": "测试文档.txt", "data": payload})
        up = json.loads(body)
        assert Path(up["path"]).read_text(encoding="utf-8") == "hello ui", up
        Path(up["path"]).unlink()
        print("PASS: /api/upload（中文名上传到 .uploads/）")

        # 空消息聊天 → 流里有 done 事件（不触发模型）
        st, body = _post(base + "/api/chat", {"message": ""})
        events = [json.loads(x) for x in body.decode("utf-8").strip().split("\n") if x]
        assert events and events[-1]["type"] == "done", events
        print("PASS: /api/chat 空消息 → notice+done（不触发模型）")

        # 剪贴板暂存轮询（空队列）
        st, body = _get(base + "/api/pending")
        assert json.loads(body)["items"] == []
        app.session.push_clip({"files": ["a.txt"], "image": None})
        st, body = _get(base + "/api/pending")
        items = json.loads(body)["items"]
        assert items and items[0]["files"] == ["a.txt"]
        print("PASS: /api/pending 热键暂存队列存取")

        # ---- 执行确认（安全口） ----
        st, body = _get(base + "/api/config")
        cfg = json.loads(body)
        assert cfg["confirm"] is True, "安全口应默认开启"

        st, body = _post(base + "/api/config", {"confirm": False})
        assert json.loads(body)["confirm"] is False
        st, body = _post(base + "/api/config", {"confirm": True})
        assert json.loads(body)["confirm"] is True
        print("PASS: confirm 开关热更新（默认开）")

        # 闸门：放行 / 拒绝 / 未知 id / 本回合全部允许
        from webui.session import ConfirmGate
        gate = ConfirmGate()
        gate.timeout = 5

        # 放行
        got = {}
        def ask_ok():
            got["v"] = gate.ask("write_file({\"path\": \"a\"})", lambda cid, q: got.update(cid=cid, q=q))
        t2 = threading.Thread(target=ask_ok, daemon=True)
        t2.start()
        for _ in range(100):
            if "cid" in got:
                break
            time.sleep(0.01)
        assert "cid" in got and "write_file" in got["q"]
        assert gate.answer(got["cid"], True) is True
        t2.join(5)
        assert got["v"] is True, "应答放行后 ask 应返回 True"

        # 拒绝
        got2 = {}
        def ask_no():
            got2["v"] = gate.ask("delete_file({\"path\": \"b\"})", lambda cid, q: got2.update(cid=cid))
        t3 = threading.Thread(target=ask_no, daemon=True)
        t3.start()
        for _ in range(100):
            if "cid" in got2:
                break
            time.sleep(0.01)
        assert gate.answer(got2["cid"], False) is True
        t3.join(5)
        assert got2["v"] is False, "拒绝后 ask 应返回 False"

        # 未知/过期 id
        assert gate.answer("nope", True) is False
        st, body = _post(base + "/api/confirm", {"id": "ghost", "approved": True})
        assert json.loads(body)["ok"] is False, "HTTP /api/confirm 对未知 id 应报 False"

        # 本回合全部允许：第一发 allow_rest 后直接放行
        got3 = {}
        def ask_rest():
            got3["v"] = gate.ask("x", lambda cid, q: got3.update(cid=cid))
        t4 = threading.Thread(target=ask_rest, daemon=True)
        t4.start()
        for _ in range(100):
            if "cid" in got3:
                break
            time.sleep(0.01)
        gate.answer(got3["cid"], True, allow_rest=True)
        t4.join(5)
        assert got3["v"] is True
        assert gate.ask("y", lambda cid, q: None) is True, "allow_rest 后续应直接放行"
        gate.reset()
        assert gate.allow_rest is False

        # HTTP 全链路：会话闸门提问 → /api/confirm 应答放行
        got5 = {}
        def ask_http():
            got5["v"] = app.session.gate.ask(
                'write_file({"path": "c"})', lambda cid, q: got5.update(cid=cid))
        t6 = threading.Thread(target=ask_http, daemon=True)
        t6.start()
        for _ in range(100):
            if "cid" in got5:
                break
            time.sleep(0.01)
        assert "cid" in got5
        st, body = _post(base + "/api/confirm",
                         {"id": got5["cid"], "approved": True, "allow_rest": False})
        assert json.loads(body)["ok"] is True, body
        t6.join(5)
        assert got5["v"] is True, "HTTP 应答后 ask 应放行"
        print("PASS: /api/confirm HTTP 全链路（提问→应答→放行）")

        # 超时自动拒绝
        gate.timeout = 0.2
        got4 = {}
        def ask_to():
            got4["v"] = gate.ask("z", lambda cid, q: got4.update(cid=cid))
        t5 = threading.Thread(target=ask_to, daemon=True)
        t5.start()
        t5.join(3)
        assert got4["v"] is False and gate.last_timeout is True, "超时应自动拒绝"
        print("PASS: ConfirmGate 放行/拒绝/过期id/全部允许/超时拒绝")

        # ---- 打断（/api/stop + cancel_event + 断连回调） ----
        st, body = _post(base + "/api/stop", {})
        assert json.loads(body)["ok"] is True
        assert app.session.cancel_event.is_set(), "request_stop 应置位 cancel_event"
        app.session.cancel_event.clear()

        # 闸门 reject_all：唤醒等待中的确认并按拒绝处理
        from webui.session import ConfirmGate as CG2
        gate_r = CG2()
        gate_r.timeout = 10
        got_r = {}
        def ask_r():
            got_r["v"] = gate_r.ask("write_file({\"path\": \"r\"})",
                                    lambda cid, q: got_r.update(cid=cid))
        tr = threading.Thread(target=ask_r, daemon=True)
        tr.start()
        for _ in range(100):
            if "cid" in got_r:
                break
            time.sleep(0.01)
        assert "cid" in got_r
        gate_r.reject_all()
        tr.join(5)
        assert got_r["v"] is False, "reject_all 应唤醒等待中的确认并拒绝"

        # 断连回调只触发一次
        class _Broken:
            def write(self, b):
                raise BrokenPipeError()
            def flush(self):
                pass
        hit = []
        em2 = StreamEmitter(_Broken(), on_disconnect=lambda: hit.append(1))
        em2.text("a")
        em2.text("b")
        em2.notice("c")
        assert hit == [1], f"断连回调应恰好触发一次: {hit}"
        print("PASS: /api/stop 打断 + 闸门 reject_all + 断连回调单次触发")

        # run_turn 取消：流式生成中途被打断 → 部分入历史 + notice + 不跑工具
        import threading as _th
        from agent.loop import run_turn
        from agent.registry import ToolContext

        class FakeStreamCancel:
            """模拟：流式输出到一半收到 /api/stop。"""
            def chat(self, messages, tools=None, on_text=None,
                     on_think=None, cancel=None):
                if on_text:
                    on_text("先说一半")
                cancel.set()   # ← 中途打断
                return {"thinking": "", "content": "先说一半", "tool_calls": [],
                        "stats": {}, "cancelled": True}

        cfg_t = AgentConfig(use_color=False, warm=False, no_tools=True)
        ctx_t = ToolContext(confirm=False, use_color=False, ask=None)
        hist_t = [{"role": "system", "content": "s"}]
        ev = _th.Event()
        buf = io.BytesIO()
        em_t = StreamEmitter(buf)
        out = run_turn(FakeStreamCancel(), cfg_t, ctx_t, hist_t, "hi",
                       emitter=em_t, cancel=ev)
        events = [json.loads(x) for x in buf.getvalue().decode("utf-8").strip().split("\n") if x]
        assert any(e["type"] == "notice" and "已打断" in e["text"] for e in events), events
        assert out == "先说一半", out
        assert hist_t[-1]["role"] == "assistant" and "打断" in hist_t[-1]["content"], hist_t

        # 工具执行前被打断：tool_calls 补占位结果，历史保持合法
        class FakeToolCancel:
            def chat(self, messages, tools=None, on_text=None,
                     on_think=None, cancel=None):
                cancel.set()   # ← 模型刚说完要调工具，用户此时打断
                return {"thinking": "", "content": "我要读文件",
                        "tool_calls": [{"function": {"name": "read_file",
                                                     "arguments": {"path": "a.txt"}}}],
                        "stats": {}, "cancelled": False}

        hist_t2 = [{"role": "system", "content": "s"}]
        ev2 = _th.Event()
        buf2 = io.BytesIO()
        run_turn(FakeToolCancel(), cfg_t, ctx_t, hist_t2, "hi",
                 emitter=StreamEmitter(buf2), cancel=ev2)
        assert hist_t2[-2]["role"] == "assistant" and hist_t2[-2].get("tool_calls"), hist_t2
        assert hist_t2[-1]["role"] == "tool" and "未执行" in hist_t2[-1]["content"], hist_t2
        events2 = [json.loads(x) for x in buf2.getvalue().decode("utf-8").strip().split("\n") if x]
        assert any(e["type"] == "notice" and "已打断" in e["text"] for e in events2), events2
        print("PASS: run_turn cancel（流中打断入历史 / 工具前打断补占位）")

        # client.chat 流式中断：cancel 置位后丢弃残缺 tool_calls、关闭连接
        from agent.client import OllamaClient

        class _FakeResp:
            def __init__(self, lines):
                self.lines = lines
                self.closed = False
            def __iter__(self):
                return iter(self.lines)
            def close(self):
                self.closed = True

        cli = OllamaClient.__new__(OllamaClient)
        cli.cfg = AgentConfig(use_color=False, warm=False)
        cli.host = "http://127.0.0.1:0"
        cli.model = "m"
        cli.think = False
        cli.num_ctx = None
        cli.temperature = 0.7
        fake = _FakeResp([
            json.dumps({"message": {"content": "你"}}).encode(),
            json.dumps({"message": {"content": "好",
                                    "tool_calls": [{"function": {"name": "read_file",
                                                                  "arguments": {}}}]}},
                       ).encode(),
            json.dumps({"message": {"content": "呀"}, "done": True,
                        "eval_count": 3}).encode(),
        ])
        cli._post_stream = lambda messages, tools: fake
        ev2 = _th.Event()
        got_c = {}
        def on_text(t):
            got_c["n"] = got_c.get("n", 0) + 1
            if got_c["n"] >= 2:
                ev2.set()   # 第二个 chunk（含 tool_calls）收完后打断
        # cancel 由外部线程随时置位（模拟 /api/stop）
        res = cli.chat([{"role": "user", "content": "x"}],
                       on_text=on_text, cancel=ev2)
        assert res["cancelled"] is True and fake.closed is True
        assert res["tool_calls"] == [], "打断后残缺 tool_calls 应弃用"
        assert res["content"] == "你好", res
        print("PASS: client.chat cancel（流中断 → cancelled + 关连接 + 弃 tool_calls）")

        # /api/skills 技能清单
        st, body = _get(base + "/api/skills")
        sk = json.loads(body)["skills"]
        assert isinstance(sk, list) and all("name" in s and "desc" in s for s in sk), sk
        print(f"PASS: /api/skills（{len(sk)} 个技能）")

        # /api/files 文件搜索（@ 上下文）
        st, body = _get(base + "/api/files?q=")
        files = json.loads(body)["files"]
        assert isinstance(files, list) and all("name" in f and "path" in f for f in files), files
        st, body = _get(base + "/api/files?q=readme")
        assert any("readme" in f["name"].lower() for f in json.loads(body)["files"]), "模糊搜索应命中 README"
        print(f"PASS: /api/files（根目录 {len(files)} 个文件，q=readme 模糊命中）")

        # /api/file 文件预览（文本 / 图片 / 表格 / 下载 / 越权 403 / 不存在 404）
        import shutil
        from pathlib import Path as _P

        def _get_any(url):
            """和 _get 一样，但 HTTP 错误码也返回而不抛异常。"""
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        fx = _P("_t_preview_test")
        fx.mkdir(exist_ok=True)
        try:
            (fx / "a.md").write_text("# 标题\n正文 `code`", encoding="utf-8")
            st, body = _get(base + "/api/file?path=_t_preview_test/a.md")
            d = json.loads(body)
            assert d["kind"] == "text" and d["markdown"] is True and "标题" in d["text"], d

            (fx / "b.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40)
            st, body = _get(base + "/api/file?path=_t_preview_test/b.png")
            d = json.loads(body)
            assert d["kind"] == "image" and d["b64"], d

            table_note = "无 openpyxl 跳过表格"
            try:
                from openpyxl import Workbook
                wb = Workbook()
                ws = wb.active
                ws.append(["列1", "列2"])
                ws.append(["a", "b"])
                wb.save(fx / "c.xlsx")
                st, body = _get(base + "/api/file?path=_t_preview_test/c.xlsx")
                d = json.loads(body)
                assert d["kind"] == "table" and d["rows"][0] == ["列1", "列2"], d
                table_note = "含表格"
            except ImportError:
                pass

            st, raw = _get(base + "/api/file?raw=1&path=_t_preview_test/a.md")
            assert raw.decode("utf-8").startswith("# 标题"), raw

            st, body = _get_any(base + "/api/file?path=../outside.txt")
            assert st == 403, (st, body)
            st, body = _get_any(base + "/api/file?path=_t_preview_test/none.md")
            assert st == 404, (st, body)
        finally:
            shutil.rmtree(fx, ignore_errors=True)
        print(f"PASS: /api/file（文本/图片/下载，{table_note}，越权拦截 403，缺失 404）")

        # /skill 斜杠命令（/api/chat 通道，不触发模型）
        st, body = _post(base + "/api/chat", {"message": "/skill no_such_skill_xyz"})
        events = [json.loads(x) for x in body.decode("utf-8").strip().split("\n") if x]
        assert events[-1]["type"] == "done", events
        assert any(e["type"] == "notice" and "未找到技能" in e["text"] for e in events), events
        st, body = _post(base + "/api/chat", {"message": "/reset"})
        events = [json.loads(x) for x in body.decode("utf-8").strip().split("\n") if x]
        assert any(e["type"] == "notice" and "已清空" in e["text"] for e in events), events
        print("PASS: /api/chat 斜杠命令（/skill 未命中报错、/reset 清空）")
    finally:
        httpd.shutdown()
        httpd.server_close()

    print("PASS: Web UI 冒烟全过")


if __name__ == "__main__":
    main()
