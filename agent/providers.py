"""协议识别与 OpenAI 请求体构造。

这里曾经塞了 StreamParser、消息转换、URL 拼接，一个文件三种职责，还带 if 分支
——后来拆掉了：
  各协议实现    agent/backends/ollama.py、agent/backends/openai_compat.py
  消息转换      agent/backends/openai_messages.py
  公共骨架      agent/backends/base.py

本文件只留「跟具体连接无关」的两件小事：认协议、造请求体。

另记一条实测结论：/api/* 是 Ollama 专有路径，aigatehub 这类中转站对它返回的是
自己 Web 后端的 success=false / UNAUTHORIZED，不是 Ollama 的响应格式。
所以「把 host 指过去就能用」是错的，必须换路径 + 换协议。
"""
from __future__ import annotations

OLLAMA = "ollama"
OPENAI = "openai"
PROVIDERS = (OLLAMA, OPENAI)

PROVIDER_LABELS = {
    OLLAMA: "Ollama 原生（本地 / 别人的 Ollama，/api/chat）",
    OPENAI: "OpenAI 兼容（中转站 / vLLM / LM Studio，/v1/chat/completions）",
}


def detect_provider(host: str) -> str:
    """按 host 猜后端类型：带 /v1 基本就是 OpenAI 兼容（Ollama 没有 /v1 前缀）。"""
    h = (host or "").lower().rstrip("/")
    return OPENAI if ("/v1" in h or "openai" in h) else OLLAMA


def base_url(host: str) -> tuple[str, str]:
    """把用户填的地址拆成 (scheme://host:port, path)。

    认这几种写法，都要能用：
      http://localhost:11434        -> (http://localhost:11434, "")
      http://localhost:11434/       -> 同上（去掉末尾斜杠）
      https://www.aigatehub.xyz/v1  -> (https://www.aigatehub.xyz, "/v1")
    """
    from urllib.parse import urlsplit

    raw = (host or "").strip().rstrip("/")
    parts = urlsplit(raw)
    scheme = parts.scheme or "http"
    netloc = parts.netloc or parts.path.split("/")[0]
    path = parts.path.rstrip("/")
    return f"{scheme}://{netloc}", path


def endpoint(host: str, provider: str, kind: str = "chat") -> str:
    """拼出最终请求 URL。kind: chat / models / version / generate。

    OpenAI 兼容后端统一挂在 /v1 下；用户已经带 /v1 就不再重复拼。
    """
    base, path = base_url(host)
    if provider == OLLAMA:
        if kind == "chat":
            return base + path + "/api/chat"
        if kind == "models":
            return base + path + "/api/tags"
        if kind == "version":
            return base + path + "/api/version"
        return base + path + "/api/generate"

    root = base + path if (path.endswith("/v1") or path.endswith("/v1beta")) \
        else base + path + "/v1"
    return root + ("/models" if kind == "models" else "/chat/completions")


def to_openai_payload(model: str, messages: list, tools: list | None = None,
                      temperature: float = 0.7) -> dict:
    """构造 OpenAI 兼容请求体。

    num_ctx / keep_alive / think 一律不下发：中转站和 vLLM 会直接 400。
    """
    from agent.backends.openai_messages import to_openai_messages

    payload: dict = {
        "model": model,
        "messages": to_openai_messages(messages),
        "stream": True,
        "temperature": temperature,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    return payload
