"""clipboard_image — 读取系统剪贴板里的截图/图片。"""
from __future__ import annotations

import base64

from agent import HIDDEN_DIRNAME
from agent.clipboard import CACHE_DIRNAME, ClipboardError, grab_and_cache
from agent.registry import ToolContext, ToolResult, tool

# 剪贴板里只会是位图，save_as 只允许这些后缀。
# 不校验的后果很脏：模型传 save_as="xxx.pdf" 会把 PNG 字节写进 .pdf 文件，
# 之后 read_file 按 PDF 解析报错、load_image 又把它当图片读出一张"纯红色空白页"，
# 模型彻底迷失。宁可在这里直接挡回去。
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


@tool(
    name="clipboard_image",
    description=(
        "读取系统剪贴板中的图片（用户刚截图但没保存文件时用这个）。"
        f"图片会缓存到工作空间 {CACHE_DIRNAME}/ 目录并作为附件返回，视觉模型可直接看到内容。"
        "用户说「看剪贴板」「我截图了」「看这张图」而没有给文件路径时调用它。"
        "【反向指引】用户拖入/上传的附件、或消息里已经给了文件路径"
        f"（{HIDDEN_DIRNAME}/uploads/xxx.pdf 等），"
        "不是剪贴板内容，直接用 read_file / load_image 读那个路径，不要调本工具。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "save_as": {
                "type": "string",
                "description": "可选：把截图另存为工作空间内的文件名（如 shots/a.png），"
                               f"只支持图片后缀；不填则只缓存到 {CACHE_DIRNAME}/",
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
        if target.suffix.lower() not in IMAGE_SUFFIXES:
            return ToolResult(
                f"[错误] save_as 只能存成图片（{'/'.join(sorted(IMAGE_SUFFIXES))}），"
                f"收到 {target.suffix or '(无扩展名)'}。"
                f"剪贴板里是位图，存成 {target.suffix or '无后缀'} 会得到一个假文件。"
                f"图片已缓存到 {p}，直接用这个路径即可。")
        target.parent.mkdir(parents=True, exist_ok=True)
        renamed = ""
        if target.exists():
            # 绝不覆盖用户已有文件。实战事故：模型传 save_as=原PDF同名，
            # 70 字节截图把用户几 MB 的 PDF 原地覆盖，回收站/卷影副本都救不回。
            stem, suf = target.stem, target.suffix
            i = 1
            while True:
                cand = target.with_name(f"{stem}_{i}{suf}")
                if not cand.exists():
                    renamed = f"（{target.name} 已存在，改存为 {cand.name}，未覆盖原文件）"
                    target = cand
                    break
                i += 1
        target.write_bytes(raw)
        extra = f"；已另存为 {target}{renamed}"

    return ToolResult(
        f"[剪贴板图片] 已缓存到 {p}（{len(raw)} 字节）{extra}。图片内容已附在本条消息中。",
        images=[base64.b64encode(raw).decode("ascii")],
    )
