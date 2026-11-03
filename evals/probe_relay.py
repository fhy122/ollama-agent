"""真实中转站验证：列模型 + 流式对话 + 工具调用协议。

用法（Key 从环境变量读，不落盘）：
    AI_GATE_HUB_KEY=... python evals/probe_relay.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.config import AgentConfig
from agent.backends import create_client

HOST = os.environ.get("RELAY_HOST", "https://www.aigatehub.xyz/v1")
MODEL = os.environ.get("RELAY_MODEL", "deepseek-v4.1-flash-1017")

cfg = AgentConfig(
    host=HOST,
    provider="openai",
    api_key=os.environ.get("AI_GATE_HUB_KEY", ""),
    model=MODEL,
)
c = create_client(cfg)
print(f"实现类: {type(c).__name__} | name={c.name}")
print(f"chat_url: {c.chat_url()}")
print(f"key: {'已读取 长度%d' % len(cfg.api_key) if cfg.api_key else '缺失'}")

print("\n--- 列模型 ---")
try:
    models = c.list_models()
    print(f"共 {len(models)} 个")
    print("前 15:", models[:15])
    print("目标模型存在:", MODEL in models)
except Exception as e:
    print(f"[失败] {type(e).__name__}: {e}")
    sys.exit(1)

print("\n--- 流式对话 ---")
chunks = []
try:
    r = c.chat([{"role": "user", "content": "只回复两个字：收到"}],
               tools=None, on_text=chunks.append)
    print(f"content: {r['content']!r}")
    print(f"流式回调收到: {''.join(chunks)!r}")
    print(f"error: {r['error']} | cancelled: {r['cancelled']}")
    print(f"stats: {r['stats']}")
except Exception as e:
    print(f"[失败] {type(e).__name__}: {e}")

print("\n--- 工具调用（tool_calls 分片拼接） ---")
TOOLS = [{
    "type": "function",
    "function": {
        "name": "read_file",
        "description": "读取文件内容",
        "parameters": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "文件路径"}},
            "required": ["path"],
        },
    },
}]
try:
    r = c.chat([{"role": "user", "content": "帮我读一下 D:/tmp/demo.txt，直接调工具，别说话"}],
               tools=TOOLS)
    print(f"tool_calls 数: {len(r['tool_calls'])}")
    for tc in r["tool_calls"]:
        print(f"  name={tc['function']['name']!r} args={tc['function']['arguments']!r}")
    print(f"content: {r['content']!r}")
    print(f"error: {r['error']}")

    # 回填一条工具结果，验证 tool_call_id 配对不会 400
    if r["tool_calls"]:
        hist = [
            {"role": "user", "content": "帮我读一下 D:/tmp/demo.txt"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_x0", "function": {"name": "read_file",
                                               "arguments": {"path": "D:/tmp/demo.txt"}}}]},
            {"role": "tool", "content": "文件内容是 hello", "tool_call_id": "call_x0"},
        ]
        r2 = c.chat(hist, tools=TOOLS)
        print(f"回填后再问一次 → content={r2['content']!r} error={r2['error']}")
except Exception as e:
    print(f"[失败] {type(e).__name__}: {e}")
