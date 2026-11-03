"""握手挂死打断测试（对应线上「已打断却不能新建对话」的卡死）。

背景（2026-11-03 线上实测，py-spy 抓栈定位）：
    中转站 TCP 可达但对 TLS 握手不响应时，回合线程挂在
    SSLContext.wrap_socket 的 do_handshake 里，一直挂满 socket 超时
    （cfg.timeout=1800s）。期间 AgentSession.lock 被攥着，新建/切换
    对话全被 409 挡回；request_stop 的 sock.shutdown() 在 Windows 上
    叫不醒阻塞中的握手（本文件的 HangServer 即最小复现）。

修复：连接类给握手阶段单独的短预算（_HANDSHAKE_TIMEOUT）。
锁住的行为：
1. 握手挂死 + 用户打断 → 回合在握手预算内结束（cancelled），不再挂 30 分钟；
2. 握手挂死、没打断 → 握手预算内报「连不上」，而不是挂满长超时；
3. 明文 HTTP 正常请求不受影响（happy path 回归）。
"""
from __future__ import annotations

import http.server
import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.backends import base as bb  # noqa: E402
from agent.backends.base import ChatClient  # noqa: E402
from agent.config import AgentConfig  # noqa: E402

TEST_HANDSHAKE = 2.0     # 测试里把握手预算压短，免得用例本身跑得慢
SLACK = 8.0              # 调度余量：超过「预算 + 余量」就算没修


class HangServer(threading.Thread):
    """accept 之后一个字节都不发——模拟「TCP 通、TLS 握手永远不来」。"""

    def __init__(self):
        super().__init__(daemon=True)
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        self.srv.listen(1)

    def run(self):
        try:
            conn, _ = self.srv.accept()
            time.sleep(120)      # 挂住握手；进程退出时 daemon 线程自动终结
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass


class EchoHandler(http.server.BaseHTTPRequestHandler):
    """明文 HTTP 的正常 POST /v1/chat/completions，回一小段 SSE。"""

    def log_message(self, *a):  # 静音
        pass

    def do_POST(self):
        body = b'data: {"choices":[{"delta":{"content":"hi"},"finish_reason":"stop"}]}\n\n' \
               b"data: [DONE]\n\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


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


def _client(host: str, port: int, scheme: str = "http") -> MinimalClient:
    cfg = AgentConfig(host=f"{scheme}://127.0.0.1:{port}", model="m",
                      api_key="", timeout=1800)   # 长超时与线上一致
    return MinimalClient(cfg, f"{scheme}://127.0.0.1:{port}/v1/chat/completions")


def test_abort_breaks_hung_handshake():
    """握手挂死时点停止：回合必须在握手预算内结束，锁才能还回去。"""
    bb._HANDSHAKE_TIMEOUT = TEST_HANDSHAKE
    srv = HangServer(); srv.start()
    cli = _client("127.0.0.1", srv.port, "https")
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
    time.sleep(0.5)                 # 先让它卡进握手
    cli.abort()                     # 与 request_stop 同款：_abort_requested + shutdown
    cancel.set()
    th.join(timeout=TEST_HANDSHAKE + SLACK)

    assert not th.is_alive(), f"打断后 {TEST_HANDSHAKE + SLACK:.0f}s 线程仍卡在握手"
    assert out["dt"] < TEST_HANDSHAKE + SLACK, f"退出耗时 {out['dt']:.1f}s 过长"
    r = out["r"]
    assert isinstance(r, dict) and r.get("cancelled"), f"应按打断收尾，实际: {r}"


def test_hung_handshake_errors_out_fast():
    """没打断时握手挂死：预算内报「连不上」，而不是挂满 cfg.timeout。"""
    bb._HANDSHAKE_TIMEOUT = TEST_HANDSHAKE
    srv = HangServer(); srv.start()
    cli = _client("127.0.0.1", srv.port, "https")
    t0 = time.time()
    try:
        r = cli.chat([{"role": "user", "content": "hi"}])
    except RuntimeError as e:
        dt = time.time() - t0
        assert dt < TEST_HANDSHAKE + SLACK, f"报错耗时 {dt:.1f}s，握手预算没生效"
        assert "连不上" in str(e) or "握手" in str(e) or "test" in str(e), str(e)
    else:
        raise AssertionError(f"应当报错，却正常返回: {r}")


def test_plain_http_still_works():
    """明文 HTTP 正常请求不受影响（连接子类只动握手超时这一个旋钮）。"""
    bb._HANDSHAKE_TIMEOUT = TEST_HANDSHAKE
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), EchoHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        cli = _client("127.0.0.1", srv.server_address[1], "http")
        r = cli.chat([{"role": "user", "content": "hi"}])
        assert r.get("content") == "hi", r
        assert not r.get("error"), r
    finally:
        srv.shutdown()


if __name__ == "__main__":
    test_abort_breaks_hung_handshake()
    print("PASS: 打断能终结挂死的握手回合")
    test_hung_handshake_errors_out_fast()
    print("PASS: 握手挂死在预算内报错")
    test_plain_http_still_works()
    print("PASS: 明文 HTTP 正常路径不受影响")
