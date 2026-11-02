# 第 10 章 · 不含模型也能测 agent

**本章产出**：一套离线回归测试的写法。跑一次 5 秒，不连 Ollama、不联网。

---

## 一、为什么不是 pytest

| 维度 | pytest | 脚本式 |
|---|---|---|
| 依赖 | 需要装 | 零依赖 |
| 运行 | `pytest` | `python tests/test_tools.py` |
| 断言失败 | 丰富 diff | 行号 + 你自己的消息 |
| 适合 | 大型项目、团队协作 | 单人项目、教学、CI 里当冒烟 |

这个项目选脚本式，是因为**测的东西很具体**：工具能不能正确读写、循环能不能正确收尾。不需要 fixture 魔法、不需要参数化、不需要插件。

标准结构就三行：

```python
def check(cond, msg):           # 断言助手
    global FAILED
    if cond:
        print(f"PASS  {msg}")
    else:
        FAILED += 1
        print(f"FAIL  {msg}")
```

跑完打印 `全部通过` 或 `N 项失败`，`sys.exit(1)` 让 CI 能感知。

---

## 二、五种造场景的手段

这是本章的核心。**能离线测，全靠这五招**：

| 手段 | 用来测 | 关键代码 |
|---|---|---|
| ① 临时工作空间 | 所有文件类工具、路径解析 | `tempfile.mkdtemp()` + `ToolContext(workspace=...)` |
| ② 记录型 emitter | `run_turn` 的事件序列 | 自己实现 8 个方法，往 list 里 append |
| ③ 桩 client | 多步循环、打断、错误恢复 | 一个类，`chat()` 返回预设脚本 |
| ④ 替换 `urlopen` | 客户端协议解析、400 重试 | 猴补丁 + 假 `HTTPError` |
| ⑤ 真 HTTP 打自己的服务 | WebUI 路由、NDJSON | 起 `WebUIApp` 线程，`urllib` 请求 |

### ① 临时工作空间

```python
import tempfile
from pathlib import Path
from agent.registry import ToolContext

tmp = Path(tempfile.mkdtemp(prefix="agent-test-"))
ctx = ToolContext(workspace=tmp)

(tmp / "a.txt").write_text("你好\n世界\n", encoding="utf-8")
r = dispatch("read_file", {"path": "a.txt"}, ctx)
check("1\t你好" in r.text, "read_file 返回带行号的文本")
```

**为什么必须用临时目录？** 因为测 `write_file` 会真的写盘。写到项目目录里，跑两次测试就有一堆垃圾文件；写到临时目录，进程退出自动清理。

### ② 记录型 emitter

```python
class Rec:
    def __init__(self): self.ev = []
    def step(self, n): self.ev.append(("step", n))
    def text(self, t): self.ev.append(("text", t))
    def think(self, t): self.ev.append(("think", t))
    def newline(self): pass
    def tool_call(self, n, a): self.ev.append(("tool_call", n, a))
    def tool_result(self, n, t, dt, images=None): self.ev.append(("tool_result", n))
    def stats(self, st): self.ev.append(("stats",))
    def notice(self, t): self.ev.append(("notice", t))
```

有了它，"agent 干了什么"变成一个可断言的列表：

```python
kinds = [e[0] for e in rec.ev]
check(kinds.count("tool_call") == 1, "只调了一次工具")
check(("step", 1) in rec.ev and ("step", 2) in rec.ev, "跑了两步")
```

### ③ 桩 client（最关键的一个）

`run_turn` 是最难测的部分 —— 它要模型配合。但你不必真的调模型，**client 的接口只有一个 `chat()`**：

```python
class StubClient:
    """按脚本返回响应，完全不联网。"""

    def __init__(self, script: list[dict]):
        self.script = script
        self.calls = 0
        self.seen_history: list[list] = []

    def chat(self, messages, tools=None, on_text=None, on_think=None, cancel=None):
        self.seen_history.append([dict(m) for m in messages])
        r = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if r.get("content") and on_text:
            on_text(r["content"])
        return {"thinking": "", "content": r.get("content", ""),
                "tool_calls": r.get("tool_calls", []),
                "stats": {}, "cancelled": r.get("cancelled", False)}
```

脚本长这样：

```python
script = [
    {"content": "", "tool_calls": [
        {"function": {"name": "read_file", "arguments": {"path": "a.txt"}}}]},
    {"content": "这个文件有两行，分别是「你好」和「世界」。"},
]
```

于是整个多步循环可以在 0.1 秒内跑完，而且**完全确定性**。

### ④ 替换 `urlopen`

测 400 重试那次，要模拟"Ollama 返回 400，body 是嵌套 JSON"：

```python
import agent.client as C

REAL_OPEN = C.urllib.request.urlopen

def fake_400(*a, **kw):
    return _http400('{"error":"{\\"error\\":{\\"code\\":400,'
                    '\\"message\\":\\"Failed to load image or audio file\\"}}"}')

C.urllib.request.urlopen = fake_400
try:
    ...
finally:
    C.urllib.request.urlopen = REAL_OPEN      # 一定要还原
```

`HTTPError` 的第四个参数要传一个文件对象：

```python
def _http400(body: str):
    return urllib.error.HTTPError("http://t/api/chat", 400, "Bad Request", {},
                                  io.BytesIO(body.encode("utf-8")))
```

因为 `client` 里 `e.read()` 读的就是它 —— 不传 `BytesIO` 会 `AttributeError`。

**注意 `finally` 里还原**。忘了还原，后面所有测试都会莫名其妙地"连不上 Ollama"。

### ⑤ 真 HTTP 打自己的服务

WebUI 的测试不必打开浏览器：

```python
app = WebUIApp(AgentConfig(), port=8899)
threading.Thread(target=app.serve, daemon=True).start()
time.sleep(0.3)                      # 等端口起来
status, body = _get("http://127.0.0.1:8899/api/health")
check(status == 200, "健康检查 200")
```

测流式响应时，直接读原始字节，验证是不是合法 NDJSON：

```python
req = urllib.request.Request(url + "/api/chat",
                             data=json.dumps({"message": "你好"}).encode(),
                             headers={"Content-Type": "application/json"})
with urllib.request.urlopen(req) as r:
    lines = [l for l in r.read().decode().splitlines() if l.strip()]
for ln in lines:
    json.loads(ln)                   # 每行都必须是合法 JSON
```

---

## 三、一个完整的 `run_turn` 测试

把 ①②③ 拼起来，这就是最有价值的一个测试：

```python
"""离线测 run_turn：多步工具调用 + 事件序列 + 历史合法性。"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tools  # noqa: E402 触发注册
from agent.config import AgentConfig  # noqa: E402
from agent.loop import new_history, run_turn, pad_unanswered_tool_calls  # noqa: E402
from agent.registry import ToolContext  # noqa: E402

FAILED = 0


def check(cond, msg):
    global FAILED
    if cond:
        print(f"PASS  {msg}")
    else:
        FAILED += 1
        print(f"FAIL  {msg}")


class Rec:
    def __init__(self): self.ev = []
    def step(self, n): self.ev.append(("step", n))
    def text(self, t): self.ev.append(("text", t))
    def think(self, t): pass
    def newline(self): pass
    def tool_call(self, n, a): self.ev.append(("tool_call", n))
    def tool_result(self, n, t, dt, images=None): self.ev.append(("tool_result", n))
    def stats(self, st): pass
    def notice(self, t): self.ev.append(("notice", t))


class StubClient:
    def __init__(self, script):
        self.script, self.calls, self.histories = script, 0, []

    def chat(self, messages, tools=None, on_text=None, on_think=None, cancel=None):
        self.histories.append([dict(m) for m in messages])
        r = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if r.get("content") and on_text:
            on_text(r["content"])
        return {"thinking": "", "content": r.get("content", ""),
                "tool_calls": r.get("tool_calls", []),
                "stats": {}, "cancelled": r.get("cancelled", False)}


def test_two_step():
    tmp = Path(tempfile.mkdtemp(prefix="agent-turn-"))
    (tmp / "a.txt").write_text("你好\n世界\n", encoding="utf-8")

    script = [
        {"content": "", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "a.txt"}}}]},
        {"content": "文件两行：你好、世界。"},
    ]
    client = StubClient(script)
    rec = Rec()
    cfg = AgentConfig(use_color=False)
    ctx = ToolContext(workspace=tmp)
    history = new_history(str(tmp))

    final = run_turn(client, cfg, ctx, history, "读一下 a.txt", emitter=rec)

    check(final == "文件两行：你好、世界。", "最终回答取自第 2 步")
    check(client.calls == 2, "调了两次模型")

    roles = [m["role"] for m in history]
    check(roles == ["system", "user", "assistant", "tool", "assistant"],
          f"历史结构正确: {roles}")
    check(history[2].get("tool_calls"), "assistant 消息保留了 tool_calls")
    check("你好" in history[3]["content"], "tool 消息里有文件内容")
    check(history[3].get("tool_name") == "read_file", "tool 消息带 tool_name")

    kinds = [e[0] for e in rec.ev]
    check(kinds == ["step", "tool_call", "tool_result", "step", "text"],
          f"事件顺序正确: {kinds}")


def test_cancel_drops_tool_calls():
    """生成中途被打断：tool_calls 必须丢弃，不产生孤立 tool 消息。"""
    client = StubClient([{"content": "说到一半", "cancelled": True}])
    rec = Rec()
    cfg = AgentConfig(use_color=False)
    tmp = Path(tempfile.mkdtemp(prefix="agent-cancel-"))
    ctx = ToolContext(workspace=tmp)
    history = new_history(str(tmp))

    run_turn(client, cfg, ctx, history, "说点什么", emitter=rec)

    check(history[-1]["role"] == "assistant", "末条是 assistant")
    check("tool_calls" not in history[-1], "被打断的 assistant 不带 tool_calls")
    check("[已被用户打断]" in history[-1]["content"], "部分文本保留 + 标注")


def test_pad_unanswered():
    """孤立 tool_calls 要被补齐（下一轮才不会 400）。"""
    history = [
        {"role": "user", "content": "读两个文件"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "a.txt"}}},
            {"function": {"name": "read_file", "arguments": {"path": "b.txt"}}}]},
        {"role": "tool", "content": "a 的内容", "tool_name": "read_file"},
    ]
    n = pad_unanswered_tool_calls(history)
    check(n == 1, "补了 1 条")
    check(history[-1]["role"] == "tool", "补的是 tool 消息")
    check("未执行" in history[-1]["content"], "占位文案正确")


if __name__ == "__main__":
    test_two_step()
    test_cancel_drops_tool_calls()
    test_pad_unanswered()
    print("\n" + ("全部通过" if not FAILED else f"{FAILED} 项失败"))
    sys.exit(1 if FAILED else 0)
```

`sys.path.insert(0, parents[1])` 那两行是必需的 —— 直接 `python tests/test_xxx.py` 时，`sys.path[0]` 是 `tests/` 而不是项目根，`import agent` 会失败。

---

## 四、真实仓库的五个测试各测什么

| 文件 | 行数 | 覆盖 |
|---|---|---|
| `test_tools.py` | 708 | 主回归：15 个工具全在、路径解析规则、xlsx/docx 生成与解析、参差行报错、图片魔数、`edit_file` 唯一匹配、`clip_text`、`glob_to_regex` |
| `test_webui.py` | 431 | 静态页 200、`/api/health`、配置读改、上传、`/api/skills`、`/api/files`、NDJSON 每行合法、`StreamEmitter` 8 个方法 |
| `test_client_errors.py` | 129 | 400 嵌套错误压平、think 字段自动去掉重试、图片无效剥离重试、历史不被改动 |
| `test_interrupt.py` | 117 | `KeyboardInterrupt` 收尾、孤立 tool_calls 补齐、`cancelled` 路径 |
| `test_cli_palette_console.py` | 278 | 真控制台下的面板按键序列、`color()`、`setup_stdio` |
| `test_ui_sidebar.py` | 126 | 用 Playwright 点真页面：侧栏收起后还能不能展开（桌面窄轨 / 窄屏抽屉 / Ctrl+B） |

每个文件的共同点：**顶部 `sys.path.insert` + `import tools`**（触发注册），然后 `main()` 里串行跑检查。

---

## 五、调试技巧

| 想知道 | 怎么做 |
|---|---|
| 工具注册对了吗 | `/tools`（CLI）或 `python -c "import tools; print([t.name for t in tools.all_tools()])"` |
| 模型到底发了什么 | 在 `client.chat` 里临时 `print(json.dumps(payload['messages'], ensure_ascii=False)[:2000])` |
| 历史坏在哪 | `/save hist.json` 然后看结构；或写个脚本检查"tool_calls 数 == 后续 tool 数" |
| 为什么模型不调工具 | 先把 `all_specs()` 打印出来，检查 description 是否被截断、`required` 是否合理 |
| token 用在哪了 | `emitter.stats()`；`prompt_eval_count` 大 = 历史太长 |
| 想复现一次对话 | `/save` + `/load`，历史原样重放 |

还有一个好用的土办法：**假 Ollama 服务器**。写个 20 行的 `http.server`，固定返回预设 NDJSON，用来测前端或 client：

```python
LINES = [
    '{"message":{"content":"你好"},"done":false}\n',
    '{"message":{"content":"，世界"},"done":false}\n',
    '{"message":{"content":""},"done":true,"eval_count":2}\n',
]
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for l in LINES:
            self.wfile.write(l.encode()); self.wfile.flush()
```

把 `--host http://127.0.0.1:11500` 指过去，就能在没用模型的情况下调 UI。

---

## 六、踩过的坑

| 现象 | 原因 | 解决 |
|---|---|---|
| `ModuleNotFoundError: agent` | 直接运行脚本，`sys.path[0]` 是 `tests/` | 顶部 `sys.path.insert(0, parents[1])` |
| 测试跑两次后莫名其妙失败 | 上次写的文件还在 | 用 `tempfile.mkdtemp()`，别写 `./tmp` |
| 猴补丁没还原，后面的测试全挂 | 忘了 `finally` | 一定 `try/finally` 还原 |
| `HTTPError` 构造报错 | 第 5 个参数不是文件对象 | `io.BytesIO(body.encode())` |
| 端口被占用，WebUI 测试卡住 | 上次的测试进程没退 | 用随机端口（`port=0` 让系统分配）或固定高位端口 + `daemon=True` |
| 测 `run_turn` 时卡在确认 | `ctx.confirm=True` 且 `ask=None` → 放行；但若 `ask` 是 `input` 就会阻塞 | 测试里 `confirm=False` |
| 断言用 `assert` 失败没消息 | 裸 `assert` 只有行号 | 用 `check(cond, msg)` |

---

## 七、过关自测

1. 为什么 `run_turn` 的测试必须用桩 client，而不能"跑一次真模型看看"？
2. `pad_unanswered_tool_calls` 的测试里，为什么构造的历史要以 `tool` 消息结尾？
3. 测 `write_file` 时不清理 temp 目录会怎样？为什么还推荐这么做？
4. 猴补丁 `urlopen` 之后，忘了还原会发生什么？
5. 一个测试"模型会不会调对工具"，能不能离线写？怎么写？

<details>
<summary>答案要点</summary>

1. 因为要断言**确定性**的中间状态（历史结构、事件顺序）。真模型每次输出不同，只能测"大概能用"。桩 client 让 12 步循环在 0.1 秒内跑完且完全可复现。
2. 要模拟"最后一个 assistant 声明了 2 个 tool_calls，只回了 1 个"的场景 —— 所以历史末尾必须是已应答的那条 `tool`，函数才能正确算出"还差 1 个"。
3. temp 目录会堆积（系统临时目录里），但通常会随系统清理；相比"污染项目目录导致第二次测试读到上次的文件"，这个代价更小。要清理就 `shutil.rmtree(tmp, ignore_errors=True)` 写在 `finally`。
4. 后面所有依赖网络的测试都会拿到你的假响应，表现为"测试莫名奇妙地成功/失败"，极难排查。
5. 能，但只能测"给定模型输出，工具是否正确执行"（桩 client + 断言 dispatch 结果）。"模型是否**愿意**调"必须用真模型，属于评估（eval）而不是单元测试，建议单独跑一批固定问题看命中率。

</details>

---

下一章：和你自己的代码对账 → [第 11 章](11-diff-to-repo.md)
