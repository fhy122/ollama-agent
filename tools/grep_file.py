"""grep_file — 在文件内容里搜正则（定位函数定义 / 调用点 / 配置项）。"""
from __future__ import annotations

import re
from pathlib import Path

from agent.registry import ToolContext, ToolResult, tool
from tools._common import (clip_text, decode_text, display_path, glob_to_regex,
                           is_binary_chunk, iter_files)

_LINE_MAX = 200  # 单条命中行展示上限（字符）


@tool(
    name="grep_file",
    description=(
        "在文件内容里搜索正则表达式（类似 grep），返回「文件:行号: 命中行」。"
        "用于定位：函数/类在哪定义、谁调用了它、哪些文件包含某配置项或字符串。"
        "path 是文件或目录（默认工作空间）；glob 过滤文件名（如 `*.py`，"
        "不含 / 的模式任意子目录都匹配）；pattern 用 Python 正则语法（普通子串直接写）。"
        "跳过 .git/node_modules 等噪声目录和二进制文件。"
        "结果过多会截断；定位到文件后用 read_file 读上下文，用 edit_file 修改。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "要搜索的正则（普通子串直接写），如 `def run_turn`、`class ToolContext`",
            },
            "path": {
                "type": "string",
                "description": "搜索起点：文件或目录。相对路径基于工作空间；默认工作空间根",
            },
            "glob": {
                "type": "string",
                "description": "只搜匹配的文件，如 `*.py`、`src/**/*.ts`。不传 = 全部文本文件",
            },
            "ignore_case": {
                "type": "boolean",
                "description": "忽略大小写，默认 false",
            },
            "max_results": {
                "type": "integer",
                "description": "最多返回多少条命中（默认 50）",
            },
        },
        "required": ["pattern"],
    },
)
def grep_file(ctx: ToolContext, pattern: str, path: str | None = None,
              glob: str | None = None, ignore_case: bool = False,
              max_results: int = 50) -> ToolResult:
    try:
        rx = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        return ToolResult(f"[错误] 正则表达式无效: {e}。普通子串直接写即可，特殊字符（. * ( ) 等）需转义")

    try:
        limit = max(1, min(int(max_results), 500))
    except (TypeError, ValueError):
        limit = 50

    frx = None
    if glob and glob.strip():
        try:
            frx = glob_to_regex(glob)
        except ValueError as e:
            return ToolResult(f"[错误] glob 无效: {e}")

    root = ctx.resolve(path)
    if not root.exists():
        return ToolResult(f"[错误] 不存在: {root}（相对路径基于工作空间 {ctx.workspace}）")

    # 收集待搜文件
    if root.is_file():
        candidates = [(root, root.name)]
    else:
        candidates = []
        for p, rel in iter_files(root):
            if frx is None or frx.match(rel):
                candidates.append((p, rel))

    hits: list[str] = []
    nmatch = 0
    truncated = False
    for p, rel in candidates:
        if len(hits) >= limit:
            truncated = True
            break
        try:
            raw = p.read_bytes()
        except OSError:
            continue
        if is_binary_chunk(raw[:8192]):
            continue
        text = decode_text(raw)[0]
        for i, line in enumerate(text.splitlines(), 1):
            if not rx.search(line):
                continue
            nmatch += 1
            if len(hits) >= limit:
                truncated = True
                break
            show = line.strip()
            if len(show) > _LINE_MAX:
                show = show[:_LINE_MAX] + "…"
            hits.append(f"{display_path(p, ctx.workspace)}:{i}: {show}")

    if not hits:
        scope = f"{display_path(root, ctx.workspace)}" + (f" [glob={glob}]" if frx else "")
        return ToolResult(f"[无命中] pattern `{pattern}` 在 {scope} 范围内没有匹配。"
                          f"确认拼写/正则，或放宽 glob 过滤")

    head = f"[命中 {nmatch} 处" + (f"，显示前 {limit} 条" if truncated else "") + \
           f"] pattern=`{pattern}` 范围={display_path(root, ctx.workspace)}"
    return ToolResult(head + "\n" + clip_text("\n".join(hits), 6000))
