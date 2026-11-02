"""Web UI 事件发射器：把回合事件写成 NDJSON 流给浏览器。

每行一个 JSON 对象：
  {"type":"step","n":1}
  {"type":"text","text":"..."}
  {"type":"think","text":"..."}
  {"type":"tool_call","name":"read_file","args":{...}}
  {"type":"tool_result","name":"read_file","text":"...","dt":1.2,"images":[...]?}
  {"type":"confirm","id":"c1","question":"write_file({\"path\":...})"}
  {"type":"stats","prompt_tokens":..,"gen_tokens":..,"duration":..}
  {"type":"notice","text":"..."}
"""
from __future__ import annotations

import json


class StreamEmitter:
    """把事件写进 HTTP 响应流（NDJSON，每行一事件）。

    on_disconnect: 浏览器断连（关页/abort fetch）时回调一次，用于自动打断后端回合。
    """

    def __init__(self, wfile, on_disconnect=None):
        self.wfile = wfile
        self._on_disconnect = on_disconnect
        self._disconnected = False

    def _send(self, obj: dict) -> None:
        try:
            self.wfile.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
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

    def step(self, n: int) -> None:
        self._send({"type": "step", "n": n})

    def text(self, t: str) -> None:
        self._send({"type": "text", "text": t})

    def think(self, t: str) -> None:
        self._send({"type": "think", "text": t})

    def newline(self) -> None:
        pass

    def tool_call(self, name: str, args: dict) -> None:
        self._send({"type": "tool_call", "name": name, "args": args})

    def tool_result(self, name: str, text: str, dt: float,
                    images: list[str] | None = None) -> None:
        obj = {"type": "tool_result", "name": name, "text": text, "dt": round(dt, 2)}
        if images:
            obj["images"] = images
        self._send(obj)

    def confirm(self, cid: str, question: str) -> None:
        """请求人工确认（工具执行前）。前端弹窗 → POST /api/confirm 应答。"""
        self._send({"type": "confirm", "id": cid, "question": question})

    def stats(self, st: dict) -> None:
        if not st:
            return
        self._send({
            "type": "stats",
            "prompt_tokens": st.get("prompt_eval_count"),
            "gen_tokens": st.get("eval_count"),
            "duration": round((st.get("total_duration") or 0) / 1e9, 2),
        })

    def notice(self, text: str) -> None:
        self._send({"type": "notice", "text": text})
