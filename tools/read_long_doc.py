"""read_long_doc — 长文档分段精读（读不完的那种文档）。

它和 read_file 的分工：
  read_file       —— 短文件整读；长文件按 offset/pages 翻页看某一段
  read_long_doc   —— 通读全文并回答「全文层面」的问题（总结、对比、找出所有…）

为什么不能靠模型自己循环调 read_file：一次只能拿一段，模型会把步数耗光，
而且读到后面就忘了前面（历史里全是原文，没有提炼）。这个工具由代码驱动
切段和进度，模型只负责「看这一段、记下与本问题相关的笔记」。
"""
from __future__ import annotations

from pathlib import Path

from agent import longdoc
from agent.registry import ToolContext, ToolResult, tool


@tool(
    name="read_long_doc",
    description=(
        "【长文档专用】把一份读不完的长文档（几十万字 / 几百页）分段精读，边读边做笔记，"
        "最后按笔记回答用户的问题。"
        "适用于：总结全文、找出文中所有关于 X 的内容、通读对比多个章节、"
        "「整份报告说了什么」这类必须看完全文才能答的问题。"
        "流程固定：先用 question 分析要找什么 → 把文档切成若干段逐段读 → 笔记超长自动压缩 → "
        "返回带出处标注的笔记，你据此作答（不要再看原文）。"
        "支持文本/Word/PPT/PDF（PDF 按页）。Excel 不用它（走 read_file 按工作表读）。"
        "question 必须写用户的原问题（不是「总结一下」，而是「总结这份合同的违约责任与赔偿上限」）。"
        "read_file 返回「超出单次可读量」提示时必须改用本工具，不要反复翻页。"
        "读完后若要追问细节，用返回笔记里的行号/页码去 read_file 精确取原文。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径。相对路径基于工作空间；用户给了绝对路径就直接用",
            },
            "question": {
                "type": "string",
                "description": "用户这次要回答的问题，尽量写全（决定每段记什么笔记）",
            },
            "chunk_chars": {
                "type": "integer",
                "description": "可调：每段多少字。不填按上下文自动算（一般不用填）",
            },
            "refresh": {
                "type": "boolean",
                "description": "强制重新精读，忽略已缓存的笔记。"
                               "仅当用户明确要求重读、或怀疑文档已改动但被判定为未变时填 true",
            },
        },
        "required": ["path", "question"],
    },
)
def read_long_doc(ctx: ToolContext, path: str, question: str = "",
                  chunk_chars: int = 0, refresh: bool = False) -> ToolResult:
    p = ctx.resolve(path)
    if not p.exists():
        return ToolResult(f"[错误] 文件不存在: {p}")
    if p.is_dir():
        return ToolResult(f"[错误] 是目录不是文件: {p}（用 list_dir）")
    if ctx.client is None:
        return ToolResult(
            "[错误] 长文精读需要模型自己逐段读，当前环境没有可用的聊天客户端；"
            "改用 read_file(offset=…, limit=…) 分段读，并在回答里说明只读了部分内容"
        )

    try:
        res = longdoc.run_long_doc(
            ctx.client, Path(p), question.strip(),
            num_ctx=ctx.num_ctx(),
            notice=ctx.notice,
            cancel=ctx.cancel,
            chunk_chars=chunk_chars if isinstance(chunk_chars, int) else 0,
            refresh=bool(refresh),
            workspace=ctx.workspace,
            _progress=lambda i, n, where: ctx.say(f"[长文精读] 第 {i}/{n} 段 · {where}"),
        )
    except ValueError as e:
        return ToolResult(f"[错误] {e}")
    except Exception as e:
        return ToolResult(f"[错误] 长文精读失败: {type(e).__name__}: {e}")
    return ToolResult(res.text)
