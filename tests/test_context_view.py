"""请求视图投影（agent/context_view.py）离线自测。

判据随功能一起写，覆盖规范第三节的三类场景：
  正常路径 —— 重复技能正文只保留一份，且消息条数/配对字段不丢
  错误路径 —— 折叠文案必须告诉模型「下一步怎么做」，不能只写「已省略」
  边界     —— 同名不同正文、非 skill 工具、无技能头、空历史、超大/中文正文
  集成     —— run_turn 真发请求时模型看到折叠视图，落盘 history 一字不改

运行: python -m pytest tests/test_context_view.py -q
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import context_view  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.loop import run_turn  # noqa: E402
from agent.registry import ToolContext  # noqa: E402

BODY = "[技能: demo]\n# Demo 技能\n先读文件，再写文件，最后跑验证。\n\n[环境提示] 用 tools."


def _skill(body: str = BODY, cid: str = "call_1") -> dict:
    return {"role": "tool", "content": body, "tool_name": "skill",
            "tool_call_id": cid}


def test_duplicate_skill_body_folded() -> None:
    """正常路径：第二份同正文技能被换成短提示，结构字段不丢。"""
    hist = [{"role": "system", "content": "s"},
            _skill(cid="call_1"), _skill(cid="call_2")]
    out = context_view.project_messages(hist)

    assert len(out) == len(hist), "绝不能删消息：tool_calls 要逐条配对"
    assert out[1] is hist[1], "第一份应原样保留"
    assert out[2]["content"] != BODY
    assert out[2]["tool_name"] == "skill"
    assert out[2]["tool_call_id"] == "call_2", "配对 id 必须保留"

    # 错误路径的文案要有指路：既说清省了什么，也给出可执行的下一步
    assert "[技能: demo]" in out[2]["content"]
    assert "请直接按上文该技能的指令继续执行任务" in out[2]["content"]
    assert 'skill(name="demo")' in out[2]["content"]


def test_projection_never_mutates_log() -> None:
    """日志不可变：投影前后 history 深比较完全一致（DSH AGENTS.md:138）。"""
    hist = [_skill(cid="call_1"), _skill(cid="call_2"),
            {"role": "user", "content": "继续"}]
    before = copy.deepcopy(hist)
    out = context_view.project_messages(hist)
    assert hist == before
    assert out[0] is hist[0] and out[2] is hist[2], "未改写的消息应复用原对象"


def test_same_name_different_body_not_folded() -> None:
    """边界：技能文件被改过 / max_chars 不同 → 内容不同就不能折叠。"""
    other = BODY + "\n[已截断] 全文更长"
    out = context_view.project_messages([_skill(), _skill(other)])
    assert out[1]["content"] == other, "正文不同的同名技能必须保留"


def test_other_tools_and_malformed_skill_untouched() -> None:
    """边界：非 skill 结果、无技能头的 skill 结果、空历史都不动。"""
    msgs = [
        {"role": "tool", "content": BODY, "tool_name": "read_file"},
        {"role": "tool", "content": "共 3 个技能", "tool_name": "skill"},
        {"role": "assistant", "content": "好的"},
    ]
    assert context_view.project_messages(msgs) == msgs
    assert context_view.project_messages([]) == []


def test_large_cjk_body_folded() -> None:
    """边界：超大中文正文按 UTF-8 摘要判定，不切字符、不误判。"""
    big = "[技能: 大技能]\n" + "中文正文" * 3000
    out = context_view.project_messages([_skill(big, "c1"), _skill(big, "c2")])
    assert out[0]["content"] == big
    assert len(out[1]["content"]) < 200


class _Recorder:
    """假模型：记下每次请求真正收到的 messages。"""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools=None, on_text=None, on_think=None, cancel=None,
             on_wait=None, think_hidden=True):
        self.calls.append([dict(m) for m in messages])
        return {"content": "完成", "tool_calls": [], "stats": {},
                "cancelled": False, "error": None, "ttft": None}


def test_run_turn_sends_folded_view_but_keeps_log() -> None:
    """集成：真跑 run_turn，模型只看一份技能正文，落盘 history 仍是两份。"""
    import tools  # noqa: F401  触发工具注册（all_specs 依赖）

    hist = [{"role": "system", "content": "s"},
            _skill(cid="call_1"), _skill(cid="call_2")]
    raw_bodies_before = [m["content"] for m in hist if m.get("tool_name") == "skill"]
    client = _Recorder()
    cfg = AgentConfig(use_color=False, warm=False, time_context=False)
    ctx = ToolContext(workspace=Path.cwd(), cfg=cfg, confirm=False)

    out = run_turn(client, cfg, ctx, hist, "开始", emitter=None)

    assert out == "完成"
    assert len(client.calls) == 1
    sent = client.calls[0]
    bodies = [m["content"] for m in sent if m.get("tool_name") == "skill"]
    assert bodies.count(BODY) == 1, f"模型应只看到一份正文: {bodies}"
    assert any("不要再次调用" in b for b in bodies)
    # 请求视图是派生品，日志本体一字未动
    assert [m["content"] for m in hist if m.get("tool_name") == "skill"] == raw_bodies_before


# ---------- spill 预览的技能去重（B：指纹忽略落盘路径差异） ----------

def _spilled_pair(tmp_path, body: str | None = None) -> tuple[str, str]:
    """用真实 spill 生成两次预览：路径带时间戳/序号，文本必然不同。"""
    from agent import spill

    big = body or ("[技能: big]\n" + "大技能指令正文：按规范同手写同手验。\n" * 700)
    a = spill.spill_if_oversize("skill", big, tmp_path, 12000)
    b = spill.spill_if_oversize("skill", big, tmp_path, 12000)
    assert a != b, "两次落盘路径不同，预览文本必须不同，否则测不到这个 bug"
    assert a.startswith("[技能:") and b.startswith("[技能:"), "预览要保留技能头"
    return a, b


def test_spilled_skill_duplicate_folded(tmp_path) -> None:
    """回归：大技能被 spill 两次 → 仍只留一份预览，第二份换成短提示。"""
    a, b = _spilled_pair(tmp_path)
    hist = [_skill(a, "c1"), _skill(b, "c2")]
    before = copy.deepcopy(hist)
    out = context_view.project_messages(hist)

    assert len(out) == 2, "绝不能删消息"
    assert out[0]["content"] == a, "第一份预览原样保留"
    assert len(out[1]["content"]) < 200, f"第二份必须折叠: {len(out[1]['content'])}"
    assert "不要再次调用" in out[1]["content"]
    assert out[1]["tool_call_id"] == "c2"
    # spill 场景下模型读不到全文，折叠文案必须给出取回指路
    assert "read_file" in out[1]["content"]
    assert hist == before, "落盘日志一字不改"


def test_spilled_skill_different_body_not_folded(tmp_path) -> None:
    """边界：同名但正文不同（长度不同）→ 归一化后仍不能误折叠。"""
    from agent import spill

    a = spill.spill_if_oversize("skill", "[技能: big]\n" + "甲" * 13000,
                                tmp_path, 12000)
    b = spill.spill_if_oversize("skill", "[技能: big]\n" + "乙" * 16000,
                                tmp_path, 12000)
    assert a != b
    out = context_view.project_messages([_skill(a, "c1"), _skill(b, "c2")])
    assert out[1]["content"] == b, "内容不同就必须保留"


def test_plain_and_spilled_same_skill_not_folded(tmp_path) -> None:
    """边界：一次全文、一次 spill 预览，可见信息量不同 → 保守不折叠。"""
    _, spilled = _spilled_pair(tmp_path, body="[技能: big]\n" + "甲" * 13000)
    plain = "[技能: big]\n" + "甲" * 13000
    out = context_view.project_messages(
        [_skill(plain, "c1"), _skill(spilled, "c2")])
    assert out[1]["content"] == spilled


# ---------- 打断轮折叠（轮次边界投影，不删日志） ----------

SAME = "现在进行开发设计吧"


def _aborted_history(prev_content: str = SAME, abort: bool = True,
                     productive: bool = False) -> tuple[list[dict], int]:
    hist = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": prev_content},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1_0", "function": {
                "name": "read_file", "arguments": {"path": "doc.md"}}}]},
        {"role": "tool", "content": "[文本] doc.md 很长的内容",
         "tool_name": "read_file", "tool_call_id": "call_1_0"},
    ]
    if productive:
        hist.append({"role": "tool", "content": "[已写入·工作空间] out.md",
                     "tool_name": "write_file", "tool_call_id": "call_2_0"})
    hist.append({"role": "assistant",
                 "content": "[已被用户打断]" if abort else "上一轮已完成"})
    hist.append({"role": "user", "content": SAME})
    return hist, len(hist) - 1


def test_aborted_repeat_turn_collapsed() -> None:
    """正常路径：同一句被打断且零产出 → 请求视图跳过整段探索。"""
    hist, start = _aborted_history()
    before = copy.deepcopy(hist)
    out = context_view.project_messages(hist, user_text=SAME, turn_start=start)

    assert len(out) == 2, f"只应剩 system + 本轮 user: {out}"
    assert out[0] is hist[0]
    assert "[文本] doc.md" not in out[-1]["content"]
    # 错误路径要指路：说清发生了什么、下一步怎么做
    assert "被打断" in out[-1]["content"] and "没有任何文件产出" in out[-1]["content"]
    assert "用工具重新读取当前文件状态" in out[-1]["content"]
    assert "不要引用上一轮的中间结果" in out[-1]["content"]
    assert SAME in out[-1]["content"]
    assert hist == before, "落盘日志一条都不能少"


def test_aborted_turn_with_output_not_collapsed() -> None:
    """边界：上一轮写过文件 → 不折叠，宁可多花 token 也不丢成果。"""
    hist, start = _aborted_history(productive=True)
    assert context_view.project_messages(hist, user_text=SAME, turn_start=start) == hist


def test_completed_turn_not_collapsed() -> None:
    """边界：上一轮正常完成（无打断标记）→ 不折叠。"""
    hist, start = _aborted_history(abort=False)
    assert context_view.project_messages(hist, user_text=SAME, turn_start=start) == hist


def test_different_instruction_not_collapsed() -> None:
    """边界：用户换了指令（哪怕上一轮被打断）→ 不折叠。"""
    hist, start = _aborted_history(prev_content="先读一下需求文档")
    assert context_view.project_messages(hist, user_text=SAME, turn_start=start) == hist


def test_turn_boundary_edge_cases() -> None:
    """边界：无上一轮 / 边界缺失 / 越界 / 空历史，一律原样返回。"""
    one = [{"role": "system", "content": "s"},
           {"role": "user", "content": SAME}]
    assert context_view.project_messages(one, SAME, 1) == one
    assert context_view.project_messages(one, SAME, None) == one
    assert context_view.project_messages(one, SAME, 99) == one
    assert context_view.project_messages([], SAME, 0) == []


def test_run_turn_collapses_aborted_turn_in_view_only() -> None:
    """集成：真跑 run_turn，模型看不到失败轮，日志仍完整。"""
    import tools  # noqa: F401  触发工具注册

    hist, _ = _aborted_history()
    hist.pop()                       # 当前轮由 run_turn 自己追加
    before = copy.deepcopy(hist)
    client = _Recorder()
    cfg = AgentConfig(use_color=False, warm=False, time_context=False)
    ctx = ToolContext(workspace=Path.cwd(), cfg=cfg, confirm=False)

    out = run_turn(client, cfg, ctx, hist, SAME, emitter=None)

    assert out == "完成"
    sent = client.calls[0]
    assert not any(m.get("tool_name") == "read_file" for m in sent), sent
    assert any("用工具重新读取当前文件状态" in (m.get("content") or "")
               for m in sent if m.get("role") == "user")
    assert hist[:-2] == before, "除本轮新增的消息外，落盘日志一字不改"


# ---------- 工具调用大参数摘要 ----------

BIG = "# 设计文档\n" + "内容" * 12000


def _write_call(content: str = BIG, path: str = "doc/out.md") -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "call_6_0", "function": {
            "name": "write_file", "arguments": {"path": path, "content": content}}}]}


def test_large_write_file_arg_slimmed() -> None:
    """正常路径：2 万字 content 换成摘要，配对字段与 path 保留，日志不动。"""
    hist = [{"role": "system", "content": "s"}, _write_call(),
            {"role": "tool", "content": "[已写入·工作空间] doc/out.md",
             "tool_name": "write_file", "tool_call_id": "call_6_0"}]
    before = copy.deepcopy(hist)
    out = context_view.project_messages(hist, cfg=AgentConfig())

    calls = out[1]["tool_calls"]
    assert len(calls) == 1 and calls[0]["id"] == "call_6_0"
    args = calls[0]["function"]["arguments"]
    assert args["path"] == "doc/out.md", "路径必须原样保留"
    assert len(args["content"]) < 200
    assert str(len(BIG)) in args["content"], "要说清省了多少"
    # 错误路径的文案要指路：可复制的取回命令 + 明确禁止凭记忆重建
    assert 'read_file(path="doc/out.md")' in args["content"]
    assert "不要凭记忆重建" in args["content"]
    assert hist == before, "落盘日志里的全文一字不改"


def test_small_args_and_other_tools_untouched() -> None:
    """边界：小参数、白名单外工具、非 dict 参数、关闭开关 → 原样。"""
    small = _write_call(content="短内容")
    other = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "function": {"name": "web_chat",
                                  "arguments": {"question": BIG}}}]}
    as_str = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c2", "function": {"name": "write_file",
                                  "arguments": '{"content": "' + "x" * 100 + '"}'}}]}
    hist = [small, other, as_str]
    assert context_view.project_messages(hist, cfg=AgentConfig()) == hist

    big = [_write_call()]
    assert context_view.project_messages(
        big, cfg=AgentConfig(tool_call_arg_max_chars=0)) == big


def test_edit_file_both_args_slimmed() -> None:
    """边界：edit_file 的 old_string / new_string 两个大字段都要处理。"""
    hist = [{"role": "assistant", "content": "", "tool_calls": [
        {"id": "c3", "function": {"name": "edit_file", "arguments": {
            "path": "a.py", "old_string": BIG, "new_string": BIG[::-1]}}}]}]
    out = context_view.project_messages(hist, cfg=AgentConfig())
    args = out[0]["tool_calls"][0]["function"]["arguments"]
    assert args["path"] == "a.py"
    assert "已省略" in args["old_string"] and "已省略" in args["new_string"]
    assert hist[0]["tool_calls"][0]["function"]["arguments"]["old_string"] == BIG


def test_run_turn_sends_slimmed_tool_calls() -> None:
    """集成：真跑 run_turn，模型看到摘要参数，落盘 history 仍是全文。"""
    import tools  # noqa: F401  触发工具注册

    hist = [{"role": "system", "content": "s"}, _write_call(),
            {"role": "tool", "content": "[已写入·工作空间] doc/out.md",
             "tool_name": "write_file", "tool_call_id": "call_6_0"}]
    client = _Recorder()
    cfg = AgentConfig(use_color=False, warm=False, time_context=False)
    ctx = ToolContext(workspace=Path.cwd(), cfg=cfg, confirm=False)

    out = run_turn(client, cfg, ctx, hist, "继续", emitter=None)

    assert out == "完成"
    sent_calls = [m for m in client.calls[0] if m.get("tool_calls")]
    assert len(sent_calls) == 1
    assert "已省略" in sent_calls[0]["tool_calls"][0]["function"]["arguments"]["content"]
    assert hist[1]["tool_calls"][0]["function"]["arguments"]["content"] == BIG
