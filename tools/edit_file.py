"""edit_file — 精确替换文件中的一段文本（外科手术式修改，免整文件重写）。"""
from __future__ import annotations

from pathlib import Path

from agent.registry import ToolContext, ToolResult, tool
from tools._common import decode_text, display_path, is_binary_chunk


@tool(
    name="edit_file",
    description=(
        "精确替换文件里的一段文本（只改目标片段，不动其他内容）。"
        "old_string 必须与文件内容完全一致（含空格/缩进/换行），且默认必须只出现一次；"
        "出现多次时会拒绝，可加更多上下文使其唯一，或传 replace_all=true 全部替换。"
        "适合改函数/配置/单行的小改动；整个文件重写或新建文件才用 write_file。"
        "改完建议 read_file 复核，代码改动用 run_command 跑测试验证。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要修改的文件路径。相对路径基于工作空间；文件必须已存在",
            },
            "old_string": {
                "type": "string",
                "description": "要被替换的原文（与文件内容完全一致，默认必须唯一）",
            },
            "new_string": {
                "type": "string",
                "description": "替换成的新文本。删除文本时传空字符串",
            },
            "replace_all": {
                "type": "boolean",
                "description": "old_string 出现多次时是否全部替换，默认 false（不唯一会拒绝）",
            },
        },
        "required": ["path", "old_string", "new_string"],
    },
    confirm=True,  # 改盘属于改动操作，配合 --confirm / Web 弹窗人工把关
    notes=("改代码的闭环：grep_file/glob_file 定位 → read_file 读懂 → edit_file 修改 → "
           "run_command 跑测试验证。小改动一律优先 edit_file，整文件重写才用 write_file。"),
)
def edit_file(ctx: ToolContext, path: str, old_string: str, new_string: str,
              replace_all: bool = False) -> ToolResult:
    p = ctx.resolve(path)
    if not p.is_file():
        return ToolResult(f"[错误] 文件不存在: {p}（相对路径基于工作空间 {ctx.workspace}）。"
                          f"新建文件请用 write_file")

    if not old_string:
        return ToolResult("[错误] old_string 不能为空。整文件写入请用 write_file")
    if old_string == new_string:
        return ToolResult("[错误] old_string 与 new_string 相同，无需修改")

    try:
        raw = p.read_bytes()
    except OSError as e:
        return ToolResult(f"[错误] 读取失败: {p}: {e}")
    if is_binary_chunk(raw[:8192]):
        return ToolResult(f"[错误] 二进制文件不支持文本替换: {p}")

    text, enc = decode_text(raw)
    # 与 read_file 对齐：读取时统一归一化换行（Windows 盘上常是 \r\n，读出是 \n），
    # 匹配在归一化文本上进行，写回时还原原换行风格——old_string 直接抄 read_file 输出即可
    crlf = "\r\n" in text
    norm = text.replace("\r\n", "\n")
    old_n = (old_string or "").replace("\r\n", "\n")
    new_n = (new_string or "").replace("\r\n", "\n")

    n = norm.count(old_n)
    if n == 0:
        return ToolResult(
            f"[错误] old_string 不在文件中: {display_path(p, ctx.workspace)}\n"
            f"可能是空格/缩进/换行不一致，先 read_file 核对原文再试"
        )
    if n > 1 and not replace_all:
        return ToolResult(
            f"[错误] old_string 在文件中出现 {n} 次，不唯一，拒绝替换（防止误伤）。\n"
            f"请扩大 old_string 附带上下文使其唯一，或确认后传 replace_all=true 全部替换"
        )

    updated = norm.replace(old_n, new_n) if replace_all else norm.replace(old_n, new_n, 1)
    out = updated.replace("\n", "\r\n") if crlf else updated
    p.write_bytes(out.encode(enc, errors="replace"))
    count = n if replace_all else 1
    return ToolResult(
        f"[已修改] {display_path(p, ctx.workspace)}（替换 {count} 处，"
        f"{norm.count(chr(10)) + 1} 行 → {updated.count(chr(10)) + 1} 行，编码 {enc}）"
    )
