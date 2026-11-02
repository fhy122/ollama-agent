"""read_file — 读取本地文件（文本 / Word / Excel / PPT）。"""
from __future__ import annotations

from pathlib import Path

from agent import docparse
from agent.registry import ToolContext, ToolResult, tool


@tool(
    name="read_file",
    description=(
        "读取本地文件内容。支持：文本（txt/md/csv/json/代码等，按行号返回）、"
        "Word(.docx)、Excel(.xlsx/.xlsm，按工作表返回单元格)、PPT(.pptx，按页返回)。"
        "相对路径在工作空间内查找；用户给了绝对路径就直接用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径。相对路径基于工作空间，绝对路径按原样",
            },
            "offset": {"type": "integer", "description": "起始行号（文本模式），从 0 开始，默认 0"},
            "limit": {"type": "integer", "description": "最多返回多少行，默认 200"},
        },
        "required": ["path"],
    },
)
def read_file(ctx: ToolContext, path: str, offset: int = 0, limit: int = 200) -> ToolResult:
    p = ctx.resolve(path)
    if not p.exists():
        return ToolResult(f"[错误] 文件不存在: {p}（相对路径基于工作空间 {ctx.workspace}）")
    if p.is_dir():
        return ToolResult(f"[错误] 是目录不是文件: {p}（用 list_dir）")

    # ---- Office 文档 ----
    if p.suffix.lower() in (docparse.DOC_SUFFIXES | docparse.SHEET_SUFFIXES
                            | docparse.SLIDE_SUFFIXES | docparse.LEGACY_SUFFIXES):
        try:
            label, text = docparse.extract_text(p)
        except ValueError as e:
            return ToolResult(f"[错误] {e}")
        except Exception as e:
            return ToolResult(f"[错误] 解析失败: {type(e).__name__}: {e}")
        lines = text.splitlines() or [""]
        return ToolResult(_with_head_and_window(p, f"{label}", lines, offset, limit))

    # ---- 文本（含未知扩展名试探）----
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolResult(
            f"[错误] 二进制文件无法按文本读取: {p.suffix or '(无扩展名)'}。"
            f"图片用 load_image；docx/xlsx/pptx 用 read_file（已支持）；其它格式暂不支持"
        )
    lines = text.splitlines()
    return ToolResult(_with_head_and_window(p, "文本", lines, offset, limit))


def _with_head_and_window(p: Path, label: str, lines: list[str],
                          offset: int, limit: int) -> str:
    if not lines:
        return f"[空文件] {p}"
    offset = max(0, int(offset or 0))
    limit = max(1, int(limit or 200))
    chunk = lines[offset:offset + limit]
    numbered = "\n".join(f"{offset + i + 1}\t{l}" for i, l in enumerate(chunk))
    more = f"\n…[后续还有 {len(lines) - offset - len(chunk)} 行，用 offset 翻页]" \
        if offset + len(chunk) < len(lines) else ""
    head = (f"[{label}] {p}  共 {len(lines)} 行，显示 {offset + 1}-"
            f"{offset + len(chunk)} 行")
    return f"{head}\n{numbered}{more}"
