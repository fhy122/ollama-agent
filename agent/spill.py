"""工具结果 spill：超大文本整体落盘，上下文只留头尾预览 + 取回指引。

借鉴 DeepSeek Harness dsh-spill-policy（Apache-2.0）：
- 超过预算的纯文本工具结果，全文写入会话私有目录（.ollama-agent/spills/），
  模型收到「头尾预览 + 省略字节数 + 完整文件路径 + 取回指引」；
- 与 clip_text 的「中间丢弃」本质不同：spill 不丢信息，模型随时可以
  read_file 分段读回、grep_file 在原文里搜索；
- 落盘失败等任何异常都保留原文（尽力而为，不因 spill 弄丢结果）；
- read 工具豁免（DSH 同款决策）：读 spill 文件本身绝不能被再 spill，防递归。
"""
from __future__ import annotations

import time
from pathlib import Path

from agent import HIDDEN_DIRNAME

# 读/列类工具自带分页与上限，且要读 spill 文件本身——豁免。
EXCLUDE = frozenset({"read_file", "read_long_doc", "skill"})

SPILLS_DIRNAME = Path(HIDDEN_DIRNAME) / "spills"

GAP = "\n\n[...]\n\n"

# 预览里识别自身输出的标记（幂等保护：spill 过的文本不再二次处理）
_NOTICE_MARK = "完整结果已存至"


def _notice(omitted_bytes: int, path: Path) -> str:
    return (f"(省略 {omitted_bytes} 字节。完整结果已存至: {path}。"
            f"可用 read_file 带 offset/limit 分段读取，或用 grep_file 在该文件内搜索。)")


def _save(root: Path, name: str, text: str) -> Path:
    """全文落盘，文件名带工具名与时间戳，冲突时追加序号。"""
    root.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = root / f"{name}-{stamp}.txt"
    i = 1
    while path.exists():
        path = root / f"{name}-{stamp}-{i}.txt"
        i += 1
    path.write_text(text, encoding="utf-8")
    return path


def spill_if_oversize(name: str, text: str, root: Path, max_chars: int) -> str:
    """超预算的文本 → 落盘 + 头尾预览；否则原样返回。纯函数，方便测试。

    - 预算按字符算（9B 模型的 num_ctx 以字符/token 为主，不做 token 估算）；
    - 头尾各占预算一半，中间 GAP 占位；Python 切片按码点，不会切碎汉字；
    - 省略字节数精确到 UTF-8 字节（与 DSH 一致，用字节计数）。
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if name in EXCLUDE:
        return text
    if _NOTICE_MARK in text[-400:]:
        return text                                   # 已是 spill 预览，幂等
    try:
        path = _save(root, name, text)
    except OSError:
        return text                                   # 落盘失败：保原文
    total_bytes = len(text.encode("utf-8"))
    half = max(1, max_chars // 2)
    head, tail = text[:half], text[-half:] if half < len(text) else ""
    kept = len(head.encode("utf-8")) + len(tail.encode("utf-8"))
    omitted = total_bytes - kept
    footer = "\n\n" + _notice(omitted, path)
    return head + GAP + tail + footer


def maybe_spill(name: str, text: str, ctx) -> str:
    """dispatch 总漏斗的挂接点：按 ctx 配置决定是否 spill。"""
    cfg = getattr(ctx, "cfg", None)
    max_chars = int(getattr(cfg, "spill_max_chars", 12000) or 0) if cfg else 12000
    if max_chars <= 0:
        return text
    root = Path(ctx.workspace) / SPILLS_DIRNAME
    return spill_if_oversize(name, text, root, max_chars)
