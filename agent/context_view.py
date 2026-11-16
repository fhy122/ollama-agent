"""请求视图投影：把持久 history 折叠成发给模型的精简视图。

设计参考 DeepSeek Harness（根 AGENTS.md:138「Model-visible ⟺ logged」）：
落盘的 history 是只追加的会话日志，可回放、可 fork；所有省 token 的折叠
只发生在「发请求前的派生视图」里，绝不改写日志本身。这样上下文变小了，
会话记录仍然完整，模型看到的每一条都能在日志里找到出处。

借鉴 DSH 的分域 digest 去重（agent-instructions/digest.ts、
skill/tool-skill/src/index.ts:347）：按「技能名 + 正文内容摘要」判定重复，
同名但内容不同不折叠；并在折叠文案里明确告诉模型下一步怎么做，
对应 tool-skill/README.zh.md 的「防双重加载」提示。

spill 过的技能正文要额外归一化：agent/spill.py:36-46 给落盘文件加时间戳
与序号，同一技能连读两次会得到「只差一个路径」的不同文本，按全文 sha1
判重会漏；:31-33 的 footer 里「省略 N 字节」则保留，避免长度不同的正文
被误判成同一份。

打断轮的处理参考 DSH「日志仅追加 + fork(boundary)」：不做删除式回滚，
而是只在请求视图里从稳定的轮次边界跳过无产出的失败轮
（docs/subsystems/session.zh.md:690-692、packages/core/agent-loop/README.zh.md:120），
磁盘上的 history 一字不改。

会话压缩（agent/compaction.py）在这三层折叠**之前**生效：它按
surfaceOp.replace 的语义把旧前缀换成一条 checkpoint，并返回下标位移，
让基于 history 绝对下标的 turn_start 能正确映射到压缩后的视图。
"""
from __future__ import annotations

import hashlib
import re

from agent import compaction

_SKILL_HEAD = re.compile(r"^\[技能:\s*([^\]]+)\]")

# spill footer 的落盘路径（agent/spill.py:31-33）；锚定文本末尾，
# 正文里偶然出现同样的句式不会被误改。末尾的「省略 N 字节」不参与归一化。
_SPILL_PATH_RE = re.compile(
    r"(完整结果已存至:\s*).+?(。可用 read_file[^\n]*\)$)")

# 上一轮「没做成」的标记：用户打断、流式断连、未执行的工具占位。
_ABORT_MARKS = ("[已被用户打断]", "[已被用户打断，未执行]", "[回答中断：")
# 上一轮「确实做成了」的标记：有任何一条命中就不折叠，避免把成果从上下文抹掉。
_PRODUCTIVE_MARKS = ("[已写入", "[已修改", "[已删除", "[已生成", "[已保存", "exit 0·")

# 只有「写文件的工具 + 大段文本参数」才摘要；工具名/字段白名单之外的
# 参数（如 web_chat.question）保持原样，避免模型忘了自己问过什么。
_ARG_KEYS = {
    "write_file": ("content",),
    "edit_file": ("old_string", "new_string"),
}


def _skill_name(text: str) -> str | None:
    m = _SKILL_HEAD.match(text or "")
    return m.group(1).strip() if m else None


def _skill_fingerprint(text: str) -> str:
    """去重指纹：把 spill 预览里随时间变化的落盘路径换成固定占位符。

    指纹只用于「同一份正文是否重复」的相对判断，不做内容寻址；
    未 spill 的正文不匹配该正则，行为与改动前完全一致。
    """
    stable = _SPILL_PATH_RE.sub(r"\1<spill>\2", text)
    return hashlib.sha1(stable.encode("utf-8")).hexdigest()


def dedup_skill_bodies(messages: list[dict]) -> list[dict]:
    """同名同正文的 skill 结果只保留第一份，后续换成短提示。

    不删消息、不丢 tool_call_id —— Ollama/OpenAI 都要求 assistant.tool_calls
    与随之而来的 tool 消息逐条配对，删消息会让下一次请求直接 400。
    返回新列表；仅对需要改写的消息做浅拷贝，原 history 不被修改。
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for msg in messages:
        if msg.get("role") != "tool" or msg.get("tool_name") != "skill":
            out.append(msg)
            continue
        text = msg.get("content") or ""
        name = _skill_name(text)
        if not name:
            out.append(msg)
            continue
        key = (name, _skill_fingerprint(text))
        if key in seen:
            new = dict(msg)
            # 预览只是头尾片段，模型本来就读不到全文：折叠后要告诉它
            # 第一份预览里的路径仍能取回完整技能，别凭记忆补内容。
            spill_hint = ("上文那份预览里有完整结果的落盘路径，"
                          "需要全文时用 read_file 取回，不要凭记忆重建。"
                          if _SPILL_PATH_RE.search(text) else "")
            new["content"] = (
                f"[技能: {name}] 本会话早前已加载过完全相同的正文，此处省略重复副本。"
                "请直接按上文该技能的指令继续执行任务，"
                f'不要再次调用 skill(name="{name}")。' + spill_hint
            )
            out.append(new)
        else:
            seen.add(key)
            out.append(msg)
    return out


def collapse_aborted_turn(messages: list[dict], user_text: str = "",
                          turn_start: int | None = None) -> list[dict]:
    """上一轮是「同一指令 + 被打断 + 零产出」时，从请求视图里跳过它。

    turn_start 是本轮 user 消息在 history 里的下标（run_turn 追加前记录），
    相当于 DSH 的 turn boundary。只折叠派生视图，日志仍完整可回放。
    """
    if (turn_start is None or turn_start <= 0
            or turn_start >= len(messages) or not messages):
        return messages
    cur = messages[turn_start]
    if cur.get("role") != "user":
        return messages
    prev_i = next((i for i in range(turn_start - 1, -1, -1)
                   if messages[i].get("role") == "user"), None)
    if prev_i is None:
        return messages
    prev = messages[prev_i]
    if (prev.get("content") or "").strip() != (cur.get("content") or "").strip():
        return messages                       # 不是重发同一句，别动

    span = messages[prev_i + 1:turn_start]    # 上一轮的探索过程
    if not any(any(k in (m.get("content") or "") for k in _ABORT_MARKS)
               for m in span):
        return messages                       # 上一轮正常完成，别动
    if any(m.get("role") == "tool"
           and any(k in (m.get("content") or "") for k in _PRODUCTIVE_MARKS)
           for m in span):
        return messages                       # 有产出，宁可多花 token 也不丢上下文

    label = (user_text or (cur.get("content") or "")).strip()[:60]
    note = (f"[系统] 上一轮收到同一条指令「{label}」后被打断，且没有任何文件产出；"
            f"该轮 {len(span)} 条探索消息已从本次请求中省略"
            "（会话日志仍完整保留，可回放）。"
            "请直接重新开始该任务，用工具重新读取当前文件状态，"
            "不要引用上一轮的中间结果。\n\n")
    new_cur = dict(cur)
    new_cur["content"] = note + (cur.get("content") or "")
    return messages[:prev_i] + [new_cur] + messages[turn_start + 1:]


def slim_tool_call_args(messages: list[dict], max_chars: int) -> list[dict]:
    """把 assistant.tool_calls 里的大段文本参数换成摘要 + 取回指引。

    生成一份 2 万字的文档，既算输出 token，又会作为工具参数回填历史被
    后续每一步重复付一遍。磁盘上那一份是事实来源，模型要改再 read_file。
    只改派生视图、不删 tool_calls、不删配对消息。
    """
    if max_chars <= 0:
        return messages
    out: list[dict] = []
    for msg in messages:
        calls = msg.get("tool_calls") if msg.get("role") == "assistant" else None
        if not calls:
            out.append(msg)
            continue
        new_calls = []
        changed = False
        for call in calls:
            fn = call.get("function") or {}
            keys = _ARG_KEYS.get(fn.get("name"))
            args = fn.get("arguments")
            if not keys or not isinstance(args, dict) or not any(
                    isinstance(args.get(k), str) and len(args[k]) > max_chars
                    for k in keys):
                new_calls.append(call)
                continue
            new_args = dict(args)
            path = str(args.get("path") or "")
            for k in keys:
                v = args.get(k)
                if not isinstance(v, str) or len(v) <= max_chars:
                    continue
                hint = (f'read_file(path="{path}")' if path else
                        "read_file 按路径读回")
                new_args[k] = (
                    f"[本条工具调用的 {k} 参数共 {len(v)} 字符，"
                    "在请求视图里已省略；文件内容以磁盘为准，"
                    f"需要时用 {hint} 读回，不要凭记忆重建这段文本。]"
                )
            new_fn = dict(fn)
            new_fn["arguments"] = new_args
            new_call = dict(call)
            new_call["function"] = new_fn
            new_calls.append(new_call)
            changed = True
        if changed:
            new = dict(msg)
            new["tool_calls"] = new_calls
            out.append(new)
        else:
            out.append(msg)
    return out


# 当前问题里的检索词：英文标识符按整词，中文按 2-gram（无分词依赖）。
_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]+")

# spill 预览的标记（agent/spill.py:28）：已是预览就不再二次降级。
_SPILL_MARK = "完整结果已存至"


def _query_terms(text: str) -> set[str]:
    """把当前问题拆成轻量检索词，用于判断哪条工具结果更相关。

    不做向量检索：会话内判断相关性，文件名/标识符命中的信号已经足够强，
    而 2-gram 能覆盖中文路径与术语，不引入分词或 embedding 依赖。
    """
    terms = {w.lower() for w in _WORD_RE.findall(text or "")}
    for run in _CJK_RUN_RE.findall(text or ""):
        for i in range(len(run) - 1):
            terms.add(run[i:i + 2])
    return terms


def _hits(text: str, terms: set[str]) -> int:
    if not terms:
        return 0
    low = (text or "").lower()
    return sum(1 for t in terms if t in low)


def _omit_tool_result(msg: dict, path: str) -> str:
    """降级文案：告诉模型「省略了什么、原文在哪、怎么取回」，禁止凭记忆重建。"""
    name = str(msg.get("tool_name") or "工具")
    n = len(msg.get("content") or "")
    if name == "read_file" and path:
        how = f'read_file(path="{path}") 重新读取'
    else:
        how = f"用相同参数重新调用 {name}"
    return (f"[工具结果已省略：{name} 原约 {n} 字符。为控制上下文，较早的工具结果"
            f"不再随请求发送；原文仍完整保留在会话日志中。需要时请{how}，"
            "不要凭记忆重建内容。]")


def cap_tool_results(messages: list[dict], cfg=None, user_text: str = "") -> list[dict]:
    """工具结果总预算：很多条加起来太大时，从最旧/最不相关的开始降级。

    spill 管「单条超过 spill_max_chars」，compaction 管「旧轮次整体」，
    这一层管中间地带：一次改代码读了十几个中等文件，单条都不超 spill 线，
    加起来却把上下文撑满，并在后续每一步、每一轮里反复重付。

    保留优先级：最近 N 条工具结果 > 与当前问题相关的 > 较新的。
    任何被降级的消息都保留 tool_call_id 与 tool_name（配对不破坏），
    只改写 content；history 一字不改，日志仍可完整回放。
    """
    if cfg is None or not getattr(cfg, "tool_result_cap_enabled", True):
        return messages
    num_ctx = int(getattr(cfg, "num_ctx", 0) or 65536)
    ratio = float(getattr(cfg, "tool_result_budget_ratio", 0.2) or 0.0)
    if ratio <= 0:
        return messages
    budget = int(num_ctx * ratio)
    keep_recent = int(getattr(cfg, "tool_result_keep_recent", 6) or 0)
    min_chars = int(getattr(cfg, "tool_result_min_chars", 800) or 0)

    # tool_call_id -> 路径：降级文案要能指回原文件，模型才知道怎么重读。
    call_path: dict[str, str] = {}
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            for c in m["tool_calls"]:
                fn = c.get("function") or {}
                args = fn.get("arguments") or {}
                call_path[str(c.get("id") or "")] = (
                    str(args.get("path") or "") if isinstance(args, dict) else "")

    tool_idxs = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    keep_idxs = set(tool_idxs[-keep_recent:]) if keep_recent > 0 else set()

    used = 0
    candidates: list[tuple[int, str, int]] = []      # (下标, 路径, tokens)
    for i in tool_idxs:
        m = messages[i]
        text = m.get("content") or ""
        n = compaction.estimate_message_tokens(m)
        path = call_path.get(str(m.get("tool_call_id") or ""), "")
        if (i in keep_idxs or len(text) < min_chars
                or _SPILL_MARK in text[-400:]):
            used += n                                  # 无条件保留，先占预算
        else:
            candidates.append((i, path, n))

    if not candidates:
        return messages

    terms = _query_terms(user_text)

    def _rank(item: tuple[int, str, int]) -> tuple[int, int]:
        i, path, _ = item
        text = messages[i].get("content") or ""
        rel = _hits(path, terms) * 3 + _hits(text[:2000], terms)
        return (-rel, -i)                              # 相关优先，其次新的优先

    ordered = sorted(candidates, key=_rank)
    total = used + sum(n for _, _, n in ordered)
    if total <= budget:
        return messages

    # 从**最差**的一条开始降级，而不是从最好的开始装预算：
    # 后者的边界很脆——最相关的那条只要比剩余预算大一点就会被砍，
    # 等于「相关性优先」形同虚设。这里保证排名第一的永不降级（除非只剩它）。
    out = list(messages)
    for k, (i, path, n) in enumerate(reversed(ordered)):
        if total <= budget:
            break
        if k == len(ordered) - 1:                     # 最相关的一条：不再降级
            break
        new = dict(out[i])
        new["content"] = _omit_tool_result(out[i], path)
        total -= n - compaction.estimate_message_tokens(new)
        out[i] = new
    return out


def project_messages(history: list[dict], user_text: str = "",
                     turn_start: int | None = None, cfg=None) -> list[dict]:
    """把落盘历史投影成本次请求的模型视图。

    顺序不能换：压缩先做（它操作 history 的绝对下标），再把 turn_start
    按位移映射到压缩视图，最后跑三条折叠规则 + 工具结果预算。
    cfg 只用来读可调阈值（DSH AGENTS.md:144）；None 时保守地不做参数摘要。
    """
    view, shift = compaction.apply_checkpoints(history)
    mapped_start = None if turn_start is None else turn_start - shift
    view = collapse_aborted_turn(view, user_text=user_text,
                                 turn_start=mapped_start)
    view = dedup_skill_bodies(view)
    max_chars = int(getattr(cfg, "tool_call_arg_max_chars", 0) or 0)
    view = slim_tool_call_args(view, max_chars)
    return cap_tool_results(view, cfg=cfg, user_text=user_text)
