"""verify — 按《编码与验证规范》跑机械可验的那几档，返回真实结果。

只做「判据能不能跑出来」这件事，不做判断标准（那部分在 skills/coding-standards）：

  syntax  第 0 档：内存里 compile 每个 .py，报文件:行号（不落 __pycache__）
  links   第六节：Markdown 相对链接是否指向真实文件（跳过 http/#）
  blocks  第六节：Markdown 代码块能否 compile（围栏写 `no-compile` 显式豁免）
  tests   第 2/3 档：真起进程跑 tests/test_*.py，报退出码与末行

为什么是工具而不是让模型「自查」：模型可以声称验证过了，但退出码骗不了人。
"""
from __future__ import annotations

import re
import subprocess
import sys
import time
from pathlib import Path

from agent.registry import ToolContext, ToolResult, tool
from tools._common import clip_text, decode_text, iter_files

CHECKS = ("syntax", "links", "blocks", "tests")

# ```lang ... ``` —— lang 里含 no-compile 的块按故意不完整处理，跳过编译
_FENCE = re.compile(r"^([ \t]*)(`{3,}|~{3,})[ \t]*([^\n`]*)$", re.MULTILINE)
_LINK = re.compile(r"\]\(([^)]+)\)")


def _read(p: Path) -> str:
    """按 BOM/编码读文本；读不出当空串（语法档会把空文件判过，属于预期）。"""
    try:
        return decode_text(p.read_bytes())[0].lstrip("\ufeff")
    except OSError:
        return ""


# ---------------- 各档检查：统一返回 (通过?, 说明) ----------------

def _check_syntax(files: list[Path]) -> list[tuple[Path, bool, str]]:
    """第 0 档：compile 不执行。报错要能直接定位到行。"""
    out = []
    for p in files:
        src = _read(p)
        try:
            compile(src, str(p), "exec")
            out.append((p, True, ""))
        except SyntaxError as e:
            out.append((p, False, f"第 {e.lineno} 行: {e.msg}"))
        except ValueError as e:            # 源码里有 NUL 等
            out.append((p, False, f"源码无法编译: {e}"))
    return out


def _strip_fences(md: str) -> str:
    """去掉围栏代码块，再找链接——代码块里的 `](x)` 不是真链接。"""
    keep, inside = [], False
    for ln in md.splitlines():
        if _FENCE.match(ln):
            inside = not inside
            keep.append("")               # 行号保持不变
            continue
        keep.append("" if inside else ln)
    return "\n".join(keep)


def _check_links(md_files: list[Path]) -> list[tuple[Path, bool, str]]:
    out = []
    for p in md_files:
        text = _strip_fences(_read(p))
        broken = []
        for raw in _LINK.findall(text):
            target = raw.split("#", 1)[0].strip().strip("<>")
            if not target or target.startswith(("http://", "https://", "mailto:", "tel:", "#")):
                continue
            dest = (p.parent / target).resolve()
            if not dest.exists():
                broken.append(target)
        if broken:
            uniq = sorted(set(broken))
            out.append((p, False, f"断链 {len(uniq)} 处: " + ", ".join(uniq[:8])
                        + ("…" if len(uniq) > 8 else "")))
        else:
            out.append((p, True, ""))
    return out


def _check_blocks(md_files: list[Path]) -> list[tuple[Path, bool, str]]:
    """文档里的代码块要 compile，否则「示例」只是长得像代码。

    围栏信息串含 `no-compile` 的块跳过：故意不完整的片段要显式豁免，不靠默契。
    """
    out = []
    for p in md_files:
        lines = _read(p).splitlines()
        bad, total, skipped = [], 0, 0
        i, start_line, info, buf = 0, 0, "", []
        while i < len(lines):
            m = _FENCE.match(lines[i])
            if m and not buf:                       # 开围栏
                info = m.group(3).strip().lower()
                start_line = i + 1
                buf = []
                i += 1
                # 找到配对的闭围栏
                while i < len(lines):
                    if _FENCE.match(lines[i]) and lines[i].strip().startswith(("```", "~~~")):
                        break
                    buf.append(lines[i])
                    i += 1
                i += 1
                if "no-compile" in info:
                    skipped += 1
                    continue
                lang = info.split()[0] if info else ""
                if lang not in ("python", "py", "python3"):
                    continue                        # 只管 Python 示例
                total += 1
                src = "\n".join(buf)
                try:
                    compile(src, f"{p}:{start_line}", "exec")
                except SyntaxError as e:
                    bad.append(f"第 {start_line} 行起（块内第 {e.lineno} 行）: {e.msg}")
                continue
            i += 1
        if bad:
            out.append((p, False, f"{len(bad)}/{total} 个 Python 块编译失败: "
                        + "; ".join(bad[:5])))
        elif total or skipped:
            out.append((p, True, f"{total} 个 Python 块通过"
                        + (f"，{skipped} 个按 no-compile 豁免" if skipped else "")))
        else:
            out.append((p, True, ""))       # 没有 Python 块：不产出噪声行
    return out


def _check_tests(ctx: ToolContext, pattern: str, timeout: float) -> list[tuple[Path, bool, str]]:
    """真起进程跑测试脚本。输出截断但保留退出码与末行——那是判据。"""
    root = ctx.workspace
    scripts = sorted(root.glob(pattern)) if not Path(pattern).is_absolute() \
        else sorted(Path(pattern).parent.glob(Path(pattern).name))
    if not scripts:
        return [(Path(pattern), False,
                 f"没有匹配到测试脚本（工作空间 {root}）。"
                 f"期望路径 tests/test_*.py —— pattern 若写错请改正，"
                 f"尚未写测试就先补一个：tests/test_<模块名>.py，末行 print PASS")]
    out = []
    for s in scripts:
        if not s.is_file():
            continue
        t0 = time.time()
        try:
            proc = subprocess.run(
                [sys.executable, str(s)], cwd=str(s.parent.parent if s.parent.name == "tests" else s.parent),
                capture_output=True, timeout=timeout,
            )
            dt = time.time() - t0
            o = decode_text(proc.stdout or b"")[0]
            e = decode_text(proc.stderr or b"")[0]
            ok = proc.returncode == 0
            tail = [ln for ln in (o.strip().splitlines() or [""]) if ln.strip()]
            last = tail[-1] if tail else "(无输出)"
            msg = f"exit {proc.returncode}·{dt:.1f}s · 末行: {last[:160]}"
            if not ok and e.strip():
                msg += f"\n      stderr: {clip_text(e.strip(), 600)}"
            out.append((s, ok, msg))
        except subprocess.TimeoutExpired:
            out.append((s, False, f"超时（>{timeout:.0f}s），长任务用 timeout 参数调大"))
        except OSError as ex:
            out.append((s, False, f"无法执行: {type(ex).__name__}: {ex}"))
    return out


# ---------------- 工具入口 ----------------

@tool(
    name="verify",
    description=(
        "按《编码与验证规范》跑机械可验的检查，返回真实退出结果（不是自查结论）。"
        "checks 可选：syntax=第0档语法编译（内存 compile，报文件:行号）、"
        "links=Markdown 相对链接断链、blocks=Markdown 里 Python 代码块能否 compile、"
        "tests=真起进程跑 tests/test_*.py 并报退出码。默认跑前三个（毫秒级，改完就调）。"
        "改完代码立刻调 verify(checks=[\"syntax\"])；改了文档调 links+blocks；"
        "动了逻辑还要跑 tests。它只覆盖机械判据——"
        "错误文案是否有指路、图是否重叠、估算是否标注，仍需你自己判断，verify 通过不等于交付合格。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "checks": {
                "type": "array",
                "items": {"type": "string", "enum": list(CHECKS)},
                "description": "要跑的检查项，默认 [\"syntax\",\"links\",\"blocks\"]",
            },
            "path": {
                "type": "string",
                "description": "只检查这个路径（文件或目录）。相对路径基于工作空间；默认整个工作空间",
            },
            "timeout": {
                "type": "number",
                "description": "tests 档每个脚本的超时秒数（1-600），默认 120",
            },
        },
    },
)
def verify(ctx: ToolContext, checks: list[str] | None = None, path: str | None = None,
           timeout: float = 120) -> ToolResult:
    # 注意用 is None 而不是 `or`：显式传 [] 是「什么都没选」，
    # 被当成缺省值静默跑三档，就会把空检查报成「全部通过」——那是最坏的一种假绿。
    want = [c for c in (["syntax", "links", "blocks"] if checks is None else checks) if c]
    unknown = [c for c in want if c not in CHECKS]
    if unknown:
        return ToolResult(
            f"[错误] 未知检查项: {', '.join(unknown)}。"
            f"可选: {', '.join(CHECKS)}（例: checks=[\"syntax\"]）")
    if not want:
        return ToolResult(f"[错误] checks 不能为空。可选: {', '.join(CHECKS)}")

    try:
        timeout = max(1.0, min(float(timeout), 600.0))
    except (TypeError, ValueError):
        timeout = 120.0

    target = ctx.resolve(path) if path else ctx.workspace
    if not target.exists():
        return ToolResult(f"[错误] 路径不存在: {target}（相对路径基于工作空间 {ctx.workspace}）")

    if target.is_file():
        py_files = [target] if target.suffix == ".py" else []
        md_files = [target] if target.suffix.lower() in (".md", ".markdown") else []
    else:
        py_files, md_files = [], []
        for p, _rel in iter_files(target):
            if p.suffix == ".py":
                py_files.append(p)
            elif p.suffix.lower() in (".md", ".markdown"):
                md_files.append(p)

    results: dict[str, list[tuple[Path, bool, str]]] = {}
    if "syntax" in want:
        results["syntax"] = _check_syntax(py_files)
    if "links" in want:
        results["links"] = _check_links(md_files)
    if "blocks" in want:
        results["blocks"] = _check_blocks(md_files)
    if "tests" in want:
        results["tests"] = _check_tests(ctx, "tests/test_*.py", timeout)

    # 汇总：只把失败项逐条列出，通过的折叠成一行——输出是给模型读的，别淹掉重点
    fails = 0
    lines = [f"[verify] 目标: {target}"]
    for name in want:
        rows = results.get(name, [])
        bad = [(p, msg) for p, ok, msg in rows if not ok]
        fails += len(bad)
        lines.append(f"\n== {name} == {len(rows) - len(bad)}/{len(rows)} 通过")
        if not bad:
            # 通过项的说明也要全列：豁免数、块数这类信息只出现在这里，
            # 只取第一条会把「N 个块按 no-compile 豁免」静默吞掉。
            details = [msg for _, ok, msg in rows if ok and msg]
            for msg in details[:6]:
                lines.append(f"   {msg}")
            if len(details) > 6:
                lines.append(f"   …另有 {len(details) - 6} 个文件通过")
            continue
        for p, msg in bad[:20]:
            try:
                rel = p.resolve().relative_to(ctx.workspace.resolve()).as_posix()
            except ValueError:
                rel = str(p)
            lines.append(f"   ✗ {rel}: {msg}")
        if len(bad) > 20:
            lines.append(f"   …还有 {len(bad) - 20} 处未列出")

    verdict = "全部通过" if fails == 0 else f"{fails} 项未通过"
    lines.append(f"\n[结论] {verdict}")
    if fails:
        lines.append(
            "下一步：按上面「文件:行号」逐条修，一次只改一处，改完重跑本工具。"
            "注意 verify 只覆盖机械判据——错误文案指路、视觉排版、估算标注仍需人工判断。")
    else:
        lines.append(
            "提醒：机械判据过了 ≠ 交付合格。涉及行为改动请另跑对应档位"
            "（单元/集成/端到端），并在回答里贴出命令与输出。")
    return ToolResult("\n".join(lines))
