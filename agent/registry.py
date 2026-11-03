"""工具注册表。

设计：每个工具一个独立文件放在 tools/ 目录，文件里用 @tool 装饰器自注册；
tools/__init__.py 自动发现并导入所有工具模块。新增工具 = 新增一个 .py 文件。

工具函数约定：
    def my_tool(ctx: ToolContext, **schema参数) -> ToolResult | str
"""
from __future__ import annotations

import inspect
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class ToolResult:
    """工具执行结果。text 回传给模型；images(base64) 附在消息上供视觉模型查看。"""
    text: str
    images: list[str] | None = None


@dataclass
class ToolContext:
    """执行环境，由 CLI / WebUI 注入。

    client / cfg / notice / cancel 是给「需要自己发子请求」的工具用的
    （目前只有 read_long_doc：它要逐段读文档、自己调模型记笔记）。
    普通工具不该碰这几个字段 —— 它们只做纯函数式的读写。
    """
    confirm: bool = False
    use_color: bool = True
    ask: Callable[[str], bool] | None = None
    workspace: Path = field(default_factory=Path.cwd)
    cfg: Any = None                          # AgentConfig（读 num_ctx 等）
    client: Any = None                       # 聊天客户端（子请求用）
    notice: Callable[[str], None] | None = None   # 进度提示 → 前端
    cancel: Any = None                       # threading.Event，置位即中断

    def say(self, text: str) -> None:
        """向用户播一条进度提示；没接发射器时静默（离线测试/纯工具场景）。"""
        if self.notice is not None:
            try:
                self.notice(text)
            except Exception:
                pass

    def stopped(self) -> bool:
        c = self.cancel
        return bool(c is not None and getattr(c, "is_set", lambda: False)())

    def num_ctx(self) -> int | None:
        return getattr(self.cfg, "num_ctx", None) if self.cfg is not None else None

    def resolve(self, path: str | Path | None) -> Path:
        """把工具收到的路径解析成绝对路径。

        规则（工作空间约定）：
        - 相对路径/未指定 → 锚定到 workspace
        - 绝对路径 / ~ 开头 → 按用户写的来（explicit is better than implicit）
        """
        if path is None or str(path).strip() == "":
            return self.workspace
        p = Path(str(path)).expanduser()
        return p if p.is_absolute() else (self.workspace / p)

    def approve(self, what: str) -> bool:
        if not self.confirm:
            return True
        if self.ask is None:
            return True
        return self.ask(what)


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict
    handler: Callable[..., "ToolResult | str"]
    confirm: bool = False  # True 且 ctx.confirm 时执行前询问

    def spec(self) -> dict:
        """OpenAI/Ollama tools 数组里的一个条目。"""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def run(self, ctx: ToolContext, args: dict) -> ToolResult:
        brief = json.dumps(args, ensure_ascii=False)[:120]
        if self.confirm and not ctx.approve(f"{self.name}({brief})"):
            return ToolResult("[用户拒绝执行]")
        try:
            sig = inspect.signature(self.handler)
            kwargs = {k: v for k, v in args.items() if k in sig.parameters}
            out = self.handler(ctx, **kwargs)
            return out if isinstance(out, ToolResult) else ToolResult(str(out))
        except Exception as e:  # 工具异常不炸整个 agent，回传给模型让它改
            return ToolResult(f"[工具执行错误] {self.name}: {type(e).__name__}: {e}")


# ---------------- 注册表（全局单例） ----------------

_REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, parameters: dict, confirm: bool = False):
    """把函数注册成一个 agent 工具。

    description 是模型选工具的唯一依据（随 tools 数组每次请求下发），
    使用策略/适用边界/反例都写在这里，不要另开通道重复下发。
    """
    def deco(fn: Callable) -> Callable:
        _REGISTRY[name] = Tool(
            name=name, description=description,
            parameters=parameters, handler=fn, confirm=confirm,
        )
        return fn
    return deco


def all_tools() -> list[Tool]:
    return list(_REGISTRY.values())


def all_specs() -> list[dict]:
    return [t.spec() for t in _REGISTRY.values()]


def get(name: str) -> Tool | None:
    return _REGISTRY.get(name)


def dispatch(name: str, args: dict, ctx: ToolContext) -> ToolResult:
    t = _REGISTRY.get(name)
    if t is None:
        return ToolResult(f"[错误] 未知工具: {name}")
    return t.run(ctx, args or {})
