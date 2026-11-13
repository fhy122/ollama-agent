"""ask_user：规划阶段把方案摆到人面前，等一个明确答复再往下走。

为什么做成工具，而不是让模型「输出一段话然后停住」：
  1. 结构化 —— options 是数组，前端能渲染成按钮，不用从自由文本里猜；
  2. 可控 —— 工具没返回，模型这一轮就走不下去，天然不会自问自答
     （小模型上「替用户编一个『用户回答：是』然后继续」非常常见）；
  3. 跨协议 —— 不依赖 Ollama 的 stop 截断。stop 是解码侧匹配，
     实测撞进 function-call 的参数 JSON 会让整次 tool_calls 变成 None，
     给工具会话加 stop 等于自伤。

与 confirm 的分工（两者不能混用）：
  - confirm：这个危险操作要不要放行 —— 布尔，作用是「刹车」；
  - ask_user：这几条路走哪条 —— 选项/文本，作用是「定方向」。
"""
from __future__ import annotations

from agent.registry import ToolContext, ToolResult, tool

# 选项上限：超过这个数说明模型自己还没收敛，先把方案想清楚再来问人。
MAX_OPTIONS = 6


@tool(
    name="ask_user",
    description=(
        "向用户提问并等待回答，用于「需要人拍板才能往下走」的场合：多个可行方案"
        "要取舍、方向或范围有歧义、要动生产数据/删东西前的确认。"
        "把候选方案写进 options（每条带一句取舍），用户会看到按钮直接选，"
        "也可以自己写答案。问完必须等结果，不要在同一条回复里替用户假设答案。"
        "能自己查清楚的事不要问：先读文件、搜代码，把问题收敛成一个具体选择再问。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "要用户定的事，一句话说清。不要复述背景，用户已经看到上下文。",
            },
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    f"候选方案，最多 {MAX_OPTIONS} 条，每条一句取舍"
                    "（如「A 缓存到磁盘：重启不丢，但多写一个文件」）。"
                    "只想要自由回答时留空。"
                ),
            },
            "allow_free_text": {
                "type": "boolean",
                "description": "是否允许用户自己写答案，默认 true。只让用户选 options 时填 false。",
            },
        },
        "required": ["question"],
    },
    # 不设 confirm：提问本身就是交互，再套一层「要不要批准提问」只会多一次点击
    confirm=False,
)
def ask_user(ctx: ToolContext, question: str, options: list | None = None,
             allow_free_text: bool = True) -> ToolResult:
    q = str(question or "").strip()
    if not q:
        return ToolResult("[错误] question 不能为空：把要用户拍板的那件事写成一句话。")

    opts: list[str] = []
    for raw in (options or []):
        s = str(raw or "").strip()
        if s and s not in opts:      # 去重：模型常把同一个方案改几个字重复列出
            opts.append(s)
    if len(opts) > MAX_OPTIONS:
        return ToolResult(
            f"[错误] options 最多 {MAX_OPTIONS} 条，你给了 {len(opts)} 条 —— "
            "说明方案还没收敛。先合并同类项，或改成自由文本提问。")

    free_text = bool(allow_free_text)
    if not opts and not free_text:
        return ToolResult("[错误] 既没有 options 又不允许自由输入，用户无法回答。")

    fn = ctx.ask_choice
    if fn is None:
        # 脚本 / 子代理 / 离线测试里没有交互通道：明确说「别重试」，
        # 否则模型会以为工具坏了，换个参数反复调，把步数耗光。
        return ToolResult(
            "[不支持] 当前环境没有接入交互通道（可能是在脚本或子代理里运行）。"
            "不要重试本工具：按最合理的方案继续，并在回答开头写明你假设了什么。")

    answer = str(fn(q, opts, free_text) or "").strip()

    if ctx.stopped():
        return ToolResult("[已被用户打断] 没有拿到答复。不要再追问，直接收尾并说明停在哪一步。")
    if not answer:
        # 沉默既不是同意也不是否决。说清楚，否则模型会把它当否决去改方案。
        return ToolResult(
            "[未拿到用户答复] 用户没有在限时内回答，也可能是关掉了弹窗。"
            "这不等于同意也不等于否决：不要反复重试本工具，按最保守的选项继续，"
            "并在结尾用一行写明「这一处待你确认」。")

    return ToolResult(f"用户的答复：{answer}")
