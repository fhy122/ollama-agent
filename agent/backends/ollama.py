"""Ollama 原生后端：/api/chat。

协议依据（官方文档）：
  think 参数        https://ollama.com/blog/thinking
  tools / 图片格式  https://github.com/ollama/ollama/blob/main/docs/api.md
  卸载模型          https://github.com/ollama/ollama/blob/main/docs/api.md#unload-a-model
"""
from __future__ import annotations

import json
import urllib.request

from agent.backends.base import ChatClient, StreamParser
from agent.providers import endpoint


class OllamaStreamParser(StreamParser):
    """一行一个完整 JSON，结束帧是 {"done": true}。

    tool_calls 在这套协议里是**整块**给的（不像 OpenAI 分片），
    所以 feed 里直接吐出去，不需要累积状态。
    """

    def feed(self, raw: bytes) -> list[dict]:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            return []
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return []
        ev: list[dict] = []
        msg = obj.get("message") or {}
        if msg.get("thinking"):
            ev.append({"type": "think", "text": msg["thinking"]})
        if msg.get("content"):
            ev.append({"type": "text", "text": msg["content"]})
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            ev.append({"type": "tool", "index": i,
                       "name": fn.get("name") or "",
                       "args": fn.get("arguments", {})})
        if obj.get("done"):
            self.finished = True
            ev.append({"type": "done", "stats": {
                "total_duration": obj.get("total_duration"),
                "eval_count": obj.get("eval_count"),
                "prompt_eval_count": obj.get("prompt_eval_count"),
            }})
        return ev


class OllamaClient(ChatClient):
    """Ollama 原生协议客户端（本地或别人的 Ollama 服务）。"""

    name = "Ollama"

    supports_num_ctx = True       # options.num_ctx
    supports_think = True         # think 字段
    supports_keep_alive = True    # keep_alive
    supports_unload = True        # /api/generate + keep_alive=0

    # 类属性兜底：测试会用 __new__ 绕过 __init__ 造实例，
    # 实例属性缺失时这里还能给出默认值，不至于 AttributeError
    _no_think = False

    def __init__(self, config):
        super().__init__(config)
        self.think = config.think_value   # False/True/"low".../None(不传)
        self.num_ctx = config.num_ctx
        self._no_think = False            # 模型拒收 think 后置位，后续不再下发

    # ---------- 协议细节 ----------

    def chat_url(self) -> str:
        return endpoint(self.host, "ollama", "chat")

    def models_url(self) -> str:
        return endpoint(self.host, "ollama", "models")

    def new_parser(self) -> StreamParser:
        return OllamaStreamParser()

    def connection_hint(self) -> str:
        return "请先启动 ollama serve（对方机器的 Ollama 需设 OLLAMA_HOST=0.0.0.0）"

    def auth_hint(self) -> str:
        return ""      # Ollama 本身没有鉴权

    def build_payload(self, messages: list, tools: list | None = None) -> dict:
        """Ollama 专有字段全家桶：options / keep_alive / think 都在这下发。"""
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
        if self.think is not None and not self._no_think:
            payload["think"] = self.think
        if tools:
            payload["tools"] = tools
        return payload

    def parse_list_models(self, data: dict) -> list[str]:
        return [m.get("name", "?") for m in data.get("models", [])]

    # ---------- 卸载（Ollama 独有） ----------

    def unload(self, model: str, timeout: float = 60) -> bool:
        """把模型从显存/内存里卸掉。返回 True = 确实卸载了。

        官方语义（空 prompt + keep_alive=0 → done_reason=unload）：
        https://github.com/ollama/ollama/blob/main/docs/api.md#unload-a-model
        没加载过 / 服务不可用都返回 False，调用方不该因为它失败就中止。
        """
        payload = {"model": model, "keep_alive": 0}
        req = urllib.request.Request(
            endpoint(self.host, "ollama", "generate"),
            data=json.dumps(payload).encode("utf-8"),
            headers=self.headers(),
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception:
            return False
        return data.get("done_reason") == "unload"
