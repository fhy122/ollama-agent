"""回合事件输出层：控制台 / HTTP 流 / 其它前端共用同一事件接口。

run_turn 不再直接 print，而是调用 emitter 的方法；不同前端各自实现发射器：
  ConsoleEmitter —— CLI 终端输出（原有行为）
  StreamEmitter  —— webui NDJSON 流（见 webui/stream.py）
新增前端（比如 GUI）只需实现同一组方法即可复用整个 agent 主循环。
"""
from __future__ import annotations

import json

from agent.console import color


class ConsoleEmitter:
    """终端输出发射器（CLI 默认，行为与历史版本一致）。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.use_color = cfg.use_color

    def step(self, n: int) -> None:
        print(color(f"\n─── 第 {n} 步", "cyan", self.use_color), flush=True)

    def text(self, t: str) -> None:
        print(t, end="", flush=True)

    def think(self, t: str) -> None:
        if self.cfg.show_think:
            print(color(t, "dim", self.use_color), end="", flush=True)

    def newline(self) -> None:
        print(flush=True)

    def tool_call(self, name: str, args: dict) -> None:
        brief = json.dumps(args, ensure_ascii=False)[:300]
        print(color(f"⚙ 工具调用: {name}({brief})", "green", self.use_color), flush=True)

    def tool_result(self, name: str, text: str, dt: float,
                    images: list[str] | None = None) -> None:
        preview = text
        if len(preview) > 1500:
            preview = preview[:1500] + f"\n…[截断，共 {len(text)} 字符]"
        print(color(f"  ↳ ({dt:.1f}s) {preview}", "dim", self.use_color), flush=True)

    def stats(self, st: dict) -> None:
        if st.get("eval_count"):
            dur = (st.get("total_duration") or 0) / 1e9
            print(color(f"─── tokens: prompt={st.get('prompt_eval_count')} "
                        f"gen={st.get('eval_count')} 用时 {dur:.1f}s",
                        "cyan", self.use_color), flush=True)

    def notice(self, text: str) -> None:
        print(color(text, "yellow", self.use_color), flush=True)
