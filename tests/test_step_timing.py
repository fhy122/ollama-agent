"""每步时间窗测试（step_time 事件：开始/结束真实时刻 + 耗时 + TTFT）。

设计参考 DeepSeek Harness 的时间概览：数据层记录真实时刻（epoch 毫秒），
视图只做投影；步窗口 = 「第 N 步」分隔线到下一条分隔线（含全部工具执行）。

锁住的行为：
1. 每步一条 step_time 事件，步号正确、时刻单调、epoch 毫秒量级；
2. chat 返回的 ttft 透传进事件（没有则不带该键）；
3. 所有出口都闭合时间窗——最后一步（无工具直接回答）也有 step_time；
4. 底层 chat() 真的能算出 ttft（流式首 token 时刻），且 <= 总耗时。
"""
from __future__ import annotations

import http.server
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.backends.base import ChatClient  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.loop import run_turn  # noqa: E402
from agent.registry import ToolContext  # noqa: E402


class _Client:
    """两步回合：第 1 步调工具（无 ttft），第 2 步直接回答（带 ttft）。"""

    def __init__(self):
        self.calls = 0

    def chat(self, messages, tools=None, on_text=None, on_think=None,
             cancel=None, on_wait=None, think_hidden=True):
        self.calls += 1
        if self.calls == 1:
            return {"thinking": "", "content": "",
                    "tool_calls": [{"function": {"name": "list_files",
                                                 "arguments": {"path": "."}}}],
                    "stats": {"prompt_eval_count": 100, "eval_count": 20,
                              "total_duration": 2e9},
                    "cancelled": False, "error": None, "ttft": None}
        return {"thinking": "", "content": "好了",
                "tool_calls": [],
                "stats": {"prompt_eval_count": 500, "eval_count": 900,
                          "total_duration": 95e9},
                "cancelled": False, "error": None, "ttft": 1.25}


class _Em:
    def __init__(self):
        self.times = []

    def step(self, n): pass
    def step_time(self, step, t_start, t_end, ttft=None):
        self.times.append({"step": step, "t_start": t_start, "t_end": t_end,
                           "ttft": ttft})
    def text(self, t): pass
    def think(self, t): pass
    def newline(self): pass
    def tool_call(self, name, args): pass
    def tool_result(self, name, text, dt, images=None): pass
    def notice(self, t): pass
    def stats(self, st, step=0): pass
    def thinking(self, elapsed, chars=0): pass


def test_step_time_events():
    """每步一条 step_time：步号、毫秒量级、时刻单调、ttft 透传。"""
    cfg = AgentConfig(use_color=False, warm=False)
    ctx = ToolContext(use_color=False, workspace=Path("."))
    client, em = _Client(), _Em()
    run_turn(client, cfg, ctx, [{"role": "system", "content": "s"}], "hi",
             emitter=em)

    assert [t["step"] for t in em.times] == [1, 2], f"应两步各一条：{em.times}"
    for t in em.times:
        # 发射器收到的是 epoch 秒（毫秒转换在 StreamEmitter 里做，另测）
        assert 1.5e9 < t["t_start"] < 4e9, f"t_start 应为 epoch 秒：{t}"
        assert t["t_end"] >= t["t_start"], f"结束应不早于开始：{t}"
        assert t["t_end"] - t["t_start"] < 60, f"桩回合不应耗时过长：{t}"
    assert em.times[0].get("ttft") is None, "第 1 步无 ttft，不应带该值"
    assert em.times[1]["ttft"] == 1.25, f"第 2 步 ttft 应透传：{em.times[1]}"
    # 步窗口按时间顺序闭合
    assert em.times[0]["t_end"] <= em.times[1]["t_start"] + 1, \
        f"前一步应先闭合：{em.times}"
    print("PASS: run_turn 每步发 step_time（毫秒时刻 + ttft 透传）")


# ---------- 底层：chat() 真能测到首 token ----------

class EchoHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # 静音
        pass

    def do_POST(self):
        time.sleep(0.3)      # 首字节前留一点延迟，ttft 才 distinguishable
        body = b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n' \
               b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class MinimalClient(ChatClient):
    name = "test"

    def __init__(self, cfg, url):
        super().__init__(cfg)
        self._url = url

    def chat_url(self): return self._url
    def models_url(self): return self._url
    def build_payload(self, messages, tools=None):
        return {"model": self.model, "messages": messages}
    def parse_list_models(self, data): return []
    def new_parser(self):
        from agent.backends.openai_compat import OpenAIStreamParser
        return OpenAIStreamParser()


def test_chat_reports_ttft():
    """真流式请求：ttft 有值、> 0、且不超过总耗时。"""
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        port = srv.server_address[1]
        cfg = AgentConfig(host=f"http://127.0.0.1:{port}", model="m",
                          api_key="", timeout=30)
        cli = MinimalClient(cfg, f"http://127.0.0.1:{port}/v1/chat/completions")
        r = cli.chat([{"role": "user", "content": "hi"}])
        total = (r["stats"].get("total_duration") or 0) / 1e9
        assert r.get("ttft") is not None, f"应有 ttft：{r.get('ttft')}"
        assert 0 < r["ttft"] <= total + 0.05, \
            f"ttft={r['ttft']:.2f} 应在 (0, 总耗时 {total:.2f}] 内"
        assert r.get("content") == "hi", r
    finally:
        srv.shutdown()
    print("PASS: chat() 返回 ttft（首个可见 token 时刻）")


def test_stream_emitter_ms_conversion():
    """StreamEmitter.step_time：epoch 秒进 → epoch 毫秒出，ttft 可选。"""
    import io
    import json as _json

    from webui.stream import StreamEmitter

    class _W:
        def __init__(self):
            self.buf = b""

        def write(self, b):
            self.buf += b

        def flush(self): pass

    w = _W()
    em = StreamEmitter(w)
    em.step_time(3, 1793704904.0255, 1793704924.3355, ttft=1.256)
    ev = _json.loads(w.buf.split(b"\n")[0])
    assert ev["type"] == "step_time" and ev["step"] == 3
    assert ev["t_start"] == 1793704904025, f"应为整毫秒：{ev}"
    assert ev["t_end"] == 1793704924335, f"应为整毫秒：{ev}"
    assert ev["duration"] == 20.31, f"耗时应为秒两位：{ev}"
    assert ev["ttft"] == 1.26, f"ttft 应秒两位：{ev}"

    w2 = _W()
    StreamEmitter(w2).step_time(1, 1793704904.0, 1793704905.0)
    ev2 = _json.loads(w2.buf.split(b"\n")[0])
    assert "ttft" not in ev2, "没有 ttft 就不该带该键"
    print("PASS: StreamEmitter 毫秒转换与 ttft 可选键")


if __name__ == "__main__":
    test_step_time_events()
    test_chat_reports_ttft()
    test_stream_emitter_ms_conversion()
