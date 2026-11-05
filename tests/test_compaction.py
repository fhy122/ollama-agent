"""会话压缩的验证判据（照 DeepSeek Harness compaction-basic.spec.ts 的关键用例）。

判据来源（文件:行）：
  :749  低于阈值不压 / 超阈值压缩 head
  :762  system head 永不被 shadow，保持在 surface 位置 0
  :880  切点回退以保护 tool-call/tool-result 配对
  :910  只有 1 个不可分工具对时拒绝压缩
  :1027 checkpoint 带 tag、精确元数据、可确定性回放
  :1277 替换物不比被遮蔽内容小 → 拒绝，且不落盘
  :1423 摘要请求结构（system + span 前缀 + 指令，不注册工具）
  core.zh.md:424 原始历史仍在日志，未被抹除
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import compaction, context_view          # noqa: E402
from agent.config import AgentConfig                 # noqa: E402

CHARS = 400


def _step(i: int, chars: int = CHARS) -> list[dict]:
    """一个完整工具对：assistant.tool_calls(1) ↔ tool(1)，配对平衡。"""
    return [
        {"role": "user", "content": f"指令{i} " + "x" * chars},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "function": {"name": "read_file",
                                         "arguments": {"path": f"f{i}.md"}}}]},
        {"role": "tool", "content": f"[文本] f{i}.md\n" + "z" * chars,
         "tool_name": "read_file", "tool_call_id": f"c{i}"},
    ]


def _hist(n: int, chars: int = CHARS) -> list[dict]:
    h = [{"role": "system", "content": "SYS"}]
    for i in range(n):
        h.extend(_step(i, chars))
    return h


def _small_cfg(**kw) -> AgentConfig:
    """小窗口让阈值容易触发；每项都可被覆盖以验证配置化。"""
    base = dict(num_ctx=1000, compaction_enabled=True,
                compaction_threshold_ratio=0.8,
                compaction_headroom_tokens=100,
                compaction_reserved_completion_tokens=100,
                compaction_retain_ratio=0.16,
                compaction_retries=1,
                compaction_summarizer="local",
                compaction_summary_max_chars=6000)
    base.update(kw)
    return AgentConfig(**base)


# ---------- 第 2 档：契约 / 估算 ----------

def test_token_estimate_follows_harness_formula():
    """estimate.ts:13-19：ceil(len/4) + 4(块) + 4(角色)。"""
    assert compaction.estimate_message_tokens({"role": "user", "content": "a" * 8}) == 10
    assert compaction.estimate_message_tokens({"role": "user", "content": ""}) == 8
    m = {"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "read_file", "arguments": {"path": "a.md"}}}]}
    assert compaction.estimate_message_tokens(m) > 8      # tool_calls 也要计价


def test_cjk_estimate_is_calibrated_against_real_usage():
    """ASCII 仍是 4 字符/token；CJK 按 2 校准，贴近本会话实测 0.3939~0.5052。

    若退回纯 4 字符/token，真实会话回放会晚 3 步才触发压缩（多烧 3 次全量）。
    """
    assert compaction.estimate_message_tokens({"role": "user", "content": "a" * 8}) == 10
    assert compaction.estimate_message_tokens({"role": "user", "content": "中文" * 100}) == 108
    text = "需求文档：请先读取文件并生成开发设计，注意保留精确路径与错误串。" * 200
    est = compaction.estimate_message_tokens({"role": "user", "content": text})
    ratio = est / len(text)
    assert 0.35 <= ratio <= 0.55, f"估算系数 {ratio:.4f} 偏离真实账单区间"


def test_threshold_and_retain_formula_is_configurable():
    """config.ts:172-197：min(W*0.8, W-O-B) / (W-O)*0.16。"""
    cfg = _small_cfg()
    assert compaction.threshold_tokens(cfg) == 800
    assert compaction.retain_tokens(cfg) == 144
    assert compaction.threshold_tokens(_small_cfg(num_ctx=500)) == 300
    # headroom 大于窗口时不能算出负数阈值
    assert compaction.threshold_tokens(_small_cfg(compaction_headroom_tokens=9999)) == 800


def test_disabled_config_never_compacts():
    h = _hist(12)
    assert compaction.compact_if_needed(h, _small_cfg(compaction_enabled=False),
                                        view=h) is None


# ---------- 触发与范围 ----------

def test_does_nothing_below_threshold_and_compacts_above(monkeypatch):
    """spec.ts:749：压力不够返回 None；够了才产 checkpoint。"""
    cfg = _small_cfg()
    small = _hist(1)
    assert compaction.estimate_tokens(small) < compaction.threshold_tokens(cfg)
    assert compaction.compact_if_needed(small, cfg, view=small) is None
    big = _hist(12)
    ck = compaction.compact_if_needed(big, cfg, view=big)
    assert ck is not None
    assert ck["shadowed_messages"] >= 1
    assert ck["checkpoint_tokens"] < ck["shadowed_tokens"]


def test_system_head_never_shadowed_and_view_shrinks():
    """spec.ts:762 + region.ts:131：system 保持第 0 条，视图确实变短。"""
    cfg, h = _small_cfg(), _hist(12)
    ck = compaction.compact_if_needed(h, cfg, view=h)
    assert ck is not None
    assert ck["shadowed_until"] >= 1
    h.append(ck)                                     # loop 的 append（只追加）
    view, shift = compaction.apply_checkpoints(h)
    assert view[0]["role"] == "system" and view[0]["content"] == "SYS"
    assert view[1]["content"].startswith(compaction.CHECKPOINT_PREAMBLE)
    assert len(view) < len(h)
    assert shift >= 0


def test_rounds_cut_headward_to_preserve_tool_pairing():
    """spec.ts:880：切点必须落在平衡边界，结果里每个 tool 都有前置 call。"""
    cfg, h = _small_cfg(), _hist(12)
    first, keep = compaction.select_compactable_range(h, compaction.retain_tokens(cfg))
    bal = compaction._pairing_balance(h)
    assert bal[keep] == 0, "切点处不能有未闭合的 tool_calls"
    assert first == 1
    view, _ = compaction.apply_checkpoints(h + [
        {"role": "user", "content": "ck", "compact_checkpoint": True,
         "shadowed_until": keep, "shadowed_tokens": 1}])
    pending = 0
    for m in view:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            pending += len(m["tool_calls"])
        elif m.get("role") == "tool":
            pending -= 1
        assert pending >= 0, "出现了没有对应 tool_call 的 tool 消息"


def test_declines_when_only_one_indivisible_tool_pair():
    """spec.ts:910 / region.ts:141-148：预算吃满时放弃；任一合法切点都是平衡边界。"""
    h = _hist(1)
    assert compaction.select_compactable_range(h, 10 ** 9) is None
    bal = compaction._pairing_balance(h)
    for budget in (1, 50, 100, 200, 400, 800, 2000):
        rng = compaction.select_compactable_range(h, budget)
        if rng is not None:
            assert bal[rng[1]] == 0, f"budget={budget} 的切点拆开了工具对"
            assert rng[0] == 1 and rng[1] > rng[0]
    # 尾部开口（assistant 声明了 tool_calls 但结果还没来）也不能压缩
    h2 = _hist(3) + [{"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "read_file", "arguments": {"path": "x"}}}]}]
    rng = compaction.select_compactable_range(h2, 1)
    assert rng is None or compaction._pairing_balance(h2)[rng[1]] == 0


def test_rejects_non_shrinking_summary_and_writes_nothing():
    """spec.ts:1277：替换物不比 span 小 → 拒绝压缩、不产生任何 checkpoint。"""
    cfg, h = _small_cfg(), _hist(12)

    def _huge(_span, _prior):
        return "W" * 100000          # 比被压缩内容还长

    assert compaction.compact_if_needed(h, cfg, view=h, summarize_fn=_huge) is None
    assert not any(compaction.is_checkpoint(m) for m in h)


# ---------- checkpoint 产物 / 回放 / 降级 ----------

def test_checkpoint_has_tags_sections_metadata_and_keeps_raw_history():
    """spec.ts:1027 + core.zh.md:424：tag/八段/元数据齐全，原始消息一字不删。"""
    cfg, h = _small_cfg(), _hist(12)
    before = json.dumps(h, ensure_ascii=False)
    ck = compaction.compact_if_needed(h, cfg, view=h)
    assert ck is not None
    assert compaction.CHECKPOINT_OPEN in ck["content"]
    assert compaction.CHECKPOINT_CLOSE in ck["content"]
    for title, _ in compaction._SECTIONS:
        assert title in ck["content"], f"缺段: {title}"
    for key in ("compaction_id", "shadowed_until", "shadowed_messages",
                "shadowed_tokens", "checkpoint_tokens"):
        assert key in ck
    assert json.dumps(h, ensure_ascii=False) == before, "压缩不能改写既有日志"
    h.append(ck)
    assert compaction.is_checkpoint(h[-1])
    assert "指令0" in before and "指令0" in json.dumps(h, ensure_ascii=False)


def test_replay_is_deterministic_after_json_roundtrip():
    """spec.ts:1063-1064：落盘再加载，派生视图必须完全一致。"""
    cfg, h = _small_cfg(), _hist(12)
    h.append(compaction.compact_if_needed(h, cfg, view=h))
    view_a, shift_a = compaction.apply_checkpoints(h)
    replayed = json.loads(json.dumps(h, ensure_ascii=False))
    view_b, shift_b = compaction.apply_checkpoints(replayed)
    assert view_b == view_a and shift_b == shift_a


def test_merges_prior_checkpoint_on_second_compaction():
    """summarizer.ts:66：旧 checkpoint 不整段照抄，合并后视图里只剩一条。"""
    cfg = _small_cfg(compaction_retries=2)
    h = _hist(12)
    ck1 = compaction.compact_if_needed(h, cfg, view=h)
    assert ck1 is not None
    h.append(ck1)
    h.extend(_step(99))
    view, _ = compaction.apply_checkpoints(h)
    ck2 = compaction.compact_if_needed(h, cfg, view=view, compacted_this_turn=1)
    assert ck2 is not None
    assert "yes" in ck2["content"]          # 标记已合并 prior
    h.append(ck2)
    out, _ = compaction.apply_checkpoints(h)
    assert sum(1 for m in out if compaction.CHECKPOINT_OPEN in (m.get("content") or "")) == 1


def test_summary_failure_falls_back_to_local_without_breaking_turn():
    """index.ts:165-172：摘要失败降级，不抛异常、仍产出有效 checkpoint。"""
    cfg, h = _small_cfg(), _hist(12)

    def _boom(_span, _prior):
        raise RuntimeError("LLM down")

    ck = compaction.compact_if_needed(h, cfg, view=h, summarize_fn=_boom)
    assert ck is not None and compaction.CHECKPOINT_OPEN in ck["content"]


def test_llm_summarizer_request_shape_and_prefix_cache(monkeypatch):
    """spec.ts:1067 / 1423：system + span 真前缀 + 指令末尾；不注册工具。"""
    captured = {}

    class FakeClient:
        def chat(self, messages, tools=None, **kw):
            captured["messages"] = messages
            captured["tools"] = tools
            return {"content": "## Primary Request and Intent\n- ok", "error": None}

    fn = compaction.make_llm_summarizer(FakeClient(), _small_cfg())
    span = _hist(2)[1:]
    out = fn(span, "")
    assert out.startswith("## Primary Request and Intent")
    assert captured["tools"] is None
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][-1]["content"] == compaction.COMPACTION_INSTRUCTION
    assert captured["messages"][1:-1] == [
        {"role": m["role"], "content": m.get("content") or "(tool call)"}
        for m in span]


# ---------- 与 B 方案投影层的集成 ----------

def test_project_messages_integrates_checkpoint_without_breaking_pairs():
    """集成档：checkpoint + skill 去重 + 大参数摘要同时生效，配对仍合法。"""
    cfg = _small_cfg(tool_call_arg_max_chars=200, warm=False)
    h = [{"role": "system", "content": "SYS"}]
    for i in range(4):
        h.extend(_step(i, CHARS))
    h.append({"role": "assistant", "content": "", "tool_calls": [
        {"id": "w1", "function": {"name": "write_file", "arguments": {
            "path": "big.md", "content": "B" * 3000}}}]})
    h.append({"role": "tool", "content": "[已写入·工作空间] big.md",
              "tool_name": "write_file", "tool_call_id": "w1"})
    view0 = context_view.project_messages(h, cfg=cfg)
    ck = compaction.compact_if_needed(h, cfg, view=view0)
    assert ck is not None
    h.append(ck)
    view = context_view.project_messages(h, user_text="继续", turn_start=len(h) - 2,
                                         cfg=cfg)
    assert view[0]["role"] == "system"
    pending = 0
    for m in view:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            pending += len(m["tool_calls"])
        elif m.get("role") == "tool":
            pending -= 1
        assert pending >= 0
    assert pending == 0, "末尾不能有未闭合的工具调用"
    # 大参数摘要仍然生效（B 的规则没被压缩吃掉）
    blob = json.dumps(view, ensure_ascii=False)
    assert "B" * 3000 not in blob


def test_session_store_roundtrip_keeps_checkpoint_and_view(tmp_path):
    """集成档：真实 SessionStore 存盘→加载后，checkpoint 字段与派生视图都不变。"""
    from webui.store import SessionStore

    cfg, h = _small_cfg(), _hist(12)
    ck = compaction.compact_if_needed(h, cfg, view=h)
    assert ck is not None
    h.append(ck)
    before_view, before_shift = compaction.apply_checkpoints(h)

    store = SessionStore(root=tmp_path)
    sid = "20261104-compaction-test"
    store.save({"id": sid, "history": h, "turns": [{"role": "user", "text": "继续"}]})
    data = store.load(sid)
    assert data is not None
    loaded = data["history"]
    cks = [m for m in loaded if compaction.is_checkpoint(m)]
    assert len(cks) == 1
    for key in ("compaction_id", "shadowed_until", "shadowed_messages",
                "shadowed_tokens", "checkpoint_tokens"):
        assert cks[0][key] == ck[key], f"字段 {key} 在存盘后丢失/变形"
    after_view, after_shift = compaction.apply_checkpoints(loaded)
    assert after_view == before_view and after_shift == before_shift
    # 恢复后继续一轮：视图仍合法、不会把 checkpoint 当成用户气泡渲染
    assert all(m.get("role") != "user" or not m.get("compact_checkpoint")
               for m in after_view)


def test_corrupt_checkpoint_metadata_is_ignored_safely():
    h = _hist(3)
    h.append({"role": "user", "content": "ck", "compact_checkpoint": True,
              "shadowed_until": 99999})
    view, shift = compaction.apply_checkpoints(h)
    assert shift == 0 and len(view) == len(h)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
