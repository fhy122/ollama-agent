"""累计 usage 统计口径（agent/usage.py）离线自测。

判据随功能一起写，覆盖规范第三节三类场景：
  正常路径 —— 每次真实请求都计一次，prompt/gen 分别累加
  错误路径 —— 后端没给 usage 时只记「缺失」，绝不估算/编造 token 数
  边界     —— 本回合增量、按来源分类、旧会话缺字段/脏数据、scope 嵌套与异常

运行: python -m pytest tests/test_usage.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import usage  # noqa: E402


def _st(prompt=0, gen=0):
    return {"prompt_eval_count": prompt, "eval_count": gen,
            "total_duration": 1_000_000_000}


def test_every_request_counted_and_tokens_summed() -> None:
    """正常路径：3 次请求（2 次带 usage + 1 次失败）都要计次并正确求和。"""
    led = usage.UsageLedger()
    led.record(_st(100, 10), source="chat")
    led.record(_st(200, 20), source="chat")
    led.record(None, source="chat", ok=False)

    s = led.snapshot()
    assert s["requests"] == 3
    assert s["prompt_tokens"] == 300
    assert s["gen_tokens"] == 30
    assert s["total_tokens"] == 330
    assert s["missing_usage"] == 1
    assert s["failed_requests"] == 1, "ok=False 的失败请求要单独计次"


def test_missing_usage_never_invents_tokens() -> None:
    """错误路径：无 usage 的请求不能凭空产出 token 数（禁止估算当账单）。"""
    led = usage.UsageLedger()
    for _ in range(4):
        led.record(None, source="chat", ok=False)
    led.record({"total_duration": 1}, source="chat")   # 只有本地耗时，无 token

    s = led.snapshot()
    assert s["requests"] == 5
    assert s["missing_usage"] == 5
    assert s["prompt_tokens"] == 0 and s["gen_tokens"] == 0
    assert s["total_tokens"] == 0


def test_failed_request_with_real_usage_still_counts() -> None:
    """边界：断流但后端给了 done 帧 → 真实消耗要计，不能因为失败就丢。"""
    led = usage.UsageLedger()
    led.record(_st(4096, 512), source="chat", ok=False)
    assert led.snapshot()["prompt_tokens"] == 4096
    assert led.snapshot()["gen_tokens"] == 512
    assert led.snapshot()["missing_usage"] == 0


def test_delta_is_turn_scoped() -> None:
    """正常路径：本回合增量 = 当前快照 − 回合开始前快照。"""
    led = usage.UsageLedger()
    led.record(_st(100, 10), source="chat")
    before = led.snapshot()
    led.record(_st(200, 20), source="chat")
    led.record(_st(300, 30), source="longdoc")
    led.record(None, source="chat", ok=False)

    d = led.delta(before)
    assert d["requests"] == 3
    assert d["prompt_tokens"] == 500
    assert d["gen_tokens"] == 50
    assert d["missing_usage"] == 1
    assert led.snapshot()["requests"] == 4, "delta 不能改动账本本身"


def test_by_source_breakdown() -> None:
    """边界：重试/子请求/摘要/warmup 各归各的来源，便于定位消耗大头。"""
    led = usage.UsageLedger()
    led.record(_st(1000, 100), source="chat")
    led.record(_st(1000, 100), source="chat")
    led.record(_st(500, 50), source="longdoc")
    led.record(_st(80, 8), source="compaction")
    led.record(None, source="warmup")

    s = led.snapshot()
    chat = s["by_source"]["chat"]
    assert chat["requests"] == 2
    assert chat["prompt_tokens"] == 2000
    assert chat["gen_tokens"] == 200
    assert chat["total_tokens"] == 2200
    assert chat["missing_usage"] == 0
    assert chat["failed_requests"] == 0
    assert s["failed_requests"] == 0
    assert s["by_source"]["longdoc"]["prompt_tokens"] == 500
    assert s["by_source"]["compaction"]["gen_tokens"] == 8
    assert s["by_source"]["warmup"]["missing_usage"] == 1


def test_snapshot_roundtrip() -> None:
    """正常路径：落盘后恢复，账本数字与分类逐字一致。"""
    led = usage.UsageLedger()
    led.record(_st(1234, 56), source="chat")
    led.record(_st(7, 8), source="longdoc")
    led.record(None, source="chat", ok=False)
    assert usage.UsageLedger.from_dict(led.snapshot()).snapshot() == led.snapshot()


def test_from_dict_tolerates_old_and_corrupt_sessions() -> None:
    """边界：旧文件无 usage 字段 / 脏数据，一律安全降级为空账本。"""
    for raw in (None, {}, {"requests": "oops"},
                {"prompt_tokens": None, "gen_tokens": -5},
                {"by_source": {"chat": "not-a-dict"}}):
        led = usage.UsageLedger.from_dict(raw)
        s = led.snapshot()
        assert s["requests"] == 0, f"旧/脏数据不得凭空造调用次数: {raw}"
        assert s["prompt_tokens"] == 0 and s["gen_tokens"] == 0
    led = usage.UsageLedger.from_dict(
        {"requests": 9, "prompt_tokens": 900, "gen_tokens": 90,
         "missing_usage": 2, "by_source": {}})
    assert led.snapshot()["prompt_tokens"] == 900
    assert led.snapshot()["total_tokens"] == 990


class _Client:
    usage_scope = "chat"


def test_scope_restores_even_on_exception() -> None:
    """边界：子请求 scope 嵌套 + 抛异常，都不能污染后续请求的分类。"""
    client = _Client()
    with usage.scope(client, "longdoc"):
        assert client.usage_scope == "longdoc"
        with usage.scope(client, "compaction"):
            assert client.usage_scope == "compaction"
        assert client.usage_scope == "longdoc"
    assert client.usage_scope == "chat"

    try:
        with usage.scope(client, "compaction"):
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert client.usage_scope == "chat"


# ---------- 后端统一埋点（真 ChatClient，替换 _chat 造剧本） ----------

def _client():
    from agent.backends.ollama import OllamaClient
    from agent.config import AgentConfig

    c = OllamaClient(AgentConfig(model="fake", warm=False, time_context=False))
    led = usage.UsageLedger()
    usage.UsageLedger.attach(c, led)
    return c, led


def _resp(stats=None, error=None):
    return {"thinking": "", "content": "ok", "tool_calls": [],
            "stats": stats or {}, "cancelled": False, "error": error,
            "ttft": None}


def test_chat_hooks_every_retry_not_just_the_last() -> None:
    """核心回归：重试 2 次失败 + 1 次成功，3 次请求都要进账。"""
    from unittest import mock

    c, led = _client()
    calls = [_resp(error="断流"), _resp(error="断流"), _resp(_st(100, 10))]
    with mock.patch.object(c, "_chat", side_effect=calls):
        for _ in range(3):
            c.chat([{"role": "user", "content": "hi"}])

    s = led.snapshot()
    assert s["requests"] == 3, "旧口径只算最后一次，这里必须记全"
    assert s["missing_usage"] == 2
    assert s["failed_requests"] == 2
    assert s["prompt_tokens"] == 100 and s["gen_tokens"] == 10


def test_chat_exception_records_request_without_inventing_tokens() -> None:
    """错误路径：_chat 抛异常也要记一次请求，且不能编造 token。"""
    from unittest import mock

    c, led = _client()
    with mock.patch.object(c, "_chat", side_effect=RuntimeError("连接失败")):
        try:
            c.chat([{"role": "user", "content": "hi"}])
        except RuntimeError:
            pass
    s = led.snapshot()
    assert s["requests"] == 1
    assert s["missing_usage"] == 1 and s["failed_requests"] == 1
    assert s["prompt_tokens"] == 0 and s["gen_tokens"] == 0


def test_warmup_and_subrequest_scopes_are_attributed() -> None:
    """集成：warmup / 子请求 / 主循环分别归类，都是真实 client.chat。"""
    from unittest import mock

    c, led = _client()
    with mock.patch.object(c, "_chat", return_value=_resp(_st(5, 1))):
        with usage.scope(c, "longdoc"):
            c.chat([{"role": "user", "content": "段一"}])
        c.warmup()                                  # 内部自带 scope
        c.chat([{"role": "user", "content": "hi"}])  # 默认 chat

    s = led.snapshot()
    assert set(s["by_source"]) == {"longdoc", "warmup", "chat"}
    assert s["requests"] == 3
    assert s["prompt_tokens"] == 15 and s["gen_tokens"] == 3


def test_broken_sink_never_breaks_conversation() -> None:
    """边界：统计回调自己炸了，对话必须照常返回。"""
    from unittest import mock
    from agent.backends.ollama import OllamaClient
    from agent.config import AgentConfig

    c = OllamaClient(AgentConfig(model="fake", warm=False))
    def boom(*a, **k):
        raise RuntimeError("统计坏了")
    c.usage_sink = boom
    with mock.patch.object(c, "_chat", return_value=_resp(_st(1, 1))):
        assert c.chat([{"role": "user", "content": "hi"}])["content"] == "ok"


def test_longdoc_subrequests_are_attributed() -> None:
    """集成：长文精读的子请求走 _ask，要归到 longdoc 而不是 chat。"""
    from unittest import mock
    from agent import longdoc

    c, led = _client()
    with mock.patch.object(c, "_chat", return_value=_resp(_st(50, 5))):
        assert longdoc._ask(c, "这一段讲了什么")
    assert led.snapshot()["by_source"]["longdoc"]["requests"] == 1


def test_compaction_llm_summary_is_attributed() -> None:
    """集成：LLM 摘要请求归 compaction（默认 local 模式不发请求）。"""
    from unittest import mock
    from agent import compaction
    from agent.config import AgentConfig

    c, led = _client()
    fn = compaction.make_llm_summarizer(c, AgentConfig())
    with mock.patch.object(c, "_chat", return_value=_resp(_st(80, 8))):
        assert fn([{"role": "user", "content": "旧内容"}], "")
    assert led.snapshot()["by_source"]["compaction"]["requests"] == 1


# ---------- 事件与回合收尾 ----------

def _delta():
    led = usage.UsageLedger()
    led.record(_st(100, 10), source="chat")
    led.record(_st(200, 20), source="longdoc")
    led.record(None, source="chat", ok=False)
    return led.delta({"requests": 0, "by_source": {}}), led.snapshot()


def test_stream_emitter_sends_usage_event() -> None:
    """契约：NDJSON 多一条 usage，含本回合增量与会话累计。"""
    import io
    import json
    from webui.stream import StreamEmitter

    buf = io.BytesIO()
    turn, total = _delta()
    StreamEmitter(buf).usage(turn, total)
    ev = json.loads(buf.getvalue().decode("utf-8").strip())
    assert ev["type"] == "usage"
    assert ev["turn"]["requests"] == 3 and ev["turn"]["total_tokens"] == 330
    assert ev["total"]["requests"] == 3
    assert ev["turn"]["missing_usage"] == 1


def test_recording_emitter_persists_usage_event() -> None:
    """集成：累计事件进 turns 回放，且转发给底层 emitter。"""
    from webui.session import RecordingEmitter

    class _Inner:
        think_hidden = True
        def __init__(self): self.got = []
        def usage(self, turn, total): self.got.append((turn, total))
        def __getattr__(self, n): return lambda *a, **k: None

    events: list[dict] = []
    inner = _Inner()
    turn, total = _delta()
    RecordingEmitter(inner, events).usage(turn, total)
    assert events[-1]["type"] == "usage"
    assert events[-1]["turn"]["prompt_tokens"] == 300
    assert inner.got == [(turn, total)]


def test_run_turn_emits_turn_usage_on_all_requests() -> None:
    """端到端（离线）：真跑 run_turn，累计包含重试与子请求，而非只算最后一步。"""
    import tools  # noqa: F401
    from webui.session import RecordingEmitter
    from agent.config import AgentConfig
    from agent.loop import run_turn
    from agent.registry import ToolContext

    class _Client:
        def __init__(self):
            self.led = usage.UsageLedger()
            usage.UsageLedger.attach(self, self.led)
            self.n = 0
        def chat(self, messages, **kw):
            # 模拟同一回合里「1 次失败重试 + 1 次成功请求」都被后端埋点记账，
            # 旧口径只会看到最后那次成功请求。
            self.n += 1
            self.usage_sink(None, source="chat", ok=False)
            st = _st(100, 10)
            self.usage_sink(st, source="chat", ok=True)
            return {"thinking": "", "content": "完成", "tool_calls": [],
                    "stats": st, "cancelled": False, "error": None, "ttft": None}

    class _Inner:
        think_hidden = True
        def __getattr__(self, n): return lambda *a, **k: None

    client = _Client()
    events: list[dict] = []
    rec = RecordingEmitter(_Inner(), events)
    cfg = AgentConfig(use_color=False, warm=False, time_context=False,
                       no_tools=True, max_steps=1)
    out = run_turn(client, cfg, ToolContext(workspace=Path.cwd(), cfg=cfg,
                                            confirm=False),
                   [{"role": "system", "content": "s"}], "开始", emitter=rec)
    assert out == "完成"
    evs = [e for e in events if e["type"] == "usage"]
    assert len(evs) == 1, f"回合收尾只应发一条累计事件: {evs}"
    turn = evs[0]["turn"]
    assert turn["requests"] == 2, "失败重试那次也必须计入调用次数"
    assert turn["prompt_tokens"] == 100 and turn["gen_tokens"] == 10
    assert turn["total_tokens"] == 110
    assert turn["missing_usage"] == 1
    assert evs[0]["total"]["failed_requests"] == 1


# ---------- 会话落盘 / 恢复 ----------

def _session(tmp_path, load_sid: str = ""):
    from agent.config import AgentConfig
    from webui.session import AgentSession
    from webui.store import SessionStore

    cfg = AgentConfig(warm=False, time_context=False)
    return AgentSession(cfg, store=SessionStore(root=tmp_path),
                        shared={"user_cfg": {}}, load_sid=load_sid)


def test_session_usage_persists_restores_and_rebinds_sink(tmp_path) -> None:
    """集成：真实 SessionStore 存盘→恢复，累计不丢且 client 挂的是恢复后的账本。"""
    from webui.store import SessionStore

    s = _session(tmp_path)
    s.sid = "usage-roundtrip"
    s.usage.record(_st(100, 10), source="chat")
    s.usage.record(None, source="chat", ok=False)
    s._save()

    data = SessionStore(root=tmp_path).load("usage-roundtrip")
    assert data["usage"]["requests"] == 2
    assert data["usage"]["total_tokens"] == 110

    s2 = _session(tmp_path, load_sid="usage-roundtrip")
    assert s2.usage.snapshot() == s.usage.snapshot()
    assert s2.current()["usage"]["requests"] == 2
    s2.client.usage_sink(_st(5, 1), source="chat", ok=True)
    assert s2.usage.snapshot()["prompt_tokens"] == 105, "sink 必须指向恢复后的账本"


def test_old_session_without_usage_field_is_not_backfilled(tmp_path) -> None:
    """边界：老会话只有逐步 stats（本就漏了重试），不能拿它冒充完整累计。"""
    from webui.store import SessionStore

    SessionStore(root=tmp_path).save({
        "id": "old-1",
        "history": [{"role": "system", "content": "s"},
                    {"role": "user", "content": "hi"}],
        "turns": [{"role": "assistant", "events": [
            {"type": "stats", "step": 1,
             "prompt_tokens": 166094, "gen_tokens": 9444}]}],
    })
    s = _session(tmp_path, load_sid="old-1")
    assert s.usage.snapshot()["requests"] == 0
    assert s.usage.snapshot()["total_tokens"] == 0


def test_new_session_resets_usage_and_sink(tmp_path) -> None:
    """边界：新对话从零开始，后续请求记进新账本。"""
    s = _session(tmp_path)
    s.usage.record(_st(1000, 100))
    s.new_session()
    assert s.usage.snapshot()["requests"] == 0
    s.client.usage_sink(_st(1, 1), source="chat", ok=True)
    assert s.usage.snapshot()["prompt_tokens"] == 1

