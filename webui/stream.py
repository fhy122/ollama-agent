"""Web UI 事件发射器：把回合事件写成 NDJSON 流给浏览器。

每行一个 JSON 对象：
  {"type":"step","n":1}
  {"type":"text","text":"..."}
  {"type":"think","text":"..."}
  {"type":"tool_call","name":"read_file","args":{...}}
  {"type":"tool_result","name":"read_file","text":"...","dt":1.2,"images":[...]?}
  {"type":"confirm","id":"c1","question":"write_file({\"path\":...})"}
  {"type":"stats","prompt_tokens":..,"gen_tokens":..,"duration":..,"step":1}
  {"type":"usage","turn":{累计增量},"total":{会话累计}}
  {"type":"thinking","elapsed":12.0,"chars":0}
  {"type":"notice","text":"..."}
"""
from __future__ import annotations

import json
import threading


class StreamEmitter:
    """把事件写进 HTTP 响应流（NDJSON，每行一事件）。

    on_disconnect: 浏览器断连（关页/abort fetch）时回调一次，用于自动打断后端回合。
    """

    def __init__(self, wfile, on_disconnect=None):
        self.wfile = wfile
        self._on_disconnect = on_disconnect
        self._disconnected = False
        # thinking 心跳由独立看门狗线程发，正文由读流线程发 —— 两个线程同时往
        # 同一个 wfile 写会互相插字（NDJSON 拼成半行 JSON，前端整批解析失败）。
        self._lock = threading.Lock()

    def _send(self, obj: dict) -> None:
        line = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self._lock:
                self.wfile.write(line)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            # 浏览器关页/断连：吞掉写错误；通知一次以便打断后端回合
            if not self._disconnected:
                self._disconnected = True
                if self._on_disconnect:
                    try:
                        self._on_disconnect()
                    except Exception:
                        pass

    # ---- 事件接口（与 ConsoleEmitter 对齐） ----

    @property
    def think_hidden(self) -> bool:
        """网页端会渲染「思考过程」折叠块，思考内容对用户可见，所以算进展。"""
        return False

    def step(self, n: int) -> None:
        self._send({"type": "step", "n": n})

    def step_time(self, step: int, t_start: float, t_end: float,
                  ttft: float | None = None) -> None:
        """每步时间窗：真实开始/结束时刻（epoch 毫秒）+ 耗时 + TTFT。

        设计参考 DeepSeek Harness：数据层记录真实时刻，前端只做投影；
        历史回放走同一份事件，不需要二次补算。
        """
        obj = {"type": "step_time", "step": step,
               "t_start": int(t_start * 1000), "t_end": int(t_end * 1000),
               "duration": round(t_end - t_start, 2)}
        if ttft is not None:
            obj["ttft"] = round(ttft, 2)
        self._send(obj)

    def text(self, t: str) -> None:
        self._send({"type": "text", "text": t})

    def think(self, t: str) -> None:
        self._send({"type": "think", "text": t})

    def newline(self) -> None:
        pass

    def tool_call(self, name: str, args: dict) -> None:
        self._send({"type": "tool_call", "name": name, "args": args})

    def user_message(self, text: str) -> None:
        """步边界注入的引导消息（输入队列「立即发送」）。前端渲染用户气泡。"""
        self._send({"type": "user", "text": text})

    def tool_result(self, name: str, text: str, dt: float,
                    images: list[str] | None = None) -> None:
        obj = {"type": "tool_result", "name": name, "text": text, "dt": round(dt, 2)}
        if images:
            obj["images"] = images
        self._send(obj)

    def confirm(self, cid: str, question: str) -> None:
        """请求人工确认（工具执行前）。前端弹窗 → POST /api/confirm 应答。"""
        self._send({"type": "confirm", "id": cid, "question": question})

    def stats(self, st: dict, step: int = 0) -> None:
        if not st:
            return
        self._send({
            "type": "stats",
            "step": step,
            "prompt_tokens": st.get("prompt_eval_count"),
            "gen_tokens": st.get("eval_count"),
            "duration": round((st.get("total_duration") or 0) / 1e9, 2),
        })

    def usage(self, turn: dict, total: dict) -> None:
        """回合收尾：本回合增量 + 会话累计（与逐次 stats 是两个口径）。"""
        self._send({"type": "usage", "turn": turn, "total": total})

    def thinking(self, elapsed: float, chars: int = 0) -> None:
        """生成参数期间的逐秒心跳（长 tool_call 执行前唯一的可见反馈）。

        实测依据：think=False 时让模型写 3 张表的 SQL，95 秒里上游只来 2 个数据块
        （1 个整块 TOOL + 1 个 done），参数生成期间流上零字节 —— Ollama 把
        tool_calls 攒完整才下发。前端无法从流里感知进度，只能由这里主动喂心跳。
        """
        self._send({"type": "thinking", "elapsed": round(elapsed, 1), "chars": chars})

    def notice(self, text: str) -> None:
        self._send({"type": "notice", "text": text})
