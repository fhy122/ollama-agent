"""glob_file — 按文件名/路径模式找文件（类似 glob/find -name）。"""
from __future__ import annotations

from agent.registry import ToolContext, ToolResult, tool
from tools._common import clip_text, display_path, glob_to_regex, iter_files


@tool(
    name="glob_file",
    description=(
        "按文件名/路径模式找文件（类似 glob），返回匹配的文件路径列表。"
        "用于：'有哪些测试文件'、'src 下有哪些 ts'、找配置文件/入口文件。"
        "pattern 支持 *（不跨目录）、**（跨目录）、?；"
        "不含 / 的模式在所有子目录递归匹配（`*.py` = 任意层级的 .py 文件）。"
        "path 是搜索根目录（默认工作空间）。跳过 .git/node_modules 等噪声目录。"
        "查文件内容用 grep_file。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "文件名/路径模式，如 `*.py`、`test_*.py`、`src/**/*.ts`、`**/config.json`",
            },
            "path": {
                "type": "string",
                "description": "搜索根目录。相对路径基于工作空间；默认工作空间根",
            },
            "max_results": {
                "type": "integer",
                "description": "最多返回多少条（默认 200）",
            },
        },
        "required": ["pattern"],
    },
)
def glob_file(ctx: ToolContext, pattern: str, path: str | None = None,
              max_results: int = 200) -> ToolResult:
    try:
        frx = glob_to_regex(pattern)
    except ValueError as e:
        return ToolResult(f"[错误] 模式无效: {e}")

    try:
        limit = max(1, min(int(max_results), 1000))
    except (TypeError, ValueError):
        limit = 200

    root = ctx.resolve(path)
    if not root.exists():
        return ToolResult(f"[错误] 不存在: {root}（相对路径基于工作空间 {ctx.workspace}）")
    if root.is_file():
        return ToolResult(f"[错误] path 需要是目录: {root}。找文件请把 path 指向目录")

    hits: list[str] = []
    total = 0
    for p, rel in iter_files(root):
        if not frx.match(rel):
            continue
        total += 1
        if len(hits) >= limit:
            continue
        hits.append(display_path(p, ctx.workspace))

    if not hits:
        return ToolResult(f"[无匹配] pattern=`{pattern}` 在 "
                          f"{display_path(root, ctx.workspace)} 下没有匹配的文件。"
                          f"递归找用 `**/{pattern}` 写法，或确认路径/后缀")

    head = f"[匹配 {total} 个文件" + (f"，显示前 {limit} 个" if total > limit else "") + \
           f"] pattern=`{pattern}` 根={display_path(root, ctx.workspace)}"
    return ToolResult(head + "\n" + clip_text("\n".join(hits), 6000))
