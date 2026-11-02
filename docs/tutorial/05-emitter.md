# 第 5 章 · 把输出抽象掉

**本章产出**：`agent/console.py` + `agent/events.py`，并把 `run_turn` 里所有 `print` 换掉。改动约 20 行，但这是整个项目里**性价比最高的一次重构** —— 第七章的 WebUI 全靠它。

---

## 一、症状：内核里全是 CLI 的假设

第 4 章的 `run_turn` 里有这些：

```python
print(f"\n─── 第 {step} 步")
print(t, end="", flush=True)          # 模型吐字
print(f"\033[2m{t}\033[0m", ...)      # 思考过程
print(f"⚙ 工具调用: {name}(...)")
print(f"  ↳ ({dt:.1f}s) {preview}")
print("[⏹ 已打断本轮回答]")
```

想加 WebUI 时，你只有两条路：

1. **把 `run_turn` 抄一份**，改成往 socket 写 —— 从此两份代码分叉，改 bug 要改两遍
2. **劫持 `sys.stdout`** —— 多线程下必然串台，而且拿不到"这是工具调用还是普通文本"的语义

两条都是死路。正确的做法是承认一件事：**内核不该知道输出去哪**。

---

## 二、接口设计：8 个方法

| 方法 | 什么时候调 | 参数 |
|---|---|---|
| `step(n)` | 每一步开始 | 步号 |
| `text(t)` | 模型吐出正文片段 | 片段字符串 |
| `think(t)` | 模型吐出思考片段 | 片段字符串 |
| `newline()` | 一步结束，需要换行 | — |
| `tool_call(name, args)` | 即将执行工具 | 名字、已解析的参数 |
| `tool_result(name, text, dt, images)` | 工具执行完 | 结果全文、耗时、可选图片 |
| `stats(dict)` | 回合结束 | token 统计 |
| `notice(text)` | 打断/警告/提示 | 文本 |

三个设计取舍：

**① 为什么不用 `on_event(type, payload)` 一个方法？**

因为 `type` 是字符串，拼错了运行时才发现。8 个具名方法写错立刻 `AttributeError`，IDE 还能补全。**多写 40 行，换来编译期（好吧，检查期）安全。**

**② 为什么 `newline()` 单独一个方法？**

终端需要在一步结束后换行，WebUI 完全不需要。如果让 `text()` 自己判断"这段是不是最后一段"，就得在内核里塞输出格式逻辑。留一个空方法给 WebUI 实现，是最便宜的解。

**③ 为什么 `tool_result` 要传 `images`？**

视觉模型看完图之后要"接着看图回答"，图必须以 base64 附在 `tool` 消息上。终端实现里忽略这个参数即可。

---

## 三、`agent/console.py`

```python
"""终端输出工具：着色 + 强制行缓冲（管道下不丢输出）。"""
from __future__ import annotations

import sys

ANSI = {
    "dim": "\033[2m",
    "cyan": "\033[36m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "red": "\033[31m",
    "bold": "\033[1m",
    "reset": "\033[0m",
}


def setup_stdio() -> None:
    """UTF-8 输出 + 行缓冲，保证管道 / 重定向下实时可见。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass


def color(text: str, name: str, enabled: bool = True) -> str:
    if not enabled:
        return text
    return f"{ANSI[name]}{text}{ANSI['reset']}"
```

`line_buffering=True` 解决一个很具体的坑：把输出重定向到文件或管道（`python main.py "..." > out.txt`）时，Python 默认改成块缓冲，你会看到文件里半分钟没内容，然后一次性刷出来。加上这一行就实时了。

---

## 四、`agent/events.py`

```python
"""回合事件输出层：控制台 / HTTP 流 / 其它前端共用同一事件接口。"""
from __future__ import annotations

import json

from agent.console import color


class ConsoleEmitter:
    """终端输出发射器（CLI 默认）。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.use_color = cfg.use_color

    def step(self, n: int) -> None:
        print(color(f"\n─── 第 {n} 步", "cyan", self.use_color), flush=True)

    def text(self, t: str) -> None:
        print(t, end="", flush=True)

    def think(self, t: str) -> None:
        if self.cfg.show_think:
            print(color(t, "dim", self.use_color), end="", flush=True)

    def newline(self) -> None:
        print(flush=True)

    def tool_call(self, name: str, args: dict) -> None:
        brief = json.dumps(args, ensure_ascii=False)[:300]
        print(color(f"⚙ 工具调用: {name}({brief})", "green", self.use_color), flush=True)

    def tool_result(self, name: str, text: str, dt: float,
                    images: list[str] | None = None) -> None:
        preview = text
        if len(preview) > 1500:
            preview = preview[:1500] + f"\n…[截断，共 {len(text)} 字符]"
        print(color(f"  ↳ ({dt:.1f}s) {preview}", "dim", self.use_color), flush=True)

    def stats(self, st: dict) -> None:
        if st.get("eval_count"):
            dur = (st.get("total_duration") or 0) / 1e9
            print(color(f"─── tokens: prompt={st.get('prompt_eval_count')} "
                        f"gen={st.get('eval_count')} 用时 {dur:.1f}s",
                        "cyan", self.use_color), flush=True)

    def notice(self, text: str) -> None:
        print(color(text, "yellow", self.use_color), flush=True)
```

**预览截断为什么在 emitter 里做？**

因为这是"显示"需求，不是"数据"需求。`history` 里存的是完整结果（模型需要全文），终端只显示前 1500 字符。截断放在 emitter，两个前端可以各自决定显示多少 —— WebUI 想显示全部就显示全部。

---

## 五、改造 `run_turn`

四处改动：

| 原来 | 改成 |
|---|---|
| 签名 6 个参数 | 加 `emitter=None`，缺省用 `ConsoleEmitter(cfg)` |
| `print(step)` | `emitter.step(step)` |
| `on_text=print...` | `on_text=emitter.text`（直接传方法引用） |
| 工具/打断/统计处的 `print` | `emitter.tool_call` / `tool_result` / `notice` / `stats` |
| 分散的 `return` | 统一成 `break` + 末尾一次 `stats()`（保证任何出口都打统计） |

新的核心部分：

```python
from agent.client import OllamaClient
from agent.config import AgentConfig
from agent.events import ConsoleEmitter
from agent.registry import ToolContext, ToolResult


def run_turn(client, cfg, ctx, history, user_text, images=None,
             emitter=None, cancel=None) -> str:
    if emitter is None:
        emitter = ConsoleEmitter(cfg)

    strip_old_images(history)

    user_msg = {"role": "user", "content": user_text}
    if images:
        user_msg["images"] = images
    history.append(user_msg)

    tools_spec = None if cfg.no_tools else tools.all_specs()
    final_text = ""
    resp: dict = {}

    def _stopped() -> bool:
        return cancel is not None and cancel.is_set()

    try:
        for step in range(1, cfg.max_steps + 1):
            if _stopped():
                emitter.notice("[⏹ 已打断本轮回答]")
                break
            emitter.step(step)
            resp = client.chat(
                history,
                tools=tools_spec,
                on_text=emitter.text,
                on_think=emitter.think,
                cancel=cancel,
            )
            emitter.newline()

            content = resp["content"]
            tool_calls = resp["tool_calls"]

            if resp.get("cancelled"):
                history.append({"role": "assistant",
                                "content": (content + "\n[已被用户打断]").strip()
                                           if content else "[已被用户打断]"})
                final_text = content
                emitter.notice("[⏹ 已打断本轮回答]")
                break

            assistant_msg: dict = {"role": "assistant", "content": content}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {"function": {"name": tc["function"]["name"],
                                  "arguments": tc["function"]["arguments"]}}
                    for tc in tool_calls
                ]
            history.append(assistant_msg)

            if not tool_calls:
                final_text = content
                break

            for i, tc in enumerate(tool_calls):
                name = tc["function"]["name"]
                args = tc["function"].get("arguments") or {}
                if not isinstance(args, dict):
                    args = {}
                if _stopped():
                    for tc2 in tool_calls[i:]:
                        history.append({"role": "tool",
                                        "content": "[已被用户打断，未执行]",
                                        "tool_name": tc2["function"]["name"]})
                    emitter.notice("[⏹ 已打断，跳过未执行的工具]")
                    final_text = content
                    return final_text       # 这里必须立刻退出，不能再进下一轮
                emitter.tool_call(name, args)
                t0 = time.time()
                result: ToolResult = tools.dispatch(name, args, ctx)
                dt = time.time() - t0
                emitter.tool_result(name, result.text, dt, images=result.images)

                tool_msg = {"role": "tool", "content": result.text, "tool_name": name}
                if result.images:
                    tool_msg["images"] = result.images
                history.append(tool_msg)
        else:
            final_text = "[已达到最大工具步数，强制结束]"
            emitter.notice(final_text)
    except KeyboardInterrupt:
        pad_unanswered_tool_calls(history)
        emitter.newline()
        emitter.notice("[⏹ 已打断本轮回答]")
        return resp.get("content") or ""

    emitter.stats(resp.get("stats") or {})
    return final_text
```

**唯一一个残留的 `return`** 在"工具间被打断"那个分支：

```python
                    return final_text
```

它和真实仓库一致，代价是**跳过了末尾的 `emitter.stats()`** —— 这一轮的 token 统计不会显示。想更一致可以改成 `break`（会多打一行统计，无害）。留着它当"例外"，提醒你：**统一出口是目标，不是铁律；每个提前返回都要自己保证历史自洽。**

---

## 六、验证：写一个 RecordingEmitter

这是本章最有价值的验证，因为**它证明"内核真的不依赖终端"**：

```python
"""把事件录下来，一个字符都不打印。"""
from pathlib import Path

from agent.client import OllamaClient
from agent.config import AgentConfig
from agent.loop import new_history, run_turn
from agent.registry import ToolContext


class RecordingEmitter:
    def __init__(self):
        self.events: list[tuple] = []

    def step(self, n):            self.events.append(("step", n))
    def text(self, t):            self.events.append(("text", t))
    def think(self, t):           self.events.append(("think", t))
    def newline(self):            self.events.append(("newline",))
    def tool_call(self, n, a):    self.events.append(("tool_call", n, a))
    def tool_result(self, n, t, dt, images=None):
        self.events.append(("tool_result", n, len(t), images))
    def stats(self, st):          self.events.append(("stats", st.get("eval_count")))
    def notice(self, t):          self.events.append(("notice", t))


cfg = AgentConfig()
client = OllamaClient(cfg)
ws = Path(".").resolve()
ctx = ToolContext(workspace=ws)
history = new_history(str(ws))

em = RecordingEmitter()
run_turn(client, cfg, ctx, history, "当前目录有多少个 py 文件？", emitter=em)

for e in em.events:
    print(e)
print("\n事件类型统计：", {k: sum(1 for e in em.events if e[0] == k)
                            for k in ("step", "text", "tool_call", "tool_result", "stats")})
```

```bash
python demo_recording.py
```

期望：**终端上一行模型输出都没有**，只有最后打印的事件列表。里面能看到 `('step', 1)`、`('tool_call', 'list_dir', {...})`、`('tool_result', 'list_dir', 128, None)`、`('stats', 215)`。

这也直接指向第 10 章的测试写法：**用 RecordingEmitter 就能对"agent 干了什么"做断言，完全不用 Ollama。**（用 stub client 替换真实模型 —— 第 10 章给代码。）

---

## 七、踩过的坑

| 现象 | 原因 | 解决 |
|---|---|---|
| 终端把思考过程和正文混在一起 | `think()` 忘了判断 `cfg.show_think` | 已加 |
| WebUI 里少一个事件 | 主循环某处还在用 `print` | `grep -n "print(" agent/loop.py` 应该只剩 0 处 |
| 输出顺序乱：工具结果出现在"第 2 步"之后 | emitter 里做了异步/缓冲 | emitter 必须**同步写**，顺序就是调用顺序 |
| 每个字都单独一个 `print` 调用，慢 | `text()` 里做了额外处理 | 保持 `print(t, end="", flush=True)` 最省 |
| 管道重定向后输出延迟 | 没 `line_buffering` | `setup_stdio()` |

---

## 八、过关自测

1. 加一个 GUI 前端要写多少行内核代码？
2. 为什么 `text` 和 `think` 要分成两个方法，不能带个 `kind` 参数？
3. `newline()` 在 WebUI 的实现是 `pass`，这算不算"接口设计失败"？
4. `tool_result` 的 `dt` 参数为什么由主循环算，而不是工具自己算？
5. 如果某个 emitter 方法抛异常，会发生什么？该怎么防？

<details>
<summary>答案要点</summary>

1. 0 行。实现那 8 个方法（外加可选的 `confirm`），然后传给 `run_turn`。
2. 分开的方法让实现方**只实现关心的**：终端要区别着色，WebUI 要区别渲染成不同气泡，而不需要的地方（比如日志 emitter）可以只实现 `text`，`think` 直接继承空实现。
3. 不算。接口的目的不是"每个方法都有意义"，而是"每个语义都有位置"。`newline` 表达的是"一步结束"，只是 WebUI 用不上这个视觉概念。
4. 因为计时应该由调用方负责：工具可能被 `dispatch` 包一层、可能有确认等待，只有主循环知道"从决定调用到拿到结果"的真实耗时。
5. 目前会穿透到 `run_turn` 的 `except KeyboardInterrupt` 之外，整个回合崩。防法：WebUI 的 `_send` 里已经吞掉 `BrokenPipeError`，任何 emitter 都应该在内部兜异常 —— **emitter 属于 I/O 层，I/O 错误不该炸业务逻辑**。

</details>

---

下一章：把它变成真正的命令行程序 → [第 6 章](06-cli.md)
