"""工具 / 技能的启用策略：只对话模式、自定义白名单、按技能开关。

为什么单独一层：
    工具 schema 是每次请求的常驻开销（本仓库 19 个工具约 8.7k 字符）。
    纯聊天时这些 schema 白吃上下文，还会让模型时不时想出个工具来调。
    技能索引同理：名字 + 描述常驻系统提示词，只有真要加载正文时才划算。

四种模式（互斥，存在用户配置的 ui 段，不随模型配置切换而重置）：
    all     全部启用 —— 默认，与加这个功能之前的行为完全一致
    chat    只对话 —— 只留「读文件 / 读图 / 联网」四件
    custom  自定义 —— 只启用 tools_enabled 里点名的那些
    none    一个都不启用 —— 等价旧的 --no-tools / cfg.no_tools

技能与 skill 工具同生共死：
    skill 工具被关掉时，系统提示词里的技能索引也不再注入 —— 索引占 token，
    而模型已经没有任何办法加载正文，留着只会让它在回答里编造技能内容。
    skills_disabled 是「按技能」的第二层开关，与上面四种模式正交：
    模式决定「能不能用技能」,它决定「哪几个技能可见」。
"""
from __future__ import annotations

MODE_ALL = "all"
MODE_CHAT = "chat"
MODE_CUSTOM = "custom"
MODE_NONE = "none"
MODES = (MODE_ALL, MODE_CHAT, MODE_CUSTOM, MODE_NONE)

MODE_LABELS = {
    MODE_ALL: "全部工具",
    MODE_CHAT: "只对话",
    MODE_CUSTOM: "自定义",
    MODE_NONE: "纯聊天（无工具）",
}

# 只对话模式保留的四件：读文件 / 读图 / 联网搜索 / 读网页。
# 刻意不含 list_dir、glob_file、grep_file —— 它们是「找文件」而不是「读」，
# 用户要的是省上下文；真要按目录翻，切到「全部」即可，选择权在界面上。
CHAT_TOOLS = ("read_file", "load_image", "web_search", "browse_url")

# 界面分组（按使用场景，不按实现）。未列出的新工具自动落到「其它」——
# 这样「所有工具都可配置」这句话对新加的工具也成立，不需要回来改这张表。
TOOL_GROUPS = (
    ("read", "读文件", ("read_file", "read_long_doc", "list_dir",
                        "glob_file", "grep_file")),
    ("visual", "图片 / 表格", ("load_image", "clipboard_image",
                             "render_sheet", "web_screenshot")),
    ("write", "写入 / 修改", ("write_file", "edit_file", "delete_file")),
    ("web", "联网", ("web_search", "browse_url", "web_chat")),
    ("exec", "执行 / 自检", ("run_command", "verify", "todo_write")),
    ("skill", "技能", ("skill",)),
)
GROUP_OTHER = ("other", "其它")

_GROUP_OF = {name: gid for gid, _label, names in TOOL_GROUPS for name in names}


def parse_spec(raw) -> tuple[str, list[str]]:
    """`--tools` 一类输入 → (模式, 白名单)。

    模式名（all/chat/none/custom）是快捷方式，其余一律当逗号分隔的工具名：
    「想只留几个就写名字」比再加一个 --enable-tools 参数少一次查帮助。
    """
    v = str(raw or "").strip()
    if not v:
        return MODE_ALL, []
    if v.lower() in MODES:
        return v.lower(), []
    return MODE_CUSTOM, split_names(v)


def normalize_mode(raw, default: str = MODE_ALL) -> str:
    """把任意输入收敛成合法模式；认不出就回默认（不猜用户意图）。"""
    m = str(raw or "").strip().lower()
    return m if m in MODES else default


def split_names(raw) -> list[str]:
    """把「逗号/空格分隔的字符串」或列表收敛成去重后的工具名列表。

    手写配置文件、CLI 参数、前端表单三种来源都会走到这里，
    统一处理：去空、去重、保持顺序、剔掉明显不是名字的东西。
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        parts = [p for chunk in raw.replace("，", ",").split(",") for p in chunk.split()]
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(p) for p in raw]
    else:
        return []
    out: list[str] = []
    for p in parts:
        n = str(p).strip()
        if n and n not in out:
            out.append(n)
    return out


def keep_known(names: list[str], known: set[str]) -> list[str]:
    """丢掉注册表里不存在的名字。

    为什么丢：工具可能被删掉或没装（web_chat 是可选私有模块），
    配置里留着一个不存在的名字，界面上会多出一个永远勾不上的条目。
    """
    return [n for n in names if n in known]


def resolve(mode: str, enabled: list[str]) -> set[str] | None:
    """模式 + 白名单 → 允许的工具名集合。

    返回 None 表示「不限制」（全部启用）—— 与「空集合」严格区分：
    空集合是「一个都不启用」，两者在请求里一个下发全部 schema、一个不带 tools 字段。
    """
    m = normalize_mode(mode)
    if m == MODE_ALL:
        return None
    if m == MODE_NONE:
        return set()
    if m == MODE_CHAT:
        return set(CHAT_TOOLS)
    return set(enabled or ())


def mode_of(cfg) -> str:
    """当前生效模式。no_tools 是老开关，视为 none 的同义词（向后兼容）。"""
    if cfg is None:
        return MODE_ALL
    if getattr(cfg, "no_tools", False):
        return MODE_NONE
    return normalize_mode(getattr(cfg, "tool_mode", MODE_ALL))


def allowed_names(cfg) -> set[str] | None:
    if cfg is None:
        return None
    return resolve(mode_of(cfg), split_names(getattr(cfg, "tools_enabled", None)))


def is_allowed(cfg, name: str) -> bool:
    names = allowed_names(cfg)
    return names is None or name in names


def skill_tool_on(cfg) -> bool:
    """skill 工具是否可用 —— 决定技能索引要不要进系统提示词。"""
    return is_allowed(cfg, "skill")


def skill_allowed(cfg, name: str) -> bool:
    """单个技能是否可见（第二层开关，与模式正交）。"""
    if cfg is None:
        return True
    return name not in disabled_skills(cfg)


def disabled_skills(cfg) -> set[str]:
    if cfg is None:
        return set()
    return set(split_names(getattr(cfg, "skills_disabled", None)))


# ---------------- 与注册表对接（延迟导入，避免循环依赖） ----------------

def specs_for(cfg) -> list[dict] | None:
    """本次请求该下发的 tools 数组；None = 不带 tools 字段。"""
    from agent.registry import all_specs

    names = allowed_names(cfg)
    if names is None:
        return all_specs()
    if not names:
        return None
    return all_specs(names)


def catalog(cfg=None) -> list[dict]:
    """给界面/CLI 的工具清单：名称、说明、分组、当前是否启用。"""
    from agent.registry import all_tools

    names = allowed_names(cfg)
    rows = []
    for t in all_tools():
        gid = _GROUP_OF.get(t.name, GROUP_OTHER[0])
        rows.append({
            "name": t.name,
            "description": t.description,
            "group": gid,
            "enabled": names is None or t.name in names,
        })
    return rows


def groups(cfg=None) -> list[dict]:
    """按分组归拢后的工具清单（界面直接渲染，省得前端再排一次）。"""
    rows = catalog(cfg)
    by_gid: dict[str, list[dict]] = {}
    for r in rows:
        by_gid.setdefault(r["group"], []).append(r)
    out = []
    # TOOL_GROUPS 的条目是 (gid, label, 工具名元组)，这里只要前两项；
    # 末尾补上「其它」，让没登记过的新工具也能出现在界面上
    for gid, label in [(g[0], g[1]) for g in TOOL_GROUPS] + [GROUP_OTHER]:
        items = by_gid.get(gid)
        if items:
            out.append({"key": gid, "label": label, "tools": items})
    return out


def enabled_count(cfg) -> int:
    """当前启用的工具数量（顶栏/状态栏显示用）。"""
    from agent.registry import all_tools

    names = allowed_names(cfg)
    if names is None:
        return len(all_tools())
    return sum(1 for t in all_tools() if t.name in names)


def describe(cfg) -> str:
    """一句话描述当前策略（CLI 状态栏 / 界面 chip 用）。"""
    mode = mode_of(cfg)
    if mode == MODE_ALL:
        return "全部"
    if mode == MODE_NONE:
        return "无工具"
    if mode == MODE_CHAT:
        return "只对话"
    enabled = split_names(getattr(cfg, "tools_enabled", None))
    if not enabled:
        return "自定义（空）"
    return f"自定义 {len(enabled)} 个"
