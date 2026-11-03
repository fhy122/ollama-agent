"""time-context 时间注入（借鉴 DeepSeek Harness dsh-time-context）。

9B 模型没有可靠的时间感：「今天」「明天」「等了多久」全靠猜。每次请求前
临时追加一条时间读数（当前时间 + 星期 + 时区 + 距基准点的经过时长），
让模型能正确解释相对时间。

与 DSH 同款的关键语义：
- **请求时注入、不入持久历史**——历史保持干净（重跑请求与首次完全一致），
  每次请求只看到一条新鲜的读数；
- 第 1 步基准是用户消息，后续步基准是本回合上一次注入（看得出步骤间隔）；
- 刷新间隔默认 600 秒：间隔内的后续步骤不重复注入，省 token。
"""
from __future__ import annotations

import time
from datetime import datetime

_WEEK = "一二三四五六日"


def fmt_elapsed(seconds: float) -> str:
    """秒数 → 紧凑时长（DSH 同款格式）：1d 2h 3m 4s；负数按 0 处理。"""
    s = max(0, int(seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _utc_offset(lt: datetime) -> str:
    """本地时区偏移 → 「UTC+8」/「UTC-3:30」样式。"""
    off = lt.utcoffset()
    if off is None:
        return "UTC"
    total = int(off.total_seconds()) // 60
    sign = "+" if total >= 0 else "-"
    h, m = divmod(abs(total), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


def time_message(step: int, baseline_ts: float, now: float | None = None) -> dict:
    """构造一条请求专用的 user 消息（调用方自行决定拼进请求，不入 history）。"""
    now = now if now is not None else time.time()
    lt = datetime.astimezone(datetime.fromtimestamp(now))
    stamp = lt.strftime("%Y-%m-%d %H:%M:%S")
    base = "用户消息" if step == 1 else "上一次时间读数"
    text = (f"[时间上下文] 第 {step} 步请求采样：{stamp}"
            f"（周{_WEEK[lt.weekday()]}，{_utc_offset(lt)} 本地时间）\n"
            f"距{base}已过去 {fmt_elapsed(now - baseline_ts)}。")
    return {"role": "user", "content": text}


def should_inject(step: int, last_inject_ts: float | None, now: float,
                  enabled: bool, refresh_sec: int) -> bool:
    """注入判定：开关 + 刷新间隔（0 = 每步都注入）。"""
    if not enabled:
        return False
    if step == 1 or refresh_sec <= 0 or last_inject_ts is None:
        return True
    return now - last_inject_ts >= refresh_sec
