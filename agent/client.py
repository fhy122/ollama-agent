"""Ollama /api/chat 薄客户端：流式、think 参数、tool_calls 合并、模型预热。

协议依据（官方文档）：
  think 参数        https://ollama.com/blog/thinking
  tools / 图片格式  https://github.com/ollama/ollama/blob/main/docs/api.md
"""
from __future__ import annotations

import http.client
import json
import socket
import urllib.error
import urllib.parse
import urllib.request

from agent.console import color
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


def _payload_has_images(payload: dict) -> bool:
    return any(m.get("images") for m in (payload.get("messages") or []))


def _strip_images(payload: dict) -> dict:
    """返回去掉所有 images 的 payload 副本（不改原 history）。"""
    msgs = []
    for m in payload.get("messages") or []:
        if m.get("images"):
            m = {k: v for k, v in m.items() if k != "images"}
            m["content"] = (m.get("content") or "") + \
                "\n[图片已剥离：数据无效，模型看不到这些图]"
        msgs.append(m)
    return {**payload, "messages": msgs}


class OllamaClient:
    def __init__(self, config: AgentConfig):
        self.cfg = config
        self.host = config.host.rstrip("/")
        self.model = config.model
        self.think = config.think_value  # False/True/"low".../None(不传)
        self.num_ctx = config.num_ctx
        self.temperature = config.temperature
        # 当前连接/响应句柄，供 abort() 强制断开（见 abort 的注释）
        self._current_conn = None
        self._current_resp = None
        self._abort_requested = False

    # ---------- 中断 ----------

    def abort(self) -> None:
        """强制中断当前正在跑的流式请求（线程安全，可从任意线程调用）。

        为什么非要这一手：chat() 里靠 `for raw in resp` 每收到一行才检查
        cancel 事件，但**长上下文 prefill 期间 Ollama 一个字节都不发**，
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

    # ---------- HTTP ----------

    def _drop_conn(self) -> None:
        conn = getattr(self, "_current_conn", None)   # 兼容绕过 __init__ 的构造（测试）
        self._current_conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def _post_chat(self, payload: dict):
        """POST /api/chat，返回流式响应对象。

        为什么不用 urllib.request.urlopen：它在「等响应头」阶段会阻塞，
        而这期间它不向调用方暴露任何句柄——实测长上下文 prefill 十几秒里
        `_current_resp` 一直是 None，abort() 根本无处下手，点停止等于没反应。
        改用 http.client 自己持有连接，abort() 才能在 conn.sock 上 shutdown 唤醒。
        """
        parts = urllib.parse.urlsplit(self.host)
        host = parts.hostname or "localhost"
        port = parts.port or (443 if parts.scheme == "https" else 80)
        cls = (http.client.HTTPSConnection if parts.scheme == "https"
               else http.client.HTTPConnection)
        conn = cls(host, port, timeout=self.cfg.timeout)
        conn.auto_open = 1
        self._current_conn = conn

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            conn.request("POST", "/api/chat", body=body,
                         headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
        except TimeoutError as e:
            self._drop_conn()
            # 首字节之前就超时：模型加载或长上下文 prefill 太慢，此时一个 token 都没收到
            raise RuntimeError(
                f"等 Ollama 超过 {self.cfg.timeout} 秒仍无响应（模型加载或处理长上下文太慢）。"
                f"调大 agent/config.py 里的 timeout，或换更小的模型/上下文") from e
        except (ConnectionError, OSError) as e:
            self._drop_conn()
            if self._abort_requested:
                raise            # 主动打断造成的断开，交给 chat() 按 cancelled 处理
            raise RuntimeError(
                f"连不上 Ollama ({self.host})：{e}。请先启动 ollama serve") from e

        if resp.status != 200:
            err_body = resp.read().decode("utf-8", "replace")
            self._drop_conn()

            # 不支持思考的模型收到 think 字段会报错 → 去掉重试一次
            if "think" in payload and "think" in err_body.lower():
                print(color(f"[提示] 该模型不接受 think 字段，自动去掉后重试: "
                            f"{_error_text(err_body)[:120]}", "yellow", self.cfg.use_color),
                      flush=True)
                payload.pop("think", None)
                return self._post_chat(payload)

            # 图片数据无效（典型：把 xlsx/docx 当图片附上了）→ 剥离图片重试一次
            if _payload_has_images(payload) and "image" in err_body.lower():
                print(color("[提示] 附带的图片数据无效（可能把非图片文件当图片附上了），"
                            "已剥离图片重试。", "yellow", self.cfg.use_color), flush=True)
                return self._post_chat(_strip_images(payload))

            raise RuntimeError(
                f"Ollama API 错误 {resp.status}: {_error_text(err_body)}")

        self._current_resp = resp
        return resp

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
        """发起一轮对话 → {"thinking","content","tool_calls","stats","cancelled","error"}

        cancel: 可选 threading.Event；置位后中断流式读取并关闭连接，
        返回已生成的部分内容（cancelled=True）。Ollama 感知到客户端断开
        会自动终止服务端生成。

        error: 流中途断开的原因（读超时 / 连接被重置 / 数据不完整 / 没收到 done 帧）。
        非空表示这轮回答是半截的：调用方必须保留 content、把 tool_calls 当废数据丢掉，
        并明确告诉用户「断了」，绝不能当正常结束处理（否则内容与提示一起消失）。
        """
        payload = self._base_payload(messages)
        if tools:
            payload["tools"] = tools

        self._abort_requested = False
        try:
            resp = self._post_chat(payload)
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
                    done_seen = True
                    stats = {
                        "total_duration": obj.get("total_duration"),
                        "eval_count": obj.get("eval_count"),
                        "prompt_eval_count": obj.get("prompt_eval_count"),
                    }
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
            # 连接是正常关的，但 Ollama 从没发结束帧 → 同样是半截回答
            stream_error = "流意外结束（未收到 done 帧）"

        if cancelled or stream_error:
            try:
                resp.close()   # 关连接 → Ollama 停止服务端生成 / 释放句柄
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

        return {
            "thinking": "".join(thinking_parts),
            "content": "".join(content_parts),
            "tool_calls": tool_calls,
            "stats": stats,
            "cancelled": cancelled,
            "error": stream_error,
        }

    # ---------- 辅助 ----------

    def list_models(self) -> list[str]:
        req = urllib.request.Request(self.host + "/api/tags")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "?") for m in data.get("models", [])]

    def unload(self, model: str, timeout: float = 60) -> bool:
        """把模型从显存/内存里卸掉。返回 True = 确实卸载了。

        官方语义（空 prompt + keep_alive=0 → done_reason=unload）：
        https://github.com/ollama/ollama/blob/main/docs/api.md#unload-a-model
        没加载过 / 服务不可用都返回 False，调用方不该因为它失败就中止。
        """
        payload = {"model": model, "keep_alive": 0}
        req = urllib.request.Request(
            self.host + "/api/generate",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return False
        return data.get("done_reason") == "unload"

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
