# 第 7 章 · 浏览器第二前端

**本章产出**：`webui/` 四个 Python 文件 + 一个能聊天的页面。约 360 行后端、90 行前端。

**关键点：`run_turn` 一行都不改。** 这一章是第 5 章那次重构的兑现 —— 如果发现自己在复制主循环，说明哪里走错了。

---

## 一、整体形状

```
浏览器 ──POST /api/chat──> Handler（一个线程）──> AgentSession.run()（持锁）
                                                      └─> run_turn(..., emitter=StreamEmitter)
                                                                └─> 往 wfile 写 NDJSON 行
浏览器 <──NDJSON 逐行───  ←────────────── 同一个 HTTP 响应流 ──────────────┘
```

三个类各管一件事：

| 类 | 管什么 | 不管什么 |
|---|---|---|
| `WebUIApp` / `Handler` | HTTP：路由、头、静态文件、请求体 | 不碰 history、不碰工具 |
| `AgentSession` | 状态：cfg / client / history / ctx / 锁 / 取消事件 | 不碰 HTTP 细节 |
| `StreamEmitter` | 事件 → NDJSON 行 | 不知道业务 |

---

## 二、为什么不用 FastAPI

只有 7 个接口，全是 JSON 进 JSON 出，其中一个还是长连接流式。用 FastAPI 要引 5 个依赖 + ASGI 服务器，换来的是"自动文档"和"路由装饰器" —— 对一个本地小工具不划算。

`ThreadingHTTPServer` 的好处是**每个请求一个线程**，这对我们的模型极其自然：

- 回合在一个线程里阻塞跑（等待模型、等待确认）
- 另一个线程可以同时处理 `POST /api/stop`
- `POST /api/confirm` 也能在回合进行中被处理

用单线程服务器就会死锁：回合没跑完，确认请求进不来。

---

## 三、`webui/stream.py`

```python
"""Web UI 事件发射器：把回合事件写成 NDJSON 流给浏览器。

每行一个 JSON 对象：
  {"type":"step","n":1}
  {"type":"text","text":"..."}
  {"type":"think","text":"..."}
  {"type":"tool_call","name":"read_file","args":{...}}
  {"type":"tool_result","name":"read_file","text":"...","dt":1.2,"images":[...]?}
  {"type":"confirm","id":"c1","question":"write_file({\"path\":...})"}
  {"type":"stats","prompt_tokens":..,"gen_tokens":..,"duration":..}
  {"type":"notice","text":"..."}
"""
from __future__ import annotations

import json


class StreamEmitter:
    def __init__(self, wfile, on_disconnect=None):
        self.wfile = wfile
        self._on_disconnect = on_disconnect
        self._disconnected = False

    def _send(self, obj: dict) -> None:
        try:
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 浏览器关页/断连：吞掉写错误；通知一次以便打断后端回合
            if not self._disconnected:
                self._disconnected = True
                if self._on_disconnect:
                    try:
                        self._on_disconnect()
                    except Exception:
                        pass

    def step(self, n):            self._send({"type": "step", "n": n})
    def text(self, t):            self._send({"type": "text", "text": t})
    def think(self, t):           self._send({"type": "think", "text": t})
    def newline(self):            pass
    def tool_call(self, name, args):
        self._send({"type": "tool_call", "name": name, "args": args})

    def tool_result(self, name, text, dt, images=None):
        obj = {"type": "tool_result", "name": name, "text": text, "dt": round(dt, 2)}
        if images:
            obj["images"] = images
        self._send(obj)

    def confirm(self, cid, question):
        self._send({"type": "confirm", "id": cid, "question": question})

    def stats(self, st, step=0):
        if not st:
            return
        self._send({"type": "stats", "step": step,
                    "prompt_tokens": st.get("prompt_eval_count"),
                    "gen_tokens": st.get("eval_count"),
                    "duration": round((st.get("total_duration") or 0) / 1e9, 2)})

    def notice(self, text):
        self._send({"type": "notice", "text": text})

    def thinking(self, elapsed, chars=0):
        self._send({"type": "thinking", "elapsed": round(elapsed, 1), "chars": chars})
```

**`thinking` 是这里唯一"每秒一次"的事件**，所以 `_send` 必须加锁：心跳跑在看门狗线程上，正文跑在读流线程上，两个线程同时往同一个 `wfile` 写会把 NDJSON 拼成半行 JSON，前端整批解析失败。

**`_send` 里的三个异常类型缺一不可**：`BrokenPipeError`（写完才失败）、`ConnectionResetError`（对端 RST）、`OSError`（Windows 上关页有时抛这个）。少接一个，用户关标签页就会在服务端控制台看到红色堆栈。

**`_disconnected` 只通知一次**：断开后每个事件都会失败，不设标志会连续触发几十次 `request_stop`。

---

## 四、`webui/session.py`

```python
"""Web UI 会话状态：配置 + 客户端 + 历史 + 确认闸，线程安全。"""
from __future__ import annotations

import threading
from pathlib import Path

from agent.client import OllamaClient
from agent.config import THINK_VALUES, AgentConfig
from agent.loop import new_history, run_turn
from agent.registry import ToolContext


class ConfirmGate:
    """工具执行前的人工确认闸（网页弹窗）。

    回合线程调 ask() 发出问题后阻塞；浏览器点按钮 → HTTP 线程调 answer() 放行。
    answer 不取会话大锁，避免与持锁等确认的回合线程互锁。
    超时未确认 = 自动拒绝（fail-safe，宁可不执行）。
    """

    timeout: float = 300.0

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}
        self._seq = 0
        self.allow_rest = False
        self.last_timeout = False

    def reset(self) -> None:
        with self._lock:
            self.allow_rest = False

    def ask(self, what: str, notify) -> bool:
        """notify(cid, question) 把确认请求推给前端；返回是否放行。"""
        self.last_timeout = False
        with self._lock:
            if self.allow_rest:
                return True
            self._seq += 1
            cid = f"c{self._seq}"
            entry = {"ok": False, "ev": threading.Event()}
            self._pending[cid] = entry
        try:
            notify(cid, what)
        except Exception:
            with self._lock:
                self._pending.pop(cid, None)
            return False
        finished = entry["ev"].wait(self.timeout)
        with self._lock:
            self._pending.pop(cid, None)
            if not finished:
                self.last_timeout = True
                return False
            return entry["ok"]

    def answer(self, cid: str, approved: bool, allow_rest: bool = False) -> bool:
        with self._lock:
            entry = self._pending.get(cid)
            if entry is None:
                return False
            entry["ok"] = bool(approved)
            if approved and allow_rest:
                self.allow_rest = True
            entry["ev"].set()
            return True

    def reject_all(self) -> None:
        """打断回合时唤醒所有等待中的确认（一律按拒绝处理）。"""
        with self._lock:
            entries = list(self._pending.values())
            self._pending.clear()
            self.allow_rest = False
        for e in entries:
            e["ok"] = False
            e["ev"].set()


class AgentSession:
    """一个对话会话。全局只有一份，HTTP 线程通过 lock 串行跑回合。"""

    def __init__(self, cfg: AgentConfig):
        self.cfg = cfg
        self.client = OllamaClient(cfg)
        self.workspace = Path(cfg.workspace) if cfg.workspace else Path.cwd()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.confirm = True          # 安全口默认开（设置面板可关）
        self.gate = ConfirmGate()
        self.ctx = ToolContext(confirm=self.confirm, use_color=False,
                               ask=None, workspace=self.workspace)
        self.history = new_history(str(self.workspace))
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()

    def run(self, text, images, emitter) -> str:
        with self.lock:
            self.gate.reset()
            self.cancel_event.clear()
            self.ctx.confirm = self.confirm
            self.ctx.ask = lambda what: self._ask(what, emitter)
            try:
                return run_turn(self.client, self.cfg, self.ctx, self.history,
                                text, images, emitter=emitter,
                                cancel=self.cancel_event)
            finally:
                self.ctx.ask = None

    def request_stop(self) -> None:
        """打断当前回合（HTTP 线程调用，不取会话锁）。"""
        self.cancel_event.set()
        self.gate.reject_all()

    def _ask(self, what, emitter) -> bool:
        ok = self.gate.ask(what, lambda cid, q: emitter.confirm(cid, q))
        if not ok and self.gate.last_timeout:
            emitter.notice("[确认超时] 未在限定时间内确认，已自动拒绝执行")
        return ok

    def answer_confirm(self, cid, approved, allow_rest=False) -> bool:
        """应答确认（HTTP 线程调用，不取会话锁）—— 关键：不取锁。"""
        return self.gate.answer(cid, approved, allow_rest)

    def reset(self) -> None:
        with self.lock:
            self.history = new_history(str(self.workspace))

    def snapshot(self) -> dict:
        return {"model": self.cfg.model, "think": self.cfg.think,
                "num_ctx": self.cfg.num_ctx, "max_steps": self.cfg.max_steps,
                "confirm": self.confirm, "workspace": str(self.workspace)}
```

### 两个必须理解的死锁点

**① `answer_confirm` 绝不能取 `self.lock`**

时序：

```
回合线程：with self.lock: … → 工具要确认 → gate.ask() 阻塞等待
HTTP 线程：POST /api/confirm → 想拿 self.lock → 永远拿不到 → 死锁
```

回合线程持着锁等确认，而确认要拿锁才能进来。所以 `answer_confirm` 只碰 `gate` 内部的**小锁**，完全不碰会话锁。

**② 超时必须是拒绝**

`gate.ask` 里 `entry["ev"].wait(300)`，300 秒没等到就返回 `False`。如果这里"默认允许"，用户关掉手机去吃午饭回来，agent 可能已经把他一整个目录删了。

> 安全设计的通用原则：**异常路径要落在安全侧**。超时=拒绝、断连=拒绝、未初始化=拒绝。

---

## 五、`webui/server.py`

```python
"""Web UI HTTP 服务：静态页 + JSON API + NDJSON 流式对话。

只绑 127.0.0.1（本地工具，不对外网开洞）。
"""
from __future__ import annotations

import json
import mimetypes
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from agent.config import AgentConfig
from webui.session import AgentSession
from webui.stream import StreamEmitter

STATIC_DIR = Path(__file__).parent / "static"


def _json_body(handler) -> dict:
    n = int(handler.headers.get("Content-Length") or 0)
    if n <= 0:
        return {}
    try:
        return json.loads(handler.rfile.read(n).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


class WebUIApp:
    def __init__(self, cfg: AgentConfig, host: str = "127.0.0.1", port: int = 8888):
        self.cfg = cfg
        self.host = host
        self.port = port
        self.session = AgentSession(cfg)

    def serve(self, open_browser: bool = False) -> None:
        app = self
        session = self.session

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"     # 响应写完即关，天然支持流式

            def log_message(self, *a):        # 静音，不然每个静态文件一行日志
                pass

            # ---------- 工具方法 ----------
            def _json(self, obj, code: int = 200) -> None:
                data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)

            def _file(self, p: Path) -> None:
                if not p.is_file():
                    self._json({"error": "not found"}, 404)
                    return
                data = p.read_bytes()
                ctype = mimetypes.guess_type(p.name)[0] or "application/octet-stream"
                if ctype.startswith("text/") or ctype in ("application/javascript",):
                    ctype += "; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)

            # ---------- GET ----------
            def do_GET(self):
                path = urlparse(self.path).path
                if path in ("/", "/index.html"):
                    self._file(STATIC_DIR / "index.html")
                elif path.startswith("/static/"):
                    self._file(STATIC_DIR / path[len("/static/"):])
                elif path == "/api/health":
                    self._json({"ok": True})
                elif path == "/api/config":
                    self._json(session.snapshot())
                else:
                    self._json({"error": "not found"}, 404)

            # ---------- POST ----------
            def do_POST(self):
                path = urlparse(self.path).path
                if path == "/api/chat":
                    self.handle_chat()
                elif path == "/api/confirm":
                    body = _json_body(self)
                    ok = session.answer_confirm(str(body.get("id") or ""),
                                                bool(body.get("approved")),
                                                bool(body.get("allow_rest")))
                    self._json({"ok": ok})
                elif path == "/api/stop":
                    session.request_stop()
                    self._json({"ok": True})
                elif path == "/api/reset":
                    session.reset()
                    self._json({"ok": True})
                else:
                    self._json({"error": "not found"}, 404)

            # ---------- 流式对话 ----------
            def handle_chat(self):
                body = _json_body(self)
                message = str(body.get("message") or "").strip()
                images = [i for i in (body.get("images") or []) if i]
                if not message and not images:
                    self._json({"error": "空消息"}, 400)
                    return

                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()

                emitter = StreamEmitter(self.wfile,
                                        on_disconnect=session.request_stop)
                try:
                    final = session.run(message or "看一下我附带的内容",
                                        images or None, emitter)
                    emitter._send({"type": "done", "text": final})
                except Exception as e:
                    emitter._send({"type": "error",
                                   "text": f"{type(e).__name__}: {e}"})
                    emitter._send({"type": "done", "text": ""})

        srv = ThreadingHTTPServer((self.host, self.port), Handler)
        url = f"http://{self.host}:{self.port}/"
        print(f"Web UI: {url}")
        if open_browser:
            webbrowser.open(url)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            srv.server_close()
```

> 真实仓库把 `Handler` 抽成了模块级的 `make_handler(app)`，这样测试可以直接构造它、不用起完整服务（`tests/test_ui_sidebar.py` 就是这么干的）。本章写在 `serve()` 里只为少一层缩进。

### 三个 HTTP 层面的坑（都在这几十行里）
**① `protocol_version = "HTTP/1.0"` 不是偷懒**

HTTP/1.1 要求每个响应有明确的结束方式（`Content-Length` 或分块编码）。我们不知道要写多少行 NDJSON，所以只能靠"连接关闭"来标记结束 —— 这是 HTTP/1.0 的行为。`Connection: close` 头是在告诉浏览器"读到 EOF 就是完了"。

改成 HTTP/1.1 又不想实现分块编码，就会看到浏览器**一直转圈**，因为它在等 `Content-Length`。

**② 每个响应都要 `end_headers()`**

`send_response()` + 若干 `send_header()` 之后必须 `end_headers()`，否则头没发出去，浏览器会当成"服务端没响应"。这是 `http.server` 最容易漏的一行。

**③ 静态文件的 charset**

```python
if ctype.startswith("text/") or ctype in ("application/javascript",):
    ctype += "; charset=utf-8"
```

`mimetypes` 给的 `text/html` 不带 charset，浏览器会用系统默认编码（中文 Windows 上可能是 GBK），页面里的中文全变问号。JS 文件同理 —— 源码里有中文注释就会 `SyntaxError`。

---

## 六、前端：90 行够用

`webui/static/index.html`

```html
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Ollama Agent</title>
<style>
  body { font: 14px/1.6 system-ui, sans-serif; max-width: 820px;
         margin: 0 auto; padding: 20px; background: #faf9f5; }
  #log > div { margin: 10px 0; padding: 10px 12px; border-radius: 8px; }
  .text { background: #fff; border: 1px solid #e5e3da; white-space: pre-wrap; }
  .tool { background: #f1f4f0; border-left: 3px solid #7a9b76; font-size: 12px; }
  .think { color: #8a8880; font-style: italic; white-space: pre-wrap; }
  .notice { color: #916c2b; }
  .step { color: #9a9890; font-size: 12px; }
  #bar { display: flex; gap: 8px; margin-top: 16px; }
  #msg { flex: 1; padding: 10px; border: 1px solid #d9d6cc; border-radius: 8px; }
  button { padding: 10px 16px; border: 0; border-radius: 8px;
           background: #2f5d50; color: #fff; cursor: pointer; }
  #confirm { display: none; position: fixed; inset: 0; background: #0006;
             align-items: center; justify-content: center; }
  #confirm > div { background: #fff; padding: 20px; border-radius: 10px; max-width: 420px; }
</style>
</head>
<body>
<h3>Ollama Agent</h3>
<div id="log"></div>
<div id="bar">
  <input id="msg" placeholder="说点什么，回车发送">
  <button id="send">发送</button>
  <button id="stop">停止</button>
</div>
<div id="confirm"><div>
  <p id="cq"></p>
  <button id="yes">允许</button> <button id="no">拒绝</button>
</div></div>
<script src="/static/app.js"></script>
</body>
</html>
```

`webui/static/app.js`

```javascript
const log = document.getElementById('log');
const msg = document.getElementById('msg');
const confirmBox = document.getElementById('confirm');

function el(cls, text) {
  const d = document.createElement('div');
  d.className = cls;
  if (text) d.textContent = text;
  log.appendChild(d);
  log.scrollTop = log.scrollHeight;
  return d;
}

let currentText = null;   // 当前正在追加的正文块

function handle(ev) {
  switch (ev.type) {
    case 'step':
      currentText = null;
      el('step', `── 第 ${ev.n} 步`);
      break;
    case 'text':
      if (!currentText) currentText = el('text', '');
      currentText.textContent += ev.text;
      break;
    case 'think':
      el('think', ev.text);              // 简化：逐片新建，生产环境应合并
      break;
    case 'tool_call':
      el('tool', `⚙ ${ev.name}(${JSON.stringify(ev.args)})`);
      break;
    case 'tool_result': {
      const t = ev.text.length > 800 ? ev.text.slice(0, 800) + '\n…' : ev.text;
      el('tool', `↳ ${ev.name} (${ev.dt}s)\n${t}`);
      break;
    }
    case 'confirm':
      document.getElementById('cq').textContent = `允许执行？\n${ev.question}`;
      confirmBox.style.display = 'flex';
      confirmBox.dataset.id = ev.id;
      break;
    case 'stats':
      el('step', `tokens: prompt=${ev.prompt_tokens} gen=${ev.gen_tokens} ${ev.duration}s`);
      break;
    case 'notice':
      el('notice', ev.text);
      break;
    case 'error':
      el('notice', `[错误] ${ev.text}`);
      break;
    case 'done':
      currentText = null;
      break;
  }
}

async function send() {
  const text = msg.value.trim();
  if (!text) return;
  msg.value = '';
  el('text', `你：${text}`).style.background = '#eef2f7';

  const resp = await fetch('/api/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message: text }),
  });

  const reader = resp.body.getReader();
  const decoder = new TextDecoder();
  let buf = '';
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += decoder.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, i).trim();
      buf = buf.slice(i + 1);
      if (!line) continue;
      try { handle(JSON.parse(line)); } catch (e) { console.warn('bad line', line); }
    }
  }
}

async function answer(approved) {
  confirmBox.style.display = 'none';
  await fetch('/api/confirm', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ id: confirmBox.dataset.id, approved }),
  });
}

document.getElementById('send').onclick = send;
document.getElementById('stop').onclick = () => fetch('/api/stop', { method: 'POST' });
document.getElementById('yes').onclick = () => answer(true);
document.getElementById('no').onclick = () => answer(false);
msg.addEventListener('keydown', e => { if (e.key === 'Enter') send(); });
```

前端那边唯一必须理解的：**`fetch` 的响应体是流**。`reader.read()` 每次给你一块字节，可能半行、可能三行半 —— 所以要自己攒 `buf`、按 `\n` 切。这跟后端 `for raw in resp` 是同一件事的两端。

---

## 七、启动入口

`webui/__main__.py`

```python
from __future__ import annotations

import argparse
from pathlib import Path

from agent.config import AgentConfig
from webui.server import WebUIApp


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("-m", "--model", default="")
    ap.add_argument("-w", "--workspace", default="")
    args = ap.parse_args()

    cfg = AgentConfig()
    if args.model:
        cfg.model = args.model
    cfg.workspace = str(Path(args.workspace).expanduser().resolve()) \
        if args.workspace else ""
    WebUIApp(cfg, host=args.host, port=args.port).serve(open_browser=True)


if __name__ == "__main__":
    main()
```

```bash
python -m webui -w . --port 8888
```

---

## 八、验证

1. 打开 `http://127.0.0.1:8888/`，问"当前目录有哪些文件" —— 应该看到 `⚙ list_dir` 卡片
2. 让它写一个文件 —— 应该弹出确认框，点"拒绝"后模型收到 `[用户拒绝执行]`
3. 生成到一半点"停止" —— 应该立刻停，并回到可输入状态
4. **关掉标签页** —— 服务端控制台不应出现堆栈，模型生成应停止（`on_disconnect` 生效）
5. 刷新页面 —— 历史还在（session 是服务端的，页面只是渲染）

第 4 条最容易做错。验证方法：问一个长问题，等它开始输出，直接关标签页，看显卡占用是不是掉下来了。

---

## 九、踩过的坑

| 现象 | 原因 | 解决 |
|---|---|---|
| 页面一直转圈不显示内容 | 缺少 `end_headers()`，或用了 HTTP/1.1 没 Content-Length | 本章已处理 |
| 中文变问号 | 静态文件 Content-Type 没带 charset | `_file` 里补 |
| 点确认没反应 | `answer_confirm` 取会话锁 → 死锁 | 只碰 `gate` 内部锁 |
| 两个标签页互相卡 | 单 session + 一把锁，本来就会串行 | 这是设计，不是 bug（多会话见下） |
| 服务端控制台刷错误 | 断连后还在写 | `_send` 吞异常 + `_disconnected` 去重 |
| 静态文件 404 | 路径拼接把 `/static/` 前缀带进去了 | 用 `path[len("/static/"):]` 剥掉 |
| 关页后模型继续跑 | `on_disconnect` 没接 | `StreamEmitter(..., on_disconnect=session.request_stop)` |

**关于"多个对话框"**：现在的 `AgentSession` 是 `WebUIApp` 上的单例。想支持多会话，最小改动是：

```python
self.sessions: dict[str, AgentSession] = {}     # 按前端生成的 sid 存
# 每个请求带 X-Session-Id，Handler 里 get_or_create
```

`run_turn` 完全不用动 —— 这正是第 0 章"没有 Agent 类、状态显式传参"的红利。

---

## 十、过关自测

1. 为什么 `ThreadingHTTPServer` 而不是 `HTTPServer`？
2. `protocol_version = "HTTP/1.0"` 对 NDJSON 流意味着什么？
3. `answer_confirm` 为什么不能取会话锁？画出死锁的两个线程。
4. 浏览器关页后，后端怎么知道要停？链路是什么？
5. 现在两个标签页会互相排队。这是缺陷还是设计？怎么改？

<details>
<summary>答案要点</summary>

1. 每个请求一个线程。回合线程在等模型/等确认时，`/api/stop` 和 `/api/confirm` 必须能被另一个线程处理。
2. 响应没有 Content-Length，靠连接关闭标记结束 —— 这恰好是流式需要的。代价是不能复用连接（每个请求新建 TCP），本地无所谓。
3. 回合线程 `with lock` 之后在 `gate.ask` 阻塞；如果 HTTP 线程应答时也要 `lock`，就永远拿不到。所以只碰 `gate._lock` 这把小锁。
4. 浏览器关页 → 服务端下一次 `wfile.write` 抛 `BrokenPipeError` → `_send` 捕获 → 调 `on_disconnect` → `session.request_stop()` → `cancel_event.set()` + `gate.reject_all()` → `run_turn` 中的 `_stopped()` 生效。**注意：是"下次写的时候"才发现，不是立即感知**，所以最坏要等一个事件周期。
5. 设计。单会话保证了历史一致（不然两个标签页同时写 `history` 会串）。要改就按 session id 分桶，每个 session 一把锁。

</details>

---

下一章：让它会写 Excel、看截图、加载技能 → [第 8 章](08-capabilities.md)
