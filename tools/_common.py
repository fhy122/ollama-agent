"""共享助手：文本解码 / 输出截断 / glob→regex / 目录遍历。

供 tools/ 下的工具模块复用。下划线开头 → 自动发现跳过，不会被当成工具注册。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# 遍历时跳过的噪声目录（用户显式指定这些路径进去搜时不受影响）
SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", ".venv", "venv", ".tox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "node_modules",
    "dist", "build", ".idea", ".vscode", ".workbuddy",
}


def decode_text(raw: bytes) -> tuple[str, str]:
    """把命令输出/文件字节解码成文本。返回 (文本, 实际编码)。

    顺序：UTF-8 → GBK（中文 Windows 控制台/ANSI 文件常见）→ UTF-8 替换符兜底。
    """
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


def clip_text(text: str, limit: int = 4000) -> str:
    """输出过长时掐头去尾，中间标注省略了多少。"""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    return (text[:head]
            + f"\n…[输出过长，中间省略 {len(text) - head - tail} 字符]…\n"
            + text[-tail:])


def glob_to_regex(pattern: str) -> re.Pattern:
    """把 glob 模式编译成「整串匹配」的正则。

    支持：*（不跨目录）、**（跨目录）、?、[...]。
    不含 / 的模式自动按递归匹配（等价 **/pattern）——`*.py` 匹配任意子目录下的 py 文件。
    """
    pat = pattern.strip().replace("\\", "/")
    if not pat:
        raise ValueError("模式不能为空")
    if "/" not in pat:
        pat = "**/" + pat
    i, n, out = 0, len(pat), []
    while i < n:
        c = pat[i]
        if c == "*":
            if pat[i:i + 3] == "**/":
                out.append("(?:[^/]+/)*")
                i += 3
            elif pat[i:i + 2] == "**":
                out.append(".*")
                i += 2
            else:
                out.append("[^/]*")
                i += 1
        elif c == "?":
            out.append("[^/]")
            i += 1
        elif c == "[":
            j = pat.find("]", i + 1)
            if j < 0:
                out.append(re.escape(c))
                i += 1
            else:
                out.append("[" + pat[i + 1:j] + "]")
                i = j + 1
        else:
            out.append(re.escape(c))
            i += 1
    return re.compile("".join(out) + "$")


def iter_files(root: Path):
    """遍历 root 下所有文件（跳过噪声目录），yield (绝对路径, 相对 POSIX 路径)。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in sorted(dirnames) if d not in SKIP_DIRS]
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            yield p, p.relative_to(root).as_posix()


def display_path(p: Path, ws: Path) -> str:
    """展示用路径：工作空间内显示相对路径（可直接喂给 read_file），否则绝对路径。"""
    try:
        return p.resolve().relative_to(ws.resolve()).as_posix()
    except ValueError:
        return str(p)


def is_binary_chunk(chunk: bytes) -> bool:
    """NUL 字节出现在开头一段里 → 按二进制文件跳过。"""
    return b"\x00" in chunk
