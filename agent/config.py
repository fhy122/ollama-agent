"""全局配置。"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from agent.retry import DEFAULT_MAX_RETRIES

DEFAULT_MODEL = "qwen3.5:9b"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_PROVIDER = "ollama"          # ollama | openai，见 agent/providers.py
DEFAULT_CTX = 65536
# num_ctx 上限：ollama 会照单全收这个值并尝试分配 KV cache，
# 填 10**9 只会换来一个难以归因的 OOM。这里按本地大模型的实际天花板截住。
MAX_CTX = 1_048_576
MAX_TOOL_STEPS = 25

# think 字段取值：off=False 关思考（默认）；auto=完全不传该字段（兼容不支持思考的模型）
THINK_VALUES: dict[str, "bool | str | None"] = {
    "off": False,
    "on": True,
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "max",
    "auto": None,
}


def parse_repeat_penalty(raw):
    """repeat_penalty 输入收敛：合法正 float → float，其余 → None（不下发）。

    空串 / 非法串 / bool / NaN / inf / <=0 都回 None：宁可沿用服务端默认，
    也不把一个用户没打算设的值悄悄下发成解码参数。0 没有官方语义，
    文档给的下限示例是 0.9（更宽松）、1.0（disabled），不替用户猜。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) and v > 0 else None


def parse_repeat_last_n(raw):
    """repeat_last_n：官方合法值是 >= -1 的整数（0=禁用、-1=num_ctx）。

    1.5 这类小数静默截断成 1 是有害的（用户以为设了 1.5），直接判非法。
    """
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, str) and not raw.strip():
        return None
    try:
        f = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f) or f != int(f):
        return None
    return int(f) if f >= -1 else None


# 基础行为规则，只写与具体工具无关的约定（模型行为、工作空间、语言风格）。
# 工具的使用策略/适用边界/反例一律写在各工具自己的 description 里——
# description 随 tools 数组每次请求下发，是模型选工具的唯一依据，
# 在系统提示里重复一遍只会白吃 token；工具增删也不会留下幽灵规则。
#
# 第 6 条单独拆出来：它点了名要「必须用 write_file」。工具被裁掉（只对话模式）
# 时这句话就是错的 —— 模型会去调一个没下发的工具，或者被告知「无法生成文件」
# 后干脆摆烂。所以由 new_history 按「当前有没有 write_file」决定拼不拼它。
SYSTEM_PROMPT_CORE = (
    "你是一个运行在本地的文件/网络/命令行助手。规则：\n"
    "1. 需要读文件、写文件、删除文件、精确改文件、列目录、搜代码、跑命令、"
    "看图片、读网页、联网搜索时，必须调用工具，不要凭空猜测文件内容。\n"
    "2. 工具结果拿到后继续调用需要的工具，直到信息足够，再给出最终回答。\n"
    "3. 工具返回[错误]或失败时，如实告知失败原因，绝对不要编造工具结果、网页内容或链接。\n"
    "4. 回答用简体中文，直接、紧凑，先结论后细节。\n"
    "5. 读写文件默认在工作空间内进行：用户说的相对路径都相对工作空间；"
    "只有用户明确给出绝对路径时才用绝对路径。写文件前确认路径合理；引用网页时给出链接。\n"
)

SYSTEM_PROMPT_FILE_RULE = (
    "6. 用户要求「生成/做/保存/导出 xxx 文件」（如 .svg/.html/.json/.csv/.md/.py）时，"
    "必须用 write_file 把内容写进工作空间并告知保存路径，不要只把代码贴在聊天里让用户自己存，"
    "更不要回答「我无法生成文件」。SVG/HTML/CSS/JS 都是纯文本，write_file 直接就能写。\n"
)

# 全部规则（向后兼容：外部/测试引用的常量仍然存在）
SYSTEM_PROMPT = SYSTEM_PROMPT_CORE + SYSTEM_PROMPT_FILE_RULE

# 「该问就问」这条只在 ask_user 工具可用时才拼：没有这个工具还让模型「去问用户」，
# 它只会输出一句「请问您想选哪个」然后结束回合 —— 用户回答后又得从头解释一遍。
SYSTEM_PROMPT_ASK_RULE = (
    "7. 需要用户拍板才能往下走时（多个可行方案要取舍、方向或范围有歧义、"
    "改动会碰到生产数据），调用 ask_user 把选项摆出来等答复，不要自己挑一个就开工，"
    "也不要用一句「请问您想选哪种」结束本轮（那样用户答了还得重讲背景）。"
    "反过来，能自己查清楚的事不要问：先读文件、搜代码，把问题收敛成一个具体选择再问。\n"
)


@dataclass
class AgentConfig:
    host: str = DEFAULT_HOST
    provider: str = DEFAULT_PROVIDER       # ollama=原生 /api/chat；openai=兼容 /v1
    api_key: str = ""                      # 仅 provider=openai 用；ollama 忽略
    model: str = DEFAULT_MODEL
    think: str = "off"                 # 键名，见 THINK_VALUES
    num_ctx: int | None = DEFAULT_CTX  # None = 不传 num_ctx（用 Ollama 服务端默认）
    temperature: float = 0.7
    # ---- 重复惩罚（仅 Ollama 原生协议的 options 下发）----
    # None = 完全不下发，沿用服务端 / Modelfile 的默认值。默认关闭是有意的：
    # 工具调用是 JSON，含大量重复结构 token，全局加惩罚可能破坏 function calling；
    # 有没有收益要用 evals/tool_hit.py 实测，不该在没人拍板时替用户改解码行为。
    # 依据 https://docs.ollama.com/modelfile#valid-parameters-and-values
    #   repeat_penalty  float  默认 1.0（disabled），官方示例 1.1，1.5 更强
    #   repeat_last_n   int    默认 64；0 = 禁用、-1 = num_ctx（这两个是合法值）
    repeat_penalty: float | None = None
    repeat_last_n: int | None = None
    keep_alive: str = "30m"
    timeout: int = 1800                # 等新数据的上限（非整轮总时长）：只要流还在吐字就一直等
    max_steps: int = MAX_TOOL_STEPS
    stream_retries: int = DEFAULT_MAX_RETRIES   # 断流/超时自动重试次数上限（0 = 关闭，行为同旧版）
    spill_max_chars: int = 12000        # 工具结果超此字符数→全文落盘+预览（0 = 关闭）
    tool_call_arg_max_chars: int = 2000  # 请求视图里工具调用大文本参数上限（0 = 关闭；只改视图不改日志）
    # ---- 长会话压缩（参考 DeepSeek Harness compaction-basic，阈值一律可配）----
    compaction_enabled: bool = True            # 关掉即完全不压缩，行为同旧版
    compaction_threshold_ratio: float = 0.8   # 触发阈值 = min(num_ctx*ratio, num_ctx-O-headroom)
    compaction_headroom_tokens: int = 8192    # 给后续工具结果/回答留的余量 B
    compaction_reserved_completion_tokens: int = 2048  # 输出预留 O
    compaction_retain_ratio: float = 0.16     # 从尾部逐字保留的预算 = (num_ctx-O)*ratio
    compaction_retries: int = 1               # 单回合最多压缩几次（防每步反复压）
    compaction_summarizer: str = "local"      # local=确定性本地摘要(零额外请求) | llm=另发一次摘要请求
    compaction_summary_max_chars: int = 6000  # 本地摘要长度上限
    time_context: bool = True           # 每次请求注入时间读数（帮模型理解「今天/等了多久」）
    time_refresh_sec: int = 600         # 时间读数刷新间隔；0 = 每步都注入
    confirm: bool = False              # True 时写/改/删文件、跑命令、联网前需人工确认
    use_color: bool = True
    show_think: bool = True            # 开思考时是否打印思考过程
    no_tools: bool = False             # 老开关：等价 tool_mode="none"（保留给旧脚本/测试）
    # ---- 工具 / 技能启用策略（见 agent/toolpolicy.py）----
    # all=全部 chat=只对话（读文件/读图/联网四件） custom=白名单 none=全关
    tool_mode: str = "all"
    tools_enabled: list[str] = field(default_factory=list)   # custom 模式的白名单
    skills_disabled: list[str] = field(default_factory=list)  # 按技能关闭（与模式正交）
    warm: bool = True                  # 交互模式先预热模型（省掉首回合模型加载等待）
    workspace: str = ""                # 工作空间目录；空 = 进程当前目录

    @property
    def think_value(self):
        return THINK_VALUES[self.think]

    @property
    def resolved_provider(self) -> str:
        """实际生效的协议。provider=auto 时按 host 猜（带 /v1 就是 OpenAI 兼容）。"""
        from agent.providers import OLLAMA, detect_provider
        if self.provider in ("ollama", "openai"):
            return self.provider
        return detect_provider(self.host) if self.host else OLLAMA
