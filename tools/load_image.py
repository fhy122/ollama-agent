"""load_image — 加载本地图片给模型看（视觉模型）。"""
from __future__ import annotations

import base64

from agent.registry import ToolContext, ToolResult, tool

MAX_IMAGE_BYTES = 20 * 1024 * 1024

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}

# 文档类扩展名 → 正确工具提示（这类文件常被误当图片加载，直接给引导）
_DOC_SUFFIXES = {
    ".xlsx": "Excel 表格", ".xlsm": "Excel 表格", ".xls": "旧版 Excel（建议另存为 .xlsx）",
    ".docx": "Word 文档", ".doc": "旧版 Word（建议另存为 .docx）",
    ".pptx": "PPT", ".ppt": "旧版 PPT（建议另存为 .pptx）",
    ".pdf": "PDF", ".csv": "CSV 表格", ".tsv": "TSV 表格",
    ".txt": "文本", ".md": "Markdown", ".json": "JSON", ".py": "代码",
}


def sniff_image(raw: bytes) -> str | None:
    """按文件头判断真实图片格式；不认识返回 None。

    光看扩展名不够：微信/QQ 传来的文件常被改名，魔数才算数。
    """
    if raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if raw.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if raw.startswith(b"BM"):
        return "bmp"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    return None


@tool(
    name="load_image",
    description=(
        "加载本地**图片**文件（png/jpg/webp/gif/bmp），返回后图片附在结果消息中，视觉模型可直接看到。"
        "相对路径基于工作空间。"
        "**只能加载图片**：xlsx/docx/pptx/pdf/csv 等文档不要用它，请改用 read_file 解析文本内容。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "图片文件路径（.png/.jpg/.jpeg/.webp/.gif/.bmp）"},
        },
        "required": ["path"],
    },
)
def load_image(ctx: ToolContext, path: str) -> ToolResult:
    p = ctx.resolve(path)
    if not p.exists():
        return ToolResult(f"[错误] 图片不存在: {p}（相对路径基于工作空间 {ctx.workspace}）")

    suffix = p.suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        kind = _DOC_SUFFIXES.get(suffix)
        if kind:
            return ToolResult(
                f"[错误] {p.name} 是 {kind}，不是图片 —— load_image 只能加载图片，"
                f"无法把它变成可看的内容。请改用 read_file(\"{path}\") 读取正文；"
                f"若确实想看表格/文档的渲染样式，先渲染成图片再 load_image。")
        return ToolResult(
            f"[错误] {p.name} 不是支持的图片格式（{'无扩展名' if not suffix else suffix}）。"
            f"仅支持 png/jpg/jpeg/webp/gif/bmp。文本类请用 read_file。")

    raw = p.read_bytes()
    if len(raw) > MAX_IMAGE_BYTES:
        return ToolResult(f"[错误] 图片过大（{len(raw)} 字节），>20MB 不支持")
    if not raw:
        return ToolResult(f"[错误] 图片是空文件: {p}")

    real = sniff_image(raw)
    if real is None:
        return ToolResult(
            f"[错误] {p.name} 扩展名是 {suffix}，但内容不符合任何已知图片格式（文件头不匹配）。"
            f"可能是改名后的其它文件或已损坏，请确认来源；文本/表格请用 read_file。")

    b64 = base64.b64encode(raw).decode("ascii")
    return ToolResult(
        f"[图片已加载] {p}（{real}，{len(raw)} 字节）。图片内容已附在本条消息中。",
        images=[b64],
    )
