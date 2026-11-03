"""首字节（响应头）挂死测试——对应线上「第 5 步干等 30 分钟才报错」。

背景（2026-11-03 线上截图）：
    中转站收了请求但迟迟不回第一个字节，getresponse() 一直挂到
    socket 读超时 cfg.timeout=1800s，用户对着转圈界面等半小时。
    与握手挂死（test_abort_handshake.py）同类，但发生在下一个阶段：
    连接、握手、发请求都完成了，卡在等响应头。

修复：等响应头阶段单独短预算 _TTFT_TIMEOUT（默认 180s，环境变量
OLLAMA_AGENT_TTFT 可调），响应头到达后立刻恢复 cfg.timeout 长超时——
深度思考不发字的块间隔可能很大，读流仍需长值。

锁住的行为：
1. 服务端收请求不吐字 → TTFT 预算内报错，文案明说「第一个字节」；
2. 等响应头期间用户打断 → 快速按 cancelled 收尾；
3. 响应头慢于握手预算但快于 TTFT → 正常返回（两段预算互不干扰）；
4. 响应头之后流中出现长间隔（> TTFT，< cfg.timeout）→ 读流不受
   TTFT 影响（证明恢复长超时生效，这是本修复最关键的回归点）。
"""
from __future__ import annotations

import http.server
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.backends import base as bb  # noqa: E402
from agent.backends.base import ChatClient  # noqa: E402
from agent.config import AgentConfig  # noqa: E402

TEST_TTFT = 1.0          # 测试里把首字节预算压短，免得用例本身跑得慢
SLACK = 8.0              # 调度余量：超过「预算 + 余量」就算没修


class SilentHandler(http.server.BaseHTTPRequestHandler):
    """收到 POST 后装死——模拟「请求收了、响应永远不来」。"""

    def log_message(self, *a):  # 静音
        pass

    def do_POST(self):
        time.sleep(120)      # 挂住响应头；进程退出时 daemon 线程自动终结


class SlowHeadersHandler(http.server.BaseHTTPRequestHandler):
    """响应头故意慢 0.4s（> 握手预算、< TTFT），之后回正常 SSE。"""

    def log_message(self, *a):  # 静音
        pass

    def do_POST(self):
        time.sleep(0.4)
        body = b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n' \
               b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class GapStreamHandler(http.server.BaseHTTPRequestHandler):
    """响应头和首块立即回，但第二块隔 2.5s 才来（> TTFT=1s）。"""

    def log_message(self, *a):  # 静音
        pass

    def do_POST(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def chunk(data: bytes):
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        chunk(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
        time.sleep(2.5)      # 长间隔：若 TTFT 没恢复成读流长超时，这里会断
        chunk(b'data: {"choices":[{"delta":{"content":" there"},"finish_reason":"stop"}]}\n\n')
        chunk(b"data: [DONE]\n\n")
        self.wfile.write(b"0\r\n\r\n")


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


def _client(port: int) -> MinimalClient:
    cfg = AgentConfig(host=f"http://127.0.0.1:{port}", model="m",
                      api_key="", timeout=1800)   # 长超时与线上一致
    return MinimalClient(cfg, f"http://127.0.0.1:{port}/v1/chat/completions")


def test_silent_server_errors_out_fast():
    """收请求不吐字：TTFT 预算内报错，而不是挂满 cfg.timeout（1800s）。"""
    bb._TTFT_TIMEOUT = TEST_TTFT
    srv = _start(SilentHandler)
    try:
        cli = _client(srv.server_address[1])
        t0 = time.time()
        try:
            r = cli.chat([{"role": "user", "content": "hi"}])
        except RuntimeError as e:
            dt = time.time() - t0
            assert dt < TEST_TTFT + SLACK, f"报错耗时 {dt:.1f}s，TTFT 预算没生效"
            assert "第一个字节" in str(e), str(e)
        else:
            raise AssertionError(f"应当报错，却正常返回: {r}")
    finally:
        srv.shutdown()


def test_abort_breaks_ttft_wait():
    """等响应头期间点停止：快速按 cancelled 收尾，锁才能还回去。"""
    bb._TTFT_TIMEOUT = TEST_TTFT
    srv = _start(SilentHandler)
    try:
        cli = _client(srv.server_address[1])
        cancel = threading.Event()
        out: dict = {}

        def turn():
            t0 = time.time()
            try:
                out["r"] = cli.chat([{"role": "user", "content": "hi"}], cancel=cancel)
            except Exception as e:
                out["r"] = f"raised: {type(e).__name__}: {e}"
            out["dt"] = time.time() - t0

        th = threading.Thread(target=turn, daemon=True)
        th.start()
        time.sleep(0.5)             # 先让它卡进等响应头
        cli.abort()                 # 与 request_stop 同款：_abort_requested + shutdown
        cancel.set()
        th.join(timeout=TEST_TTFT + SLACK)

        assert not th.is_alive(), f"打断后 {TEST_TTFT + SLACK:.0f}s 线程仍卡在等响应头"
        r = out["r"]
        assert isinstance(r, dict) and r.get("cancelled"), f"应按打断收尾，实际: {r}"
    finally:
        srv.shutdown()


def test_slow_headers_still_ok():
    """响应头慢但没超 TTFT：正常返回（响应头预算和握手预算互不干扰）。"""
    bb._TTFT_TIMEOUT = TEST_TTFT
    srv = _start(SlowHeadersHandler)
    try:
        cli = _client(srv.server_address[1])
        r = cli.chat([{"role": "user", "content": "hi"}])
        assert r.get("content") == "hi", r
    finally:
        srv.shutdown()


def test_stream_gap_unaffected_by_ttft():
    """响应头之后的长块间隔（> TTFT）不断流：读流恢复长超时生效。"""
    bb._TTFT_TIMEOUT = TEST_TTFT
    srv = _start(GapStreamHandler)
    try:
        cli = _client(srv.server_address[1])
        r = cli.chat([{"role": "user", "content": "hi"}])
        assert r.get("content") == "hi there", r
    finally:
        srv.shutdown()


if __name__ == "__main__":
    test_silent_server_errors_out_fast()
    print("PASS: 收请求不吐字 → TTFT 预算内报错")
    test_abort_breaks_ttft_wait()
    print("PASS: 等响应头期间打断 → 快速 cancelled 收尾")
    test_slow_headers_still_ok()
    print("PASS: 响应头慢于握手预算但快于 TTFT → 正常返回")
    test_stream_gap_unaffected_by_ttft()
    print("PASS: 响应头后长块间隔 → 读流不受 TTFT 影响")
