"""多会话持久化测试（离线，不依赖 Ollama）。

覆盖：
  1. SessionStore 的建/存/列/删、标题生成、坏文件容错；
  2. AgentSession 记录回合事件并落盘，重开进程能还原 history 与 turns；
  3. 新建 / 切换 / 删除会话的行为（空会话复用、忙碌拒绝）；
  4. HTTP 接口 /api/sessions、/new、/open、/delete。

运行：python tests/test_sessions.py
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
TMP = tempfile.mkdtemp(prefix="oa-sessions-test-")
os.environ["OLLAMA_AGENT_SESSIONS"] = TMP

from agent.config import AgentConfig  # noqa: E402
import webui.session as WS  # noqa: E402
from webui.store import MAX_TURNS, SessionStore, make_title  # noqa: E402
from webui.stream import StreamEmitter  # noqa: E402


@pytest.fixture(autouse=True)
def _restore_run_turn():
    """本文件直接对 WS.run_turn 赋值打桩；不管测试成败都必须还原，
    否则按字母序排在后面的文件（test_steer/test_webui…）会拿到替身——
    而把本文件排在别的文件前面跑时，污染的就是别人（实测坑过 test_parallel）。"""
    saved = WS.run_turn
    yield
    WS.run_turn = saved


def _emitter():
    import io
    return StreamEmitter(io.BytesIO())


def _fake_run_turn(client, cfg, ctx, history, text, images, emitter=None, cancel=None, steer=None):
    """替身：走一遍「文字 + 工具卡片 + 统计」，把 history 也补上。"""
    emitter.step(1)
    emitter.text("你说的是：")
    emitter.text(text[:20])
    # 心跳：每秒一次的那种。必须只转发给前端、不进 _events，否则一次长工具
    # 调用就能往历史里灌几百条「已等 N 秒」，回放时把有用内容全冲掉。
    emitter.thinking(3.0, 0)
    emitter.thinking(4.0, 1200)
    emitter.tool_call("read_file", {"path": "a.txt"})
    emitter.tool_result("read_file", "文件内容", 0.42)
    emitter.stats({"prompt_eval_count": 12, "eval_count": 7, "total_duration": 1.5e9}, step=1)
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": "你说的是：" + text[:20]})
    return "你说的是：" + text[:20]


# ---------------- 1. Store ----------------

def test_store():
    root = Path(tempfile.mkdtemp(prefix="oa-store-"))
    st = SessionStore(root)

    assert st.list() == [], "空目录应返回空列表"

    d1 = st.create(workspace="D:/ws", model="m1")
    assert st.path(d1["id"]).is_file(), "create 应落盘"
    assert d1["title"] == "新对话"

    d1["title"] = make_title("帮我看一下这个 Excel 的第三行数据对不对")
    d1["turns"] = [{"role": "user", "text": "x"}, {"role": "assistant", "events": []}]
    st.save(d1)

    got = st.load(d1["id"])
    assert got["title"].startswith("帮我看一下这个 Excel"), got["title"]
    assert len(got["turns"]) == 2

    d2 = st.create(workspace="D:/ws")
    # 没有提问的空会话不进列表
    assert [x["id"] for x in st.list()] == [d1["id"]], "空会话不该出现在历史里"
    d2["turns"] = [{"role": "user", "text": "第二个"}]
    st.save(d2)
    names = [x["id"] for x in st.list()]
    assert names[0] == d2["id"], "列表按 updated 倒序，后写的在前"
    assert set(names) == {d1["id"], d2["id"]}

    # 轮次上限
    d2["turns"] = [{"role": "user", "text": str(i)} for i in range(MAX_TURNS + 30)]
    st.save(d2)
    assert len(st.load(d2["id"])["turns"]) == MAX_TURNS

    # 坏文件不炸列表
    (root / "broken.json").write_text("{不是 json", encoding="utf-8")
    assert len(st.list()) == 2, "坏 JSON 应被跳过"

    # 清空壳
    d3 = st.create(workspace="D:/ws")
    assert st.purge_empty() == 1
    assert not st.path(d3["id"]).exists() and st.path(d1["id"]).exists()

    # id 消毒
    assert "/" not in st.path("../../etc/passwd").name
    assert st.load("../../etc/passwd") is None

    assert st.delete(d2["id"]) is True
    assert st.delete(d2["id"]) is False
    print("PASS: SessionStore 建/存/列/删 + 标题 + 轮次上限 + 坏文件容错")


# ---------------- 2. AgentSession 记录与还原 ----------------

def test_session_record_and_resume():
    root = Path(tempfile.mkdtemp(prefix="oa-sess-"))
    WS.run_turn = _fake_run_turn          # 打掉真实模型调用
    cfg = AgentConfig(use_color=False, warm=False, workspace=str(root))

    s = WS.AgentSession(cfg, store=SessionStore(root / "sessions"))
    assert s.turns == [] and s.history and len(s.history) == 1, "新会话只有 system"

    s.run("读一下 a.txt", None, _emitter(), files=["a.txt"], display="读一下 a.txt")
    assert len(s.turns) == 2, s.turns
    assert s.turns[0] == {"role": "user", "text": "读一下 a.txt",
                          "files": ["a.txt"], "imgs": 0}
    evs = [e["type"] for e in s.turns[1]["events"]]
    assert evs == ["step", "text", "tool_call", "tool_result", "stats"], evs
    assert "thinking" not in evs, "心跳不能进历史事件（回放会刷屏）"
    assert "confir" not in "".join(evs)
    assert s.title.startswith("读一下"), s.title

    files = list((root / "sessions").glob("*.json"))
    assert len(files) == 1, files
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["turns"][0]["text"] == "读一下 a.txt"
    assert len(data["history"]) == 3, "system + user + assistant"

    # 模拟重启：新进程/新实例应接上这条对话
    s2 = WS.AgentSession(cfg, store=SessionStore(root / "sessions"))
    assert s2.sid == s.sid, "启动应恢复最近一次对话"
    assert len(s2.turns) == 2 and len(s2.history) == 3

    # 新对话 → 旧对话仍在列表里（新会话没提问，还不占历史）
    r = s2.new_session()
    assert r["id"] and r["id"] != s.sid and r["turns"] == []
    assert [x["id"] for x in s2.list_sessions()] == [s.sid], s2.list_sessions()

    # 空会话再点「新对话」：复用，不再新增文件
    before = list((root / "sessions").glob("*.json"))
    r2 = s2.new_session()
    assert r2["id"] == r["id"], "空会话应复用同一个 id"
    assert len(list((root / "sessions").glob("*.json"))) == len(before)

    # 切回旧对话：turns / history 都回来了
    back = s2.open_session(s.sid)
    assert back["id"] == s.sid and len(back["turns"]) == 2
    assert len(s2.history) == 3

    # 删除当前对话 → 当前变空壳
    d = s2.delete_session(s.sid)
    assert d["ok"] is True and d["current"]["turns"] == []
    assert d["sessions"] == [], "删掉唯一的旧对话后，历史里就只剩空壳（不展示）"

    # 忙碌时拒绝切换
    s2.lock.acquire()
    try:
        assert "error" in s2.open_session(s.sid)
        assert "error" in s2.new_session()
    finally:
        s2.lock.release()
    print("PASS: 回合事件落盘 + 重启恢复 + 新建/切换/删除 + 忙碌拒绝")


# ---------------- 3. 搜索 ----------------

def test_search():
    root = Path(tempfile.mkdtemp(prefix="oa-search-"))
    st = SessionStore(root)

    a = st.create(workspace="D:/ws")
    a["title"] = "侧栏收起后不能展开"
    a["updated"] = 1000
    a["turns"] = [
        {"role": "user", "text": "侧栏点箭头收起之后就找不到了"},
        {"role": "assistant", "events": [
            {"type": "think", "text": "先看 setSidebar 的实现"},
            {"type": "text", "text": "改成窄轨方案，同一颗按钮负责收起和展开"},
            {"type": "tool_call", "name": "read_file", "args": {"path": "webui/static/style.css"}},
            {"type": "tool_result", "name": "read_file", "text": "#sb-collapsed 规则"},
        ]},
    ]
    st.save(a)

    b = st.create(workspace="D:/ws")
    b["title"] = "Excel 第三行对不对"
    b["updated"] = 2000
    b["turns"] = [{"role": "user", "text": "帮我看下这张表的合计"}]
    st.save(b)

    # 标题命中，且排在前（updated 更大的是 b）；正文没有该词时片段留空，不重复标题
    r = st.search("Excel")
    assert len(r) == 1 and r[0]["id"] == b["id"], r
    assert r[0]["field"] == "标题" and r[0]["snippet"] == "", r[0]

    # 正文命中：用户提问
    r = st.search("合 计".replace(" ", ""))
    assert [x["id"] for x in r] == [b["id"]], r
    assert r[0]["field"] == "提问", r[0]

    # 命中回答 / 思考 / 工具，字段标签要分清
    assert st.search("窄轨")[0]["field"] == "回答"
    assert st.search("setSidebar")[0]["field"] == "思考"
    tool_hit = st.search("style.css")
    assert tool_hit and tool_hit[0]["field"] == "工具", tool_hit

    # 一个会话只出一条结果，但 hits 记总命中数（标题 + 两处正文）
    r = st.search("侧栏")
    assert len(r) == 1 and r[0]["id"] == a["id"], r
    assert r[0]["field"] == "提问", "标题和提问都命中时，片段取正文（标题那行已高亮）"
    assert r[0]["hits"] >= 2, r[0]

    # 多词 AND：都要出现
    assert st.search("侧栏 箭头"), "两词都在提问里，应命中"
    assert st.search("侧栏 不存在的词") == [], "缺一个词就不该命中"

    # 大小写不敏感
    assert len(st.search("excel")) == 1 and len(st.search("EXCEL")) == 1

    # 空查询 / 纯空格：不返回全量（避免把搜索当列表用）
    assert st.search("") == [] and st.search("   ") == []

    # 片段截断：长文本压成一行，带省略号且不超长
    c = st.create(workspace="D:/ws")
    c["title"] = "长文"
    c["turns"] = [{"role": "user", "text": "前言" * 200 + "关键词" + "后记" * 200}]
    st.save(c)
    snip = st.search("关键词")[0]["snippet"]
    assert "关键词" in snip and len(snip) <= 96 and "\n" not in snip, snip
    assert snip.startswith("…") and snip.endswith("…"), snip

    # 标题命中要排在正文命中之前：搜「你好」时，标题就叫「你好」的那条必须第一，
    # 哪怕另一条对话更近更新（截图 bug 就是这个：被人误以为搜不到）
    d = st.create(workspace="D:/ws")
    d["title"] = "你好"
    d["updated"] = 500                      # 比 c 旧
    d["turns"] = [{"role": "user", "text": "你好"}]
    st.save(d)
    e = st.create(workspace="D:/ws")
    e["title"] = "写个文档"
    e["updated"] = 9999                     # 最新
    e["turns"] = [
        {"role": "user", "text": "帮我生成一个 docx"},
        {"role": "assistant", "events": [
            {"type": "text", "text": 'doc.add_paragraph("第一段你好")'},
        ]},
    ]
    st.save(e)
    r = st.search("你好")
    assert [x["id"] for x in r] == [d["id"], e["id"]], \
        f"标题命中要优先于更新的正文命中: {[(x['title'], x['hits']) for x in r]}"
    assert r[0]["field"] == "标题" and r[0]["snippet"] == "", \
        f"标题与提问是同一句时不该再重复一行片段: {r[0]}"
    assert r[1]["field"] == "回答" and "你好" in r[1]["snippet"], r[1]

    print("PASS: 搜索（标题/提问/回答/思考/工具 + 多词 AND + 大小写 + 片段截断 + 标题优先）")


# ---------------- 4. 清空当前对话 ----------------

def test_reset_only_current():
    """「清空」只能动当前这条：以前它留着空壳文件、界面上却整条消失，看着像清了全部。"""
    root = Path(tempfile.mkdtemp(prefix="oa-reset-"))
    WS.run_turn = _fake_run_turn
    cfg = AgentConfig(use_color=False, warm=False, workspace=str(root))
    s = WS.AgentSession(cfg, store=SessionStore(root / "sessions"))

    s.run("第一条", None, _emitter(), display="第一条")
    first = s.sid
    s.new_session()
    s.run("第二条", None, _emitter(), display="第二条")
    second = s.sid
    assert len(s.list_sessions()) == 2 and list((root / "sessions").glob("*.json"))

    r = s.reset()
    assert r["ok"] is True, r
    assert [x["id"] for x in r["sessions"]] == [first], r["sessions"]
    assert len(list((root / "sessions").glob("*.json"))) == 1, "清空后不该留空壳文件"
    assert not (root / "sessions" / f"{second}.json").exists()
    assert (root / "sessions" / f"{first}.json").exists(), "另一条不许被牵连"

    # 清空后回到一条空白新对话
    cur = s.current()
    assert cur["id"] == "" and cur["turns"] == [] and len(s.history) == 1, cur
    assert s._is_empty()

    # 剩下这条还能正常打开
    back = s.open_session(first)
    assert back["id"] == first and len(back["turns"]) == 2

    # 忙碌时拒绝清空
    s.lock.acquire()
    try:
        bad = s.reset()
        assert bad.get("code") == 409 and bad.get("error"), bad
    finally:
        s.lock.release()

    print("PASS: 清空只动当前这一条（删存档、回空白新对话、忙碌拒绝）")


# ---------------- 5. HTTP 接口 ----------------

def _get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _post(url, obj):
    data = json.dumps(obj).encode()
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_api():
    from http.server import ThreadingHTTPServer
    from webui.server import WebUIApp, make_handler

    root = Path(tempfile.mkdtemp(prefix="oa-api-"))
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    cfg = AgentConfig(use_color=False, warm=False, workspace=str(root))
    app = WebUIApp(cfg, port=port)
    app.start_hotkey = lambda: False
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    try:
        st, r = _get(base + "/api/sessions")
        assert st == 200 and "sessions" in r and "current" in r, r

        st, r = _post(base + "/api/sessions/new", {})
        assert st == 200 and r["id"], r
        first = r["id"]

        st, r = _get(base + "/api/sessions")
        assert r["current"]["id"] == first, "新建的空会话就是当前对话"
        assert all(x["id"] != first for x in r["sessions"]), "没提问的空会话不进历史列表"

        st, r = _post(base + "/api/sessions/open", {"id": first})
        assert st == 200 and r["id"] == first, r

        st, r = _post(base + "/api/sessions/open", {"id": "no-such-id"})
        assert st == 404 and r.get("error"), (st, r)

        st, r = _post(base + "/api/sessions/delete", {"id": first})
        assert st == 200 and r["ok"] is True, r
        assert all(x["id"] != first for x in r["sessions"])

        # 搜索接口：中文关键词要能正确 URL 解码，并带出命中片段
        sd = app.session.store.create(workspace=str(root))
        sd["title"] = "侧栏收起后不能展开"
        sd["turns"] = [{"role": "user", "text": "侧栏点箭头收起来就找不到了"}]
        app.session.store.save(sd)
        st, r = _get(base + "/api/sessions/search?q=" + urllib.parse.quote("侧栏"))
        assert st == 200 and r["q"] == "侧栏", (st, r)
        assert [x["id"] for x in r["results"]] == [sd["id"]], r
        assert r["results"][0]["field"] == "提问" and "侧栏" in r["results"][0]["snippet"], r
        st, r = _get(base + "/api/sessions/search?q=" + urllib.parse.quote("压根没有的词"))
        assert st == 200 and r["results"] == [], r
        st, r = _get(base + "/api/sessions/search?q=")
        assert st == 200 and r["results"] == [], "空查询不该把全部对话倒出来"
        print("PASS: /api/sessions 列表 / 新建 / 打开 / 删除（含不存在 id 的报错）+ 搜索接口")
    finally:
        httpd.shutdown()


def main():
    test_store()
    test_session_record_and_resume()
    test_search()
    test_reset_only_current()
    test_api()
    print("PASS: 多会话持久化全过")


if __name__ == "__main__":
    main()
