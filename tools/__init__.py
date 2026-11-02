"""工具包：自动发现并注册本目录下的所有工具模块。

新增工具的步骤：
  1. 在本目录新建 your_tool.py
  2. 用 @tool(name=..., description=..., parameters=...) 装饰处理函数
  3. 完事——导入本包时自动注册，无需改任何其他文件

处理函数签名：def fn(ctx: ToolContext, **参数) -> ToolResult | str
"""
from __future__ import annotations

import importlib
import pkgutil

from agent.registry import (  # noqa: F401  对外统一从 tools 入口拿
    Tool, ToolContext, ToolResult, all_notes, all_specs, all_tools, dispatch, get,
)


def load_all() -> list[str]:
    """导入 tools/ 下所有非下划线开头的模块，触发 @tool 注册。

    私有可选模块（如 web_chat，见 .gitignore）缺失时告警跳过，不影响其余工具。
    """
    import sys

    loaded = []
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_"):
            continue
        try:
            importlib.import_module(f"{__name__}.{mod.name}")
        except ImportError as e:
            print(f"[tools] 跳过模块 {mod.name}: {e}", file=sys.stderr)
            continue
        loaded.append(mod.name)
    return loaded


LOADED_MODULES = load_all()
