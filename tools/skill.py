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


def _discover(workspace: Path | None) -> dict[str, dict]:
    """扫描全部技能目录 → {name: {path, desc, display}}，同名先者胜（工作空间优先）。"""
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
            if name in out:
                continue
            out[name] = {
                "path": md,
                "desc": (meta.get("description_zh") or meta.get("description") or "").strip(),
                "display": meta.get("display_name") or name,
            }
    return out


def list_skills(workspace: str | None = None) -> list[dict]:
    """技能清单（给 Web UI / API）：[{name, display, desc}]。"""
    return [
        {"name": n, "display": it["display"], "desc": it["desc"]}
        for n, it in sorted(_discover(Path(workspace) if workspace else None).items())
    ]


def skill_index(workspace: str | None) -> str:
    """给系统提示词的技能索引（名称+短描述）；无技能返回空串。"""
    items = _discover(Path(workspace) if workspace else None)
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
    items = _discover(ctx.workspace)
    if not items:
        return ToolResult(
            "[提示] 未发现任何技能。技能目录："
            f"{ctx.workspace / '.workbuddy' / 'skills'}（工作空间级）、~/.workbuddy/skills（用户级）")

    if not (name or "").strip():
        lines = [f"共 {len(items)} 个技能（skill(name=\"...\") 加载全文）："]
        for n, it in sorted(items.items()):
            d = re.sub(r"\s+", " ", it["desc"])
            lines.append(f"- {n}（{it['display']}）: {d[:80]}{'…' if len(d) > 80 else ''}")
        return ToolResult("\n".join(lines))

    key = name.strip()
    it = items.get(key) or items.get(key.lower())
    if it is None:  # 允许唯一前缀/包含匹配
        cand = [n for n in items if key.lower() in n.lower()]
        if len(cand) == 1:
            it = items[cand[0]]
            key = cand[0]
        else:
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
