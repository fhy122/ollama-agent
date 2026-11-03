"""todo_write：模型侧任务清单（借鉴 DeepSeek Harness dsh-tool-todo）。

- 整表替换：每次调用提交完整清单，没有增量编辑（回放 = last-write-wins）；
- 单活跃纪律：同一时间最多一个 in_progress（我们是串行单 agent，并列进行
  只会诱导 9B 模型乱跳任务）；
- 校验 fail-loud：空描述 / 重复描述 / 非法状态 / 多个 in_progress 都明确报错，
  让模型自己修正，而不是静默吞掉；
- 持久化到工作区 .ollama-agent/todo.json，跨轮次、跨会话重开都在；
- 前端渲染零新增事件：tool_call 事件的 args 就是完整清单，直接画面板。
"""
from __future__ import annotations

import json

from agent import HIDDEN_DIRNAME
from agent.registry import ToolContext, ToolResult, tool

STATUSES = ("pending", "in_progress", "completed")

TODO_PATH = f"{HIDDEN_DIRNAME}/todo.json"

_DESCRIPTION = (
    "记录并更新任务清单，用于规划多步工作、向用户展示进度；"
    "简单的单步任务不要用它。开工前把工作拆成具体任务（一句一条），"
    "每次提交【完整清单】整体替换旧清单。同一时间只允许一个任务处于 in_progress"
    "（做完一项立刻标 completed，再开始下一项）。"
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "todos": {
            "type": "array",
            "description": "完整任务清单，整体替换之前的清单",
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "任务内容，一句祈使句"},
                    "status": {"type": "string", "enum": list(STATUSES),
                               "description": "pending=未开始 | in_progress=进行中 | completed=已完成"},
                },
                "required": ["content", "status"],
            },
        },
    },
    "required": ["todos"],
}


def _normalize(raw: list) -> list[dict]:
    """校验 + 规范化。任何违反契约的地方都抛 ValueError（fail-loud）。"""
    if not isinstance(raw, list) or not raw:
        raise ValueError("todos 必须是非空数组")
    out: list[dict] = []
    seen: set[str] = set()
    active = 0
    for item in raw:
        if not isinstance(item, dict) or set(item) - {"content", "status"}:
            raise ValueError("每个 todo 只能有 content 和 status 两个字段")
        content = str(item.get("content", "")).strip()
        status = item.get("status")
        if not content:
            raise ValueError("content 不能为空")
        if content in seen:
            raise ValueError(f"重复的任务: {content!r}")
        if status not in STATUSES:
            raise ValueError(f"status 必须是 {list(STATUSES)} 之一，收到 {status!r}")
        if status == "in_progress":
            active += 1
        seen.add(content)
        out.append({"content": content, "status": status})
    if active > 1:
        raise ValueError(f"同一时间最多一个 in_progress，收到 {active} 个")
    return out


@tool(name="todo_write", description=_DESCRIPTION, parameters=_SCHEMA)
def todo_write(ctx: ToolContext, todos: list) -> ToolResult:
    try:
        items = _normalize(todos)
    except ValueError as e:
        return ToolResult(f"[错误] invalid todos: {e}。清单未更新，请修正后重提完整清单。")

    path = ctx.workspace / TODO_PATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        return ToolResult(f"[错误] 清单写入失败: {e}")

    n = {s: sum(1 for i in items if i["status"] == s) for s in STATUSES}
    return ToolResult(f"Updated todo list: {n['pending']} pending, "
                      f"{n['in_progress']} in progress, {n['completed']} completed.")
