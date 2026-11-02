# 第 3 章 · 工具是给模型看的 API

**本章产出**：`agent/registry.py` + `tools/` 三个文件 + 两个演示脚本。到本章结束，模型会**主动要求**读你的文件。

---

## 一、工具到底是什么

一句话：**工具 = 一段 JSON Schema + 一个 Python 函数，两者用名字绑在一起。**

模型看到的是 Schema：

```json
{
  "type": "function",
  "function": {
    "name": "read_file",
    "description": "读取本地文件内容…",
    "parameters": {
      "type": "object",
      "properties": {"path": {"type": "string", "description": "文件路径"}},
      "required": ["path"]
    }
  }
}
```

模型输出的也是名字 + 参数：

```json
{"function": {"name": "read_file", "arguments": {"path": "README.md"}}}
```

**你的代码负责把后者接到前者上。** 就这么点事。

但魔鬼在三个地方：

1. `description` 写不好，模型就调不对（这是 prompt，不是注释）
2. 参数名和函数形参对不上，执行时 `TypeError`
3. 工具抛异常，整个 agent 崩 —— 而正确做法是把异常**当成工具结果**回给模型

---

## 二、`agent/registry.py`：40 行核心 + 60 行防御

```python
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
    """执行环境，由前端注入。"""
    confirm: bool = False
    use_color: bool = True
    ask: Callable[[str], bool] | None = None
    workspace: Path = field(default_factory=Path.cwd)

    def resolve(self, path: str | Path | None) -> Path:
        """把工具收到的路径解析成绝对路径。

        规则（工作空间约定）：
        - 相对路径 / 未指定 → 锚定到 workspace
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
    confirm: bool = False      # True 且 ctx.confirm 时执行前询问

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
        except Exception as e:
            # 工具异常不炸整个 agent，回传给模型让它自己改
            return ToolResult(f"[工具执行错误] {self.name}: {type(e).__name__}: {e}")


# ---------------- 注册表（全局单例） ----------------

_REGISTRY: dict[str, Tool] = {}


def tool(name: str, description: str, parameters: dict, confirm: bool = False):
    """把函数注册成一个 agent 工具。"""
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
```

### 逐条解释 `Tool.run` 里的三个防御

**① 为什么过滤 kwargs？**

```python
sig = inspect.signature(self.handler)
kwargs = {k: v for k, v in args.items() if k in sig.parameters}
```

因为模型会**多给参数**。你只声明了 `path`，它却传 `{"path": "a.txt", "encoding": "utf-8"}`。不做过滤就是 `TypeError: got an unexpected keyword argument`，一个参数冗余就把整个工具废掉。

> 反过来，模型**少给**参数时不用管：给缺省值的形参会用缺省值，必填的会抛 `TypeError` —— 被下面的 `except` 接住，转成"工具执行错误"回给模型，它会自己补上。

**② 为什么异常要吞掉？**

这是整个设计里最反直觉、也最重要的一条。

普通程序里，`open()` 失败就该抛。但在这里，**调用方是一个会读错误信息的语言模型**。你抛出 `FileNotFoundError`，它就拿到一次崩溃；你返回 `[错误] 文件不存在: D:/proj/a.txt（相对路径基于工作空间 D:/proj）`，它就知道该换个路径、或者先 `list_dir` 看看。

> 一条经验：**错误信息要写成"给模型的下一步建议"**，不是给程序员的堆栈。凡是 `[错误]` 开头的返回，都要能让模型据此调整。

**③ `confirm` 为什么在 `Tool` 上，不在工具函数里？**

因为"要不要确认"是**运行时策略**，不是工具属性。同一个 `write_file`，CLI 加 `--confirm` 时要问、WebUI 默认要问、CI 场景下不问。放函数里就得读全局变量，工具就依赖上层了。

`ToolContext.approve()` 里也有个细节：`confirm=True` 但 `ask=None` 时返回 `True`（放行）。理由是"没人可问就不能卡死"，非交互场景下这是唯一合理的选择。

---

## 三、`tools/__init__.py`：自动发现

```python
"""工具包：自动发现并注册本目录下的所有工具模块。

新增工具的步骤：
  1. 在本目录新建 your_tool.py
  2. 用 @tool(name=..., description=..., parameters=...) 装饰处理函数
  3. 完事 —— 导入本包时自动注册，无需改任何其他文件
"""
from __future__ import annotations

import importlib
import pkgutil

from agent.registry import (  # noqa: F401  对外统一从 tools 入口拿
    Tool, ToolContext, ToolResult, all_specs, all_tools, dispatch, get,
)


def load_all() -> list[str]:
    """导入 tools/ 下所有非下划线开头的模块，触发 @tool 注册。"""
    loaded = []
    for mod in pkgutil.iter_modules(__path__):
        if not mod.name.startswith("_"):
            importlib.import_module(f"{__name__}.{mod.name}")
            loaded.append(mod.name)
    return loaded


LOADED_MODULES = load_all()
```

**`LOADED_MODULES = load_all()` 在 import 时执行** —— 这个副作用就是全部魔法。`from tools import ...` 的那一刻，15 个 `.py` 被逐个 import，每个文件顶部的 `@tool` 装饰器依次执行，注册表自动填满。

两个细节：

- **下划线开头跳过**：`_common.py` 是共享助手，不是工具，靠这个约定避开。
- **import 顺序不确定**：`pkgutil` 按文件名排序导入，所以工具之间**不能互相依赖初始化顺序**。真实仓库里 `web_search.py` 直接用 `browse_url.py` 里的单例，是函数内 import 解决的，不是模块级。

---

## 四、第一个工具：`read_file`

```python
"""read_file — 读取本地文本文件（带行号窗口）。"""
from __future__ import annotations

from pathlib import Path

from agent.registry import ToolContext, ToolResult, tool


@tool(
    name="read_file",
    description=(
        "读取本地文件内容，按行号返回。"
        "相对路径在工作空间内查找；用户给了绝对路径就直接用。"
        "文件很长时用 offset/limit 翻页，不要一次读几千行。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径。相对路径基于工作空间，绝对路径按原样",
            },
            "offset": {"type": "integer", "description": "起始行号（从 0 开始），默认 0"},
            "limit": {"type": "integer", "description": "最多返回多少行，默认 200"},
        },
        "required": ["path"],
    },
)
def read_file(ctx: ToolContext, path: str, offset: int = 0, limit: int = 200) -> ToolResult:
    p = ctx.resolve(path)
    if not p.exists():
        return ToolResult(f"[错误] 文件不存在: {p}（相对路径基于工作空间 {ctx.workspace}）")
    if p.is_dir():
        return ToolResult(f"[错误] 是目录不是文件: {p}（用 list_dir）")

    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolResult(
            f"[错误] 二进制文件无法按文本读取: {p.suffix or '(无扩展名)'}。"
            f"图片用 load_image；其它格式暂不支持")

    lines = text.splitlines()
    return ToolResult(_head_and_window(p, lines, offset, limit))


def _head_and_window(p: Path, lines: list[str], offset: int, limit: int) -> str:
    if not lines:
        return f"[空文件] {p}"
    offset = max(0, int(offset or 0))
    limit = max(1, int(limit or 200))
    chunk = lines[offset:offset + limit]
    numbered = "\n".join(f"{offset + i + 1}\t{l}" for i, l in enumerate(chunk))

    more = (f"\n…[后续还有 {len(lines) - offset - len(chunk)} 行，用 offset 翻页]"
            if offset + len(chunk) < len(lines) else "")
    head = (f"[文本] {p}  共 {len(lines)} 行，显示 {offset + 1}-"
            f"{offset + len(chunk)} 行")
    return f"{head}\n{numbered}{more}"
```

### 三处设计，每处都对应一个失败模式

| 做法 | 不这么做会怎样 |
|---|---|
| **带行号**返回 | 模型说"第 42 行有问题"时你无法定位；`edit_file` 也拿不到上下文 |
| **窗口 + 翻页提示** | 读一个 5000 行文件，直接吃满 32k 上下文，模型开始胡说 |
| 错误信息里**带上 `ctx.workspace`** | 模型换个相对路径再试，还是同一个错，来回三次才明白根在哪 |

### `description` 是 prompt，不是注释

对比一下同一个工具的三版描述：

```python
# ❌ 模型不知道什么时候该用、什么时候不该用
"description": "读取文件"

# 😐 说了能力，没说边界
"description": "读取本地文件内容，支持 offset/limit"

# ✅ 说了场景、边界、负面情况
"description": ("读取本地文件内容，按行号返回。"
                "相对路径在工作空间内查找。"
                "文件很长时用 offset/limit 翻页，不要一次读几千行。")
```

差别在调用准确率上非常明显。写 description 时问自己三件事：**什么时候该用 / 什么时候不该用 / 参数怎么填**。

---

## 五、第二个工具：`list_dir`

```python
"""list_dir — 列出目录内容。"""
from __future__ import annotations

from agent.registry import ToolContext, ToolResult, tool

SKIP = {".git", "__pycache__", "node_modules", ".venv", ".idea"}


@tool(
    name="list_dir",
    description=("列出目录下的文件和子目录（含大小）。"
                 "不知道文件在哪时先用它看目录结构，再用 read_file 读具体文件。"),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目录路径，默认工作空间根目录"},
        },
    },
)
def list_dir(ctx: ToolContext, path: str = "") -> ToolResult:
    d = ctx.resolve(path)
    if not d.exists():
        return ToolResult(f"[错误] 目录不存在: {d}")
    if not d.is_dir():
        return ToolResult(f"[错误] 是文件不是目录: {d}（用 read_file）")

    entries = sorted(d.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    lines, hidden = [], 0
    for p in entries:
        if p.name in SKIP or p.name.startswith("."):
            hidden += 1
            continue
        if p.is_dir():
            lines.append(f"{p.name}/")
        else:
            lines.append(f"{p.name}  ({p.stat().st_size} B)")

    body = "\n".join(lines) or "(空目录)"
    note = f"\n…[已隐藏 {hidden} 个隐藏/噪声项]" if hidden else ""
    return ToolResult(f"[目录] {d}\n{body}{note}")
```

注意 `path: str = ""` 的写法：**不给 `required`**，模型不传就列工作空间根。这比强制它传 `"."` 更自然。

---

## 六、验证一：先手动调，别急着接模型

写 `demo_dispatch.py`：

```python
"""不经过模型，直接调工具 —— 先确认工具本身是对的。"""
from pathlib import Path

import tools
from agent.registry import ToolContext

ctx = ToolContext(workspace=Path(".").resolve())

print("已注册工具：", [t.name for t in tools.all_tools()])

r = tools.dispatch("list_dir", {}, ctx)
print(r.text[:400])

r = tools.dispatch("read_file", {"path": "main.py", "limit": 5}, ctx)
print(r.text)

r = tools.dispatch("read_file", {"path": "不存在的.txt"}, ctx)
print(r.text)          # 看错误信息是否可行动
```

```bash
python demo_dispatch.py
```

**这一步不能跳过。** 工具本身有 bug 时，你在模型那边看到的现象是"模型老调错工具"，会浪费几小时怀疑 prompt。

---

## 七、验证二：让模型自己决定用哪个工具

写 `demo_toolcall.py`：

```python
"""只问模型"你要调什么"，不执行 —— 观察工具调用的原始形态。"""
from __future__ import annotations

import json

import tools
from agent.client import OllamaClient
from agent.config import AgentConfig, SYSTEM_PROMPT

cfg = AgentConfig(think="off")
client = OllamaClient(cfg)

history = [
    {"role": "system", "content": SYSTEM_PROMPT},
    {"role": "user", "content": "看一下当前目录有什么文件"},
]

resp = client.chat(history, tools=tools.all_specs(), on_text=lambda t: print(t, end=""))
print("\n\ntool_calls =", json.dumps(resp["tool_calls"], ensure_ascii=False, indent=2))
print("content    =", repr(resp["content"]))
```

```bash
python demo_toolcall.py
```

期望输出类似：

```
tool_calls = [
  {
    "function": {
      "name": "list_dir",
      "arguments": {"path": "."}
    }
  }
]
content    = ''
```

**关键观察：`content` 是空的。** 模型这一步只输出"我要调工具"，不说话。如果你的模型在这里返回了一大段文字还带了 `tool_calls`，也算正常 —— 第 4 章会说明为什么这两种情况都要兼容。

再试一个更难的，检验 description 质量：

```bash
# 应该调 list_dir，然后……它没有后续步骤，所以只会停在 list_dir
python demo_toolcall.py "看看 D:/projectspace 下面有什么"
```

---

## 八、踩过的坑

| 现象 | 原因 | 解决 |
|---|---|---|
| `TypeError: got an unexpected keyword argument` | 没过滤 kwargs | 本章已用 `inspect.signature` 过滤 |
| 模型从来不调工具 | 没传 `tools=`，或 `no_tools=True` | 检查 `client.chat(..., tools=all_specs())` |
| 模型调的参数名不对 | `parameters` 里写的名字 ≠ 函数形参名 | 两边**必须完全一致**；`required` 也要对 |
| 所有工具都没注册 | `tools/__init__.py` 没 import，或文件名以 `_` 开头 | 打印 `LOADED_MODULES` 看 |
| 模型循环调同一个工具 | 工具返回内容没变化（比如一直在报同一个错） | 错误信息要给出"下一步该干什么" |
| 工具执行了但模型说"没看到结果" | 返回值不是 `ToolResult` / `str` | 看 `Tool.run` 的 `isinstance` 分支 |
| 同一个工具被注册两次 | 两个文件用了同一个 `name` | 后注册的覆盖前一个，`all_tools()` 里查重 |

---

## 九、过关自测

1. 加第 16 个工具要改哪些文件？为什么？
2. `Tool.run` 里吞掉异常，会不会掩盖真正的 bug？怎么兼顾？
3. `ctx.resolve("~/a.txt")`、`ctx.resolve("/tmp/a.txt")`、`ctx.resolve("")` 分别返回什么（设 workspace = `D:/proj`）？
4. 为什么 `confirm` 放在 `Tool` 上而不是工具函数里？
5. 模型给了 `{"path": "a.txt", "extra": 1}`，你的工具只接收 `path`，会发生什么？

<details>
<summary>答案要点</summary>

1. 1 个：新建 `tools/your_tool.py`。注册靠 `pkgutil` 自动发现，不需要动 `__init__.py`、不需要动 `loop.py`、不需要改 prompt（除非要教它什么时候用）。
2. 会。兼顾方式是：返回的 `[工具执行错误]` 里带 `type(e).__name__` 和原始 message，调试时能看到；单元测试直接调 handler（绕过 `run`）就能拿到真异常。
3. `Path("C:/Users/你/a.txt")`（expanduser）、原样的 `/tmp/a.txt`（绝对路径不改）、`D:/proj`（空 → workspace 本身）。
4. 因为"要不要确认"是运行时策略：同一个工具在 CLI、WebUI、CI 里的策略不同。放函数里就得读全局，工具就依赖上层了。
5. 正常执行，`extra` 被静默忽略。这是刻意的：宁可忽略多余参数，也不要因为一个冗余字段让整个调用失败。

</details>

---

下一章：把这些零件装成会自己跑的循环 → [第 4 章](04-agent-loop.md)
