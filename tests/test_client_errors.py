"""客户端错误处理自测（离线，不连 Ollama）。

验证四件事：
1. Ollama 返回 400（典型：把 xlsx 当图片附上）时自动剥离图片重试一次，
   错误信息压成人话，且不改动对话历史；
2. abort() 能打断「等响应头/prefill 卡住」的请求——这是「停止按钮失灵」的根因。

注意 _post_chat 用的是 http.client 而不是 urllib.request.urlopen：
urlopen 在等响应头阶段不向调用方暴露任何句柄，没法中断。
所以这里的替身打在 http.client.HTTPConnection 上。
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent.backends.base as B  # noqa: E402
from agent.backends import OllamaClient  # noqa: E402
from agent.config import AgentConfig  # noqa: E402

NESTED_400 = ('{"error":"{\\"error\\":{\\"code\\":400,'
              '\\"message\\":\\"Failed to load image or audio file\\",'
              '\\"type\\":\\"invalid_request_error\\"}}"}')

REAL_CONN = B.http.client.HTTPConnection


class _FakeResp:
    """假响应：400 用 read() 取错误体，200 用迭代取流式行。"""

    def __init__(self, status: int = 200, body: bytes = b"", lines=None):
        self.status = status
        self._body = body
        self._lines = lines or []
        self.closed = False

    def read(self) -> bytes:
        return self._body

    def __iter__(self):
        return iter(self._lines)

    def close(self) -> None:
        self.closed = True


class _ScriptedConn:
    """替身 http.client.HTTPConnection：按脚本吐响应，并记录发出去的请求体。"""

    def __init__(self, resp: _FakeResp):
        self.sock = None            # 普通场景不需要 shutdown
        self._resp = resp
        self.closed = False
        self.bodies: list[dict] = []

    def request(self, method, url, body=None, headers=None):
        if body:
            self.bodies.append(json.loads(body.decode("utf-8")))

    def getresponse(self) -> _FakeResp:
        return self._resp

    def close(self) -> None:
        self.closed = True


def _client() -> OllamaClient:
    cli = OllamaClient.__new__(OllamaClient)
    cli.cfg = AgentConfig(use_color=False, warm=False)
    cli.host = "http://t"
    cli.model = "m"
    cli.think = False
    cli.num_ctx = None
    cli.temperature = 0.7
    cli._current_conn = None
    cli._current_resp = None
    cli._abort_requested = False
    cli._no_think = False
    return cli
    cli._current_resp = None
    cli._abort_requested = False
    return cli


def test_error_text() -> None:
    assert B.error_text(NESTED_400) == "Failed to load image or audio file"
    assert B.error_text('{"error":"直白消息"}') == "直白消息"
    assert B.error_text('{"error":{"message":"dict 消息"}}') == "dict 消息"
    assert B.error_text("完全不是 JSON") == "完全不是 JSON"
    print("PASS: _error_text（嵌套 JSON / 字符串 / dict / 非 JSON）")


def test_strip_images() -> None:
    hist = [{"role": "user", "content": "看图", "images": ["AAA"]},
            {"role": "assistant", "content": "好的"}]
    payload = {"model": "m", "messages": hist, "stream": True}
    stripped = B.strip_images(payload["messages"])
    assert "images" not in stripped[0], stripped
    assert "图片已剥离" in stripped[0]["content"]
    assert hist[0]["images"] == ["AAA"], "原历史不能被改动"
    assert B.has_images(payload["messages"]) is True
    assert B.has_images([{"content": "无图"}]) is False
    print("PASS: _strip_images（副本剥离、原历史不动）")


def test_image_400_retry() -> None:
    attempt = {"n": 0}
    made: list[_ScriptedConn] = []

    def make_conn(*args, **kwargs):
        attempt["n"] += 1
        if attempt["n"] == 1:
            conn = _ScriptedConn(_FakeResp(400, NESTED_400.encode()))
        else:
            conn = _ScriptedConn(_FakeResp(200, lines=[
                json.dumps({"message": {"content": "好了"},
                            "done": True, "eval_count": 1}).encode()]))
        made.append(conn)
        return conn

    B.http.client.HTTPConnection = make_conn
    try:
        hist = [{"role": "user", "content": "看图", "images": ["BAD"]}]
        r = _client().chat(hist)
        assert r["content"] == "好了", r
        assert len(made) == 2, f"应重试一次，实际建连 {len(made)} 次"
        assert made[0].bodies[0]["messages"][0]["images"] == ["BAD"], "首次请求应带原图"
        assert "images" not in made[1].bodies[0]["messages"][0], "重试请求应已剥离图片"
        assert "图片已剥离" in made[1].bodies[0]["messages"][0]["content"]
        assert hist[0]["images"] == ["BAD"], "历史里的图片应保留（未污染）"
    finally:
        B.http.client.HTTPConnection = REAL_CONN
    print("PASS: 400 图片错误 → 剥离图片重试一次（历史保留原图）")


def test_other_400_no_retry() -> None:
    made: list[_ScriptedConn] = []

    def make_conn(*args, **kwargs):
        conn = _ScriptedConn(
            _FakeResp(400, '{"error":"model not found"}'.encode()))
        made.append(conn)
        return conn

    B.http.client.HTTPConnection = make_conn
    try:
        try:
            _client().chat([{"role": "user", "content": "x"}])
            raise AssertionError("应当抛 RuntimeError")
        except RuntimeError as e:
            assert "model not found" in str(e) and "400" in str(e), str(e)
        assert len(made) == 1, "非图片类 400 不应重试"
    finally:
        B.http.client.HTTPConnection = REAL_CONN
    print("PASS: 非图片 400 → 可读 RuntimeError（不盲目重试）")


class _StuckConn:
    """替身连接：模拟长上下文 prefill——getresponse() 迟迟不返回。

    这是真实故障现场：Ollama 在 prefill 期间不发响应头，调用方卡在
    getresponse() 上，此时 _current_resp 还是 None，只有 conn.sock 可下手。
    """

    def __init__(self):
        self.sock = self          # abort() 会对 conn.sock 调 shutdown
        self.shutdown_called = False
        self.closed = False
        self.release = threading.Event()

    def request(self, method, url, body=None, headers=None):
        pass

    def getresponse(self):
        self.release.wait(timeout=20)
        raise ConnectionResetError("socket shutdown by abort()")

    def shutdown(self, how):
        self.shutdown_called = True
        self.release.set()

    def close(self):
        self.closed = True
        self.release.set()


def test_abort_unblocks_stuck_request() -> None:
    stuck = _StuckConn()

    def make_conn(*args, **kwargs):
        return stuck

    B.http.client.HTTPConnection = make_conn
    try:
        cli = _client()
        out: dict = {}
        finished = threading.Event()

        def run():
            out["r"] = cli.chat([{"role": "user", "content": "x"}])
            finished.set()

        threading.Thread(target=run, daemon=True).start()

        # 先确认它确实卡在等响应头上（此时 abort 才有意义）
        assert not finished.wait(0.3), "不该这么快结束——此刻应阻塞在 getresponse()"
        assert cli._current_resp is None, "等响应头阶段还没有 resp，只能靠 conn.sock"

        t0 = time.time()
        cli.abort()                    # 用户点「停止」
        assert finished.wait(5), "abort 之后必须立刻返回，否则会话锁不放、界面卡死"
        elapsed = time.time() - t0

        r = out["r"]
        assert r["cancelled"] is True, f"应标记为打断，实际 {r}"
        assert r["error"] is None, f"主动停止不该报成断流错误: {r['error']}"
        assert elapsed < 3, f"abort 响应太慢: {elapsed:.2f}s"
        assert stuck.shutdown_called, "必须 shutdown 底层 socket 才能唤醒阻塞的读"
        assert cli._current_conn is None and cli._current_resp is None, "句柄应清空"
    finally:
        B.http.client.HTTPConnection = REAL_CONN
    print("PASS: abort 打断卡在等响应头的请求（prefill 场景 → cancelled 而非 error）")


def main() -> None:
    test_error_text()
    test_strip_images()
    test_image_400_retry()
    test_other_400_no_retry()
    test_abort_unblocks_stuck_request()
    print("PASS: 客户端错误处理全过")


if __name__ == "__main__":
    main()
