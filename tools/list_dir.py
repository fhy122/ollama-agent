"""list_dir — 列出目录内容（默认工作空间）。"""
from __future__ import annotations

from agent.registry import ToolContext, ToolResult, tool


@tool(
    name="list_dir",
    description=(
        "列出目录内容（文件大小、子目录）。查看项目结构用这个。"
        "不传 path 时列出工作空间根目录；相对路径基于工作空间。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目录路径，默认工作空间根目录"},
        },
        "required": [],
    },
)
def list_dir(ctx: ToolContext, path: str = "") -> ToolResult:
    p = ctx.resolve(path)
    if not p.exists():
        return ToolResult(f"[错误] 目录不存在: {p}（相对路径基于工作空间 {ctx.workspace}）")
    if not p.is_dir():
        st = p.stat()
        return ToolResult(f"[文件] {p}  {st.st_size} 字节")
    entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
    rows = []
    for e in entries[:200]:
        kind = "d" if e.is_dir() else "f"
        size = "" if e.is_dir() else f"{e.stat().st_size:>10} B"
        rows.append(f"{kind}  {size:>12}  {e.name}")
    more = f"\n… 共 {len(entries)} 项，仅显示前 200" if len(entries) > 200 else ""
    return ToolResult(f"[目录] {p}\n" + "\n".join(rows) + more)
