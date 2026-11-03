"""刷新不断线：断开 HTTP 流不再打断回合，/api/live 可接管。

背景：原实现 StreamEmitter(on_disconnect=request_stop) 把「浏览器断开」
当成「用户要打断」，刷新页面就会掐死正在跑的回合。现在打断只走显式
/api/stop，断连后回合照跑、事件照记，前端用 /api/live 增量接管。
"""
import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import AgentConfig
from webui.stream import StreamEmitter


class _SlowStreamClient:
    """流式客户端：text 分片吐字，可卡在 barrier 上模拟长生成。"""

    def __init__(self, pieces, gate=None):
        self.pieces = pieces
        self.gate = gate          # threading.Event：set 前一直卡（模拟生成中）
        self.calls = 0

    def chat(self, messages, tools=None, on_text=None, on_think=None,
             cancel=None, on_wait=None, think_hidden=True):
        self.calls += 1
        for p in self.pieces:
            if cancel is not None and cancel.is_set():
                break
            on_text(p)
            if self.gate is not None:
                self.gate.wait(timeout=10)   # 分片之间挂起，等测试放行
        return {"content": "".join(self.pieces), "tool_calls": [],
                "stats": {}, "cancelled": False, "ttft": 0.01}


def _make_session(tmp_path, client):
    import webui.session as WS
    from webui.store import SessionStore
    cfg = AgentConfig(use_color=False, warm=False, workspace=str(tmp_path))
    s = WS.AgentSession(cfg, store=SessionStore(tmp_path / "sessions"))
    s.client = client
    return s


class _Null:
    """吞掉所有事件的哑 emitter（模拟已断开的连接）。"""
    think_hidden = False

    def __getattr__(self, name):
        return lambda *a, **k: None


def test_disconnect_does_not_abort_turn(tmp_path):
    """断连（emitter 写失败）后回合必须照跑完——刷新页面不再掐死会话。

    新 server 接线不再传 on_disconnect；这里再用源码断言锁住该接线，
    防止将来有人把「断开即打断」接回去。
    """
    import inspect
    import webui.server as srv
    src = inspect.getsource(srv)
    assert "on_disconnect=app.session.request_stop" not in src, \
        "断连回调不能接 request_stop（刷新页面会掐死回合）"

    client = _SlowStreamClient(["你好", "，世界"])
    s = _make_session(tmp_path, client)

    broken = StreamEmitter(_BrokenPipe())
    t = threading.Thread(
        target=lambda: s.run("hi", None, broken), daemon=True)
    t.start()
    t.join(10)
    assert not t.is_alive(), "写失败的 emitter 不应拖死回合"

    assert client.calls == 1
    assert s.history[-1]["role"] == "assistant", s.history[-1]
    assert "你好，世界" in str(s.history[-1].get("content", "")), s.history[-1]
    assert not s.cancel_event.is_set(), "断连不应置位打断标志"


class _BrokenPipe:
    def write(self, b):
        raise BrokenPipeError()

    def flush(self):
        pass


def test_live_snapshot_visible_mid_run(tmp_path):
    """运行中 /api/live 能看到已记录事件；结束后 busy=False。"""
    gate = threading.Event()
    client = _SlowStreamClient(["第一段", "第二段"], gate=gate)
    s = _make_session(tmp_path, client)

    t = threading.Thread(target=lambda: s.run("hi", None, _Null()), daemon=True)
    t.start()
    try:
        deadline = time.time() + 5
        snap = {}
        while time.time() < deadline:
            snap = s.live()
            if snap.get("busy") and snap.get("events"):
                break
            time.sleep(0.02)
        assert snap.get("busy") is True, snap
        evs = snap["events"]
        assert evs, "运行中应能拿到已记录的事件快照"
        texts = [e for e in evs if e.get("type") == "text"]
        assert texts, f"应有 text 事件: {evs}"

        gate.set()
        t.join(10)
        assert not t.is_alive()
        assert s.live() == {"busy": False}, "回合结束后 live 应报空闲"
    finally:
        gate.set()
        t.join(5)


def test_live_events_are_cumulative_text(tmp_path):
    """text 事件合并累积：前端按同位置比长度取增量。"""
    gate = threading.Event()
    client = _SlowStreamClient(["A", "B", "C"], gate=gate)
    s = _make_session(tmp_path, client)

    t = threading.Thread(target=lambda: s.run("q", None, _Null()), daemon=True)
    t.start()
    try:
        deadline = time.time() + 5
        s1 = {}
        while time.time() < deadline:
            s1 = s.live()
            if s1.get("busy") and s1.get("events"):
                break
            time.sleep(0.02)
        n1 = len(s1["events"])
        assert n1 >= 1
        gate.set()
        t.join(10)

        # 完整事件里的 text 应是所有分片的拼接（合并在同一元素）
        full = json.loads(json.dumps(_last_assistant_events(s)))
        texts = [e["text"] for e in full if e.get("type") == "text"]
        assert "".join(texts) == "ABC", texts
    finally:
        gate.set()
        t.join(5)


def _last_assistant_events(s):
    for turn in reversed(s.turns):
        if turn.get("role") == "assistant":
            return turn["events"]
    return []
