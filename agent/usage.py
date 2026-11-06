"""累计 usage 账本：把「一次会话真正发了多少请求、烧了多少 token」记全。

为什么需要它
------------
改动前系统只有「每步一条 stats 事件」，而 agent/loop.py 的重试循环只上报
**最后一次** resp 的 stats；warmup、read_long_doc 的逐段子请求、compaction
LLM 摘要、外脑委派写问题这几类请求根本不经过 emitter。于是账单式数字
（例如某轮 1.47M tokens）只是「成功的那些步」的下限，不是实际用量。

口径（老板的硬要求：数字只能来自运行时或原始来源）
------------------------------------------------
- 只累加后端真实返回的 `prompt_eval_count` / `eval_count`；
- 后端没给 usage 的请求（连接失败、断流、部分模型服务端不下发 usage）只记
  **调用次数 + 缺失计数 + 失败计数**，绝不用字符估算补一个 token 数；
  `agent/compaction.py:estimate_tokens` 的误差可达 ±40%，且只覆盖输入侧，
  拿它当账单会把「未知」伪装成「已测量」；
- 断流但后端给了 done 帧的请求，真实消耗照计，不因 ok=False 丢弃；
- 账本是**会话级**的，落盘在会话 JSON 顶层，不依赖 turns 回放求和
  （webui/store.py 的 MAX_TURNS 会裁掉历史事件）。

埋点在 `agent/backends/base.py:ChatClient.chat` 这一层：所有 client.chat
调用（主循环含重试、warmup、长文精读、摘要、委派）自动进账，调用点不用
逐个补统计。HTTP 层 _retry_on_known_error 的内部重发不走 chat()，是已知
边界，见 README。
"""
from __future__ import annotations

from contextlib import contextmanager

_NUM_KEYS = ("requests", "prompt_tokens", "gen_tokens", "missing_usage",
             "failed_requests")


def _int(v) -> int:
    """旧文件/脏数据安全降级：非整数、负数、None 一律当 0。"""
    if isinstance(v, bool) or not isinstance(v, int):
        return 0
    return max(0, v)


def _blank() -> dict:
    return {k: 0 for k in _NUM_KEYS}


def _bump(d: dict, stats: dict | None, ok: bool) -> None:
    d["requests"] += 1
    if not ok:
        d["failed_requests"] += 1
    st = stats if isinstance(stats, dict) else {}
    has_p = st.get("prompt_eval_count") is not None
    has_g = st.get("eval_count") is not None
    if not has_p and not has_g:
        d["missing_usage"] += 1
        return
    d["prompt_tokens"] += _int(st.get("prompt_eval_count"))
    d["gen_tokens"] += _int(st.get("eval_count"))


def _with_total(d: dict) -> dict:
    out = dict(d)
    out["total_tokens"] = out.get("prompt_tokens", 0) + out.get("gen_tokens", 0)
    return out


class UsageLedger:
    """会话级累计账本；纯数据，无 IO、无线程锁（写入都在回合线程内）。"""

    def __init__(self) -> None:
        self._total = _blank()
        self._by_source: dict[str, dict] = {}

    def record(self, stats: dict | None, source: str = "chat",
               ok: bool = True) -> None:
        """记一次真实发出的模型请求。

        ok=False 只表示请求未成功收尾；usage 仍按后端返回计入（不丢真实消耗）。
        """
        name = source if isinstance(source, str) and source else "chat"
        _bump(self._total, stats, ok)
        _bump(self._by_source.setdefault(name, _blank()), stats, ok)

    def snapshot(self) -> dict:
        return {
            **_with_total(self._total),
            "by_source": {k: _with_total(v) for k, v in self._by_source.items()},
        }

    def delta(self, before: dict | None) -> dict:
        """本回合增量 = 当前 − 回合开始前快照，不改账本本身。"""
        prev = before if isinstance(before, dict) else {}
        after = self.snapshot()
        out = {k: max(0, after[k] - _int(prev.get(k))) for k in _NUM_KEYS}
        prev_src = prev.get("by_source") if isinstance(prev.get("by_source"), dict) else {}
        by: dict[str, dict] = {}
        for name, cur in after["by_source"].items():
            old = prev_src.get(name)
            old = old if isinstance(old, dict) else {}
            d = {k: max(0, cur.get(k, 0) - _int(old.get(k)))
                 for k in _NUM_KEYS}
            if any(d.values()):
                by[name] = _with_total(d)
        return {**_with_total(out), "by_source": by}

    @classmethod
    def from_dict(cls, data: dict | None) -> "UsageLedger":
        """从会话 JSON 顶层字段恢复；旧文件无此字段 / 脏数据 → 空账本。"""
        led = cls()
        if not isinstance(data, dict):
            return led
        led._total = {k: _int(data.get(k)) for k in _NUM_KEYS}
        src = data.get("by_source")
        if isinstance(src, dict):
            for name, raw in src.items():
                if isinstance(name, str) and isinstance(raw, dict):
                    led._by_source[name] = {k: _int(raw.get(k)) for k in _NUM_KEYS}
        return led

    @staticmethod
    def attach(client, ledger: "UsageLedger") -> "UsageLedger":
        """把账本挂到后端实例上（client 换后端 / 会话恢复后要重新挂）。

        usage_sink 供 ChatClient.chat 逐次记账；usage_ledger 供 run_turn
        在回合收尾时取增量并发出累计事件。
        """
        client.usage_sink = ledger.record
        client.usage_ledger = ledger
        return ledger


@contextmanager
def scope(client, name: str):
    """临时标注子请求来源，嵌套/异常都能恢复，避免污染后续请求分类。"""
    old = getattr(client, "usage_scope", "chat")
    try:
        client.usage_scope = name
        yield client
    finally:
        client.usage_scope = old
