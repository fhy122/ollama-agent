"""聊天后端抽象基类。

基类负责**与协议无关**的部分：持连接以便中断、发请求、读流、把流事件
攒成统一结果、错误文案压缩。子类只负责**协议本身**：

    name            后端显示名（报错文案用）
    chat_url()      聊天端点
    models_url()    模型列表端点
    headers()       鉴权头
    build_payload() 请求体
    parse_list_models() 模型列表 → 名字数组
    new_parser()    流解析器
    unload()        卸载语义（可不同，OpenAI 兼容后端直接 no-op）
"""
from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from agent.console import color


def error_text(body: str) -> str:
    """把后端错误压成一行人话。

    常见两种嵌套：
      Ollama      {"error":"{\\"error\\":{\\"code\\":400,\\"message\\":\\"...\\"}}"}
      中转站/OpenAI {"error":{"message":"...","code":"..."}}
    直接打印原文对用户毫无用处。
    """
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body.strip()[:300]
    if not isinstance(obj, dict):
        return body.strip()[:300]
    err = obj.get("error")
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


class StreamParser:
    """把后端流切成统一事件：

        {"type": "think", "text": ...}
        {"type": "text",  "text": ...}
        {"type": "tool",  "index": 0, "name": ..., "args": ...}
        {"type": "done",  "stats": {...}}
        {"type": "error", "text": ...}

    finished 标记「后端明确说过本轮结束了」，用来区分正常收尾和断流。
    """

    finished = False

    def feed(self, raw: bytes) -> list[dict]:
        raise NotImplementedError

    def flush(self) -> list[dict]:
        """流读完时的兜底。协议没有增量态就返回空。"""
        return []


class ChatClient:
    """聊天后端公共骨架。子类填协议细节，这里管连接、中断、流读取。"""

    name = "后端"

    # 这套协议认哪些参数。界面据此决定显示什么——
    # 显示一个实际没下发的 num_ctx 等于骗人，用户会以为调了上下文长度。
    supports_num_ctx = False
    supports_think = False
    supports_keep_alive = False
    supports_unload = False

    def __init__(self, config):
        self.cfg = config
        self.host = (config.host or "").rstrip("/")
        self.model = config.model
        self.temperature = config.temperature
        # 当前连接/响应句柄，供 abort() 强制断开（见 abort 的注释）
        self._current_conn = None
        self._current_resp = None
        self._abort_requested = False

    # ---------- 子类必须实现 ----------

    def chat_url(self) -> str:
        raise NotImplementedError

    def models_url(self) -> str:
        raise NotImplementedError

    def headers(self) -> dict:
        return {"Content-Type": "application/json"}

    def build_payload(self, messages: list, tools: list | None = None) -> dict:
        raise NotImplementedError

    def parse_list_models(self, data: dict) -> list[str]:
        raise NotImplementedError

    def new_parser(self) -> StreamParser:
        raise NotImplementedError

    # ---------- 可选覆盖 ----------

    def connection_hint(self) -> str:
        """连不上时给一句可执行的建议。"""
        return "检查地址是否正确、服务是否已启动"

    def auth_hint(self) -> str:
        """401/403 时补一句原因提示。"""
        return ""

    def unload(self, model: str, timeout: float = 60) -> bool:
        """把模型从显存卸掉。默认实现：该后端没有这个概念。"""
        return False

    # ---------- 中断 ----------

    def abort(self) -> None:
        """强制中断当前正在跑的流式请求（线程安全，可从任意线程调用）。

        为什么非要这一手：chat() 里靠 `for raw in resp` 每收到一行才检查
        cancel 事件，但**长上下文 prefill 期间后端一个字节都不发**，
        迭代器阻塞在那儿，cancel 检查点根本轮不到。用户点「停止」之后，
        后端要一直等到 prefill 跑完才有机会响应——这段时间会话锁没释放，
        新建/切换对话全被 409 「正在生成」挡回去，界面上却已经没有停止按钮了。

        注意只调 resp.close() 是**没用的**：真正卡住的地方往往还在「等响应头」，
        那时连 resp 都还没拿到（实测 `_current_resp` 十几秒里一直是 None）。
        urlopen 那种写法根本不暴露连接，所以这里用 http.client 自己持连接，
        abort 时 shutdown 底层 socket，让阻塞中的 recv / getresponse 立刻返回。
        """
        self._abort_requested = True
        conn = getattr(self, "_current_conn", None)
        sock = getattr(conn, "sock", None)
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)   # 唤醒阻塞中的 recv / 等响应头
            except Exception:
                pass
        resp = getattr(self, "_current_resp", None)
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass

    def _drop_conn(self) -> None:
        conn = getattr(self, "_current_conn", None)   # 兼容绕过 __init__ 的构造（测试）
        self._current_conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # ---------- HTTP ----------

    def _post_stream(self, messages: list, tools: list | None):
        """发聊天请求，返回流式响应对象。

        为什么不用 urllib.request.urlopen：它在「等响应头」阶段会阻塞，
        而这期间它不向调用方暴露任何句柄——实测长上下文 prefill 十几秒里
        `_current_resp` 一直是 None，abort() 根本无处下手，点停止等于没反应。
        改用 http.client 自己持有连接，abort() 才能在 conn.sock 上 shutdown 唤醒。

        messages 传的是内部原始历史：某些协议的图片塞在 payload 里已无法还原，
        「图片无效 → 剥离重试」只能从原始消息重来。
        """
        url = self.chat_url()
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname or "localhost"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        cls = (http.client.HTTPSConnection if parts.scheme == "https"
               else http.client.HTTPConnection)
        conn = cls(host, port, timeout=self.cfg.timeout)
        conn.auto_open = 1
        self._current_conn = conn

        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query

        payload = self.build_payload(messages, tools)
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            conn.request("POST", path, body=body, headers=self.headers())
            resp = conn.getresponse()
        except TimeoutError as e:
            self._drop_conn()
            # 首字节之前就超时：模型加载或长上下文 prefill 太慢，此时一个 token 都没收到
            raise RuntimeError(
                f"等{self.name}超过 {self.cfg.timeout} 秒仍无响应"
                f"（模型加载或处理长上下文太慢）。"
                f"调大 agent/config.py 里的 timeout，或换更小的模型/上下文") from e
        except (ConnectionError, OSError) as e:
            self._drop_conn()
            if self._abort_requested:
                raise            # 主动打断造成的断开，交给 chat() 按 cancelled 处理
            raise RuntimeError(
                f"连不上{self.name} ({url})：{e}。{self.connection_hint()}") from e

        if resp.status != 200:
            err_body = resp.read().decode("utf-8", "replace")
            self._drop_conn()
            retried = self._retry_on_known_error(resp.status, err_body, messages, tools)
            if retried is not None:
                return retried
            raise RuntimeError(
                f"{self.name} API 错误 {resp.status}: {error_text(err_body)}")

        self._current_resp = resp
        return resp

    def _retry_on_known_error(self, status: int, err_body: str,
                              messages: list, tools: list | None):
        """两个协议都遇到过的「去掉某个字段就好了」的错误，各只重试一次。

        返回新响应表示已重试；返回 None 表示不归这里管，交给调用方报错。
        """
        # 1) 模型不接受 think 字段 → 去掉重试
        low = err_body.lower()
        if "think" in low and "think" in json.dumps(
                self.build_payload(messages, tools)).lower():
            print(color(f"[提示] 该模型不接受 think 字段，自动去掉后重试: "
                        f"{error_text(err_body)[:120]}", "yellow", self.cfg.use_color),
                  flush=True)
            self._no_think = True
            return self._post_stream(messages, tools)

        # 2) 图片数据无效（典型：把 xlsx/docx 当图片附上了）→ 剥离重试
        if "image" in low and has_images(messages):
            print(color("[提示] 附带的图片数据无效（可能把非图片文件当图片附上了），"
                        "已剥离图片重试。", "yellow", self.cfg.use_color), flush=True)
            return self._post_stream(strip_images(messages), tools)

        return None

    # ---------- 对话 ----------

    def chat(self, messages: list, tools: list | None = None,
             on_text=None, on_think=None, cancel=None) -> dict:
        """发起一轮对话 → {"thinking","content","tool_calls","stats","cancelled","error"}

        cancel: 可选 threading.Event；置位后中断流式读取并关闭连接，
        返回已生成的部分内容（cancelled=True）。后端感知到客户端断开
        会自动终止服务端生成。

        error: 流中途断开的原因（读超时 / 连接被重置 / 数据不完整 / 没收到 done 帧）。
        非空表示这轮回答是半截的：调用方必须保留 content、把 tool_calls 当废数据丢掉，
        并明确告诉用户「断了」，绝不能当正常结束处理（否则内容与提示一起消失）。
        """
        import time as _time

        messages = list(messages)
        self._abort_requested = False
        t_start = _time.time()
        try:
            resp = self._post_stream(messages, tools)
        except Exception:
            self._drop_conn()
            if self._abort_requested:
                # 还没拿到响应就被打断：按 cancelled 收尾，别报成连接错误
                return {"thinking": "", "content": "", "tool_calls": [],
                        "stats": {}, "cancelled": True, "error": None}
            raise

        thinking_parts: list[str] = []
        content_parts: list[str] = []
        tool_calls: list[dict] = []
        stats: dict = {}
        cancelled = False
        done_seen = False
        stream_error: str | None = None
        parser = self.new_parser()

        def _absorb(events: list[dict]) -> None:
            nonlocal done_seen, stats, stream_error
            for ev in events:
                kind = ev.get("type")
                if kind == "think":
                    thinking_parts.append(ev["text"])
                    if on_think:
                        on_think(ev["text"])
                elif kind == "text":
                    content_parts.append(ev["text"])
                    if on_text:
                        on_text(ev["text"])
                elif kind == "tool":
                    i = ev.get("index", 0)
                    while len(tool_calls) <= i:
                        tool_calls.append({"function": {"name": "", "arguments": {}}})
                    if ev.get("name"):
                        tool_calls[i]["function"]["name"] = ev["name"]
                    args = ev.get("args", {})
                    merged = tool_calls[i]["function"].setdefault("arguments", {})
                    if isinstance(args, dict):
                        merged.update(args)
                    elif isinstance(args, str) and args.strip():
                        merged["_raw"] = merged.get("_raw", "") + args
                elif kind == "done":
                    done_seen = True
                    stats = ev.get("stats") or stats
                elif kind == "error":
                    stream_error = ev.get("text") or "后端返回错误"

        try:
            for raw in resp:
                if cancel is not None and cancel.is_set():
                    cancelled = True
                    break
                _absorb(parser.feed(raw))
                if done_seen or stream_error:
                    break
            else:
                # 流正常读完但没见到结束帧：有的后端只给 finish_reason，
                # 这里再 flush 一次兜底
                _absorb(parser.flush())
        except KeyboardInterrupt:
            # 终端 Ctrl+C：当作打断本轮（不向上抛，交给 run_turn 优雅收尾）
            cancelled = True
        except (TimeoutError, OSError, http.client.HTTPException, ValueError) as e:
            # 读超时 / 连接被重置 / chunked 数据不完整：保住已收到的部分，标记为断流。
            # 这三类以前会直接冒到 run_turn 之外，导致半截回答连历史都进不去。
            # ValueError 那条是 abort() 关掉连接后继续 read 抛的 "read of closed file"——
            # 用户主动停止，按打断处理，不能报成错误。
            if self._abort_requested:
                cancelled = True
            else:
                stream_error = f"{type(e).__name__}: {e}"

        self._current_resp = None          # 句柄用完即弃，避免 abort 打到下一轮
        self._drop_conn()

        if not cancelled and not stream_error and not done_seen:
            if parser.finished:
                done_seen = True      # 明确说过结束，只是没给 usage
            else:
                # 连接是正常关的，但后端从没发结束帧 → 同样是半截回答
                stream_error = "流意外结束（未收到 done 帧）"

        if cancelled or stream_error:
            try:
                resp.close()   # 关连接 → 后端停止服务端生成 / 释放句柄
            except Exception:
                pass

        for tc in tool_calls:
            args = tc["function"].get("arguments")
            if isinstance(args, dict) and set(args.keys()) == {"_raw"}:
                try:
                    tc["function"]["arguments"] = json.loads(args["_raw"])
                except (json.JSONDecodeError, TypeError):
                    tc["function"]["arguments"] = {"_raw": args["_raw"]}

        if cancelled or stream_error:
            # 流断了，tool_calls 的参数可能只到一半：执行它轻则报错、重则写坏文件
            tool_calls = []

        # 用时统一在客户端算：Ollama 会回 total_duration，OpenAI 兼容后端不回，
        # 不补这一手就会出现「用时 0.0s」这种明显错的显示
        stats = dict(stats or {})
        if not stats.get("total_duration"):
            stats["total_duration"] = int((_time.time() - t_start) * 1e9)
            stats["duration_local"] = True

        return {
            "thinking": "".join(thinking_parts),
            "content": "".join(content_parts),
            "tool_calls": tool_calls,
            "stats": stats,
            "cancelled": cancelled,
            "error": stream_error,
        }

    # ---------- 模型 ----------

    def list_models(self) -> list[str]:
        url = self.models_url()
        req = urllib.request.Request(url, headers=self.headers())
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # 401/403 是最常见的：把「缺 Key」这个真正原因说出来，
            # 而不是甩一个 "HTTP Error 401: Unauthorized" 让人猜
            if e.code in (401, 403):
                hint = self.auth_hint()
                raise RuntimeError(
                    f"{self.name} 拒绝访问（HTTP {e.code}）。{hint}".strip()
                ) from e
            raise RuntimeError(f"列模型失败（HTTP {e.code}）：{url}") from e
        return self.parse_list_models(data)

    def switch_model(self, new_model: str) -> bool:
        """切模型：先把旧模型从显存卸掉再换名字。

        不卸的后果是实测过的：切到 4B 后 9B 仍留在显存里（keep_alive 30m 各自计时），
        4GB 卡上两个模型 VRAM 需求合计超 4GB，靠 Ollama 反复换入换出，慢且可能 OOM。
        返回旧模型是否成功卸载。
        """
        old = self.model
        if not new_model or new_model == old:
            return False
        released = self.unload(old)
        self.model = new_model
        self.cfg.model = new_model
        return released

    def warmup(self) -> float:
        """发一条最小消息把模型载入内存，避免用户第一回合干等加载。返回耗时秒。"""
        import time
        t0 = time.time()
        self.chat([{"role": "user", "content": "say hello"}], tools=None)
        return time.time() - t0


def has_images(messages: list) -> bool:
    """这批消息里有没有带图。"""
    return any(m.get("images") for m in messages)


def strip_images(messages: list) -> list:
    """返回去掉所有 images 的消息副本（不改原 history）。"""
    out = []
    for m in messages:
        if m.get("images"):
            m = {k: v for k, v in m.items() if k != "images"}
            m["content"] = (m.get("content") or "") + \
                "\n[图片已剥离：数据无效，模型看不到这些图]"
        out.append(m)
    return out
