# 第 2 章 · 让文字流出来

**本章产出**：`agent/config.py`、`agent/client.py`、`main.py`。合计约 300 行，仍然是零依赖，但已经是个能用于生产的协议客户端了。

---

## 一、这一章解决什么

| 第 1 章的毛病 | 本章的做法 |
|---|---|
| 干等 20 秒 | `stream: true` + 逐行解析，首字 1 秒内出现 |
| 错误信息看不懂 | `_error_text()` 把嵌套错误压成一行中文 |
| 配置散落在代码里 | 全部收进 `AgentConfig`，一个对象贯穿全局 |
| 不支持"思考模式" | `think` 三态 + 不支持时自动降级重试 |
| 关不掉正在生成的回答 | `cancel` 事件 + `resp.close()` |

---

## 二、目录先搭好

```bash
mkdir -p agent tools
touch agent/__init__.py tools/__init__.py
```

`agent/__init__.py` 留空即可 —— 有它 `agent` 才是包，才能 `from agent.config import AgentConfig`。

---

## 三、`agent/config.py`：所有可调项的唯一出处

```python
"""全局配置。"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MODEL = "qwen3.5:9b"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_CTX = 32768
MAX_TOOL_STEPS = 12

# think 字段取值：off=False 关思考（默认）；auto=None 表示"完全不传这个字段"
THINK_VALUES: dict[str, "bool | str | None"] = {
    "off": False,
    "on": True,
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "max",
    "auto": None,
}

SYSTEM_PROMPT = (
    "你是一个运行在本地的助手。规则：\n"
    "1. 回答用简体中文，直接、紧凑，先结论后细节。\n"
    "2. 不确定的事情说不确定，绝不编造事实、链接或文件内容。\n"
    "3. 需要读文件、跑命令、联网时先调用工具，不要凭猜测回答。\n"
    "4. 读写文件的相对路径一律相对于工作空间。\n"
)


@dataclass
class AgentConfig:
    host: str = DEFAULT_HOST
    model: str = DEFAULT_MODEL
    think: str = "off"                 # 键名，查 THINK_VALUES
    num_ctx: int | None = DEFAULT_CTX  # None = 不传，用服务端默认
    temperature: float = 0.7
    keep_alive: str = "30m"
    timeout: int = 1800
    max_steps: int = MAX_TOOL_STEPS
    confirm: bool = False
    use_color: bool = True
    show_think: bool = True
    no_tools: bool = False
    warm: bool = True
    workspace: str = ""                # 空 = 进程当前目录

    @property
    def think_value(self):
        return THINK_VALUES[self.think]
```

三个值得说的点：

**1. 为什么 `think` 存的是字符串键，不直接存 `False`/`True`？**

因为 `--think auto` 和 `--think off` 在 JSON 里的表现**完全不同**：`off` 传 `"think": false`，`auto` 是**整个字段都不发**。用一个字符串键 + 一张映射表，才能表达"不发"（`None`）这个第三态。

**2. 为什么 `num_ctx: int | None`？**

`0` 在命令行是合法的"我没设"，但传给 Ollama 会报错。用 `None` 表示"不传"，调用方写 `num_ctx=(args.ctx if args.ctx > 0 else None)` 就行。

**3. `SYSTEM_PROMPT` 为什么现在就写？**

因为它不是"介绍"，是**行为约束**。等第 3 章工具有了，这个 prompt 会扩到 9 条 —— 到时候你会发现，每一条规则都对应一个具体工具或一个具体失败模式。**先有工具才有规则**，所以这里只有 4 条。

---

## 四、`agent/client.py`：唯一跟 Ollama 说话的地方

```python
"""Ollama /api/chat 薄客户端：流式、think 参数、tool_calls 合并、模型预热。"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from agent.config import AgentConfig


def _error_text(body: str) -> str:
    """把 Ollama 的嵌套错误压成一行人话。

    Ollama 有时把上游错误字符串化再包一层：
      {"error":"{\"error\":{\"code\":400,\"message\":\"...\"}}"}
    直接打印对用户毫无用处。
    """
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body.strip()[:300]
    err = obj.get("error") if isinstance(obj, dict) else None
    if isinstance(err, dict):
        return str(err.get("message") or err)[:300]
    if isinstance(err, str):
        try:
            inner = json.loads(err)
        except json.JSONDecodeError:
            return err.strip()[:300]
        ie = inner.get("error") if isinstance(inner, dict) else None
        if isinstance(ie, dict):
            return str(ie.get("message") or ie)[:300]
        return str(ie or inner)[:300]
    return body.strip()[:300]


class OllamaClient:
    def __init__(self, config: AgentConfig):
        self.cfg = config
        self.host = config.host.rstrip("/")
        self.model = config.model
        self.think = config.think_value   # False / True / "low" / None(不传)
        self.num_ctx = config.num_ctx
        self.temperature = config.temperature

    # ---------- HTTP ----------

    def _post_chat(self, payload: dict):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.host + "/api/chat",
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        def _send(p: dict):
            req.data = json.dumps(p, ensure_ascii=False).encode("utf-8")
            return urllib.request.urlopen(req, timeout=self.cfg.timeout)

        try:
            return urllib.request.urlopen(req, timeout=self.cfg.timeout)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")

            # 不支持思考的模型收到 think 字段会报错 → 去掉重试一次
            if "think" in payload and "think" in body.lower():
                print(f"[提示] 该模型不接受 think 字段，自动去掉后重试："
                      f"{_error_text(body)[:120]}", flush=True)
                payload.pop("think", None)
                return _send(payload)

            raise RuntimeError(f"Ollama API 错误 {e.code}: {_error_text(body)}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(
                f"连不上 Ollama ({self.host})：{e.reason}。请先启动 ollama serve") from e

    def _base_payload(self, messages: list) -> dict:
        options: dict = {"temperature": self.temperature}
        if self.num_ctx is not None:
            options["num_ctx"] = self.num_ctx
        payload: dict = {
            "model": self.model,
            "messages": messages,
            "stream": True,
            "options": options,
            "keep_alive": self.cfg.keep_alive,
        }
        if self.think is not None:
            payload["think"] = self.think
        return payload

    # ---------- 对话 ----------

    def chat(self, messages: list, tools: list | None = None,
             on_text=None, on_think=None, cancel=None) -> dict:
        """发起一轮对话 → {"thinking", "content", "tool_calls", "stats", "cancelled"}"""
        payload = self._base_payload(messages)
        if tools:
            payload["tools"] = tools

        resp = self._post_chat(payload)

        thinking_parts: list[str] = []
        content_parts: list[str] = []
        tool_calls: list[dict] = []
        stats: dict = {}
        cancelled = False

        try:
            for raw in resp:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = obj.get("message") or {}

                th = msg.get("thinking")
                if th:
                    thinking_parts.append(th)
                    if on_think:
                        on_think(th)

                ct = msg.get("content")
                if ct:
                    content_parts.append(ct)
                    if on_text:
                        on_text(ct)

                # 流式下 tool_calls 按下标合并（arguments 可能是 dict 或 JSON 字符串）
                for i, tc in enumerate(msg.get("tool_calls") or []):
                    while len(tool_calls) <= i:
                        tool_calls.append({"function": {"name": "", "arguments": {}}})
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        tool_calls[i]["function"]["name"] = fn["name"]
                    args = fn.get("arguments", {})
                    merged = tool_calls[i]["function"].setdefault("arguments", {})
                    if isinstance(args, dict):
                        merged.update(args)
                    elif isinstance(args, str) and args.strip():
                        merged["_raw"] = merged.get("_raw", "") + args

                if obj.get("done"):
                    stats = {
                        "total_duration": obj.get("total_duration"),
                        "eval_count": obj.get("eval_count"),
                        "prompt_eval_count": obj.get("prompt_eval_count"),
                    }
        except KeyboardInterrupt:
            cancelled = True

        # _raw 收尾：流式里可能一路都是字符串片段，全部拼完再解析
        for tc in tool_calls:
            args = tc["function"].get("arguments")
            if isinstance(args, dict) and set(args.keys()) == {"_raw"}:
                try:
                    tc["function"]["arguments"] = json.loads(args["_raw"])
                except (json.JSONDecodeError, TypeError):
                    tc["function"]["arguments"] = {"_raw": args["_raw"]}

        if cancelled:
            try:
                resp.close()    # 关连接 → Ollama 停止服务端生成
            except Exception:
                pass
            tool_calls = []     # 流中断时参数可能不完整，弃用

        return {
            "thinking": "".join(thinking_parts),
            "content": "".join(content_parts),
            "tool_calls": tool_calls,
            "stats": stats,
            "cancelled": cancelled,
        }

    # ---------- 辅助 ----------

    def list_models(self) -> list[str]:
        req = urllib.request.Request(self.host + "/api/tags")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "?") for m in data.get("models", [])]

    def warmup(self) -> float:
        """发一条最小消息把模型载入内存，避免用户第一回合干等。返回耗时秒。"""
        import time
        t0 = time.time()
        self.chat([{"role": "user", "content": "say hello"}], tools=None)
        return time.time() - t0
```

### 四段需要单独解释的

**① `for raw in resp` 为什么能一行一行读？**

`urlopen` 返回的 `HTTPResponse` 是 `io.BufferedIOBase`，对它做迭代就是**按行迭代**。Ollama 的 `stream: true` 恰好是 NDJSON（每行一个完整 JSON），所以这个迭代方式天然对齐 —— 不需要手写缓冲区拼接。

对比一下别的写法，就知道这个有多省事：

```python
# 错误示范：read() 一次性读完 —— 流式白设了
data = resp.read()

# 错误示范：自己攒 buffer 切 \n —— 半行 JSON 会解析失败
buf = ""
while chunk := resp.read(1024):
    buf += chunk.decode()
```

**② `cancelled` 为什么要单独一个标志，不直接抛异常？**

因为"被打断"不是错误，是**正常的用户操作**。抛异常会穿透 `run_turn` 的 `try`，让调用方去区分"业务异常"和"用户按了 Ctrl+C"，很别扭。用一个布尔标志返回，调用方看 `resp["cancelled"]` 决定怎么收尾（第 4 章会看到，收尾逻辑还挺讲究）。

**③ 为什么要 `resp.close()`？**

Ollama 把生成结果往 socket 里写，客户端一断开，它下一次 write 就会失败并**立刻停止推理**。不 close 的话，你在终端按了 Ctrl+C，显卡还会继续跑完那 500 个 token。

**④ `_raw` 是干什么的？**

第 4 章详解。现在只需知道：某些模型/版本下 `arguments` 不是 `{"path": "a.txt"}` 这种 dict，而是一段**被切成多片的 JSON 字符串**，必须攒完再 `json.loads`。

---

## 五、`main.py`：临时入口

```python
"""临时入口（第 6 章会被 cli.py 取代）。"""
from __future__ import annotations

import argparse
import sys

from agent.client import OllamaClient
from agent.config import AgentConfig, SYSTEM_PROMPT


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("prompt", nargs="*", help="一次性问题；留空则进入多轮对话")
    ap.add_argument("-m", "--model")
    ap.add_argument("--think", default="off")
    args = ap.parse_args()

    cfg = AgentConfig(think=args.think)
    if args.model:
        cfg.model = args.model

    client = OllamaClient(cfg)
    history = [{"role": "system", "content": SYSTEM_PROMPT}]

    def on_text(t: str) -> None:
        print(t, end="", flush=True)

    def on_think(t: str) -> None:
        print(f"\033[2m{t}\033[0m", end="", flush=True)

    if args.prompt:
        history.append({"role": "user", "content": " ".join(args.prompt)})
        r = client.chat(history, on_text=on_text, on_think=on_think)
        print(f"\n--- tokens: {r['stats'].get('eval_count')}")
        return

    while True:
        try:
            text = input("\n你> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        if text in ("/exit", "/quit"):
            break
        history.append({"role": "user", "content": text})
        r = client.chat(history, on_text=on_text, on_think=on_think)
        history.append({"role": "assistant", "content": r["content"]})
        print()


if __name__ == "__main__":
    main()
```

注意 `history.append({"role": "assistant", ...})` 这一行：**模型自己的回答也要进历史**，否则下一轮它不知道刚才说了什么。这是第 1 章"模型无状态"的直接后果。

---

## 六、验证

```bash
# 1. 流式：文字应该逐字出现，不是一次性刷出来
python main.py "用 200 字介绍一下杭州"

# 2. 多轮：能记住上一句
python main.py
你> 我叫老王
你> 我叫什么？

# 3. 思考模式（换一个支持思考的模型，比如 qwen3 系列）
python main.py --think on -m qwen3:8b "9.11 和 9.9 哪个大"

# 4. 模型名写错 → 应该是中文人话，不是 HTTP Error
python main.py -m 不存在的模型 "你好"
# 期望：[错误] Ollama API 错误 404: model "不存在的模型" not found ...
```

第 4 条是本章最重要的验证。如果还是 `urllib.error.HTTPError`，说明 `_error_text` 没接上。

---

## 七、踩过的坑

| 现象 | 原因 | 解决 |
|---|---|---|
| 输出不是流式，还是一坨 | `payload["stream"]` 写成了 `False`，或者中间被 `print` 的缓冲挡住 | 每个 `print` 都要 `flush=True` |
| 首次调用 60 秒无响应 | 模型加载 + `num_ctx` 第一次生效 | 交互模式先 `warmup()`；命令行模式可以接受 |
| 改了 `num_ctx` 之后第一次特别慢 | `num_ctx` 变化会导致模型重新加载 KV cache 甚至整模型 | 正常，提示"改 ctx 会触发重载" |
| `--think on` 报 400 | 模型不支持思考 | 客户端已自动去掉 `think` 重试，只需确认提示打印出来了 |
| 中文乱码 | `resp.read().decode("utf-8")` 少了，或终端不是 UTF-8 | 第 5 章 `setup_stdio()` 统一处理 |
| Ctrl+C 后整个进程退出 | `KeyboardInterrupt` 在 `for raw in resp` 里没接 | 本章已接（`except KeyboardInterrupt: cancelled = True`） |
| 第二次请求就 400 | `history` 里塞了非标准字段 | 检查你 append 的 dict 只有 `role`/`content` |

---

## 八、过关自测

1. `THINK_VALUES["auto"]` 是 `None`，代码里怎么体现"不传"？
2. 流式响应里 `message.content` 是一整段还是碎片？为什么 `content_parts` 要用列表存再 `join`？
3. `_post_chat` 里有两个 `except`，分别对应什么故障？为什么 `HTTPError` 的 `body` 一定要 `read()`？
4. `warmup()` 为什么发"say hello"而不是空消息？
5. 如果 Ollama 返回的某一行不是合法 JSON（比如日志混进来了），代码会怎样？这是不是正确的选择？

<details>
<summary>答案要点</summary>

1. `_base_payload` 里 `if self.think is not None:` 才写字段；`None` 直接跳过。
2. 碎片。单条 fragment 可能只有一两个字，也可能半个 UTF-8 字符（解码用 `replace` 兜底），所以必须按序拼接。
3. `HTTPError` = 服务通了但返回 4xx/5xx（模型不存在、参数非法、显存不足）；`URLError` = 根本没连上（服务没起、端口错、代理劫持）。`HTTPError` 的响应体里才有真正的 error message，不读就只有 `404`。
4. 空消息某些模型会拒；`say hello` 保证是最短的有效请求，只为触发加载。
5. `continue` 跳过该行，其余部分照常。这是正确的 —— 流式传输中途出问题，宁可丢一行也不要整轮失败。

</details>

---

下一章：模型开始"要工具" → [第 3 章](03-tool-protocol.md)
