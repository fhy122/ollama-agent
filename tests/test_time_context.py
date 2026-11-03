"""time-context 时间注入测试。

契约（借鉴 DSH dsh-time-context）：
- 注入是请求专用的 user 消息：内容含日期/星期/时区/经过时长
- 不入持久 history（重试请求与首次一致的前提）
- 第 1 步必注入；后续步按刷新间隔（600s 默认，0=每步）；开关可关
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import timectx
from agent.config import AgentConfig
from agent.registry import ToolContext


NOW = 1793796000.0   # 固定「当前时刻」，避免测试依赖真实时钟


# ---------- 单元 ----------

def test_message_contains_date_weekday_zone_elapsed():
    lt = timectx.time_message(1, NOW - 95, now=NOW)
    assert lt["role"] == "user"
    c = lt["content"]
    assert "[时间上下文]" in c and "第 1 步" in c
    assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", c), "应含完整日期时间"
    assert "周" in c and "UTC" in c
    assert "距用户消息已过去 1m 35s" in c


def test_step3_baseline_is_last_injection():
    c = timectx.time_message(3, NOW - 7380, now=NOW)["content"]
    assert "第 3 步" in c and "距上一次时间读数已过去 2h 3m 0s" in c


def test_elapsed_clamps_negative():
    assert "0s" in timectx.time_message(2, NOW + 100, now=NOW)["content"]


def test_should_inject_matrix():
    f = timectx.should_inject
    assert f(1, None, NOW, True, 600)                    # 第 1 步必注入
    assert f(1, NOW - 1, NOW, True, 600)                 # 跨回合也注入
    assert not f(2, NOW - 60, NOW, True, 600)            # 间隔内不重复
    assert f(2, NOW - 601, NOW, True, 600)               # 超间隔注入
    assert f(2, NOW - 1, NOW, True, 0)                   # 0 = 每步
    assert not f(1, None, NOW, False, 600)               # 总开关


# ---------- 回路集成 ----------

class _StubClient:
    """一步工具调用 + 一步收尾；记录每次请求的 messages。"""

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


def _turn(cfg_overrides=None):
    from agent.loop import run_turn
    cfg = AgentConfig(use_color=False, warm=False, **(cfg_overrides or {}))
    ctx = ToolContext(use_color=False, workspace=Path("."))
    client = _StubClient()
    history: list[dict] = [{"role": "system", "content": "s"}]
    run_turn(client, cfg, ctx, history, "hi")
    return client, history


def test_injection_transient_not_in_history():
    """时间消息进请求但不进 history；第 1 步请求末尾是它。"""
    client, history = _turn()
    first = client.seen[0]
    assert first[-1]["role"] == "user" and "时间上下文" in first[-1]["content"]
    assert all("时间上下文" not in str(m.get("content", "")) for m in history), \
        "history 必须保持干净"


def test_step2_respects_refresh_interval():
    """间隔内第 2 步不注入；refresh=0 时每步都注入。"""
    client, _ = _turn({"time_refresh_sec": 600})
    assert "时间上下文" not in str(client.seen[1][-1].get("content", "")), \
        "默认 600s 间隔内第 2 步不应注入"

    client2, _ = _turn({"time_refresh_sec": 0})
    assert "时间上下文" in client2.seen[1][-1]["content"], "0 = 每步都注入"
    assert "第 2 步" in client2.seen[1][-1]["content"]


def test_disabled_when_off():
    client, _ = _turn({"time_context": False})
    assert all("时间上下文" not in str(m.get("content", ""))
               for req in client.seen for m in req)
