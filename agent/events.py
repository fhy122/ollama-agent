"""回合事件输出层：控制台 / HTTP 流 / 其它前端共用同一事件接口。

run_turn 不再直接 print，而是调用 emitter 的方法；不同前端各自实现发射器：
  ConsoleEmitter —— CLI 终端输出（原有行为）
  StreamEmitter  —— webui NDJSON 流（见 webui/stream.py）
新增前端（比如 GUI）只需实现同一组方法即可复用整个 agent 主循环。
"""
from __future__ import annotations

import json

from agent.console import color

# 等超过这么多秒才提示一次「模型仍在生成」。
# 太小的值会让每个普通回答都闪一行废话；3 秒是个还能忍的静止长度。
_WAIT_HINT_SEC = 3.0


class ConsoleEmitter:
    """终端输出发射器（CLI 默认，行为与历史版本一致）。"""

    def __init__(self, cfg):
        self.cfg = cfg
        self.use_color = cfg.use_color

    @property
    def think_hidden(self) -> bool:
        """思考内容对用户是否不可见（决定长等待时要不要发心跳）。

        /hide-think 之后模型照样在吐 think 块，但终端一个都不打。如果把这些
        看不见的块当成「有进展」，心跳会被一路抑制 —— 实测出现过 155 秒
        一声不响，用户以为死机。所以藏起来的时候要持续报心跳。
        """
        return not self.cfg.show_think

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

    def user_message(self, text: str) -> None:
        """步边界注入的引导消息（输入队列「立即发送」）。"""
        print(color(f"\n💬 引导: {text}", "cyan", self.use_color), flush=True)

    def tool_result(self, name: str, text: str, dt: float,
                    images: list[str] | None = None) -> None:
        preview = text
        if len(preview) > 1500:
            preview = preview[:1500] + f"\n…[截断，共 {len(text)} 字符]"
        print(color(f"  ↳ ({dt:.1f}s) {preview}", "dim", self.use_color), flush=True)

    def stats(self, st: dict, step: int = 0) -> None:
        """报一次生成耗时。step>0 表示这是「第 N 步」的耗时。

        以前一个回合只在末尾报一次，用的是最后一次 resp —— 生成 9000 字符
        工具参数那一步的耗时在界面上根本不存在，用户只看到最后一项小数字，
        怎么也对不上自己的等待时间。现在每步各报一次。
        """
        if st.get("eval_count"):
            dur = (st.get("total_duration") or 0) / 1e9
            tag = f"[第 {step} 步] " if step else ""
            print(color(f"─── {tag}tokens: prompt={st.get('prompt_eval_count')} "
                        f"gen={st.get('eval_count')} 用时 {dur:.1f}s",
                        "cyan", self.use_color), flush=True)

    def usage(self, turn: dict, total: dict) -> None:
        """回合收尾的累计用量：含重试、warmup、子请求；缺失 usage 明确标注。

        与上面逐次的 stats 是两个口径：stats 是「这一步最后一次请求」，
        usage 是「本回合所有请求之和」，别混着读。
        """
        miss = int(turn.get("missing_usage") or 0)
        extra = f"，其中 {miss} 次后端未返回 usage（未计入 token）" if miss else ""
        print(color(f"─── 本回合 {turn.get('requests', 0)} 次调用 · "
                    f"prompt {turn.get('prompt_tokens', 0)} · "
                    f"生成 {turn.get('gen_tokens', 0)} · "
                    f"合计 {turn.get('total_tokens', 0)} tokens{extra}",
                    "cyan", self.use_color), flush=True)
        print(color(f"    会话累计 {total.get('requests', 0)} 次调用 · "
                    f"{total.get('total_tokens', 0)} tokens",
                    "dim", self.use_color), flush=True)

    def notice(self, text: str) -> None:
        print(color(text, "yellow", self.use_color), flush=True)

    def thinking(self, elapsed: float, chars: int = 0) -> None:
        """模型正在生成、但还没有可显示内容时的心跳。

        终端不做逐秒刷新（会把命令行刷成一片），只在超过阈值时提一句，
        否则用户看着光标不动会以为程序卡死。
        """
        if elapsed >= _WAIT_HINT_SEC and int(elapsed) % 5 == 0:
            tail = f"，已生成 {chars} 字符" if chars else ""
            print(color(f"  … 模型仍在生成（已等 {elapsed:.0f}s{tail}）",
                        "dim", self.use_color), flush=True)
