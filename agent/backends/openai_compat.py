"""OpenAI 兼容后端：/v1/chat/completions。

覆盖 token 中转站、vLLM、LM Studio、OpenRouter、官方 OpenAI 等所有
「说 OpenAI 协议」的服务。

与 Ollama 的差异都收敛在这个文件里：
  鉴权       Authorization: Bearer <key>（Ollama 无）
  字段       num_ctx / keep_alive / think 一律不下发（中转站会 400）
  工具消息   必须带 tool_call_id（Ollama 靠顺序配对）
  图片       content 数组里的 image_url data URL（Ollama 是 message.images）
  流格式     SSE "data: {...}" + [DONE]（Ollama 是整行 JSON）
"""
from __future__ import annotations

import json

from agent.backends.base import ChatClient, StreamParser
from agent.providers import endpoint, to_openai_payload
from agent.backends.openai_messages import to_openai_messages


class OpenAIStreamParser(StreamParser):
    """SSE 流：每行 `data: {...}`，以 `data: [DONE]` 或 finish_reason 结束。

    tool_calls 的 arguments 会跨多个 chunk 分片到达，必须先累积再 json 解析，
    否则每个分片都是半截 JSON。
    """

    def __init__(self):
        self._tool_acc: dict[int, dict] = {}

    def feed(self, raw: bytes) -> list[dict]:
        line = raw.decode("utf-8", "replace").strip()
        if not line or not line.startswith("data:"):
            return []      # 忽略 ": ping" 注释行和 event: 行
        data = line[5:].strip()
        if not data:
            return []
        if data == "[DONE]":
            self.finished = True
            return self._drain_tools()
        try:
            obj = json.loads(data)
        except json.JSONDecodeError:
            return []
        return self._feed_obj(obj)

    def _feed_obj(self, obj: dict) -> list[dict]:
        ev: list[dict] = []
        if obj.get("error"):
            err = obj["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            ev.append({"type": "error", "text": str(msg)})
            return ev
        for ch in obj.get("choices") or []:
            d = ch.get("delta") or {}
            # 思考过程：DeepSeek 用 reasoning_content，OpenRouter 用 reasoning
            th = d.get("reasoning_content") or d.get("reasoning")
            if th:
                ev.append({"type": "think", "text": th})
            if d.get("content"):
                ev.append({"type": "text", "text": d["content"]})
            for tc in d.get("tool_calls") or []:
                self._acc_tool(tc)
            if ch.get("finish_reason"):
                # 部分中转站只给 finish_reason、不给 [DONE]，标一下免得被当成断流
                self.finished = True
                ev.extend(self._drain_tools())
        usage = obj.get("usage")
        if usage:
            ev.append({"type": "done", "stats": {
                "eval_count": usage.get("completion_tokens"),
                "prompt_eval_count": usage.get("prompt_tokens"),
            }})
        return ev

    def _acc_tool(self, tc: dict) -> None:
        """增量累加一个 tool_call（同一个 index 会来很多次）。"""
        try:
            idx = int(tc.get("index", 0))
        except (TypeError, ValueError):
            idx = 0
        slot = self._tool_acc.setdefault(idx, {"name": "", "args": ""})
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["name"] = fn["name"]
        frag = fn.get("arguments")
        if isinstance(frag, str):
            slot["args"] += frag
        elif isinstance(frag, dict):
            slot["args"] = json.dumps(frag, ensure_ascii=False)

    def _drain_tools(self) -> list[dict]:
        ev: list[dict] = []
        for idx in sorted(self._tool_acc):
            slot = self._tool_acc[idx]
            if not slot.get("name"):
                continue
            raw = slot.get("args") or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = {"_raw": raw}   # 拼坏了原样交出去，让上层报错而不是静默吞掉
            ev.append({"type": "tool", "index": idx,
                       "name": slot["name"], "args": args})
        self._tool_acc.clear()
        return ev

    def flush(self) -> list[dict]:
        """流读完仍没吐出工具调用（中转站漏了结束帧）→ 这里补上。"""
        return self._drain_tools()


class OpenAICompatClient(ChatClient):
    """OpenAI 兼容协议客户端（中转站 / vLLM / LM Studio / 官方 OpenAI）。"""

    name = "中转站"

    def __init__(self, config):
        super().__init__(config)
        self.api_key = (getattr(config, "api_key", "") or "").strip()

    # ---------- 协议细节 ----------

    def chat_url(self) -> str:
        return endpoint(self.host, "openai", "chat")

    def models_url(self) -> str:
        return endpoint(self.host, "openai", "models")

    def new_parser(self) -> StreamParser:
        return OpenAIStreamParser()

    def headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def connection_hint(self) -> str:
        return "检查地址是否可访问、是否需要代理（国内直连境外站点常被墙）"

    def auth_hint(self) -> str:
        return "请在设置里填 API Key"

    def build_payload(self, messages: list, tools: list | None = None) -> dict:
        """注意：num_ctx / keep_alive / think 一个都不能下发。"""
        return to_openai_payload(
            self.model, messages, tools=tools,
            temperature=self.temperature,
        )

    def parse_list_models(self, data: dict) -> list[str]:
        # OpenAI: {"data":[{"id":"gpt-4o"}, ...]}
        return [m.get("id", "?") for m in data.get("data", [])]

    # ---------- 卸载：这套协议没有这个概念，显存由服务方管 ----------
    # 不覆盖 unload()，继承基类的 False 即可。
