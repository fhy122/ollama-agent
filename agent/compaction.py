"""会话级滚动压缩（长会话 compaction）。

参考 DeepSeek Harness `packages/compaction/*`，逐条对应关系：

* 触发在**每个模型请求前**（`compaction-basic/src/index.ts:157` 的 `agent/pre-step`
  waterfall），不是只在回合边界——本项目的痛点正是单轮 8 步内 history 持续膨胀。
* 阈值 `floor(min(W*thresholdRatio, W-O-B))`、尾部保留 `(W-O)*retainRatio`
  （`compaction-basic/src/config.ts:172-197`），全部配置化（根 `AGENTS.md:144`）。
* token 估算用固定启发式 4 字符/token + 块/角色开销
  （`packages/llm/token-meter/src/estimate.ts:13-19, 42-63, 88-91`）。
* 切点从尾部按预算累加，再回退到**工具调用配对平衡**边界
  （`compaction-basic/src/region.ts:117-155`、`compaction/src/tool-pairing.ts`）；
  system head 永不进入被压缩范围。
* checkpoint 是一条带 `<compacted-summary>` 的 user 消息，八段结构
  （`compaction-basic/src/summarizer.ts:32-67, 188-194`、`region.ts:410-413`）。
* 压缩后必须真的变短，否则放弃（`region.ts:414-422`）；失败一律降级为
  「不压缩、继续本轮」（`index.ts:165-172`）。
* 旧消息**物理不删除**：checkpoint 只追加到 history 末尾，携带
  `shadowed_until` 作为 `surfaceOp.replace` 的等价物；请求视图据此跳过被遮蔽
  的前缀（`compaction/compaction/README.md:12`、
  `docs/subsystems/core.zh.md:424`「模型可见即已记录」）。日志可完整回放。

与 harness 的两处有意偏离（按本项目实际，不照抄）：
1. harness 用 LLM 生成摘要另发一次全量请求；本项目 1.47M tokens 的痛点就是
   重复请求，再叠一次全量摘要会先烧一笔。故默认用**本地确定性摘要**
   （零额外请求、可回放、可单测），LLM 摘要保留为 `compaction_summarizer="llm"`
   开关，prompt 原样取自 harness。
2. harness 用 JSONL 的 `compaction/start|summary|end` 三个 log-only 事件做事务
   括号；本项目 history 是单个 JSON list，故把同样的元数据（id / 范围 / token 数）
   放在 checkpoint 消息自身字段里，避免引入第二套日志格式。
"""
from __future__ import annotations

import hashlib
import json
from typing import Callable

# ---- token-meter 的固定启发式（estimate.ts:13-19）----
CHARS_PER_TOKEN = 4
CJK_CHARS_PER_TOKEN = 2         # 按本项目真实账单校准，见 _text_tokens
BLOCK_OVERHEAD = 4
ROLE_OVERHEAD = 4

# 摘要框（summarizer.ts:21-23, 69-71, 188-194）
CHECKPOINT_OPEN = "<compacted-summary>"
CHECKPOINT_CLOSE = "</compacted-summary>"
CHECKPOINT_PREAMBLE = (
    "This is an automatically generated checkpoint condensing an earlier span "
    "of the conversation to free up context. Treat the captured context as "
    "established background and build on it without restating it. Continue the "
    "task directly from the messages that follow, without acknowledging this "
    "checkpoint."
)

# LLM 模式专用：原样取自 compaction-basic/src/summarizer.ts:32-67
COMPACTION_INSTRUCTION = """You are now acting as a compaction engine for this AI coding assistant. Condense the conversation ABOVE into a structured checkpoint that lets another model resume the work with no loss of essential context.

Output EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write "(none)" for an empty section - never drop a section.

## Primary Request and Intent
- [the user's original and evolving goals; quote verbatim where the exact wording matters]

## Key Technical Concepts
- [technologies, frameworks, patterns, and conventions in play]

## Files and Code
- [exact path: why it matters, key changes or snippets]

## Errors and Fixes
- [error: how it was resolved, plus any related user feedback]

## Pending Jobs
- [explicitly requested work not yet completed]

## Current Work
- [precisely what was in progress at this checkpoint]

## Next Step
- [the single next action, directly in line with the most recent request, or "(none)"]

## Critical Context
- [decisions and their rationale, constraints, user preferences, open questions, data needed to continue]

Rules:
- Write concise English engineering prose. Preserve exact file paths, commands, error strings, identifiers, numeric values, function signatures, and syntax fragments.
- Capture user feedback and explicit instructions faithfully, especially corrections.
- Do NOT mention this summarization request or that the context was compacted.
- Output only the checkpoint text: do not call any tool or take any other action.
- If the conversation already contains a <compacted-summary> block, it is a PRIOR checkpoint. Do not copy it forward verbatim: preserve still-true facts, drop stale ones, and merge newer information into a single consolidated summary under the same structure."""

# 本地摘要的八段（与上面 LLM prompt 同构，便于两种模式互换）
_SECTIONS = (
    ("## Primary Request and Intent", "主要请求与意图"),
    ("## Key Technical Concepts", "关键技术概念"),
    ("## Files and Code", "文件与代码"),
    ("## Errors and Fixes", "错误与修复"),
    ("## Pending Jobs", "待办任务"),
    ("## Current Work", "当前进展"),
    ("## Next Step", "下一步"),
    ("## Critical Context", "关键上下文"),
)

_ERROR_MARKS = ("[错误]", "error:", "Error:", "Traceback", "Exception", "失败", "[⚠")
_WRITE_TOOLS = ("write_file", "edit_file", "append_file", "delete_file")
_MAX_ITEMS = 8          # 每段最多列几条，防止摘要自己变成新的大块
_PREVIEW_CHARS = 180    # 单条事实的截断长度


def _ceil_div(n: int, d: int) -> int:
    return (n + d - 1) // d


def _is_cjk(ch: str) -> bool:
    o = ord(ch)
    return (0x2E80 <= o <= 0x9FFF or 0x3000 <= o <= 0x303F
            or 0xF900 <= o <= 0xFAFF or 0xFF00 <= o <= 0xFFEF)


def _text_tokens(text: str) -> int:
    """文本 token 估算：沿用 harness 的 4 字符/token，对 CJK 按 2 字符/token 校准。

    harness 在 token-meter/README.md:68 自承「CJK text ... underprice badly at
    four characters per token」。本项目实测更具体：会话
    20261103-185415-gfbb.json 的 usage.prompt_tokens / 请求字符数 =
    0.3939~0.5052 tokens/字符，纯 4 字符/token 低估 1.6~2 倍，会让压缩
    晚触发好几步、白烧几次全量请求。CJK 取 0.5、其余取 0.25，是拿这份
    真实账单校准的固定启发式（不引入 tokenizer 依赖，仍可离线复现）。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if _is_cjk(ch))
    return _ceil_div(cjk, CJK_CHARS_PER_TOKEN) + _ceil_div(
        len(text) - cjk, CHARS_PER_TOKEN)


def estimate_message_tokens(msg: dict) -> int:
    """单条消息的估算 token（estimate.ts:42-63, 88-91）。

    只用于**相对压力判断**，不作为账单数字；CJK 校准见 _text_tokens。
    """
    text = msg.get("content") or ""
    if not isinstance(text, str):
        text = json.dumps(text, ensure_ascii=False)
    n = _text_tokens(text) + BLOCK_OVERHEAD
    for call in msg.get("tool_calls") or []:
        fn = call.get("function") or {}
        args = fn.get("arguments") or {}
        raw = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
        n += (_text_tokens(str(fn.get("name") or ""))
              + _text_tokens(raw) + BLOCK_OVERHEAD)
    return n + ROLE_OVERHEAD


def estimate_tokens(messages: list[dict]) -> int:
    return sum(estimate_message_tokens(m) for m in messages)


def is_checkpoint(msg: dict) -> bool:
    """识别 checkpoint（等价 harness 的 isCompactCheckpointSource，checkpoint.ts:49）。"""
    return bool(msg.get("compact_checkpoint")) and msg.get("role") == "user"


def strip_checkpoint_meta(msg: dict) -> dict:
    """发给模型时只留干净消息，内部元数据不占 token、不污染上下文。"""
    return {"role": "user", "content": msg.get("content") or ""}


def apply_checkpoints(messages: list[dict]) -> tuple[list[dict], int]:
    """派生请求视图：用最新 checkpoint 替换被遮蔽的旧前缀。

    返回 `(view, shift)`；对原下标 `i >= shadowed_until` 的消息，
    其在 view 中的下标为 `i - shift`（供 turn_start 映射）。
    旧消息一条不删，只在视图里不出现——harness 的 surfaceOp.replace。
    """
    idxs = [i for i, m in enumerate(messages) if is_checkpoint(m)]
    if not idxs:
        return list(messages), 0
    ckpt = messages[idxs[-1]]
    until = int(ckpt.get("shadowed_until") or 0)
    first = 1 if messages and messages[0].get("role") == "system" else 0
    if until < first or until > len(messages):
        return list(messages), 0           # 元数据损坏 → 宁可不压缩，绝不断上下文
    head = list(messages[:first])          # system head 永不 shadow（region.ts:131）
    # 尾部里所有 checkpoint 都是日志记录，不是模型可见消息；只输出最新那一条
    tail = [m for m in messages[until:] if not is_checkpoint(m)]
    view = head + [strip_checkpoint_meta(ckpt)] + tail
    return view, until - (len(head) + 1)


def _pairing_balance(messages: list[dict]) -> list[int]:
    """bal[i] = messages[:i] 里未闭合的 tool_calls 数（tool-pairing.ts:29-38, 71-92）。

    harness 用 assistant/message 的 tool-call 块计数、tool/result 减一；
    本项目 tool_calls 挂在 assistant 消息上，语义一致。
    """
    bal = [0]
    n = 0
    for m in messages:
        if m.get("role") == "assistant" and m.get("tool_calls"):
            n += len(m["tool_calls"])
        elif m.get("role") == "tool":
            n -= 1
        bal.append(n)
    return bal


def select_compactable_range(messages: list[dict], retain_tokens: int) -> tuple[int, int] | None:
    """选出被压缩范围 `[first, keep_from)`，逐字保留 `messages[keep_from:]`。

    照 region.ts:117-155：从尾部按 token 预算累加，再把切点向更旧方向回退
    到工具配对平衡处；退无可退（会拆工具对 / 吃掉 system head）就放弃压缩。
    """
    first = 1 if messages and messages[0].get("role") == "system" else 0
    if len(messages) - first < 3:
        return None
    bal = _pairing_balance(messages)
    acc = 0
    keep_from = len(messages)
    for i in range(len(messages) - 1, first - 1, -1):
        acc += estimate_message_tokens(messages[i])
        keep_from = i
        if acc >= retain_tokens:
            break
    while keep_from > first and bal[keep_from] != 0:
        keep_from -= 1                    # 边界落在工具对中间 → 向头部回退
    if keep_from <= first or bal[keep_from] != 0:
        return None                       # 只有 1 个不可分工具对 → 拒绝压缩
    return first, keep_from


def frame_summary(summary: str) -> str:
    """preamble + <compacted-summary> 包裹（summarizer.ts:188-194）。"""
    return (f"{CHECKPOINT_PREAMBLE}\n\n{CHECKPOINT_OPEN}\n"
            f"{summary.strip()}\n{CHECKPOINT_CLOSE}")


def _prior_checkpoint_text(span: list[dict]) -> str:
    """span 里若含旧 checkpoint，取其正文作为「既有背景」供合并（prompt 末条规则）。"""
    parts = [m.get("content") or "" for m in span if is_checkpoint(m)]
    return parts[-1] if parts else ""


def _bullets(items: list[str]) -> str:
    return "\n".join(f"- {x}" for x in items[:_MAX_ITEMS]) if items else "- (none)"


def build_local_summary(span: list[dict], prior: str = "",
                        max_chars: int = 6000) -> str:
    """确定性八段摘要：零额外模型请求、同一输入恒定同一输出、可离线回放。

    事实只取自原始消息（用户原话逐字、工具名/路径/错误串、assistant 结论），
    不做任何推测性复述；被压掉的工具结果明确告诉模型「全文仍在日志，按需 read_file」。
    """
    users: list[str] = []
    tools_seen: list[str] = []
    files: list[str] = []
    errors: list[str] = []
    pending: list[str] = []
    last_assistant = ""

    for m in span:
        if is_checkpoint(m):
            continue
        role = m.get("role")
        text = m.get("content") or ""
        if not isinstance(text, str):
            continue
        if role == "user":
            users.append(text.strip().replace("\n", " ")[:_PREVIEW_CHARS])
            if any(k in text for k in ("待", "下一步", "接下来", "TODO", "继续")):
                pending.append(text.strip().replace("\n", " ")[:_PREVIEW_CHARS])
        elif role == "assistant":
            if text.strip():
                last_assistant = text.strip()[:400]
            for c in m.get("tool_calls") or []:
                fn = c.get("function") or {}
                name = str(fn.get("name") or "?")
                args = fn.get("arguments") or {}
                if name not in tools_seen:
                    tools_seen.append(name)
                if name in _WRITE_TOOLS and isinstance(args, dict):
                    p = str(args.get("path") or "")
                    size = max(len(str(args.get(k) or ""))
                               for k in ("content", "new_string", "text") if k in args) \
                        if any(k in args for k in ("content", "new_string", "text")) else 0
                    if p:
                        files.append(f"{p}: 由 {name} 修改，参数约 {size} 字符（以磁盘为准）")
        elif role == "tool":
            name = str(m.get("tool_name") or "?")
            if name not in tools_seen:
                tools_seen.append(name)
            first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
            if any(k in first_line for k in _ERROR_MARKS):
                errors.append(f"{name}: {first_line[:_PREVIEW_CHARS]}")
            if name in ("write_file", "edit_file") and first_line:
                files.append(f"{name}: {first_line[:_PREVIEW_CHARS]}")

    if prior:
        # 旧 checkpoint 不整段照抄：只把它的 Critical Context 里仍然成立的事实带过来
        seg = prior.split("## Critical Context")
        tail = seg[-1].split(CHECKPOINT_CLOSE)[0] if len(seg) > 1 else ""
        facts = [ln.strip() for ln in tail.splitlines()
                 if ln.strip().startswith("- ") and "(none)" not in ln.strip()]
        errors.extend([])             # 旧错误不自动带过来，避免陈旧结论污染
        pending.extend(facts[:3])

    sections = [
        _bullets(list(reversed(users))[:5]),
        _bullets([f"已用工具: {', '.join(tools_seen)}"] if tools_seen else []),
        _bullets(list(dict.fromkeys(files))),
        _bullets(list(dict.fromkeys(errors))),
        _bullets(list(dict.fromkeys(pending))),
        _bullets([last_assistant] if last_assistant else []),
        _bullets([users[-1]] if users else []),
        _bullets([f"本次共压缩 {len(span)} 条旧消息。这些原文仍完整保留在会话日志中"
                  f"（可回放），但已不在本次请求的上下文里，你无法直接读取会话日志："
                  f"文件类结果请用 read_file(path=...) 重新读取，命令/网页类结果请重新"
                  f"调用相应工具；不要凭记忆重建、更不要假装看过。"
                  f"（Prior checkpoints merged: {'yes' if prior else 'no'}）"]),
    ]
    out = "\n\n".join(f"{title}\n{body}" for (title, _), body in zip(_SECTIONS, sections))
    if len(out) > max_chars:
        out = out[:max_chars].rstrip() + "\n- [摘要已达长度上限，更早细节请查会话日志]"
    return out


def threshold_tokens(cfg) -> int:
    """harness config.ts:172-194：floor(min(W*ratio, W-O-B))。"""
    W = int(getattr(cfg, "num_ctx", 0) or 65536)
    O = int(getattr(cfg, "compaction_reserved_completion_tokens", 2048) or 0)
    B = int(getattr(cfg, "compaction_headroom_tokens", 8192) or 0)
    ratio = float(getattr(cfg, "compaction_threshold_ratio", 0.8) or 0.8)
    pressure = W - O - B
    return max(1, int(min(W * ratio, pressure if pressure > 0 else W * ratio)))


def retain_tokens(cfg) -> int:
    """harness config.ts:195-197：floor((W-O)*retainRatio)，且必须小于阈值。"""
    W = int(getattr(cfg, "num_ctx", 0) or 65536)
    O = int(getattr(cfg, "compaction_reserved_completion_tokens", 2048) or 0)
    ratio = float(getattr(cfg, "compaction_retain_ratio", 0.16) or 0.16)
    n = max(1, int((W - O) * ratio))
    return min(n, max(1, threshold_tokens(cfg) - 1))


def compact_if_needed(history: list[dict], cfg, view: list[dict],
                      summarize_fn: Callable[[list[dict], str], str] | None = None,
                      compacted_this_turn: int = 0) -> dict | None:
    """压力检查：超阈值就返回**要追加**的 checkpoint，否则返回 None。

    纯函数，不改 history；由 loop 决定 append（保持「日志只追加」）。
    触发用的是**视图** token（模型真正会收到的量），范围用的是 history
    的绝对下标与原始估算——两者目的不同，别混。
    任何一步不满足条件都静默放弃，绝不因为压缩失败而打断本轮。
    """
    if not getattr(cfg, "compaction_enabled", True):
        return None
    if compacted_this_turn >= int(getattr(cfg, "compaction_retries", 1) or 0):
        return None
    if estimate_tokens(view) < threshold_tokens(cfg):
        return None
    rng = select_compactable_range(history, retain_tokens(cfg))
    if rng is None:
        return None
    first, keep = rng
    span = history[first:keep]
    prior = _prior_checkpoint_text(span)
    max_chars = int(getattr(cfg, "compaction_summary_max_chars", 6000) or 6000)
    if summarize_fn is not None:
        try:
            body = (summarize_fn(span, prior) or "").strip()
        except Exception:
            body = ""                      # 摘要失败 → 降级本地，不打断回合
        if not body:
            body = build_local_summary(span, prior, max_chars)
    else:
        body = build_local_summary(span, prior, max_chars)
    if not body:
        return None
    content = frame_summary(body)
    visible = {"role": "user", "content": content}
    shadowed_tokens = estimate_tokens(span)
    # harness region.ts:414-422：替换物必须比被遮蔽内容小，否则不压
    if estimate_message_tokens(visible) >= shadowed_tokens:
        return None
    digest = hashlib.sha1(
        (content + str(len(history))).encode("utf-8")).hexdigest()[:12]
    return {
        "role": "user",
        "content": content,
        "compact_checkpoint": True,
        "compaction_id": digest,
        "shadowed_until": keep,
        "shadowed_messages": len(span),
        "shadowed_tokens": shadowed_tokens,
        "checkpoint_tokens": estimate_message_tokens(visible),
    }


def make_llm_summarizer(client, cfg, cancel=None):
    """可选 LLM 摘要：prompt 与请求结构照 summarizer.ts:120-181。

    system head + 被压缩 span 逐字重放（真前缀，便于复用 KV cache），
    最后追加 COMPACTION_INSTRUCTION；不注册工具，避免摘要时又去调工具。
    调用失败由 compact_if_needed 统一降级为本地摘要。
    """
    def _summarize(span: list[dict], prior: str) -> str:
        msgs = [{"role": "system", "content": "You are a compaction engine."}]
        for m in span:
            if is_checkpoint(m):
                continue
            if m.get("role") in ("user", "assistant", "tool"):
                msgs.append({"role": m["role"],
                             "content": m.get("content") or "(tool call)"})
        msgs.append({"role": "user", "content": COMPACTION_INSTRUCTION})
        resp = client.chat(msgs, tools=None, cancel=cancel)
        if resp.get("error"):
            raise RuntimeError(str(resp["error"])[:120])
        return (resp.get("content") or "").strip()
    return _summarize
