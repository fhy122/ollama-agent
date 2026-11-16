"""工具结果预算淘汰的验证判据（context-view 第四层折叠）。

病灶：改代码时一次 read 十几个中等文件，单条都没到 spill 线（12000 字符），
却全部留在 history，后续每一步工具调用、每一轮新对话都重推一遍。
spill 管「单条太大」，compaction 管「旧轮次整体」，这一层管「很多条加起来太大」。

判据：
  1. cfg=None / 开关关闭 → 原样返回（与 slim_tool_call_args 的保守约定一致）
  2. 预算内 → 一条不动
  3. 超预算 → 只降级 content，tool_call_id / tool_name 原样保留（不破坏配对）
  4. 最近 N 个工具批次无条件保留（当前任务正在用）
  5. 与当前问题相关的早期结果优先保留（相关性 > 新旧）
  6. history 一字不改，日志仍可回放
  7. 已是 spill 预览的不二次降级
  8. 短结果不参与淘汰（省不了多少还伤可读性）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import compaction, context_view          # noqa: E402
from agent.config import AgentConfig                 # noqa: E402

CHARS = 3000          # 每个文件约 750 tokens，稳稳超过单条 800 字符门槛


def _file(i: int, name: str, chars: int = CHARS, marker: str = "") -> list[dict]:
    body = ((marker or "def f(): return 1") + "\n") * max(1, chars // 20)
    return [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": f"c{i}", "function": {"name": "read_file",
                                         "arguments": {"path": name}}}]},
        {"role": "tool", "content": f"[文本] {name}\n" + body,
         "tool_name": "read_file", "tool_call_id": f"c{i}"},
    ]


def _hist(names: list[str], chars: int = CHARS) -> list[dict]:
    h = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "重构模块"}]
    for i, name in enumerate(names):
        marker = "auth login token session" if "auth" in name else ""
        h.extend(_file(i, name, chars, marker))
    return h


def _cfg(**kw) -> AgentConfig:
    """小窗口让预算容易触发；每项都可覆盖以验证配置化。"""
    base = dict(num_ctx=8000, tool_result_cap_enabled=True,
                tool_result_budget_ratio=0.2, tool_result_keep_recent=2,
                tool_result_min_chars=800)
    base.update(kw)
    return AgentConfig(**base)


def _paths_by_id(view: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for m in view:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for c in m["tool_calls"]:
                out[c["id"]] = str(c["function"]["arguments"].get("path") or "")
    return out


def _omitted(view: list[dict]) -> list[str]:
    return [m["tool_call_id"] for m in view
            if m.get("role") == "tool" and "工具结果已省略" in (m.get("content") or "")]


def test_none_cfg_and_disabled_are_noops():
    """判据 1：不传 cfg（老调用）/ 开关关闭，都不能偷偷改视图。"""
    h = _hist([f"src/mod{i}.py" for i in range(12)])
    assert context_view.project_messages(h) == h
    cfg = _cfg(tool_result_cap_enabled=False)
    assert context_view.project_messages(h, user_text="改 auth", cfg=cfg) == h


def test_under_budget_keeps_every_result():
    """判据 2：总量没超预算，一条都不动。"""
    h = _hist([f"src/mod{i}.py" for i in range(3)])
    cfg = _cfg(num_ctx=65536)          # 预算 13107 tokens，远大于 3 条
    out = context_view.project_messages(h, user_text="继续", cfg=cfg)
    assert _omitted(out) == []
    assert out == h


def test_over_budget_degrades_but_keeps_pairing_fields():
    """判据 3：超预算只改 content，tool_call_id / tool_name / 条数不变。"""
    h = _hist([f"src/mod{i}.py" for i in range(12)])
    cfg = _cfg()
    out = context_view.project_messages(h, user_text="继续", cfg=cfg)
    assert _omitted(out), "12 个文件应触发淘汰"
    assert len([m for m in out if m.get("role") == "tool"]) == 12
    for m in out:
        if m.get("role") == "tool":
            assert m.get("tool_call_id") and m.get("tool_name") == "read_file"
    # 配对平衡：每个 tool 都有前置 call
    pending = 0
    for m in out:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            pending += len(m["tool_calls"])
        elif m.get("role") == "tool":
            pending -= 1
        assert pending >= 0
    assert pending == 0


def test_recent_results_are_always_kept():
    """判据 4：最近 N 条工具结果无条件保留——当前任务正靠它们干活。"""
    h = _hist([f"src/mod{i}.py" for i in range(12)])
    cfg = _cfg()
    out = context_view.project_messages(h, user_text="继续", cfg=cfg)
    ids = [m["tool_call_id"] for m in out if m.get("role") == "tool"]
    paths = _paths_by_id(out)
    omitted = set(_omitted(out))
    assert ids[-1] not in omitted
    assert paths[ids[-1]].endswith("mod11.py")
    assert ids[-2] not in omitted


def test_relevant_early_result_beats_newer_irrelevant_ones():
    """判据 5：auth.py 虽然最早读，但命中当前问题，必须优先于更新的无关文件。"""
    names = ["src/auth.py", "src/db.py", "src/utils.py"] + [
        f"src/mod{i}.py" for i in range(9)]
    h = _hist(names)
    cfg = _cfg(num_ctx=30000)          # 预算 6000：留出相关性排序的空间
    out = context_view.project_messages(h, user_text="auth 登录 token 怎么处理", cfg=cfg)
    omitted = set(_omitted(out))
    paths = _paths_by_id(out)
    auth_id = next(cid for cid, p in paths.items() if p.endswith("auth.py"))
    assert auth_id not in omitted, "相关文件被误删了——这比膨胀更糟"
    # 无关的旧文件应被淘汰（预算装不下全部）
    assert any("auth" not in paths[cid] for cid in omitted)


def test_history_is_never_modified():
    """判据 6：只改派生视图，落盘日志一字不动、可回放。"""
    h = _hist([f"src/mod{i}.py" for i in range(12)])
    before = json.dumps(h, ensure_ascii=False)
    context_view.project_messages(h, user_text="继续", cfg=_cfg())
    assert json.dumps(h, ensure_ascii=False) == before


def test_spill_preview_is_not_degraded_twice():
    """判据 7：已经是 spill 预览（带落盘路径）的结果，不再二次改写。"""
    h = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "继续"}]
    h.extend(_file(0, "src/big.py"))
    preview = h[-1]["content"][:600] + (
        "\n\n(省略 9000 字节。完整结果已存至: /tmp/spill/big.txt。"
        "可用 read_file 带 offset/limit 分段读取，或用 grep_file 在该文件内搜索。)")
    h[-1] = {**h[-1], "content": preview}
    h.extend(_file(1, "src/other.py"))
    out = context_view.project_messages(h, user_text="继续", cfg=_cfg())
    assert preview in [m.get("content") for m in out if m.get("role") == "tool"]


def test_short_results_are_never_pruned():
    """判据 8：短结果（< tool_result_min_chars）不参与淘汰。"""
    h = _hist([f"src/mod{i}.py" for i in range(2)], chars=200)
    cfg = _cfg(num_ctx=1000)           # 预算 200 tokens，但每条都太短
    out = context_view.project_messages(h, user_text="继续", cfg=cfg)
    assert _omitted(out) == []


def test_config_fields_survive_normalize_and_patch(tmp_path):
    """判据 9：ui 段的新字段能被配置存储保留，并透传到 AgentConfig 补丁。

    否则「写进 config.json 但被白名单丢掉」会变成静默失效——用户以为调了，
    实际还在用默认值。
    """
    from agent.userconfig import ConfigStore, to_agent_patch

    store = ConfigStore(str(tmp_path / "config.json"))
    store.save({"ui": {"tool_result_budget_ratio": 0.15,
                       "tool_result_keep_recent": 2,
                       "tool_result_min_chars": 1200},
                "profiles": [], "active": ""})
    data = store.load()
    assert data["ui"]["tool_result_budget_ratio"] == 0.15
    assert data["ui"]["tool_result_keep_recent"] == 2
    assert data["ui"]["tool_result_min_chars"] == 1200

    patch = to_agent_patch({}, data["ui"])
    assert patch["tool_result_budget_ratio"] == 0.15
    assert patch["tool_result_keep_recent"] == 2
    assert patch["tool_result_min_chars"] == 1200


def test_bad_config_values_fall_back_instead_of_crashing(tmp_path):
    """手改坏的配置当没写：不抛异常，沿用默认值。"""
    from agent.userconfig import ConfigStore

    store = ConfigStore(str(tmp_path / "config.json"))
    store.save({"ui": {"tool_result_budget_ratio": "abc",
                       "tool_result_keep_batches": None},
                "profiles": [], "active": ""})
    data = store.load()
    assert "tool_result_budget_ratio" not in data["ui"]
    assert "tool_result_keep_batches" not in data["ui"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
