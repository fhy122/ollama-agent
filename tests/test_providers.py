"""后端协议适配自测（离线，不连任何真实服务）。

覆盖两件事，都是接中转站时踩过的坑：
1. 内部历史 → OpenAI 请求体：tool_call_id 必须**沿用**历史里已有的值。
   曾经两处各生成一套（assistant 是 call_2_0、tool 是 call_1_0），
   OpenAI 兼容后端直接 400。
2. SSE 流解析：tool_calls 的 arguments 跨 chunk 增量拼接后要还原成 dict；
   只给 finish_reason 不给 [DONE] 的中转站不能被误判成断流。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.backends import (  # noqa: E402
    OllamaClient, OllamaStreamParser, OpenAICompatClient, OpenAIStreamParser,
    create_client,
)
from agent.backends.openai_messages import to_openai_messages  # noqa: E402
from agent.providers import (  # noqa: E402
    OLLAMA, OPENAI, detect_provider, endpoint, to_openai_payload,
)


def test_detect_provider():
    assert detect_provider("https://www.aigatehub.xyz/v1") == OPENAI
    assert detect_provider("https://api.openai.com/v1/") == OPENAI
    assert detect_provider("http://localhost:11434") == OLLAMA
    assert detect_provider("http://192.168.1.9:11434") == OLLAMA


def test_endpoint_join():
    # 带不带 /v1、带不带末尾斜杠，都不能拼出 // 或重复 /v1
    assert endpoint("https://x.com/v1", OPENAI, "chat") == \
        "https://x.com/v1/chat/completions"
    assert endpoint("https://x.com/v1/", OPENAI, "chat") == \
        "https://x.com/v1/chat/completions"
    assert endpoint("https://x.com", OPENAI, "chat") == \
        "https://x.com/v1/chat/completions"
    assert endpoint("http://localhost:11434", OLLAMA, "chat") == \
        "http://localhost:11434/api/chat"
    assert endpoint("http://localhost:11434/", OLLAMA, "models") == \
        "http://localhost:11434/api/tags"


def test_tool_call_id_kept_in_sync():
    """assistant 声明的 id 必须和随后 tool 消息的 id 一致。"""
    hist = [
        {"role": "user", "content": "读文件"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call_1_0",
             "function": {"name": "read_file", "arguments": {"path": "a.py"}}},
            {"id": "call_1_1",
             "function": {"name": "list_dir", "arguments": {"path": "."}}},
        ]},
        {"role": "tool", "content": "print(1)", "tool_call_id": "call_1_0"},
        {"role": "tool", "content": "a.py", "tool_call_id": "call_1_1"},
    ]
    msgs = to_openai_messages(hist)
    declared = [t["id"] for m in msgs if m.get("tool_calls")
                for t in m["tool_calls"]]
    answered = [m["tool_call_id"] for m in msgs if m["role"] == "tool"]
    assert declared == answered == ["call_1_0", "call_1_1"]


def test_tool_call_id_generated_when_missing():
    """历史里没带 id（旧会话存档）时要补上，不能让配对各写各的。"""
    hist = [
        {"role": "user", "content": "x"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {"path": "a.py"}}}]},
        {"role": "tool", "content": "ok", "tool_call_id": "call_9_0"},
    ]
    msgs = to_openai_messages(hist)
    declared = [t["id"] for m in msgs if m.get("tool_calls") for t in m["tool_calls"]]
    answered = [m["tool_call_id"] for m in msgs if m["role"] == "tool"]
    assert declared == answered


def test_no_ollama_only_fields():
    """num_ctx / keep_alive / think 不能漏进 OpenAI 请求体（中转站会 400）。"""
    p = to_openai_payload("m", [{"role": "user", "content": "hi"}], tools=None)
    for k in ("keep_alive", "options", "think", "num_ctx"):
        assert k not in p


def test_tools_passed_through():
    tools = [{"type": "function", "function": {"name": "x", "parameters": {}}}]
    p = to_openai_payload("m", [{"role": "user", "content": "hi"}], tools=tools)
    assert p["tools"] == tools
    assert p["tool_choice"] == "auto"


def test_sse_content_and_reasoning():
    p = OpenAIStreamParser()
    evs = []
    for line in [
        'data: {"choices":[{"delta":{"reasoning_content":"想"}}]}',
        'data: {"choices":[{"delta":{"content":"你好"}}]}',
    ]:
        evs += p.feed(line.encode())
    assert {"type": "think", "text": "想"} in evs
    assert {"type": "text", "text": "你好"} in evs


def test_sse_tool_args_split_across_chunks():
    """arguments 分片到达，拼完才能 json 解析。"""
    p = OpenAIStreamParser()
    evs = []
    for line in [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"name":"read_file","arguments":"{\\"path\\":"}}]}}]}',
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"\\"a.py\\"}"}}]}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
    ]:
        evs += p.feed(line.encode())
    tools = [e for e in evs if e["type"] == "tool"]
    assert tools == [{"type": "tool", "index": 0,
                      "name": "read_file", "args": {"path": "a.py"}}]
    assert p.finished is True


def test_sse_done_marker_marks_finished():
    p = OpenAIStreamParser()
    p.feed(b'data: {"choices":[{"delta":{"content":"x"}}]}')
    assert p.finished is False
    p.feed(b"data: [DONE]")
    assert p.finished is True


def test_ollama_stream_still_works():
    """改协议层不能把 Ollama 原生分支弄坏。"""
    p = OllamaStreamParser()
    evs = p.feed(b'{"message":{"content":"hi"},"done":false}')
    assert evs == [{"type": "text", "text": "hi"}]
    evs = p.feed(b'{"message":{"content":""},"done":true,"eval_count":5}')
    assert evs[-1]["type"] == "done"
    assert evs[-1]["stats"]["eval_count"] == 5


def test_provider_switch_via_session():
    """设置面板切后端：协议/地址/Key 一起生效，且缺 Key 时列模型不炸。"""
    from agent.config import AgentConfig
    from webui.session import AgentSession

    s = AgentSession(AgentConfig(api_key="sk-old"))
    out = s.update_config({"provider": "openai",
                           "host": "https://relay.example/v1",
                           "api_key": "sk-new"})
    assert out["provider"] == "openai"
    assert out["host"] == "https://relay.example/v1"
    assert out["has_key"] is True
    # 关键：切换协议后拿到的是 OpenAI 实现类，不是给 Ollama 换个 host
    assert isinstance(s.client, OpenAICompatClient)
    assert s.client.chat_url() == "https://relay.example/v1/chat/completions"
    assert s.client.headers()["Authorization"] == "Bearer sk-new"

    # 留空表示「不修改」，不能把已存的 Key 抹掉
    s.update_config({"host": "https://relay.example/v1", "api_key": ""})
    assert s.cfg.api_key == "sk-new"

    # 显式清空
    s.update_config({"api_key": "__CLEAR__"})
    assert s.cfg.api_key == ""


def test_switch_to_builtin_syncs_active(tmp_path):
    """点内置模型走的是 /api/config（内置条目不是 profile），换协议那一刻
    user_cfg["active"] 必须同步成内置 —— 不同步的实测后果：设置面板里
    内置区和自定义区同时亮「使用中」，没人分得清哪个真在生效；
    且 _sync_user_cfg 会把本地配置回写进还挂着的 openai profile，把它污染。
    """
    from agent.config import AgentConfig
    from agent.userconfig import BUILTIN_ID, ConfigStore
    from webui.session import AgentSession

    cs = ConfigStore(tmp_path / "config.json")
    cs.save({"active": "p-1", "profiles": [
        {"id": "p-1", "name": "中转站", "provider": "openai",
         "host": "https://relay.example/v1", "api_key": "sk-keep",
         "model": "deepseek-x"}]})
    s = AgentSession(AgentConfig(api_key=""), config_store=cs)
    assert s.user_cfg["active"] == "p-1"
    assert s.cfg.model == "deepseek-x"      # _restore_profile 灌回了中转站

    # 用户点内置模型 qwen3.5:9b（前端 activateOllama 发的就是这个补丁）
    s.update_config({"provider": "ollama", "host": "http://127.0.0.1:11434",
                     "model": "qwen3.5:9b"})
    assert s.user_cfg["active"] == BUILTIN_ID, s.user_cfg["active"]

    # 配置文件里的 profile 不能被本地配置污染
    disk = cs.load()
    p = [x for x in disk["profiles"] if x["id"] == "p-1"][0]
    assert p["provider"] == "openai", p
    assert p["host"] == "https://relay.example/v1", p
    assert p["api_key"] == "sk-keep", p
    assert p["model"] == "deepseek-x", p
