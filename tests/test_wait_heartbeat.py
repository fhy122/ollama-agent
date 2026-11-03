"""长工具调用期间的心跳自测（离线，不连 Ollama）。

背景：用户在界面上看到的现象是「模型说完一句『现在更新数据模型』，然后画面
完全静止几分钟，接着 write_file 卡片突然出现」。查下来不是卡死 —— 是 Ollama
把 tool_calls 攒成整块才下发，参数生成的那几十上百秒里连接上一个字节都没有。

这里锁住四条：
  1. 流静默期间 on_wait 必须被周期性调用（否则前端没有任何可见反馈）
  2. on_wait 拿得到「已等秒数」和「已生成字符数」，两个都单调不减
  3. chat 返回后心跳必须停（漏停会一直往已收尾的界面刷「仍在生成」）
  4. 心跳回调抛异常不能影响读流（前端 bug 不该弄坏后端）
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

REAL_CONN = B.http.client.HTTPConnection


def _line(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


class _SlowStream:
    """假响应：前 `silent` 秒一个字节都不给（模拟参数生成期），再吐整块 tool_calls。

    这正是实测到的行为 —— think=off 请求写 3 表 SQL 时，95 秒里只来 2 个数据块。
    """

    status = 200

    def __init__(self, lines, silent: float = 0.0):
        self.lines = lines
        self.silent = silent
        self.closed = False

    def __iter__(self):
        if self.silent:
            time.sleep(self.silent)
        for ln in self.lines:
            yield ln

    def close(self):
        self.closed = True


class _LateHeaderStream(_SlowStream):
    """**响应头本身就要等 `delay` 秒**才回来。

    这是真机上真正发生的场景，也是最初那版实现漏掉的那段时间：
        实测 qwen3.5:9b 写 40 行 CSV 的 write_file 参数 →
        [ 128.6s] ★ 响应头到达（getresponse 返回）
        [ 128.6s] 第1块 长度=901
    Ollama 在工具参数整块生成完之前连 HTTP 响应头都不发。心跳线程如果是在
    `_post_stream()` 之后才启动，这 128 秒就完全没有心跳 —— 而这段时间恰恰
    是用户盯着白屏骂人的全部时间。

    用 _SlowStream（只在 __iter__ 里 sleep）测不出这个漏洞：那时响应头早回来了。
    """

    def __init__(self, lines, delay: float):
        super().__init__(lines)
        self.delay = delay

    def slow_headers(self):
        time.sleep(self.delay)


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


def _patched(stream: _SlowStream) -> _SlowStream:
    class _Conn:
        def __init__(self, *args, **kwargs):
            self.sock = None

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            # 有 slow_headers 的流：响应头本身也要等（真机就是这样，
            # Ollama 攒完整块 tool_calls 才发第一行 HTTP）
            slow = getattr(stream, "slow_headers", None)
            if slow:
                slow()
            return stream

        def close(self):
            pass

    B.http.client.HTTPConnection = _Conn
    return stream


def _tool_block(name: str, args: dict) -> bytes:
    return _line({"message": {"content": "",
                              "tool_calls": [{"function": {"name": name,
                                                           "arguments": args}}]},
                  "done": False})


def _done() -> bytes:
    return _line({"message": {"content": ""}, "done": True,
                  "eval_count": 900, "prompt_eval_count": 13652,
                  "total_duration": 95_000_000_000})


def test_heartbeat_fires_while_stream_is_silent() -> None:
    """静默 2.5 秒的流：心跳必须报过，且秒数递增。"""
    stream = _patched(_SlowStream([_tool_block("write_file",
                                               {"path": "a.py", "content": "x" * 50}),
                                   _done()], silent=2.5))
    seen: list[tuple[float, int]] = []
    try:
        r = _client().chat([{"role": "user", "content": "x"}],
                           on_wait=lambda el, ch: seen.append((el, ch)))
    finally:
        B.http.client.HTTPConnection = REAL_CONN

    assert r["tool_calls"], "整块 tool_calls 应该照常解析出来"
    assert len(seen) >= 2, f"2.5 秒静默至少应报 2 次心跳，实际 {seen}"
    assert seen[0][0] < seen[-1][0], f"已等秒数应递增，实际 {seen}"
    assert all(ch == 0 for _, ch in seen), "还没收到任何内容时字符数应为 0"
    print(f"PASS: 静默期心跳 {len(seen)} 次，最后一次 {seen[-1][0]:.1f}s")


def test_heartbeat_stops_after_chat_returns() -> None:
    """chat 返回后不能再有心跳 —— 否则界面会一直显示「仍在生成」。"""
    stream = _patched(_SlowStream([_tool_block("write_file", {"path": "a"}), _done()],
                                  silent=1.5))
    seen: list[float] = []
    try:
        _client().chat([{"role": "user", "content": "x"}],
                       on_wait=lambda el, ch: seen.append(el))
    finally:
        B.http.client.HTTPConnection = REAL_CONN

    n_at_return = len(seen)
    assert n_at_return >= 1, f"1.5 秒静默应至少报 1 次，实际 {seen}"
    time.sleep(2.2)          # 等两个心跳周期，看还有没有漏网的线程
    assert len(seen) == n_at_return, \
        f"chat 已返回却仍在心跳：{n_at_return} → {len(seen)}"
    print(f"PASS: 返回后心跳已停（返回时 {n_at_return} 次，2.2s 后仍 {len(seen)} 次）")


def test_heartbeat_reports_generated_chars() -> None:
    """有内容进来时，字符数要跟着涨（工具参数也算）。"""
    # 先把一段正文吐出来，再静默一会儿：静默期心跳应带上已生成的字符数
    lines = [_line({"message": {"content": "好的，现在更新数据模型"},
                    "done": False}),
             _tool_block("write_file", {"path": "a.py", "content": "y" * 200}),
             _done()]

    class _Stream2(_SlowStream):
        def __iter__(self):
            yield self.lines[0]
            time.sleep(1.8)                  # 模拟参数生成期的静默
            for ln in self.lines[1:]:
                yield ln

    stream = _patched(_Stream2(lines))
    seen: list[tuple[float, int]] = []
    try:
        _client().chat([{"role": "user", "content": "x"}],
                       on_wait=lambda el, ch: seen.append((el, ch)))
    finally:
        B.http.client.HTTPConnection = REAL_CONN

    assert seen, "静默 1.8 秒应有心跳"
    assert any(ch > 0 for _, ch in seen), \
        f"已生成过正文，字符数不该全是 0：{seen}"
    print(f"PASS: 心跳带字符数，最大 {max(ch for _, ch in seen)} 字符")


def test_bad_on_wait_does_not_break_stream() -> None:
    """心跳回调抛异常：读流必须照常走完（前端 bug 不能弄坏后端）。"""
    stream = _patched(_SlowStream([_line({"message": {"content": "正文"}, "done": False}),
                                   _done()], silent=1.5))

    def boom(el, ch):
        raise RuntimeError("前端渲染炸了")

    try:
        r = _client().chat([{"role": "user", "content": "x"}], on_wait=boom)
    finally:
        B.http.client.HTTPConnection = REAL_CONN
    assert r["content"] == "正文", r
    assert r["error"] is None, f"回调异常不该被当成断流：{r['error']}"
    print("PASS: on_wait 抛异常不影响读流")


def test_no_on_wait_no_thread() -> None:
    """没传 on_wait 就不该起线程（每个回合都白起一个线程是浪费）。"""
    stream = _patched(_SlowStream([_line({"message": {"content": "hi"}, "done": False}),
                                   _done()]))
    before = threading.active_count()
    try:
        r = _client().chat([{"role": "user", "content": "x"}])
    finally:
        B.http.client.HTTPConnection = REAL_CONN
    after = threading.active_count()
    assert r["content"] == "hi", r
    assert after <= before, f"没传 on_wait 却多出了线程：{before} → {after}"
    print(f"PASS: 无 on_wait 不起线程（{before} → {after}）")


def _loop_stub():
    """两步回合：第 1 步调工具，第 2 步给最终回答。两步各带不同的 stats。"""
    from agent.loop import run_turn
    from agent.registry import ToolContext

    class _Client:
        def __init__(self):
            self.calls = 0
            self.got_on_wait = False

        def chat(self, messages, tools=None, on_text=None, on_think=None,
                 cancel=None, on_wait=None, think_hidden=True):
            self.calls += 1
            self.got_on_wait = on_wait is not None
            if self.calls == 1:
                return {"thinking": "", "content": "",
                        "tool_calls": [{"function": {"name": "list_files",
                                                     "arguments": {"path": "."}}}],
                        "stats": {"prompt_eval_count": 100, "eval_count": 20,
                                  "total_duration": 2e9},
                        "cancelled": False, "error": None}
            return {"thinking": "", "content": "好了",
                    "tool_calls": [],
                    "stats": {"prompt_eval_count": 500, "eval_count": 900,
                              "total_duration": 95e9},
                    "cancelled": False, "error": None}

    class _Em:
        def __init__(self):
            self.seen = []        # 别叫 stats：会盖住下面的 stats 方法
            self.waits = 0

        def step(self, n): pass
        def text(self, t): pass
        def think(self, t): pass
        def newline(self): pass
        def tool_call(self, name, args): pass
        def tool_result(self, name, text, dt, images=None): pass
        def notice(self, t): pass
        def stats(self, st, step=0): self.seen.append((step, st.get("eval_count")))
        def thinking(self, elapsed, chars=0): self.waits += 1

    cfg = AgentConfig(use_color=False, warm=False)
    ctx = ToolContext(use_color=False, workspace=Path("."))
    return run_turn, cfg, ctx, _Client, _Em


def test_loop_reports_stats_per_step() -> None:
    """每一步各报一次统计 —— 生成 9000 字符工具参数那步的耗时不能再消失。"""
    run_turn, cfg, ctx, _Client, _Em = _loop_stub()
    client, em = _Client(), _Em()
    run_turn(client, cfg, ctx, [{"role": "system", "content": "s"}], "hi", emitter=em)

    assert client.got_on_wait, "run_turn 必须把心跳回调传给 chat"
    assert [s for s, _ in em.seen] == [1, 2], f"两步应各报一次且带步号：{em.seen}"
    assert em.seen[1][1] == 900, f"第 2 步的生成量不该被第 1 步覆盖：{em.seen}"
    print(f"PASS: 每步单独报统计 {em.seen}")


def test_old_signature_emitter_still_works() -> None:
    """老签名 stats(st) 的第三方前端不能因为多了 step 就中途崩掉。"""
    from agent.loop import _stats_with_step

    class Old:
        def __init__(self):
            self.n = 0

        def stats(self, st):        # 没有 step —— 教程里就是这样写的
            self.n += 1

    class New:
        def __init__(self):
            self.steps = []

        def stats(self, st, step=0):
            self.steps.append(step)

    o = Old()
    _stats_with_step(o)({"eval_count": 1}, step=3)      # 不能抛 TypeError
    assert o.n == 1
    n = New()
    _stats_with_step(n)({"eval_count": 1}, step=3)
    assert n.steps == [3], n.steps
    print("PASS: 老/新 stats 签名都能用")


def test_heartbeat_covers_header_wait() -> None:
    """**响应头本身要等 3 秒**：心跳必须照样报，且从发请求那一刻算起。

    这是真机 bug 的回归测试。最初那版把看门狗起在 `_post_stream()` 之后，
    Ollama 又偏偏在工具参数生成完之前不发响应头 —— 两件事叠加，最需要反馈的
    那 100 多秒完全没有心跳。用 _SlowStream（只在 __iter__ 里 sleep）测不出来，
    因为那时响应头早就到了。
    """
    stream = _patched(_LateHeaderStream(
        [_tool_block("write_file", {"path": "a.py", "content": "z" * 50}), _done()],
        delay=3.0))
    seen: list[tuple[float, int]] = []
    try:
        r = _client().chat([{"role": "user", "content": "x"}],
                           on_wait=lambda el, ch: seen.append((el, ch)))
    finally:
        B.http.client.HTTPConnection = REAL_CONN

    assert r["tool_calls"], "等完响应头后应照常解析出 tool_calls"
    assert len(seen) >= 2, \
        f"响应头等了 3 秒，这段时间至少该报 2 次心跳，实际 {len(seen)} 次：{seen}"
    assert seen[0][0] >= 1.0, f"第一次心跳的秒数应从发请求算起：{seen[0]}"
    print(f"PASS: 响应头等待期也有心跳（{len(seen)} 次，首次 {seen[0][0]:.1f}s）")


def test_heartbeat_not_suppressed_by_hidden_think() -> None:
    """**思考内容被藏起来时，持续吐 think 不能把心跳吞掉**。

    这是第二个真机 bug 的回归测试。实测日志长这样：
        [   3.0s] 心跳 已等3s 已0字符
        [ 158.6s] 心跳 已等159s 已1052字符     ← 中间 155 秒一声不响
    中间那 155 秒模型在拼命吐 think 块，只是 show_think=off 一个都没显示。
    如果「来了 think」也算进展，心跳就永远等不到静默 —— 用户的白屏也永远是白的。
    """

    class _ThinkStream(_SlowStream):
        """先慢慢吐一串 think（用户看不见），再给一段正文。"""

        def __iter__(self):
            for i in range(6):
                time.sleep(0.5)
                yield _line({"message": {"content": "", "thinking": f"思考第{i}步…"},
                             "done": False})
            yield _line({"message": {"content": "答案"}, "done": False})
            yield _done()

    def run(think_hidden: bool) -> list[tuple[float, int]]:
        stream = _patched(_ThinkStream([]))
        seen: list[tuple[float, int]] = []
        try:
            _client().chat([{"role": "user", "content": "x"}],
                           on_wait=lambda el, ch: seen.append((el, ch)),
                           think_hidden=think_hidden)
        finally:
            B.http.client.HTTPConnection = REAL_CONN
        return seen

    hidden = run(True)
    assert len(hidden) >= 2, \
        f"think 藏起来时，3 秒内应持续报心跳（不能被子虚乌有的进展吞掉）：{hidden}"

    visible = run(False)
    assert len(visible) < len(hidden) or len(visible) <= 1, \
        f"think 可见时不该狂刷心跳（用户能看到字，提示就是噪音）：" \
        f"hidden={len(hidden)} visible={len(visible)}"
    print(f"PASS: 藏 think 时报 {len(hidden)} 次心跳，可见时只报 {len(visible)} 次")


def main() -> None:
    test_heartbeat_fires_while_stream_is_silent()
    test_heartbeat_stops_after_chat_returns()
    test_heartbeat_reports_generated_chars()
    test_bad_on_wait_does_not_break_stream()
    test_no_on_wait_no_thread()
    test_loop_reports_stats_per_step()
    test_old_signature_emitter_still_works()
    test_heartbeat_covers_header_wait()
    test_heartbeat_not_suppressed_by_hidden_think()
    print("\n全部通过")


if __name__ == "__main__":
    main()
