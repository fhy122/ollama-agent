"""内部历史（Ollama 风格）→ OpenAI 消息数组。

只做这一件事，但坑不少，尤其 tool_call_id：

内部历史里 tool 消息靠**顺序**配对（Ollama 的玩法），OpenAI 却强制要求
tool_call_id 显式对上。曾经两处各生成一套 id（assistant 是 call_2_0、
tool 是 call_1_0），中转站直接 400。所以这里先扫一遍历史，把已有的 id
收集起来，assistant 声明时**沿用**它，而不是重新编。
"""
from __future__ import annotations

import json


def _mime_of(b64: str) -> str:
    """按 base64 开头几个字符猜图片类型，拼 data URL 要用。"""
    head = b64[:8]
    if head.startswith("/9j/"):
        return "image/jpeg"
    if head.startswith("iVBOR"):
        return "image/png"
    if head.startswith("R0lGO"):
        return "image/gif"
    if head.startswith("UklGR"):
        return "image/webp"
    return "image/png"


def _data_url(b64: str) -> str:
    """OpenAI 的图片要完整 data URL，Ollama 给的是裸 base64。"""
    if not b64:
        return ""
    return b64 if b64.startswith("data:") else f"data:{_mime_of(b64)};base64,{b64}"


def _collect_tool_ids(messages: list) -> list[str]:
    """按 assistant 出现顺序，收集紧随其后的 tool 消息里声明的 id。

    返回扁平队列，元素可能为空串（那份历史确实没带 id，调用处再兜底生成）。
    """
    slots: list[list[str | None]] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant" and m.get("tool_calls"):
            slots.append([None] * len(m["tool_calls"]))
        elif role == "tool" and slots:
            cur = slots[-1]
            for k, v in enumerate(cur):
                if v is None:
                    cur[k] = m.get("tool_call_id") or ""
                    break
    queue: list[str] = []
    for chunk in slots:
        queue.extend(x or "" for x in chunk)
    return queue


def to_openai_messages(messages: list) -> list:
    """把内部历史翻成 OpenAI 消息数组。"""
    out: list = []
    queue = _collect_tool_ids(messages)

    for mi, m in enumerate(messages):
        role = m.get("role")
        content = m.get("content") or ""

        if role == "tool":
            tid = m.get("tool_call_id") or f"call_fallback_{mi}"
            out.append({"role": "tool", "tool_call_id": tid, "content": content})
            continue

        if role == "assistant":
            msg: dict = {"role": "assistant", "content": content}
            calls = m.get("tool_calls") or []
            if calls:
                arr = []
                for ci, tc in enumerate(calls):
                    fn = tc.get("function") or {}
                    cid = queue.pop(0) if queue else ""
                    if not cid:
                        cid = f"call_{mi}_{ci}"
                    args = fn.get("arguments", {})
                    arr.append({
                        "id": cid,
                        "type": "function",
                        "function": {
                            "name": fn.get("name") or "",
                            "arguments": args if isinstance(args, str)
                            else json.dumps(args, ensure_ascii=False),
                        },
                    })
                msg["tool_calls"] = arr
                if not content:
                    msg["content"] = None
            out.append(msg)
            continue

        # system / user
        imgs = m.get("images") or []
        if imgs and role == "user":
            parts = [{"type": "text", "text": content}] if content else []
            for b in imgs:
                parts.append({"type": "image_url",
                              "image_url": {"url": _data_url(b)}})
            out.append({"role": "user", "content": parts})
        else:
            out.append({"role": role, "content": content})

    return out
