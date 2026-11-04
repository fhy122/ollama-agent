"""断流自动重试（设计参考 DeepSeek Harness 的 dsh-llm-retry，Apache-2.0）。

DSH 的核心原则是「先持久、后等待，在步骤边界重跑」。单机单会话场景下
对应为三条：

1. **按错误类别决定重试**：限流 / 服务端错误 / 超时 / 传输断开是瞬态的，
   重试常可恢复（base.py 对「等第一个字节超时」的注释也是同一结论）；
   Key 错、参数错重试多少次都一样，绝不重试。
2. **重试前不动历史**：空输出失败时历史完全没变，重跑是干净的；半截输出
   先把已生成部分落进历史，再用一条「从中断处继续」的系统指令续写——
   绝不回滚重来（用户已经看到的字不能凭空消失或重复生成）。
3. **退避期间随时可被打断**：sleep 分片检查 cancel，用户停止立即生效，
   不留看不见的等待。

错误类别从错误文本识别。文本来自两处：_post_stream 抛的 RuntimeError
（连不上 / 等第一个字节 / API 错误 NNN）和 _read_stream 的 stream_error
（"TimeoutError: ..." / "ConnectionResetError: ..." / 流意外结束）。
"""
from __future__ import annotations

import random
import time

RATE_LIMIT = "rate_limit"
SERVER = "server"
TIMEOUT = "timeout"
TRANSPORT = "transport"
FATAL = "fatal"          # Key 错 / 参数错 / 鉴权：重试无意义

RETRYABLE = frozenset({RATE_LIMIT, SERVER, TIMEOUT, TRANSPORT})

# 默认重试上限（DSH 默认 5，与老板偏好一致）。
# 退避期间 interruptible_sleep 随时响应打断，不会让用户对着「重试中」干等。
DEFAULT_MAX_RETRIES = 5

_BASE_DELAY = 1.0        # 首次退避秒数
_CAP_DELAY = 8.0         # 退避上限
_JITTER = 0.1            # 对称抖动比例


def classify_error(text: str) -> str:
    """从错误文本识别类别。识别不出按 FATAL 处理（宁可多报一次错，不可空转）。"""
    low = (text or "").lower()
    # 限流：HTTP 429 / 各家措辞（中转站并发超限常写作 "Concurrency limit
    # exceeded"——按账号算的瞬态限制，退避重试常可自愈）
    if ("429" in low or "rate limit" in low or "rate_limit" in low
            or "too many requests" in low or "concurrency" in low
            or "并发超限" in low):
        return RATE_LIMIT
    # 服务端错误：API 错误 5xx / overloaded
    for marker in ("api 错误 50", "api 错误 52", "internal server", "overloaded", "bad gateway"):
        if marker in low:
            return SERVER
    # 超时：等第一个字节 / TimeoutError（stream_error 的格式是 "TimeoutError: ..."）
    if "第一个字节" in low or "timeout" in low or "timed out" in low:
        return TIMEOUT
    # 传输层：连不上 / 连接被重置 / 管道断裂 / 流意外结束
    if any(m in low for m in ("连不上", "connection", "broken pipe", "remote end",
                              "eof", "流意外结束", "reset by peer", "incomplete")):
        return TRANSPORT
    return FATAL


def backoff_delay(attempt: int) -> float:
    """指数退避 + 对称抖动：1s → 2s → 4s，上限 8s。attempt 从 1 起。"""
    delay = min(_CAP_DELAY, _BASE_DELAY * (2 ** (attempt - 1)))
    jitter = delay * _JITTER
    return max(0.1, delay + random.uniform(-jitter, jitter))


def interruptible_sleep(seconds: float, cancel=None) -> bool:
    """分片睡眠，随时响应取消。返回 False 表示被取消（调用方按打断收尾）。"""
    deadline = time.time() + seconds
    while True:
        if cancel is not None and cancel.is_set():
            return False
        remaining = deadline - time.time()
        if remaining <= 0:
            return True
        time.sleep(min(0.2, remaining))
