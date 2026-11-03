"""聊天客户端入口（门面）。

实现已按协议拆到 agent/backends/：
    agent/backends/base.py            ChatClient 抽象基类 + StreamParser 基类
    agent/backends/ollama.py          OllamaClient   → /api/chat
    agent/backends/openai_compat.py   OpenAICompatClient → /v1/chat/completions

原来这里是一个 OllamaClient 装两种协议（每个方法开头一个 if provider == ...），
名字和实际行为对不上。现在：
  - 生产代码用 create_client(cfg)，拿到的是具体实现，按 ChatClient 接口调用；
  - OllamaClient 这个名字只指向 Ollama 那一个实现，不再兼职 OpenAI。
"""
from __future__ import annotations

from agent.backends import (
    BACKENDS,
    ChatClient,
    OllamaClient,
    OllamaStreamParser,
    OpenAICompatClient,
    OpenAIStreamParser,
    create_client,
)

__all__ = [
    "BACKENDS",
    "ChatClient",
    "OllamaClient",
    "OllamaStreamParser",
    "OpenAICompatClient",
    "OpenAIStreamParser",
    "create_client",
]
