"""防呆循环 guard 测试（重复工具调用提醒）。

设计移植自 DeepSeek Harness 的 dsh-repeat-tool-reminder，语义对齐其官方测试：
链语义（相同连击 / 换调用重置 / 不受跟踪透明 / 键序无关）、阈值升级
（首个阈值温和文案、后续详细文案）、新用户消息清零、fail-loud 配置校验，
外加 loop 集成：提醒在「tool 结果之后、下一次模型请求之前」注入，
且不破坏打断收尾的 pad_unanswered_tool_calls 依赖的尾部结构。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.repeat_guard import (  # noqa: E402
    GENTLE_REMINDER, RepeatGuard, canonical_args)

from agent.config import AgentConfig  # noqa: E402
from agent.loop import run_turn  # noqa: E402
from agent.registry import ToolContext  # noqa: E402


# ---------- 单元：链语义 ----------

def test_chain_counts_identical_calls_and_hits_thresholds():
    """相同调用连击：3 温和、5/8 详细，非阈值点不提醒。"""
    g = RepeatGuard()
    outs = [g.observe("grep", {"pattern": "x"}) for _ in range(8)]
    assert all(o is None for o in outs[:2]), "前两次不应提醒"
    assert outs[2] == GENTLE_REMINDER, "第 3 次应是温和提醒"
    assert outs[3] is None and outs[4] is not None, "第 4 次无、第 5 次有"
    assert outs[7] is not None, "第 8 次应有"
    d = outs[4]
    assert "grep" in d and "5" in d, f"详细提醒要点名工具与次数：{d}"


def test_different_args_or_tool_resets_chain():
    """换参数或换工具都把连击重置为 1。"""
    g = RepeatGuard()
    for _ in range(2):
        g.observe("grep", {"pattern": "x"})
    assert g.observe("grep", {"pattern": "y"}) is None
    assert g.count == 1, "换参数应重置"
    g.observe("grep", {"pattern": "y"})
    g.observe("grep", {"pattern": "y"})
    assert g.count == 3
    g.observe("ls", {"path": "."})
    assert g.count == 1, "换工具应重置"


def test_arg_key_order_irrelevant():
    """参数键序不同视为同一调用（deep 规范化）。"""
    g = RepeatGuard()
    g.observe("t", {"a": 1, "b": {"x": 1, "y": 2}})
    g.observe("t", {"b": {"y": 2, "x": 1}, "a": 1})
    assert g.count == 2, "键序不同应视为相同调用"
    assert canonical_args({"b": 2, "a": 1}) == canonical_args({"a": 1, "b": 2})


def test_excluded_tool_is_transparent():
    """不受跟踪的工具既不计数也不重置：穿插不能掩盖循环。"""
    g = RepeatGuard(thresholds=(3,), exclude=("todo",))
    g.observe("grep", {"p": "x"})
    g.observe("todo", {"any": 1})     # 透明：不重置
    g.observe("grep", {"p": "x"})
    assert g.count == 2
    assert g.observe("grep", {"p": "x"}) == GENTLE_REMINDER, "穿插后仍应到 3"


def test_preview_caps_long_args():
    """详细提醒（后续阈值）里的参数预览截断，标注省略量。"""
    g = RepeatGuard(thresholds=(2, 3), preview_chars=10)
    for _ in range(2):
        g.observe("write", {"content": "a" * 100})
    d = g.observe("write", {"content": "a" * 100})   # 第 3 次：详细版
    assert "(+" in d and "字符)" in d, f"应标注省略量：{d}"
    assert len(d) < 160, f"预览应被截断到预算附近：{len(d)}"


def test_reset_clears_chain():
    g = RepeatGuard(thresholds=(2,))
    g.observe("t", {})
    g.reset()
    assert g.count == 0
    assert g.observe("t", {}) is None, "清零后第一次不应提醒"


# ---------- 单元：fail-loud 配置 ----------

@pytest.mark.parametrize("bad", [([], "空"), ((1,), "<2"), ((3, 3), "重复"),
                                 ((2.5,), "非整数")])
def test_invalid_thresholds_raise(bad):
    values, _ = bad
    with pytest.raises(ValueError):
        RepeatGuard(thresholds=values)


# ---------- loop 集成 ----------

class _LoopClient:
    """3 步重复同一工具、第 4 步带着提醒回答。记录每次请求的完整 history。"""

    def __init__(self):
        self.calls = 0
        self.seen = []

    def chat(self, messages, **kw):
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        if self.calls <= 3:
            return {"thinking": "", "content": "",
                    "tool_calls": [{"function": {"name": "list_files",
                                                 "arguments": {"path": "."}}}],
                    "stats": {}, "cancelled": False, "error": None}
        return {"thinking": "", "content": "完成", "tool_calls": [],
                "stats": {}, "cancelled": False, "error": None}


class _Em:
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


def test_loop_injects_reminder_before_next_request():
    """第 3 次相同调用命中阈值：提醒出现在第 4 次请求里、tool 消息之后。"""
    cfg = AgentConfig(use_color=False, warm=False)
    ctx = ToolContext(use_color=False, workspace=Path("."))
    client, em = _LoopClient(), _Em()
    run_turn(client, cfg, ctx, [{"role": "system", "content": "s"}], "hi",
             emitter=em)

    assert client.calls == 4
    last = client.seen[3]
    kinds = [m["role"] for m in last]
    assert kinds[-1] == "user" and last[-1]["content"].startswith("[系统提醒]"), \
        f"提醒应是最后注入的 user 消息：{kinds}"
    reminder = last[-1]["content"]
    assert "[系统提醒]" in reminder
    # 时序：提醒必须在其对应的 tool 结果之后
    assert any(m["role"] == "tool" for m in last[:-1]), "提醒前应有工具结果"
    assert any("连续第 3 次" in n for n in em.notices), \
        f"用户应看到提示：{em.notices}"


def test_new_turn_resets_count():
    """新回合（新用户消息）guard 清零：每回合最多 2 次重复不触发提醒。"""
    cfg = AgentConfig(use_color=False, warm=False)
    ctx = ToolContext(use_color=False, workspace=Path("."))
    for _ in range(2):
        client, em = _LoopClient(), _Em()
        run_turn(client, cfg, ctx, [{"role": "system", "content": "s"}], "hi",
                 emitter=em)
        # 默认阈值 3：每回合只重复 3 次同调用，第 3 次会提醒——
        # 这里断言两个独立回合互不累计即可通过回合数区分：
        # 若没有 reset，第二回合的第 1 次调用就会立即到 4（超过 3），
        # 提醒会提前出现在第 2 次请求里。
        for i, msgs in enumerate(client.seen[:-1], start=1):
            injected = [m for m in msgs if m.get("role") == "user"
                        and "[系统提醒]" in str(m.get("content"))]
            if i < 3:
                assert not injected, f"第 {i} 次请求不应带提醒"
