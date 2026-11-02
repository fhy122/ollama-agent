"""全局配置。"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass

DEFAULT_MODEL = "qwen3.5:9b"
DEFAULT_HOST = "http://localhost:11434"
DEFAULT_CTX = 32768
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

SYSTEM_PROMPT = (
    "你是一个运行在本地的文件/网络/命令行助手。规则：\n"
    "1. 需要读文件、写文件、删除文件、精确改文件、列目录、搜代码、跑命令、"
    "看图片、读网页、联网搜索时，必须调用工具，不要凭空猜测文件内容。\n"
    "2. 工具结果拿到后继续调用需要的工具，直到信息足够，再给出最终回答。"
    "改代码的闭环：grep_file/glob_file 定位 → read_file 读懂 → edit_file 修改 → run_command 跑测试验证。\n"
    "3. 工具返回[错误]或失败时，如实告知失败原因，绝对不要编造工具结果、网页内容或链接。\n"
    "4. 回答用简体中文，直接、紧凑，先结论后细节。\n"
    "5. 读写文件默认在工作空间内进行：用户说的相对路径都相对工作空间；"
    "只有用户明确给出绝对路径时才用绝对路径。写文件前确认路径合理；引用网页时给出链接。\n"
    "6. 删除文件不可恢复：只在用户明确要求删除时调用 delete_file，删前复述要删的路径；"
    "非空目录必须传 recursive=true。\n"
    "7. 改文件优先 edit_file（old_string 与原文完全一致且唯一），整文件重写才用 write_file。"
    "run_command 的破坏性命令（删数据、git push --force 等）只在用户明确要求时执行。\n"
    "8. 生成 Excel（write_file 的 .xlsx）时 content 用 JSON 二维数组："
    '[[\"列1\",\"列2\"],[\"值1\",\"值2\"]]，首行表头；'
    "每行列数必须与表头一致，缺值用空字符串占位，绝不为省事少写整列（否则整行错位）。"
    "同一分组下有多条记录时，每条都写全各列（重复分组值或留空串），不要合并成一行。"
    "字段按语义归列（邮箱进邮箱列、电话进联系人列），不要照原文出现顺序硬搬。\n"
    "9. load_image 只能加载图片（png/jpg/webp/gif/bmp）。用户说\"打开/看一下 Excel、Word、"
    "PPT、PDF、CSV\"时一律用 read_file 解析文字内容，绝不要用 load_image —— "
    "文档不是图片，硬当图片加载会让模型直接报错。用户要「看表格长什么样/截个屏/"
    "检查排版对不对」时，用 render_sheet（xlsx/xlsm/csv/tsv 直接渲染成图片给视觉模型看），"
    "不要自己写 python 画图。\n"
)

# 外脑 web_chat 是可选工具（私有模块，公开仓库不含），注册了才写进系统提示
if importlib.util.find_spec("tools.web_chat") is not None:
    SYSTEM_PROMPT += (
        "10. 外脑（web_chat）是你的分析顾问：它看不到你的本地文件、不能执行任何命令，"
        "但你可以先在本地读完，把片段发给它，拿回建议再落地。以下是「主动使用」的场景，"
        "不用先问用户要不要用：用户说\"用外脑/问问外脑/让豆包或文心看看\"；"
        "或者任务需要长文理解、架构设计、疑难 bug 定位、方案对比、代码审查，你自己把握不足时。\n"
        "    用法四步：① 先用本地工具把事实采集齐（read_file/grep_file/glob_file/list_dir）；"
        "② 把背景+具体问题+期望的输出格式写进一个 question，写成自包含的（说清\"这是 XX 项目的代码，"
        "请分析 YY\"），代码片段直接粘进去——这是预期用法，不算泄露；"
        "③ 调 web_chat，等它回答；④ 拿到回答后直接按它的建议继续干，该改的地方立刻本地改并验证，"
        "**不要停下来问用户\"要不要我继续\"**。\n"
        "    底线：密钥、密码、身份证号、手机号这类个人信息先脱敏再发；外脑回答可能过时或编造，"
        "与本地事实冲突时以本地为准，并如实说明分歧。外脑只能给建议，落地永远由本地工具完成。\n"
    )


@dataclass
class AgentConfig:
    host: str = DEFAULT_HOST
    model: str = DEFAULT_MODEL
    think: str = "off"                 # 键名，见 THINK_VALUES
    num_ctx: int | None = DEFAULT_CTX  # None = 不传 num_ctx（用 Ollama 服务端默认）
    temperature: float = 0.7
    keep_alive: str = "30m"
    timeout: int = 1800                # 等新数据的上限（非整轮总时长）：只要流还在吐字就一直等
    max_steps: int = MAX_TOOL_STEPS
    confirm: bool = False              # True 时写/改/删文件、跑命令、联网前需人工确认
    use_color: bool = True
    show_think: bool = True            # 开思考时是否打印思考过程
    no_tools: bool = False
    warm: bool = True                  # 交互模式先预热模型（省掉首回合模型加载等待）
    workspace: str = ""                # 工作空间目录；空 = 进程当前目录

    @property
    def think_value(self):
        return THINK_VALUES[self.think]
