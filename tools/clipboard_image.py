"""clipboard_image — 读取系统剪贴板里的截图/图片。"""
from __future__ import annotations

import base64

from agent.clipboard import ClipboardError, grab_and_cache
from agent.registry import ToolContext, ToolResult, tool


@tool(
    name="clipboard_image",
    description=(
        "读取系统剪贴板中的图片（用户刚截图但没保存文件时用这个）。"
        "图片会缓存到工作空间 .clip_cache/ 目录并作为附件返回，视觉模型可直接看到内容。"
        "用户说「看剪贴板」「我截图了」「看这张图」而没有给文件路径时调用它。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "save_as": {
                "type": "string",
                "description": "可选：把截图另存为工作空间内的文件名（如 shots/a.png），"
                               "不填则只缓存到 .clip_cache/",
            },
        },
        "required": [],
    },
)
def clipboard_image(ctx: ToolContext, save_as: str = "") -> ToolResult:
    try:
        p = grab_and_cache(ctx.workspace)
    except ClipboardError as e:
        return ToolResult(f"[错误] {e}")
    except Exception as e:
        return ToolResult(f"[错误] 剪贴板取图失败: {type(e).__name__}: {e}")

    raw = p.read_bytes()
    extra = ""
    if save_as and save_as.strip():
        target = ctx.resolve(save_as.strip())
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
        extra = f"；已另存为 {target}"

    return ToolResult(
        f"[剪贴板图片] 已缓存到 {p}（{len(raw)} 字节）{extra}。图片内容已附在本条消息中。",
        images=[base64.b64encode(raw).decode("ascii")],
    )
