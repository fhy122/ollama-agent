"""读流空闲预算测试（_IDLE_TIMEOUT）。

场景（2026-11-03 线上实证，py-spy 抓栈定位）：中转站把上游挂起后，
流上再无任何字节，线程阻塞在 readline，界面「正在生成」字符数停着
不动——而旧实现这里用 cfg.timeout（1800s），要干等半小时才报错。

锁住的行为：
1. 流中途静默超过空闲预算 → 快速报 stream_error（TimeoutError），
   且该错误可被 run_turn 的自动重试分类为可重试；
2. 块间隔小于空闲预算 → 不断流（GapStreamHandler 2.5s 间隔的既有
   用例在 test_ttft_timeout.py，这里用远小于预算的间隔再验一遍）。
"""
from __future__ import annotations

import http.server
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.backends import base as bb  # noqa: E402
from agent.backends.base import ChatClient  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.retry import classify_error  # noqa: E402

IDLE = 0.6      # 测试里把空闲预算压短
SLACK = 6.0     # 调度余量


@pytest.fixture(autouse=True)
def _short_idle(monkeypatch):
    """压短空闲预算并在用例结束后自动恢复原值（模块变量不能跨用例泄漏）。"""
    monkeypatch.setattr(bb, "_IDLE_TIMEOUT", IDLE)


class StallAfterFirstChunkHandler(http.server.BaseHTTPRequestHandler):
    """先正常推一块，然后装死不关连接——模拟对端半死。"""

    log_message = lambda self, *a, **k: None

    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(data: bytes):
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        chunk(b'data: {"choices":[{"delta":{"content":"part1"}}]}\n\n')
        time.sleep(120)   # 挂住不吐字不关流；daemon 线程随进程退出终结


class MinimalClient(ChatClient):
    name = "test"

    def __init__(self, cfg, url):
        super().__init__(cfg)
        self._url = url

    def chat_url(self): return self._url
    def models_url(self): return self._url
    def build_payload(self, messages, tools=None):
        return {"model": self.model, "messages": messages}
    def parse_list_models(self, data): return []
    def new_parser(self):
        from agent.backends.openai_compat import OpenAIStreamParser
        return OpenAIStreamParser()


def _start(handler) -> http.server.ThreadingHTTPServer:
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_stall_after_first_chunk_errors_within_idle_budget():
    """首块之后静默：空闲预算内报 stream_error，而不是挂满 cfg.timeout。"""
    srv = _start(StallAfterFirstChunkHandler)
    try:
        cfg = AgentConfig(host=f"http://127.0.0.1:{srv.server_address[1]}",
                          model="m", api_key="", timeout=1800)
        cli = MinimalClient(cfg, f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions")
        t0 = time.time()
        r = cli.chat([{"role": "user", "content": "hi"}])
        dt = time.time() - t0
        assert dt < IDLE + SLACK, f"报错耗时 {dt:.1f}s，空闲预算没生效"
        assert r["error"], f"应有 stream_error：{r}"
        assert "TimeoutError" in r["error"], r["error"]
        # 半截内容保住了，且错误可被自动重试接力
        assert r["content"] == "part1"
        assert classify_error(r["error"]) == "timeout"
    finally:
        srv.shutdown()


def _gap_handler_cls(gap: float) -> type:
    """块间隔可调的正常 SSE 流（工厂注入间隔，避免动 handler 签名）。"""

    class GapHandler(http.server.BaseHTTPRequestHandler):
        log_message = lambda self, *a, **k: None

        def do_POST(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def chunk(data: bytes):
                self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
                self.wfile.flush()

            chunk(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
            time.sleep(gap)
            chunk(b'data: {"choices":[{"delta":{"content":" there"},"finish_reason":"stop"}]}\n\n')
            chunk(b"data: [DONE]\n\n")
            self.wfile.write(b"0\r\n\r\n")

    return GapHandler


def test_normal_stream_unaffected():
    """间隔远小于空闲预算的正常流不受影响。"""
    srv = _start(_gap_handler_cls(0.1))
    try:
        cfg = AgentConfig(host=f"http://127.0.0.1:{srv.server_address[1]}",
                          model="m", api_key="", timeout=1800)
        cli = MinimalClient(cfg, f"http://127.0.0.1:{srv.server_address[1]}/v1/chat/completions")
        r = cli.chat([{"role": "user", "content": "hi"}])
        assert r["error"] is None, r["error"]
        assert r["content"] == "hi there", r
    finally:
        srv.shutdown()

