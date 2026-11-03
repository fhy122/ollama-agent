"""后端抽象结构自测（离线）。

锁住重构后的设计约束，防止又退回「一个类 + if provider」：
1. 两种协议是两个独立的类，都实现 ChatClient 接口；
2. 工厂按配置返回正确实现类；
3. 同一接口方法在两种协议下行为不同（这正是需要多态的地方）；
4. OllamaClient 只负责 Ollama，不再兼职 OpenAI。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.backends import (  # noqa: E402
    BACKENDS, ChatClient, OllamaClient, OpenAICompatClient, create_client,
)
from agent.config import AgentConfig  # noqa: E402

TOOLS = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
HIST = [{"role": "user", "content": "hi"}]


def _cfg(**kw) -> AgentConfig:
    base = dict(host="http://localhost:11434", model="m", api_key="")
    base.update(kw)
    return AgentConfig(**base)


def test_two_independent_classes():
    """两个协议必须是两个类，且都是 ChatClient 的子类。"""
    assert OllamaClient is not OpenAICompatClient
    assert issubclass(OllamaClient, ChatClient)
    assert issubclass(OpenAICompatClient, ChatClient)
    assert set(BACKENDS) == {"ollama", "openai"}


def test_factory_picks_by_provider():
    assert type(create_client(_cfg(provider="ollama"))).__name__ == "OllamaClient"
    assert type(create_client(_cfg(provider="openai"))).__name__ == "OpenAICompatClient"


def test_factory_auto_detects_from_host():
    c = create_client(_cfg(provider="auto", host="https://relay.example/v1"))
    assert isinstance(c, OpenAICompatClient)
    c = create_client(_cfg(provider="auto", host="http://localhost:11434"))
    assert isinstance(c, OllamaClient)


def test_same_interface_different_behaviour():
    """同一个方法名，两种协议给出不同结果——这就是要靠多态的地方。"""
    oll = create_client(_cfg(provider="ollama", host="http://localhost:11434",
                             model="qwen3.5:9b"))
    opn = create_client(_cfg(provider="openai", host="https://relay.example/v1",
                             model="deepseek-v4.1-flash", api_key="sk-x"))

    # 端点
    assert oll.chat_url().endswith("/api/chat")
    assert opn.chat_url().endswith("/v1/chat/completions")

    # 鉴权
    assert "Authorization" not in oll.headers()
    assert opn.headers()["Authorization"] == "Bearer sk-x"

    # 请求体：Ollama 专有字段只出现在 Ollama 侧
    op = oll.build_payload(HIST, TOOLS)
    pp = opn.build_payload(HIST, TOOLS)
    assert "options" in op and "keep_alive" in op
    assert "options" not in pp and "keep_alive" not in pp
    assert "think" not in pp
    assert pp["tools"] == TOOLS and pp["tool_choice"] == "auto"

    # 模型列表解析格式不同
    assert oll.parse_list_models({"models": [{"name": "a:1b"}]}) == ["a:1b"]
    assert opn.parse_list_models({"data": [{"id": "gpt-4o"}]}) == ["gpt-4o"]

    # 卸载语义：只有 Ollama 有
    assert opn.unload("anything") is False


def test_ollama_client_does_not_speak_openai():
    """老名字不能再兼职 OpenAI：给它 OpenAI 的地址，它仍然按 Ollama 拼路径。"""
    c = OllamaClient(_cfg(host="https://relay.example/v1"))
    assert c.chat_url() == "https://relay.example/v1/api/chat"


def test_api_key_only_affects_openai():
    oll = OllamaClient(_cfg(api_key="sk-secret"))
    assert "Authorization" not in oll.headers()
