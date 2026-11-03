"""断流自测（离线，不连 Ollama）。

背景：WebUI 里出现过「说了 600 字就停」，事后查存档发现那条 assistant 压根没进历史，
前端也收不到 stats / notice —— 因为 client 读流时只 catch KeyboardInterrupt，
socket 超时直接冒到 run_turn 之外，半截回答陪着提示一起蒸发。

这里把三类断流做出来，锁住三条底线：
  1. 断流不抛异常，已生成的 content 必须留住
  2. 断流时的 tool_calls 一律丢弃（参数可能只到一半，执行会写坏文件）
  3. run_turn 要把部分内容写进历史 + 明确告知用户，不能当正常结束
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import agent.backends.base as B  # noqa: E402
from agent.backends import OllamaClient  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.loop import run_turn  # noqa: E402
from agent.registry import ToolContext  # noqa: E402

REAL_CONN = B.http.client.HTTPConnection


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def _text(t: str) -> bytes:
    return _line({"message": {"content": t}, "done": False})


def _done() -> bytes:
    return _line({"message": {"content": ""}, "done": True,
                  "eval_count": 3, "prompt_eval_count": 9, "total_duration": 1_000_000_000})


class _Stream:
    """假响应：按顺序吐字节行，可在中途抛异常（模拟读超时 / 连接重置）。"""

    status = 200          # _post_chat 会检查状态码

    def __init__(self, lines, boom: Exception | None = None, at: int = 10 ** 9):
        self.lines, self.boom, self.at = lines, boom, at
        self.closed = False

    def __iter__(self):
        for i, ln in enumerate(self.lines):
            if i == self.at and self.boom is not None:
                raise self.boom
            yield ln
        if self.boom is not None and self.at >= len(self.lines):
            raise self.boom

    def close(self):
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
    return cli


def _patched(stream: _Stream):
    """把 http.client.HTTPConnection 换成直接返回 stream 的替身。

    _post_chat 改用 http.client 之后（为了能 abort），这里也得打在连接上，
    打 urlopen 已经没用了。
    """

    class _Conn:
        def __init__(self, *args, **kwargs):
            self.sock = None
            self.closed = False

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return stream

        def close(self):
            self.closed = True

    B.http.client.HTTPConnection = _Conn
    return stream


def test_no_done_frame() -> None:
    """连接正常关闭，但服务端从没发 done 帧 —— 必须判为断流。"""
    stream = _patched(_Stream([_text("前半段"), _text("后半段")]))
    try:
        r = _client().chat([{"role": "user", "content": "x"}])
    finally:
        B.http.client.HTTPConnection = REAL_CONN
    assert r["content"] == "前半段后半段", r
    assert r["error"], "缺 done 帧必须标记为断流"
    assert "done" in r["error"], r["error"]
    assert r["cancelled"] is False
    assert stream.closed is True, "断流后应关闭连接释放资源"
    print("PASS: 无 done 帧 → 标记断流、保留内容、关闭连接")


def test_timeout_midstream() -> None:
    """读到一半抛 TimeoutError：不抛出、内容留住、标记原因。"""
    stream = _patched(_Stream([_text("已经说了一半")], boom=TimeoutError("timed out"), at=1))
    try:
        r = _client().chat([{"role": "user", "content": "x"}])
    finally:
        B.http.client.HTTPConnection = REAL_CONN
    assert r["content"] == "已经说了一半", r
    assert "TimeoutError" in (r["error"] or ""), r["error"]
    assert stream.closed is True
    print("PASS: 读超时 → 不抛异常、保留已生成内容")


def test_tool_calls_dropped() -> None:
    """断流前收到的 tool_calls 参数可能只到一半，必须整体丢弃。"""
    half = _line({"message": {"tool_calls": [
        {"function": {"name": "write_file", "arguments": '{"path": "a.txt", "cont'}}]},
        "done": False})
    stream = _patched(_Stream([_text("我来写文件"), half],
                              boom=ConnectionResetError("连接被重置"), at=2))
    try:
        r = _client().chat([{"role": "user", "content": "x"}])
    finally:
        B.http.client.HTTPConnection = REAL_CONN
    assert r["content"] == "我来写文件", r
    assert r["tool_calls"] == [], f"半截工具调用必须丢弃，实际 {r['tool_calls']}"
    assert "ConnectionResetError" in (r["error"] or ""), r["error"]
    assert stream.closed is True
    print("PASS: 断流时丢弃半截 tool_calls（不执行写坏文件）")


class _StubClient:
    """按剧本返回 chat 结果的最小 client。"""

    def __init__(self, result: dict):
        self.result = result
        self.calls = 0

    def chat(self, history, tools=None, on_text=None, on_think=None, cancel=None):
        self.calls += 1
        if on_text and self.result.get("content"):
            on_text(self.result["content"])
        return self.result


class _Emitter:
    """记录事件的最小 emitter（对齐 ConsoleEmitter 的接口）。"""

    def __init__(self):
        self.events: list[tuple[str, object]] = []

    def step(self, n): self.events.append(("step", n))
    def text(self, t): self.events.append(("text", t))
    def think(self, t): self.events.append(("think", t))
    def newline(self): pass
    def tool_call(self, name, args): self.events.append(("tool_call", name))
    def tool_result(self, name, text, dt, images=None): self.events.append(("tool_result", name))
    def confirm(self, cid, q): return True
    def stats(self, st): self.events.append(("stats", st))
    def notice(self, t): self.events.append(("notice", t))


def test_run_turn_keeps_partial() -> None:
    """断流时 run_turn 要把半截内容写进历史，并明确告知用户。"""
    cfg = AgentConfig(use_color=False, warm=False)
    ctx = ToolContext(use_color=False, workspace=Path("."))
    em = _Emitter()
    client = _StubClient({"content": "自动化测试策略前半段",
                          "tool_calls": [], "stats": {},
                          "cancelled": False, "error": "TimeoutError: timed out"})
    history = [{"role": "system", "content": "sys"}]

    out = run_turn(client, cfg, ctx, history, "怎么做自动化测试", emitter=em)

    assert out == "自动化测试策略前半段", out
    assert client.calls == 1, "断流后不应继续跑下一步"
    last = history[-1]
    assert last["role"] == "assistant", last
    assert "自动化测试策略前半段" in last["content"], "已生成内容必须留在历史里"
    assert "回答中断" in last["content"], last["content"]
    notes = [v for k, v in em.events if k == "notice"]
    assert any("回答中断" in str(n) for n in notes), notes
    assert any("继续" in str(n) for n in notes), "应告诉用户可以直接说继续"
    print("PASS: run_turn 断流兜底（内容入历史 + 明确提示 + 不继续跑）")


def test_post_chat_first_byte_timeout() -> None:
    """首字节前超时：给一句能照着做的提示，而不是裸 TimeoutError。"""

    class _Conn:
        def __init__(self, *args, **kwargs):
            self.sock = None

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            raise TimeoutError("timed out")

        def close(self):
            pass

    B.http.client.HTTPConnection = _Conn
    try:
        try:
            _client().chat([{"role": "user", "content": "x"}])
            raise AssertionError("应当抛 RuntimeError")
        except RuntimeError as e:
            msg = str(e)
            assert "timeout" in msg and "秒" in msg, msg
    finally:
        B.http.client.HTTPConnection = REAL_CONN
    print("PASS: 首字节超时 → 可读提示（含调参建议）")


def main() -> None:
    test_no_done_frame()
    test_timeout_midstream()
    test_tool_calls_dropped()
    test_run_turn_keeps_partial()
    test_post_chat_first_byte_timeout()
    print("PASS: 断流处理全过")


if __name__ == "__main__":
    main()
