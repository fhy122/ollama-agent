"""CLI 打断自测（离线，不依赖 Ollama）。

验证：Ctrl+C 落在流式生成或工具执行中时，只打断本轮 —— 停生成、
历史保持合法、回到输入提示，而不是整个进程退出。
"""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.client import OllamaClient  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.loop import pad_unanswered_tool_calls, run_turn  # noqa: E402
from agent.registry import ToolContext  # noqa: E402
from webui.stream import StreamEmitter  # noqa: E402


class _FakeResp:
    def __init__(self, lines):
        self.lines = lines
        self.closed = False

    def __iter__(self):
        return iter(self.lines)

    def close(self):
        self.closed = True


def _make_client(resp):
    """绕过 __init__ 造一个走假响应的客户端。"""
    cli = OllamaClient.__new__(OllamaClient)
    cli.cfg = AgentConfig(use_color=False, warm=False)
    cli.host = "http://127.0.0.1:0"
    cli.model = "m"
    cli.think = False
    cli.num_ctx = None
    cli.temperature = 0.7
    cli._current_conn = None
    cli._current_resp = None
    cli._abort_requested = False
    cli._post_stream = lambda messages, tools: resp
    return cli


def main() -> None:
    # ---- 1. client.chat：流读取中 Ctrl+C → cancelled（不向上抛）----
    fake = _FakeResp([
        json.dumps({"message": {"content": "前半句"}}).encode(),
        json.dumps({"message": {"content": "后半句"}, "done": True}).encode(),
    ])
    cli = _make_client(fake)
    seen = {"n": 0}

    def on_text(t):
        seen["n"] += 1
        if seen["n"] == 1:
            raise KeyboardInterrupt   # 模拟用户此刻按了 Ctrl+C

    r = cli.chat([{"role": "user", "content": "x"}], on_text=on_text)
    assert r["cancelled"] is True, r
    assert fake.closed is True, "打断后应关闭响应连接（让 Ollama 停止生成）"
    assert r["content"] == "前半句", r
    print("PASS: client.chat 流中 Ctrl+C → cancelled + 关连接 + 保留已生成内容")

    # ---- 2. pad_unanswered_tool_calls：补齐缺响应的 tool_calls ----
    h = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "read_file", "arguments": {}}},
            {"function": {"name": "write_file", "arguments": {}}},
        ]},
        {"role": "tool", "content": "ok", "tool_name": "read_file"},
    ]
    assert pad_unanswered_tool_calls(h) == 1, h
    assert h[-1]["role"] == "tool" and h[-1]["tool_name"] == "write_file", h
    assert "未执行" in h[-1]["content"]
    assert pad_unanswered_tool_calls(h) == 0, "重复调用不应重复补"
    print("PASS: pad_unanswered_tool_calls（按已执行数补齐 + 幂等）")

    # ---- 3. run_turn：工具执行中 Ctrl+C → 不抛出、补占位、给 notice ----
    import agent.loop as loop_mod

    class FakeToolClient:
        def chat(self, messages, tools=None, on_text=None, on_think=None, cancel=None,
                 on_wait=None, think_hidden=True):
            return {"thinking": "", "content": "我要写文件",
                    "tool_calls": [{"function": {"name": "write_file",
                                                 "arguments": {"path": "a.txt"}}}],
                    "stats": {}, "cancelled": False}

    def _boom(name, args, ctx):
        raise KeyboardInterrupt   # 工具执行到一半按了 Ctrl+C

    orig = loop_mod.tools.dispatch
    loop_mod.tools.dispatch = _boom
    try:
        cfg = AgentConfig(use_color=False, warm=False, no_tools=True)
        ctx = ToolContext(confirm=False, use_color=False, ask=None)
        hist = [{"role": "system", "content": "s"}]
        buf = io.BytesIO()
        out = run_turn(FakeToolClient(), cfg, ctx, hist, "写个文件",
                       emitter=StreamEmitter(buf))
        events = [json.loads(x) for x in buf.getvalue().decode("utf-8").strip().split("\n")
                  if x]
        assert any(e["type"] == "notice" and "已打断" in e["text"] for e in events), events
        assert hist[-1]["role"] == "tool" and "未执行" in hist[-1]["content"], hist
        assert out == "我要写文件", out
    finally:
        loop_mod.tools.dispatch = orig
    print("PASS: run_turn 工具执行中 Ctrl+C → 不抛出 + 补占位 + notice")

    print("PASS: CLI 打断全过")


if __name__ == "__main__":
    main()
