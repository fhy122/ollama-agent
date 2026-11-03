"""重复工具调用提醒（advisory guard）。

设计移植自 DeepSeek Harness 的 @deepseek-ai/dsh-repeat-tool-reminder（Apache-2.0）。
核心语义：

- **只提醒、不否决**：guard 绝不阻止或改写调用，合理重复不受打扰。
- **精确匹配**：同一工具 + 规范化后相同的参数才算连续（参数键序无关）。
- **命中阈值注入提醒**：第一个阈值用温和文案，后续阈值用详细文案
  （点名工具、连击次数、参数预览），提醒在下一次模型调用前注入历史。
- **新用户消息清零**：全新指令绝不会被当成循环。

为什么放在 post-execute 数：执行失败（[错误]）的调用同样经过这里，
模型反复砸一个失败工具正是最需要打破的循环。
"""
from __future__ import annotations

import json

# 温和版（第一个阈值）：先分析上次结果再决定要不要重复
GENTLE_REMINDER = (
    "[系统提醒] 你正在用完全相同的参数重复调用同一个工具。"
    "请先仔细分析上一次的结果：如果任务还没完成，换一种方法或换一组参数，"
    "而不是原样重复调用。"
)

# 详细版（后续阈值）：点名工具、连击次数、参数预览
_DETAILED_TMPL = (
    "[系统提醒] 检测到重复工具调用：\n"
    "- 工具: {tool}\n"
    "- 连续次数: {count}\n"
    "- 参数: {args}\n"
    "这些重复调用没有产生进展。不要再用完全相同的参数调用该工具。"
    "请检查最近一次结果，换一个动作、换一组参数；"
    "如果证据已经足够，直接完成任务并回答。"
)


def _validate_thresholds(values) -> list[int]:
    """fail-loud：空列表 / 非整数 / 小于 2 / 有重复都在构造时抛错，绝不静默回退。"""
    if not values:
        raise ValueError("repeat_guard: thresholds 不能为空")
    for v in values:
        if not isinstance(v, int) or isinstance(v, bool) or v < 2:
            raise ValueError(f"repeat_guard: 非法阈值 {v!r} —— 必须是 >= 2 的整数")
    if len(set(values)) != len(values):
        raise ValueError("repeat_guard: thresholds 不能有重复值")
    return sorted(values)


def canonical_args(args) -> str:
    """参数规范化：递归按键排序后序列化，键序不同视为相同调用。

    args 来自模型输出的 JSON 解析结果，值域就是 JSON——不存在需要特殊
    处理的 bigint / 循环引用 / undefined。解析失败时退回原始字符串比较。
    """
    if isinstance(args, str):
        return args
    try:
        return json.dumps(args, sort_keys=True, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(args)


class RepeatGuard:
    """单个 agent 的连续重复链。非线程安全（agent 回合本身串行）。"""

    def __init__(self, thresholds=(3, 5, 8), exclude: tuple = (),
                 preview_chars: int = 500):
        self._thresholds = _validate_thresholds(thresholds)
        self._threshold_set = set(self._thresholds)
        self._exclude = tuple(exclude)
        self._preview_chars = preview_chars
        self._key: str | None = None    # 最近一次受跟踪调用的身份键
        self.count: int = 0             # 该键的连续次数（公开：loop 用于提示文案）

    def reset(self) -> None:
        """新用户消息到达时清零：跨指令的重复不是循环。"""
        self._key = None
        self.count = 0

    def _tracked(self, name: str) -> bool:
        return name not in self._exclude

    def observe(self, name: str, args) -> str | None:
        """记录一次调用尝试；命中阈值时返回提醒文本（否则 None）。

        不受跟踪的调用是透明的：既不计数也不重置——穿插进循环的
        记录类调用不能掩盖循环。
        """
        if not self._tracked(name):
            return None
        canonical = canonical_args(args)
        key = json.dumps([name, canonical], ensure_ascii=False)
        if self._key == key:
            self.count += 1
        else:
            self._key = key
            self.count = 1
        if self.count not in self._threshold_set:
            return None
        if self.count == self._thresholds[0]:
            return GENTLE_REMINDER
        return _DETAILED_TMPL.format(
            tool=name, count=self.count,
            args=self._preview(canonical))

    def _preview(self, canonical: str) -> str:
        """参数预览截断：只限制模型可见文本，链身份始终用完整规范串。"""
        if len(canonical) <= self._preview_chars:
            return canonical
        return f"{canonical[:self._preview_chars]}…(+{len(canonical) - self._preview_chars} 字符)"
