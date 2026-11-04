"""断流自动重试测试（agent/retry.py + loop.py 集成）。

设计参考 DSH dsh-llm-retry。锁住的行为：
1. 空输出断流（没吐一个字）→ 退避后干净重跑，历史不动；
2. 半截断流 → 已生成部分入历史 + 注入「从中断处继续」指令，自动续写；
3. 错误分类：429/5xx/超时/传输断开可重试，Key 错/参数错绝不重试；
4. 次数用尽后按原样收尾（保留部分、明确告知）；
5. 退避期间用户打断立即生效，不留隐形等待。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import AgentConfig  # noqa: E402
from agent.loop import run_turn  # noqa: E402
from agent.registry import ToolContext  # noqa: E402
from agent.retry import (FATAL, RETRYABLE, backoff_delay,  # noqa: E402
                         classify_error, interruptible_sleep)

import agent.loop as loop_mod  # noqa: E402


# ---------- 单元：分类与退避 ----------

def test_classify_retryable_vs_fatal():
    assert classify_error("等xxx第一个字节超过 180 秒仍无响应") == "timeout"
    assert classify_error("TimeoutError: timed out") == "timeout"
    assert classify_error("xxx API 错误 429: rate limited") == "rate_limit"
    assert classify_error("xxx API 错误 503: overloaded") == "server"
    assert classify_error("连不上xxx：ConnectionResetError") == "transport"
    assert classify_error("流意外结束（未收到 done 帧）") == "transport"
    # 裸抛的 OSError 家族：ssl.SSLError 是今天实发的形态（中转站 TLS 被掐）
    assert classify_error(
        "SSLError: EOF occurred in violation of protocol (_ssl.c:2406)") == "transport"
    # Key 错 / 参数错：重试无意义
    assert classify_error("xxx API 错误 401: invalid api key") == FATAL
    assert classify_error("xxx API 错误 400: invalid request") == FATAL
    assert RETRYABLE == frozenset({"rate_limit", "server", "timeout", "transport"})


def test_backoff_grows_and_bounded():
    ds = [backoff_delay(i) for i in (1, 2, 3, 4)]
    assert ds[0] < ds[1] < ds[2], ds
    assert ds[3] <= 8.0 * 1.11, "退避应有上限"
    for d in ds:
        assert d > 0


# ---------- 集成桩 ----------

class _Emitter:
    def __init__(self):
        self.notices = []

    def step(self, n): pass
    def text(self, t): pass
    def think(self, t): pass
    def newline(self): pass
    def tool_call(self, name, args): pass
    def tool_result(self, name, text, dt, images=None): pass
    def notice(self, t): self.notices.append(t)
    def stats(self, st, step=0): pass
    def thinking(self, elapsed, chars=0): pass


def _turn(client, retries=3, cancel=None):
    cfg = AgentConfig(use_color=False, warm=False, stream_retries=retries)
    ctx = ToolContext(use_color=False, workspace=Path("."))
    em = _Emitter()
    history = [{"role": "system", "content": "s"}]
    out = run_turn(client, cfg, ctx, history, "hi", emitter=em, cancel=cancel)
    return out, history, em


# 让退避瞬间完成（不真睡）：把 interruptible_sleep 换成直通桩
@pytest.fixture(autouse=True)
def _fast_sleep(monkeypatch):
    monkeypatch.setattr(loop_mod, "interruptible_sleep",
                        lambda seconds, cancel=None: interruptible_sleep(
                            0.001, cancel))


class _ScriptedClient:
    """按剧本逐次返回；记录每次调用收到的完整历史。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0
        self.seen = []

    def chat(self, messages, **kw):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        item = self.script[min(self.calls - 1, len(self.script) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


def _resp(content="", error=None, cancelled=False, done=True):
    return {"thinking": "", "content": content, "tool_calls": [],
            "stats": {}, "cancelled": cancelled, "error": error,
            "ttft": None}


def test_empty_failure_retries_then_recovers():
    """空输出断流：历史不动，重试后正常完成。"""
    client = _ScriptedClient([
        _resp(error="TimeoutError: timed out"),   # 没吐字就断
        _resp(content="最终回答"),
    ])
    out, history, em = _turn(client)
    assert out == "最终回答"
    assert client.calls == 2
    # 重跑请求与第一次完全相同（历史未被污染）
    assert client.seen[0] == client.seen[1]
    assert any("自动重试" in n for n in em.notices), em.notices


def test_bare_ssl_error_gets_retried():
    """裸抛的 ssl.SSLError（OSError 家族）必须被重试网接住。

    实发案例：中转站 TLS 连接被掐，ssl.SSLError 从 http.client 裸抛，
    旧代码只捕 RuntimeError，一步重试都没走直接把回合弄死。
    """
    import ssl
    client = _ScriptedClient([
        ssl.SSLError("EOF occurred in violation of protocol (_ssl.c:2406)"),
        _resp(content="恢复后完成"),
    ])
    out, history, em = _turn(client)
    assert out == "恢复后完成"
    assert client.calls == 2
    assert any("自动重试" in n for n in em.notices), em.notices


def test_bare_fatal_error_still_raises():
    """裸抛的非瞬态错误（如 ValueError 包 401）照旧 raise，不因撒宽重试网而空转。"""
    client = _ScriptedClient([
        ValueError("xxx API 错误 401: invalid api key"),
        _resp(content="不该走到这"),
    ])
    with pytest.raises(ValueError):
        _turn(client)
    assert client.calls == 1


def test_exhausted_empty_failures_end_with_note():
    """空输出连续失败到次数用尽：按原样收尾，报错明示。"""
    client = _ScriptedClient([_resp(error="TimeoutError: timed out")])
    out, history, em = _turn(client, retries=2)
    assert client.calls == 1 + 2, "首次 + 2 次重试"
    assert "中断" in out or out == ""
    # 空输出：历史里没有半截 assistant，只有收尾 note
    assert all("从中断处" not in str(m.get("content")) for m in history)


def test_partial_failure_auto_resumes_and_recovers():
    """半截断流：partial 入历史 + 续写指令注入，第二次成功衔接。"""
    client = _ScriptedClient([
        _resp(content="前半段内容", error="ConnectionResetError: reset by peer"),
        _resp(content="，后半段内容"),
    ])
    out, history, em = _turn(client)
    assert out == "，后半段内容"
    assert client.calls == 2
    roles = [(m["role"], str(m.get("content", ""))) for m in client.seen[1]]
    # 第二次请求里：partial assistant + 续写指令都在
    assert any(r == "assistant" and "前半段内容" in c for r, c in roles)
    assert any(r == "user" and "从中断处" in c for r, c in roles)
    # 收尾后历史：partial assistant、续写指令、最终 assistant 三段齐全
    assert any(m["role"] == "assistant" and "前半段内容" in str(m.get("content"))
               for m in history)


def test_fatal_error_never_retries():
    """Key 错（401）等 FATAL：绝不重试，原样抛出。"""
    client = _ScriptedClient([
        RuntimeError("xxx API 错误 401: invalid api key"),
        _resp(content="不应到达"),
    ])
    with pytest.raises(RuntimeError):
        _turn(client)
    assert client.calls == 1, "FATAL 不应重试"


def test_backoff_cancel_stops_waiting(monkeypatch):
    """退避期间用户打断：立即收尾为 cancelled，不再发请求。

    覆盖 autouse 的快速退避桩：这里用真 interruptible_sleep + 长退避，
    让 cancel 在睡眠中置位。
    """
    monkeypatch.setattr(loop_mod, "backoff_delay", lambda attempt: 5.0)
    monkeypatch.setattr(loop_mod, "interruptible_sleep",
                        lambda seconds, cancel=None: interruptible_sleep(
                            seconds, cancel))

    class _CancelClient:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, **kw):
            self.calls += 1
            raise RuntimeError("连不上xxx：ConnectionError")

    import threading
    cancel = threading.Event()
    client = _CancelClient()
    t = threading.Timer(0.05, cancel.set)
    t.start()
    try:
        cfg = AgentConfig(use_color=False, warm=False, stream_retries=3)
        ctx = ToolContext(use_color=False, workspace=Path("."))
        out = run_turn(client, cfg, ctx, [{"role": "system", "content": "s"}],
                       "hi", emitter=_Emitter(), cancel=cancel)
    finally:
        t.cancel()
    assert client.calls == 1, "打断后不应再重试"
    assert out == ""


def test_zero_retries_disables_feature():
    """stream_retries=0：行为同旧版，断流立即收尾。"""
    client = _ScriptedClient([_resp(error="TimeoutError: timed out")])
    out, _, em = _turn(client, retries=0)
    assert client.calls == 1
