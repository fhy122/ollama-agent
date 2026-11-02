"""客户端错误处理自测（离线，不连 Ollama）。

验证：Ollama 返回 400（典型：把 xlsx 当图片附上）时要能自动剥离图片重试一次，
错误信息要压成人话，且不能改动对话历史。
"""
from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent.client as C  # noqa: E402
from agent.config import AgentConfig  # noqa: E402

NESTED_400 = ('{"error":"{\\"error\\":{\\"code\\":400,'
              '\\"message\\":\\"Failed to load image or audio file\\",'
              '\\"type\\":\\"invalid_request_error\\"}}"}')

REAL_OPEN = C.urllib.request.urlopen


class _Stream:
    def __init__(self, lines):
        self.lines = lines
        self.closed = False

    def __iter__(self):
        return iter(self.lines)

    def close(self):
        self.closed = True


def _http400(body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://t/api/chat", 400, "Bad Request", {},
                                  io.BytesIO(body.encode("utf-8")))


def _client() -> C.OllamaClient:
    cli = C.OllamaClient.__new__(C.OllamaClient)
    cli.cfg = AgentConfig(use_color=False, warm=False)
    cli.host = "http://t"
    cli.model = "m"
    cli.think = False
    cli.num_ctx = None
    cli.temperature = 0.7
    return cli


def test_error_text() -> None:
    assert C._error_text(NESTED_400) == "Failed to load image or audio file"
    assert C._error_text('{"error":"直白消息"}') == "直白消息"
    assert C._error_text('{"error":{"message":"dict 消息"}}') == "dict 消息"
    assert C._error_text("完全不是 JSON") == "完全不是 JSON"
    print("PASS: _error_text（嵌套 JSON / 字符串 / dict / 非 JSON）")


def test_strip_images() -> None:
    hist = [{"role": "user", "content": "看图", "images": ["AAA"]},
            {"role": "assistant", "content": "好的"}]
    payload = {"model": "m", "messages": hist, "stream": True}
    stripped = C._strip_images(payload)
    assert "images" not in stripped["messages"][0], stripped
    assert "图片已剥离" in stripped["messages"][0]["content"]
    assert hist[0]["images"] == ["AAA"], "原历史不能被改动"
    assert C._payload_has_images(payload) is True
    assert C._payload_has_images({"messages": [{"content": "无图"}]}) is False
    print("PASS: _strip_images（副本剥离、原历史不动）")


def test_image_400_retry() -> None:
    sent = []

    def fake_urlopen(req, timeout=None):
        sent.append(json.loads(req.data.decode("utf-8")))
        if len(sent) == 1:
            raise _http400(NESTED_400)
        return _Stream([json.dumps({"message": {"content": "好了"},
                                    "done": True, "eval_count": 1}).encode()])

    C.urllib.request.urlopen = fake_urlopen
    try:
        hist = [{"role": "user", "content": "看图", "images": ["BAD"]}]
        r = _client().chat(hist)
        assert r["content"] == "好了", r
        assert len(sent) == 2, f"应重试一次，实际请求 {len(sent)} 次"
        assert sent[0]["messages"][0]["images"] == ["BAD"], "首次请求应带原图"
        assert "images" not in sent[1]["messages"][0], "重试请求应已剥离图片"
        assert "图片已剥离" in sent[1]["messages"][0]["content"]
        assert hist[0]["images"] == ["BAD"], "历史里的图片应保留（未污染）"
    finally:
        C.urllib.request.urlopen = REAL_OPEN
    print("PASS: 400 图片错误 → 剥离图片重试一次（历史保留原图）")


def test_other_400_no_retry() -> None:
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(1)
        raise _http400('{"error":"model not found"}')

    C.urllib.request.urlopen = fake_urlopen
    try:
        try:
            _client().chat([{"role": "user", "content": "x"}])
            raise AssertionError("应当抛 RuntimeError")
        except RuntimeError as e:
            assert "model not found" in str(e) and "400" in str(e), str(e)
        assert len(calls) == 1, "非图片类 400 不应重试"
    finally:
        C.urllib.request.urlopen = REAL_OPEN
    print("PASS: 非图片 400 → 可读 RuntimeError（不盲目重试）")


def main() -> None:
    test_error_text()
    test_strip_images()
    test_image_400_retry()
    test_other_400_no_retry()
    print("PASS: 客户端错误处理全过")


if __name__ == "__main__":
    main()
