"""run_command — 执行 shell 命令（跑测试 / git / 装依赖 / 任意命令行）。"""
from __future__ import annotations

import os
import subprocess
import time

from agent.registry import ToolContext, ToolResult, tool
from tools._common import clip_text, decode_text


@tool(
    name="run_command",
    description=(
        "执行 shell 命令并返回退出码、stdout、stderr。"
        "用于：跑测试/脚本、git 操作（git 不用单独工具，直接命令）、装依赖、lint、编译等一切命令行操作。"
        "工作目录默认是工作空间，cwd 可改（相对路径基于工作空间）。"
        "Windows 用 cmd 语法、Linux/Mac 用 sh 语法；stdin 不可用，交互式命令（如 git rebase -i）会卡住直到超时。"
        "输出过长会截断；默认 120 秒超时，长任务（装依赖/编译）用 timeout 参数调大（上限 600）。"
        "破坏性命令（删数据、git push --force、覆盖发布等）只在用户明确要求时执行。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "要执行的命令，如 `python tests/test_tools.py`、`git diff`、`git add -A && git commit -m \"msg\"`",
            },
            "cwd": {
                "type": "string",
                "description": "工作目录。相对路径基于工作空间；默认工作空间根",
            },
            "timeout": {
                "type": "number",
                "description": "超时秒数（1-600），默认 120。超时自动终止命令",
            },
        },
        "required": ["command"],
    },
    confirm=True,  # 任意命令执行是高危能力，配合 --confirm / Web 弹窗人工把关
    notes=("破坏性命令（删数据、git push --force 等）只在用户明确要求时执行。"),
)
def run_command(ctx: ToolContext, command: str, cwd: str | None = None,
                timeout: float = 120) -> ToolResult:
    cmd = (command or "").strip()
    if not cmd:
        return ToolResult("[错误] command 不能为空")
    try:
        timeout = max(1.0, min(float(timeout), 600.0))
    except (TypeError, ValueError):
        timeout = 120.0

    work = ctx.resolve(cwd) if cwd else ctx.workspace
    if not work.is_dir():
        return ToolResult(f"[错误] 工作目录不存在: {work}")

    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUNBUFFERED", "1")

    t0 = time.time()
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(work), env=env,
            capture_output=True, timeout=timeout,
        )
        dt = time.time() - t0
        out = decode_text(proc.stdout or b"")[0]
        err = decode_text(proc.stderr or b"")[0]
        code = proc.returncode
    except subprocess.TimeoutExpired as e:
        dt = time.time() - t0
        out = decode_text(e.stdout or b"")[0] if e.stdout else ""
        err = decode_text(e.stderr or b"")[0] if e.stderr else ""
        return ToolResult(
            f"[超时] 命令超过 {timeout:.0f}s 被终止（已跑 {dt:.1f}s）: {cmd}\n"
            f"--- stdout ---\n{clip_text(out) if out.strip() else '(空)'}\n"
            f"--- stderr ---\n{clip_text(err) if err.strip() else '(空)'}"
        )
    except OSError as e:
        return ToolResult(f"[错误] 无法执行命令: {type(e).__name__}: {e}")

    return ToolResult(
        f"[exit {code}·{dt:.1f}s] {cmd}\n工作目录: {work}\n"
        f"--- stdout ---\n{clip_text(out) if out.strip() else '(空)'}\n"
        f"--- stderr ---\n{clip_text(err) if err.strip() else '(空)'}"
    )
