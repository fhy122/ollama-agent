"""delete_file — 删除工作空间内的文件或目录（破坏性操作，不可恢复）。"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from agent.registry import ToolContext, ToolResult, tool


def _norm(p: Path) -> Path:
    """归一化路径（消掉 .. / .，不解析符号链接）——用于安全边界判断。"""
    return Path(os.path.abspath(p))


def _dir_stats(p: Path) -> tuple[int, int, int]:
    """统计目录内容：(文件数, 子目录数, 总字节)。"""
    files = dirs = size = 0
    for root, dnames, fnames in os.walk(p):
        dirs += len(dnames)
        for fn in fnames:
            files += 1
            try:
                size += (Path(root) / fn).stat().st_size
            except OSError:
                pass
    return files, dirs, size


@tool(
    name="delete_file",
    description=(
        "删除工作空间内的文件或目录（不可恢复）。相对路径基于工作空间。"
        "只允许删除工作空间内的路径，工作空间外一律拒绝。"
        "文件、空目录直接删；非空目录必须显式传 recursive=true 才会整个删除。"
        "破坏性操作删错无法找回：只在用户明确要求删除时使用，删前先确认路径。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "要删除的文件或目录路径。相对路径基于工作空间",
            },
            "recursive": {
                "type": "boolean",
                "description": "删除非空目录时必须传 true（连同目录下所有内容一起删），默认 false",
            },
        },
        "required": ["path"],
    },
    confirm=True,  # 删除是破坏性操作，配合 CLI --confirm 可人工把关
    notes=("删除不可恢复：只在用户明确要求删除时调用，删前复述要删的路径；"
           "非空目录必须传 recursive=true。"),
)
def delete_file(ctx: ToolContext, path: str, recursive: bool = False) -> ToolResult:
    p = _norm(ctx.resolve(path))
    ws = _norm(ctx.workspace)

    # ---- 安全底线：工作空间外、工作空间根，一律不删 ----
    if p == ws:
        return ToolResult(f"[错误] 拒绝删除工作空间根目录: {ws}")
    try:
        p.relative_to(ws)
    except ValueError:
        return ToolResult(
            f"[错误] 拒绝删除：{p} 在工作空间 {ws} 之外，delete_file 只删工作空间内的路径"
        )

    if not p.exists() and not p.is_symlink():
        return ToolResult(f"[错误] 不存在: {p}（相对路径基于工作空间 {ctx.workspace}）")

    # ---- 文件 / 符号链接：删链接或文件本身，不跟随到目标 ----
    if p.is_symlink() or p.is_file():
        size = 0
        try:
            size = p.stat().st_size
        except OSError:
            pass
        try:
            p.unlink()
        except OSError:
            os.rmdir(p)  # Windows 目录联接/符号链接按目录删链接本身
        return ToolResult(f"[已删除] 文件 {p}（{size} 字节）")

    # ---- 目录 ----
    if not p.is_dir():
        return ToolResult(f"[错误] 不是可删除的文件或目录: {p}")

    entries = sorted(p.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
    if entries and not recursive:
        # 拒绝时列出顶层内容，方便模型跟用户核对后再重试
        names = ", ".join(e.name + ("/" if e.is_dir() else "") for e in entries[:20])
        more = " …" if len(entries) > 20 else ""
        nfiles, ndirs, size = _dir_stats(p)
        return ToolResult(
            f"[错误] 目录非空，拒绝删除: {p}\n"
            f"含 {nfiles} 个文件、{ndirs} 个子目录、约 {size} 字节；顶层内容: {names}{more}\n"
            f"确认要连同以上内容整个删除，请传 recursive=true 重试"
        )
    if not entries:
        p.rmdir()
        return ToolResult(f"[已删除] 空目录 {p}")

    nfiles, ndirs, size = _dir_stats(p)
    shutil.rmtree(p)
    return ToolResult(
        f"[已删除] 目录 {p}（含 {nfiles} 个文件、{ndirs} 个子目录、约 {size} 字节）"
    )
