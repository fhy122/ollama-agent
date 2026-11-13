"""skill — 按需加载技能指令文档（SKILL.md）。

技能 = 一份 Markdown 工作流指令（WorkBuddy 用户技能 / 工作空间技能）。本工具
只做"按需读入"：名称+一句话描述的索引注入系统提示词（见 agent/loop.py），
正文在模型判断任务命中技能时才加载，避免撑爆 32K 上下文。

技能目录（按优先级，同名先者胜）：
  1. {workspace}/.workbuddy/skills/<name>/SKILL.md   工作空间级
  2. ~/.workbuddy/skills/<name>/SKILL.md             用户级
  3. {repo}/skills/<name>/SKILL.md                   内置（随仓库走，优先级最低）

内置目录放最后，是为了让用户在任何工作空间都能用上随仓库发布的技能
（如 coding-standards），同时保留「放一份同名技能到用户目录即可覆盖」的退路。
"""
from __future__ import annotations

import re
from pathlib import Path

from agent.registry import ToolContext, ToolResult, tool

# 技能文档按 WorkBuddy 工具链撰写，本环境无同名工具，给模型一张映射表防跑偏
_TOOL_MAP = (
    "[环境提示] 技能文档中的工具名在本环境的对应关系：Read→read_file、Write→write_file、"
    "Edit→edit_file（精确替换；整文件重写才用 write_file）、Glob→glob_file、Grep→grep_file、"
    "List→list_dir、Bash/Shell→run_command、WebSearch→web_search、"
    "WebFetch/Browser→browse_url、Image→load_image、skill→本工具。"
    "Task/子代理本环境没有：涉及多角色/子任务拆分的步骤，改为由你连续调用工具逐步完成，不要编造子代理。"
)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """极简 frontmatter 解析（零依赖）：--- 包围的 key: value，多行值粗拼一行。"""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head, body = text[3:end], text[end + 4:]
    meta: dict[str, str] = {}
    cur = None
    for ln in head.splitlines():
        if ln[:1] in (" ", "\t") and cur:
            meta[cur] += " " + ln.strip()
            continue
        m = re.match(r"^([\w.-]+):\s*(.*)$", ln)
        if not m:
            continue
        cur = m.group(1)
        val = m.group(2).strip().strip('"').strip("'")
        if val in ("|", ">", "|-", ">-"):  # 块标量：值在后续缩进行
            val = ""
        meta[cur] = val
    return meta, body.lstrip("\n")


def _skill_dirs(workspace: Path | None) -> list[Path]:
    dirs = []
    if workspace:
        dirs.append(Path(workspace) / ".workbuddy" / "skills")
    home = Path.home() / ".workbuddy"
    dirs.append(home / "skills")
    dirs.extend(sorted(home.glob("user-*-personal/skills")))
    # 内置技能：仓库根 skills/（本文件在 <repo>/tools/ 下，故取 parent.parent）。
    # 放最后 → 同名时用户自己的技能先命中，不会被仓库锁死。
    dirs.append(Path(__file__).resolve().parent.parent / "skills")
    return [d for d in dirs if d.is_dir()]


def _discover(workspace: Path | None, disabled=()) -> dict[str, dict]:
    """扫描全部技能目录 → {name: {path, desc, display}}，同名先者胜（工作空间优先）。

    disabled 里的技能直接不出现（用户在「设置 → 工具与技能」里关掉的）：
    从发现阶段就剔掉，索引、清单、加载三条路径自动一致，不用各处再判一遍。
    """
    skip = set(disabled or ())
    out: dict[str, dict] = {}
    for d in _skill_dirs(workspace):
        try:
            subs = sorted(d.iterdir())
        except OSError:
            continue
        for sub in subs:
            md = sub / "SKILL.md"
            if not md.is_file():
                continue
            try:
                meta, _ = _parse_frontmatter(md.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                continue
            name = meta.get("name") or sub.name
            if name in out or name in skip:
                continue
            out[name] = {
                "path": md,
                "desc": (meta.get("description_zh") or meta.get("description") or "").strip(),
                "display": meta.get("display_name") or name,
            }
    return out


def list_skills(workspace: str | None = None, disabled=()) -> list[dict]:
    """技能清单（给 Web UI / API）：[{name, display, desc}]。

    disabled 默认空 → 清单包含全部（界面要能看见、能重新打开）；
    要「当前可见的」就传进去，与索引一致。
    """
    return [
        {"name": n, "display": it["display"], "desc": it["desc"]}
        for n, it in sorted(_discover(Path(workspace) if workspace else None,
                                      disabled).items())
    ]


def skill_index(workspace: str | None, disabled=()) -> str:
    """给系统提示词的技能索引（名称+短描述）；无技能返回空串。"""
    items = _discover(Path(workspace) if workspace else None, disabled)
    if not items:
        return ""
    lines = ["可用技能（任务命中技能主题时，先调用 skill(name=\"...\") 加载指令全文再执行）："]
    for name, it in sorted(items.items()):
        d = re.sub(r"\s+", " ", it["desc"])
        lines.append(f"- {name}: {d[:60]}{'…' if len(d) > 60 else ''}")
    return "\n".join(lines)


@tool(
    name="skill",
    description=(
        "加载技能（SKILL.md 工作流指令文档）全文。当用户任务命中系统提示词"
        "「可用技能」列表中某技能的主题时，必须先加载该技能、再按指令执行；"
        "不带 name 调用则返回技能清单。"),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "技能名（见系统提示词「可用技能」）；留空=列出全部技能",
            },
            "max_chars": {
                "type": "integer",
                "description": "正文最大字符数，默认 20000（防撑爆上下文）",
            },
        },
    },
)
def skill(ctx: ToolContext, name: str = "", max_chars: int = 20000) -> ToolResult:
    """加载技能全文。被用户关掉的技能不出现，显式点名时给出明确原因。"""
    from agent import toolpolicy

    disabled = toolpolicy.disabled_skills(getattr(ctx, "cfg", None))
    items = _discover(ctx.workspace, disabled)
    if not items:
        hint = ("；当前所有技能都被关掉了（设置 → 工具与技能 可重新打开）"
                if disabled else "")
        return ToolResult(
            "[提示] 未发现任何技能。技能目录："
            f"{ctx.workspace / '.workbuddy' / 'skills'}（工作空间级）、~/.workbuddy/skills（用户级）"
            + hint)

    if not (name or "").strip():
        lines = [f"共 {len(items)} 个技能（skill(name=\"...\") 加载全文）："]
        for n, it in sorted(items.items()):
            d = re.sub(r"\s+", " ", it["desc"])
            lines.append(f"- {n}（{it['display']}）: {d[:80]}{'…' if len(d) > 80 else ''}")
        if disabled:
            lines.append(f"（另有 {len(disabled)} 个技能被关闭，未列出）")
        return ToolResult("\n".join(lines))

    key = name.strip()
    it = items.get(key) or items.get(key.lower())
    if it is None:  # 允许唯一前缀/包含匹配
        cand = [n for n in items if key.lower() in n.lower()]
        if len(cand) == 1:
            it = items[cand[0]]
            key = cand[0]
        else:
            # 点名的正好是被关掉的那个：说清是「关了」而不是「不存在」，
            # 否则用户会以为技能丢了，去翻目录也找不到问题。
            hit = [n for n in disabled if key.lower() in n.lower()]
            if hit:
                return ToolResult(
                    f"[已禁用] 技能 {hit[0]} 已被关闭。要使用请在「设置 → 工具与技能」"
                    "里重新勾选，或用 /tools on skill 类的入口打开。")
            return ToolResult(f"[错误] 未找到技能 {name!r}。可用: {', '.join(sorted(items))}")

    try:
        _, body = _parse_frontmatter(it["path"].read_text(encoding="utf-8", errors="replace"))
    except OSError as e:
        return ToolResult(f"[错误] 读取技能失败: {e}")

    cap = max(500, int(max_chars or 20000))
    if len(body) > cap:
        head = body[:cap]
        tail = (f"\n\n[已截断] 全文 {len(body)} 字符，此处仅前 {cap}。"
                f"续读用 read_file(path=\"{it['path']}\")（按行取后续部分）。")
    else:
        head, tail = body, ""
    return ToolResult(f"[技能: {key}]\n{head}{tail}\n\n{_TOOL_MAP}")
