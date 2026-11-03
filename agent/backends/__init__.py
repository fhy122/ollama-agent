"""聊天后端抽象。

同一套接口（chat / list_models / unload / switch_model / warmup / abort），
两种协议各自实现，调用方不需要知道背后是 Ollama 还是 OpenAI 兼容中转站。

    agent/backends/base.py             ChatClient 抽象基类 + StreamParser 基类
    agent/backends/ollama.py           /api/chat
    agent/backends/openai_compat.py    /v1/chat/completions

为什么不用一个类加 if 分支：这套接口有 7 个方法在两种协议下行为完全不同
（地址、鉴权、请求体、流格式、模型列表、卸载语义、错误文案）。塞进一个类
就是每个方法开头一个 if，改一处要通读全局；而且名字只能叫 OllamaClient，
实际却在跑 OpenAI 协议——名不副实比代码丑更麻烦。
"""
from agent.backends.base import ChatClient, StreamParser
from agent.backends.ollama import OllamaClient, OllamaStreamParser
from agent.backends.openai_compat import OpenAICompatClient, OpenAIStreamParser

__all__ = [
    "ChatClient",
    "StreamParser",
    "OllamaClient",
    "OllamaStreamParser",
    "OpenAICompatClient",
    "OpenAIStreamParser",
    "create_client",
    "BACKENDS",
]


# 协议名 → 实现类。新增后端只在这里登记一行。
BACKENDS: dict[str, type[ChatClient]] = {
    "ollama": OllamaClient,
    "openai": OpenAICompatClient,
}


def create_client(cfg) -> ChatClient:
    """按配置造出对应的后端实例。

    cfg.provider 为 auto 时按 host 判断（带 /v1 → OpenAI 兼容）。
    未知值退回 Ollama，免得配置写错就整个起不来。
    """
    from agent.providers import OLLAMA, detect_provider

    key = getattr(cfg, "provider", "auto")
    if key not in BACKENDS:
        key = detect_provider(getattr(cfg, "host", "") or "")
    if key not in BACKENDS:
        key = OLLAMA
    return BACKENDS[key](cfg)
