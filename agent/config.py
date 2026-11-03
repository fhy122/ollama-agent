"""全局配置。"""
from __future__ import annotations

from dataclasses import dataclass

DEFAULT_MODEL = "qwen3.5:9b"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_PROVIDER = "ollama"          # ollama | openai，见 agent/providers.py
DEFAULT_CTX = 65536
# num_ctx 上限：ollama 会照单全收这个值并尝试分配 KV cache，
# 填 10**9 只会换来一个难以归因的 OOM。这里按本地大模型的实际天花板截住。
MAX_CTX = 1_048_576
MAX_TOOL_STEPS = 12

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

# 基础行为规则，只写与具体工具无关的约定（模型行为、工作空间、语言风格）。
# 工具的使用策略/适用边界/反例一律写在各工具自己的 description 里——
# description 随 tools 数组每次请求下发，是模型选工具的唯一依据，
# 在系统提示里重复一遍只会白吃 token；工具增删也不会留下幽灵规则。
SYSTEM_PROMPT = (
    "你是一个运行在本地的文件/网络/命令行助手。规则：\n"
    "1. 需要读文件、写文件、删除文件、精确改文件、列目录、搜代码、跑命令、"
    "看图片、读网页、联网搜索时，必须调用工具，不要凭空猜测文件内容。\n"
    "2. 工具结果拿到后继续调用需要的工具，直到信息足够，再给出最终回答。\n"
    "3. 工具返回[错误]或失败时，如实告知失败原因，绝对不要编造工具结果、网页内容或链接。\n"
    "4. 回答用简体中文，直接、紧凑，先结论后细节。\n"
    "5. 读写文件默认在工作空间内进行：用户说的相对路径都相对工作空间；"
    "只有用户明确给出绝对路径时才用绝对路径。写文件前确认路径合理；引用网页时给出链接。\n"
    "6. 用户要求「生成/做/保存/导出 xxx 文件」（如 .svg/.html/.json/.csv/.md/.py）时，"
    "必须用 write_file 把内容写进工作空间并告知保存路径，不要只把代码贴在聊天里让用户自己存，"
    "更不要回答「我无法生成文件」。SVG/HTML/CSS/JS 都是纯文本，write_file 直接就能写。\n"
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
    keep_alive: str = "30m"
    timeout: int = 1800                # 等新数据的上限（非整轮总时长）：只要流还在吐字就一直等
    max_steps: int = MAX_TOOL_STEPS
    stream_retries: int = 3             # 断流/超时自动重试次数上限（0 = 关闭，行为同旧版）
    confirm: bool = False              # True 时写/改/删文件、跑命令、联网前需人工确认
    use_color: bool = True
    show_think: bool = True            # 开思考时是否打印思考过程
    no_tools: bool = False
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
