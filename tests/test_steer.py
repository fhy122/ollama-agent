"""输入队列（引导消息 steering）测试。

契约（参考 DSH inbox next-step）：
- run_turn 在每个步边界消费 steer()：消息以 user 身份入 history，
  emitter.user_message 事件同步发出（前端气泡 / 回放）；
- 模型不中断：引导消息出现在两次模型调用之间，不取消任何东西；
- AgentSession.steer：忙碌时入队（回合线程在步边界取走），空闲时回 idle；
- 打断时清空滞留队列。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import AgentConfig
from agent.registry import ToolContext
from agent.loop import run_turn


class _StubClient:
    def __init__(self):
        self.calls = 0
        self.seen: list[list[dict]] = []

    def chat(self, messages, **kw):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        if self.calls == 1:
            return {"thinking": "", "content": "", "tool_calls": [
                {"function": {"name": "list_dir", "arguments": {"path": "."}}}],
                "stats": {}, "cancelled": False, "error": None, "ttft": None}
        return {"thinking": "", "content": "完成", "tool_calls": [],
                "stats": {}, "cancelled": False, "error": None, "ttft": None}


class _Em:
    def __init__(self):
        self.events: list[tuple] = []
        self.think_hidden = True

    def step(self, n): self.events.append(("step", n))
    def step_time(self, *a, **k): pass
    def text(self, t): self.events.append(("text", t))
    def think(self, t): pass
    def newline(self): pass
    def tool_call(self, name, args): self.events.append(("tool_call", name))
    def tool_result(self, *a, **k): self.events.append(("tool_result",))
    def stats(self, *a, **k): pass
    def notice(self, t): self.events.append(("notice", t))
    def user_message(self, t): self.events.append(("user", t))


def _turn(steer=None, client=None):
    cfg = AgentConfig(use_color=False, warm=False, time_context=False)
    ctx = ToolContext(use_color=False, workspace=Path("."))
    client = client or _StubClient()
    em = _Em()
    history: list[dict] = [{"role": "system", "content": "s"}]
    run_turn(client, cfg, ctx, history, "hi", emitter=em, steer=steer)
    return client, history, em


def test_steering_injected_between_steps():
    """步 1 执行工具时用户插话 → 步 2 请求带上引导，历史同步、模型不中断。"""
    client = _StubClient()
    fired = {"n": 0}

    def steer():
        # 第 1 次请求已发出（=步 1 请求完成、工具执行完），用户插入一条
        if client.calls >= 1 and fired["n"] == 0:
            fired["n"] = 1
            return ["先只看 tests 目录"]
        return []

    client, history, em = _turn(steer, client)

    assert client.calls == 2
    assert "先只看 tests 目录" not in str(client.seen[0]), "第 1 次请求不应包含"
    second = [str(m.get("content", "")) for m in client.seen[1]]
    assert any("先只看 tests 目录" in c for c in second), second
    # 入历史的顺序：tool 结果之后追加 user 引导
    roles = [m["role"] for m in history]
    assert roles[roles.index("tool") + 1:] == ["user", "assistant"], history
    # 前端气泡事件
    assert ("user", "先只看 tests 目录") in em.events
    # 不打断：没有打断收尾
    assert ("notice", "[⏹ 已打断本轮回答]") not in em.events


def test_blank_steering_skipped():
    """空串引导直接丢弃，不入历史。"""
    client = _StubClient()
    fired = {"n": 0}

    def steer():
        if client.calls >= 1 and fired["n"] == 0:
            fired["n"] = 1
            return ["", "有效引导"]
        return []

    client, history, _ = _turn(steer, client)
    contents = [str(m.get("content", "")) for m in history]
    assert "有效引导" in contents
    # 空串引导不入历史（history 里本来就有「模型吐工具调用时的空 assistant
    # 消息」这类合法空串，所以只查 user 消息）
    assert all(str(m.get("content", "")) for m in history if m["role"] == "user"), history


def test_no_steer_noop():
    """不传 steer（CLI 老路径）一切照旧。"""
    client, history, em = _turn(None)
    assert client.calls == 2
    assert not [e for e in em.events if e[0] == "user"], "无引导就不该有 user 事件"


# ---------- AgentSession 队列语义 ----------

def _session(tmp_path):
    import webui.session as WS
    from webui.store import SessionStore
    cfg = AgentConfig(use_color=False, warm=False, workspace=str(tmp_path))
    return WS.AgentSession(cfg, store=SessionStore(tmp_path / "sessions"))


def test_session_steer_busy_vs_idle(tmp_path):
    """忙碌时入队（回合线程取走）；空闲时回 idle。"""
    s = _session(tmp_path)
    r = s.steer("你好")                      # 空闲
    assert r.get("error") == "idle" and r.get("code") == 409

    s.lock.acquire()                          # 模拟回合线程持锁
    try:
        r = s.steer("插一条")
        assert r.get("ok") is True, r
        assert s._drain_steer() == ["插一条"]
        assert s._drain_steer() == []         # 取走即空
        r2 = s.steer("")                      # 空消息拒绝
        assert r2.get("code") == 400
    finally:
        s.lock.release()


def test_request_stop_clears_steer_queue(tmp_path):
    """打断当前回合：滞留的引导消息一并作废。"""
    s = _session(tmp_path)
    s.lock.acquire()
    try:
        s.steer("来不及发的话")
        assert s._steer_q.qsize() == 1
        s.request_stop()
        assert s._steer_q.qsize() == 0, "滞留引导应随回合作废"
    finally:
        s.lock.release()
