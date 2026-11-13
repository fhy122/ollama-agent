"""命令行入口：参数解析 + 交互 REPL。"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

from agent.backends import create_client
from agent.config import DEFAULT_CTX, DEFAULT_HOST, DEFAULT_MODEL, MAX_TOOL_STEPS, \
    THINK_VALUES, AgentConfig
from agent.console import color, setup_stdio
from agent.loop import new_history, run_turn
from agent.registry import ToolContext
from agent import toolpolicy
from agent.toolpolicy import MODE_CUSTOM, MODES

HELP_TEXT = """命令:
  /                 列出指令与技能（本清单 + 技能库）
  /img <路径>       给下一条消息附图片（可连续多次）
  /paste            把剪贴板内容附到下一条消息（截图→图；Ctrl+C 的文件→路径）
                    （全局热键 Alt+V 效果相同）
  /think <模式>     off|on|low|medium|high|max|auto   （off=默认关思考；auto=不传该字段）
  /model <模型名>   切换模型
  /ctx <整数>       设置 num_ctx（0 = 用 Ollama 服务端默认）。改值会触发模型重载
  /repeat <p> [n]   repeat_penalty [repeat_last_n]；/repeat off = 恢复服务端默认
  /ws [目录]        查看 / 切换工作空间（相对路径读写的根）
  /reset            清空对话历史
  /tools [指令]     工具启停。无参数=查看清单（✓/✗）；all|chat|none 切模式；
                    on|off <工具名> 单个开关（自动进自定义模式）。chat=只对话
                    （只留读文件/读图/联网四件，省上下文）
  /skill <名称> [问题]  加载技能进对话；带问题则加载后直接提问
  /<技能名> [问题]  快捷调用技能，等价 /skill <技能名>
  /save <文件>      保存对话历史为 JSON
  /load <文件>      读入对话历史
  /help             本帮助
  /exit             退出
@ 上下文（把工作空间文件附进对话）:
  "总结 @report 的要点"   @后跟文件名/相对路径，工作空间内模糊匹配；
                          唯一命中自动附上（图片→视觉，其它→模型自己读），
                          多个命中选编号；没命中原文保留
  "@报告.docx"            整条消息只有 @引用 → 附上后按"看一下我附带的内容"发出
普通输入即对话。示例: "读一下 report.docx"；截图后 Alt+V 再问"这张图写了什么" """


# ---------- @ 上下文引用（对齐 WebUI 的 @ 面板语义） ----------
# @ 前不接拉丁字母/数字/下划线：拦住 foo@bar.com 这类邮箱，但允许 "总结@报告" 中文紧邻
AT_RE = re.compile(r"(?<![A-Za-z0-9_])@([^\s@]+)")
SKIP_DIRS = {".uploads", ".clip_cache", ".workbuddy", "__pycache__", ".git", "node_modules"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
PALETTE_FILES = 500     # @ 面板文件候选上限（工作空间扫描）
_TOKEN_TRIM = "，。；：！？、,.!?;:)]}）】》」』\"'"


def find_context_files(ws: Path, q: str, limit: int = 20) -> list[str]:
    """工作空间文件模糊搜索，返回相对路径列表（与 WebUI /api/files 同语义）。"""
    q = (q or "").strip().lower().replace("\\", "/")
    out: list[str] = []
    for p in sorted(ws.rglob("*")):
        if len(out) >= limit:
            break
        try:
            rel = p.relative_to(ws)
        except ValueError:
            continue
        if not p.is_file() or any(part in SKIP_DIRS for part in rel.parts):
            continue
        rel_s = str(rel).replace("\\", "/")
        if not q or q in rel_s.lower():
            out.append(rel_s)
    return out


def _token_queries(tok: str) -> list[str]:
    """@token → 尝试用的匹配串：原串 → 去尾部标点 → 逐个去尾部中文（"@报告的要点"→"报告"）。"""
    qs = [tok]
    t = tok.rstrip(_TOKEN_TRIM)
    if t and t != tok:
        qs.append(t)
    for _ in range(8):
        if len(t) <= 1 or not re.search(r"[一-鿿]$", t):
            break
        t = t[:-1]
        if t:
            qs.append(t)
    return qs


def resolve_at_refs(text: str, workspace: Path, use_color: bool = False,
                    picker=None) -> tuple[str, list[str], list[str]]:
    """解析消息里的 @文件 引用 → (新文本, 图片base64列表, 文件相对路径列表)。

    唯一命中自动附上；多个命中调 picker(候选列表) 返回选定项（None=放弃）；
    picker 缺省（单次模式）时多命中跳过。未命中/放弃 → 原文保留。
    整条消息只有 @引用时文本置空，由调用方补默认提示词。
    """
    images: list[str] = []
    files: list[str] = []
    cache: dict[str, tuple[str, str] | None] = {}   # token -> (选定相对路径, 匹配串) | None
    seen: set[str] = set()                          # 已附路径，去重
    out: list[str] = []
    only_refs = True        # 整条消息是否只有 @引用
    resolved_any = False
    last = 0

    for m in AT_RE.finditer(text):
        seg = text[last:m.start()]
        if seg.strip():
            only_refs = False
        out.append(seg)
        tok = m.group(1)

        if tok not in cache:
            chosen: str | None = None
            matched_q = tok
            cands: list[str] = []
            for q in _token_queries(tok):
                cands = find_context_files(workspace, q)
                if cands:
                    matched_q = q
                    break
            if cands:
                # 精确相对路径 / 精确文件名优先
                exact = [c for c in cands if c.lower() == matched_q.lower()] or \
                        [c for c in cands if c.rsplit("/", 1)[-1].lower() == matched_q.lower()]
                if exact:
                    cands = exact
                if len(cands) == 1:
                    chosen = cands[0]
                elif len(cands) > 1:
                    chosen = picker(cands) if picker else None
                    if chosen is None:
                        print(color(f"[提示] @{tok} 匹配 {len(cands)} 个文件，未附上: "
                                    + ", ".join(cands[:5]), "yellow", use_color), flush=True)
            else:
                print(color(f"[提示] 未找到匹配 @{tok} 的文件（原文保留）",
                            "yellow", use_color), flush=True)
            cache[tok] = (chosen, matched_q) if chosen else None

        hit = cache[tok]
        if not hit:
            out.append(m.group(0))   # 未命中/放弃：原文保留
            only_refs = False
            last = m.end()
            continue

        chosen, matched_q = hit
        try:
            if chosen not in seen:
                p = workspace / chosen
                if p.suffix.lower() in IMG_EXT:
                    images.append(base64.b64encode(p.read_bytes()).decode("ascii"))
                else:
                    files.append(chosen)
                seen.add(chosen)
            resolved_any = True
        except OSError as e:
            print(color(f"[警告] 读取 {chosen} 失败: {e}", "yellow", use_color), flush=True)
            out.append(m.group(0))
            only_refs = False
            last = m.end()
            continue
        # 用相对路径替换 @token，尾部没匹配上的部分保留（"@报告的要点"→"报告.md的要点"）
        out.append(chosen + tok[len(matched_q):])
        last = m.end()

    tail = text[last:]
    if tail.strip():
        only_refs = False
    out.append(tail)

    new_text = "".join(out)
    if resolved_any and only_refs:
        new_text = ""   # 整条消息只有 @引用 → 交由调用方补默认提示
    return new_text, images, files


def _match_skills(workspace: Path, query: str) -> list[dict]:
    """按名称/显示名找技能：精确优先，其次子串。"""
    try:
        from tools.skill import list_skills
        items = list_skills(str(workspace))
    except Exception:
        return []
    q = query.strip().lower()
    if not q:
        return []
    exact = [s for s in items if q == s["name"].lower() or q == s["display"].lower()]
    return exact or [s for s in items
                     if q in s["name"].lower() or q in s["display"].lower()]


# Key 从环境变量读，避免写进命令行被 shell history 记下来。
# 按顺序取第一个非空的；专用名放前面，通用名兜底。
API_KEY_ENVS = ("AI_GATE_HUB_KEY", "OLLAMA_AGENT_API_KEY", "OPENAI_API_KEY")


def _api_key_from_env(explicit: str = "") -> str:
    if explicit:
        return explicit.strip()
    for name in API_KEY_ENVS:
        v = (os.environ.get(name) or "").strip()
        if v:
            return v
    return ""


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="ollama_agent",
        description="极简本地 Agent（Ollama / OpenAI 兼容后端 + 工具插件化）")
    ap.add_argument("--host", default=DEFAULT_HOST,
                    help=f"后端地址，默认 {DEFAULT_HOST}；接 OpenAI 兼容的模型服务端时填 https://xxx/v1")
    ap.add_argument("--provider", default="auto", choices=["auto", "ollama", "openai"],
                    help="后端协议：ollama=/api/chat；openai=/v1/chat/completions；"
                         "auto=按 host 猜（带 /v1 判为 openai），默认 auto")
    ap.add_argument("--api-key", default="",
                    help="API Key（provider=openai 需要；也可用环境变量 "
                         "OLLAMA_AGENT_API_KEY / AI_GATE_HUB_KEY）")
    ap.add_argument("--model", default=DEFAULT_MODEL, help=f"模型名，默认 {DEFAULT_MODEL}")
    ap.add_argument("--think", default="off", choices=list(THINK_VALUES),
                    help="思考模式，默认 off（think=false）。auto=不传 think 字段")
    ap.add_argument("--ctx", type=int, default=DEFAULT_CTX,
                    help=f"num_ctx，默认 {DEFAULT_CTX}；0=用服务端默认（注意：改 ctx 会触发模型重载）")
    ap.add_argument("--temp", type=float, default=0.7, help="temperature，默认 0.7")
    ap.add_argument("--repeat-penalty", type=float, default=None,
                    help="options.repeat_penalty：抑制文字复读，建议从 1.1 试起；"
                         "留空=不下发（用服务端默认）。仅 Ollama 原生协议生效")
    ap.add_argument("--repeat-last-n", type=int, default=None,
                    help="options.repeat_last_n：回看多长的窗口，默认 64；"
                         "0=禁用、-1=num_ctx。留空=不下发。仅 Ollama 原生协议生效")
    ap.add_argument("--img", action="append", default=[],
                    help="给第一条消息附图片，可多次")
    ap.add_argument("-p", "--prompt", help="单次执行（非交互）")
    ap.add_argument("--workspace", "-w", default=None,
                    help="工作空间目录：相对路径读写的根，默认当前目录")
    ap.add_argument("--max-steps", type=int, default=MAX_TOOL_STEPS,
                    help=f"单回合最大工具步数，默认 {MAX_TOOL_STEPS}")
    ap.add_argument("--no-tools", action="store_true", help="禁用全部工具，纯聊天")
    ap.add_argument("--tools", default=None, metavar="MODE|名称,...",
                    help="工具启用策略：all=全部（默认）/ chat=只对话（读文件、读图、"
                         "联网四件）/ none=全关 / 逗号分隔的工具名=自定义白名单。"
                         "例：--tools chat；--tools read_file,web_search")
    ap.add_argument("--confirm", action="store_true", help="写/改/删文件、跑命令、联网前需要确认")
    ap.add_argument("--no-color", action="store_true", help="关闭彩色输出")
    ap.add_argument("--hide-think", action="store_true", help="开思考时也不打印思考过程")
    ap.add_argument("--no-warm", action="store_true",
                    help="跳过启动时的模型预热（交互模式与 Web UI 都适用）")
    ap.add_argument("--no-palette", action="store_true",
                    help="关闭 / 与 @ 的行内选择面板，回退为普通输入")
    ap.add_argument("--no-hotkey", action="store_true",
                    help="不注册全局热键 Alt+V（剪贴板进对话）")
    ap.add_argument("--ui", action="store_true",
                    help="启动 Web UI（浏览器聊天界面）")
    ap.add_argument("--port", type=int, default=8888, help="Web UI 端口，默认 8888")
    return ap


def load_images(paths: list[str], use_color: bool) -> list[str]:
    out = []
    for p in paths:
        path = Path(p).expanduser()
        if path.exists():
            out.append(base64.b64encode(path.read_bytes()).decode("ascii"))
        else:
            print(color(f"[警告] 图片不存在: {p}", "yellow", use_color), flush=True)
    return out


def make_context(cfg: AgentConfig, workspace: Path, client=None) -> ToolContext:
    def ask(what: str) -> bool:
        ans = input(color(f"? 允许执行 {what} ? [y/N] ", "yellow", cfg.use_color)).strip().lower()
        return ans in ("y", "yes")

    def ask_choice(question: str, options: list, allow_free_text: bool) -> str:
        """ask_user 的终端版：列编号让人挑，也可以直接写一句。

        输入到 EOF（管道/重定向）当「没答复」返回空串，别让工具线程卡死。
        """
        print(color(f"? {question}", "yellow", cfg.use_color), flush=True)
        for i, opt in enumerate(options, 1):
            print(f"  {i}) {opt}", flush=True)
        hint = "输入序号" + ("，或直接写你的答案" if allow_free_text else "")
        try:
            ans = input(f"  [{hint}] ").strip()
        except EOFError:
            return ""
        if not ans:
            return ""
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return str(options[int(ans) - 1])
        return ans if allow_free_text else ""

    def notice(text: str) -> None:
        print(color(text, "yellow", cfg.use_color), flush=True)

    return ToolContext(confirm=cfg.confirm, use_color=cfg.use_color, ask=ask,
                       ask_choice=ask_choice,
                       workspace=workspace, cfg=cfg, client=client,
                       notice=notice)


def parse_tools_arg(raw: str | None) -> tuple[str, list[str]]:
    """`--tools` 取值 → (模式, 白名单)。规则见 agent.toolpolicy.parse_spec。"""
    return toolpolicy.parse_spec(raw)


def _sync_ctx(ctx: ToolContext | None, cfg: AgentConfig) -> None:
    """把新策略同步到执行上下文。

    dispatch 是按 ctx.tools_allowed 拦截的（防模型硬调已关掉的工具）。
    这里不刷新的话，/tools off skill 之后同一会话里还能直接 /skill 加载成功 —— 
    状态两处不一致，用户会觉得开关没生效。
    """
    if ctx is not None:
        ctx.tools_allowed = toolpolicy.allowed_names(cfg)


def refresh_system_prompt(history: list, workspace: Path, cfg: AgentConfig) -> None:
    """只替换 system 消息（技能索引在里面），对话内容原样保留。

    策略改了却整条重建历史 = 把用户聊了半天的话清空，代价太大；
    但什么都不做的话，技能索引会一直停在旧状态。
    """
    fresh = new_history(str(workspace), cfg)[0]
    if history and history[0].get("role") == "system":
        history[0] = fresh
    else:
        history.insert(0, fresh)


def tools_command(arg: str, cfg: AgentConfig, history: list, workspace: Path,
                  use_color: bool, ctx: ToolContext | None = None) -> None:
    """/tools —— 查看工具启用状态，或切换模式 / 单个工具。

    语法：
      /tools                    列出全部工具（✓ 启用 / ✗ 关闭）
      /tools all|chat|none      整体切模式
      /tools on  <名字>         打开一个（自动切到自定义模式）
      /tools off <名字>         关掉一个（自动切到自定义模式）
    """
    parts = (arg or "").split()
    import tools as tools_pkg
    names = {t.name for t in tools_pkg.all_tools()}

    if not parts:
        print(color(f"工具策略：{toolpolicy.describe(cfg)}"
                    f"（{toolpolicy.enabled_count(cfg)}/{len(names)} 启用）",
                    "bold", use_color), flush=True)
        for g in toolpolicy.groups(cfg):
            print(color(f"  [{g['label']}]", "dim", use_color), flush=True)
            for t in g["tools"]:
                mark = "✓" if t["enabled"] else "✗"
                print(f"    {mark} {t['name']:<14}", flush=True)
        print(color("  /tools chat   只对话（读文件/读图/联网四件）\n"
                    "  /tools all    全部启用\n"
                    "  /tools none   全关，纯聊天\n"
                    "  /tools off read_long_doc（同理 on）", "dim", use_color),
              flush=True)
        return

    head = parts[0].lower()
    if head in MODES and len(parts) == 1:
        cfg.tool_mode = head
        cfg.no_tools = head == "none"     # 老开关同步，免得两处状态打架
        _sync_ctx(ctx, cfg)
        refresh_system_prompt(history, workspace, cfg)
        print(f"工具策略 = {toolpolicy.describe(cfg)}"
              f"（{toolpolicy.enabled_count(cfg)}/{len(names)} 启用，下一条消息生效）",
              flush=True)
        return

    if head in ("on", "off") and len(parts) >= 2:
        name = parts[1].strip()
        if name not in names:
            near = [n for n in names if name.lower() in n.lower()]
            hint = f"（是不是想写 {', '.join(near)}？）" if near else ""
            print(f"[错误] 没有这个工具: {name}{hint}", flush=True)
            print("       用 /tools 看全部工具名", flush=True)
            return
        # 基线用「当前生效的集合」，不是白名单本身：
        # 在「全部」模式下 off 一个，应该得到「19 减 1」，而不是「一个都不剩」
        # （白名单此时是空的，直接拿它当基线会把别的工具一起关掉）。
        base = toolpolicy.allowed_names(cfg)
        cur = set(base) if base is not None else set(names)
        if head == "on":
            cur.add(name)
        else:
            cur.discard(name)
        cfg.tool_mode = MODE_CUSTOM
        cfg.no_tools = False
        cfg.tools_enabled = sorted(cur)
        _sync_ctx(ctx, cfg)
        refresh_system_prompt(history, workspace, cfg)
        state = "启用" if head == "on" else "关闭"
        print(f"已{state} {name}；当前 {toolpolicy.describe(cfg)}"
              f"（{toolpolicy.enabled_count(cfg)}/{len(names)} 启用）", flush=True)
        return

    print("用法: /tools | /tools all|chat|none | /tools on|off <工具名>", flush=True)


def main(argv: list[str] | None = None):
    setup_stdio()
    args = build_parser().parse_args(argv)
    use_color = not args.no_color and sys.stdout.isatty()
    tool_mode, tool_names = parse_tools_arg(args.tools)

    cfg = AgentConfig(
        host=args.host,
        provider=args.provider,
        api_key=_api_key_from_env(args.api_key),
        model=args.model,
        think=args.think,
        num_ctx=(args.ctx if args.ctx > 0 else None),
        temperature=args.temp,
        repeat_penalty=args.repeat_penalty,
        repeat_last_n=args.repeat_last_n,
        max_steps=args.max_steps,
        confirm=args.confirm,
        use_color=use_color,
        show_think=not args.hide_think,
        no_tools=args.no_tools and args.tools is None,
        tool_mode=tool_mode,
        tools_enabled=tool_names,
        warm=not args.no_warm,
        workspace=str(Path(args.workspace).expanduser().resolve()) if args.workspace else "",
    )
    # 显式 --model 高于「上次选的模型」（Web UI 启动路径也要这个信息）
    cfg._model_explicit = args.model != DEFAULT_MODEL
    workspace = Path(cfg.workspace) if cfg.workspace else Path.cwd()
    if not workspace.is_dir():
        print(color(f"[错误] 工作空间不是目录: {workspace}", "red", use_color), flush=True)
        sys.exit(1)

    # ---------- Web UI 模式 ----------
    if args.ui:
        from webui.server import WebUIApp
        app = WebUIApp(cfg, port=args.port)
        if args.no_hotkey:
            app.start_hotkey = lambda: False  # type: ignore
        app.serve(open_browser=True)
        return

    client = create_client(cfg)
    ctx = make_context(cfg, workspace, client)
    history = new_history(str(workspace), cfg)

    print(color(f"{client.name} | 模型 {cfg.model} | think={cfg.think} "
                f"| ctx={cfg.num_ctx or '服务端默认'}"
                f" | repeat_penalty={cfg.repeat_penalty if cfg.repeat_penalty is not None else '服务端默认'}",
                "bold", use_color), flush=True)
    print(color(f"工作空间: {workspace}", "dim", use_color), flush=True)

    try:
        models = client.list_models()
        if cfg.model not in models:
            print(color(f"[警告] 本地未发现 {cfg.model}，已装: {', '.join(models[:10])}",
                        "yellow", use_color), flush=True)
    except Exception as e:
        print(color(f"[警告] 无法列出模型: {e}", "yellow", use_color), flush=True)

    # ---------- 单次模式 ----------
    if args.prompt:
        imgs = load_images(args.img, use_color)
        text, at_imgs, at_files = resolve_at_refs(args.prompt, workspace, use_color)
        imgs = imgs + at_imgs
        send_text = text or "看一下我附带的内容"
        if at_files:
            listing = "\n".join(f"- {p}" for p in at_files)
            send_text += (f"\n\n[我附带的文件：短的用 read_file 读；很长（几十万字/几百页）的必须用 read_long_doc 分段精读]\n{listing}")
        try:
            run_turn(client, cfg, ctx, history, send_text, imgs or None)
        except KeyboardInterrupt:
            print(color("\n[⏹ 已打断]", "yellow", use_color), flush=True)
            raise SystemExit(130)
        except Exception as e:
            print(color(f"[错误] {type(e).__name__}: {e}", "red", use_color), flush=True)
            raise SystemExit(1)
        return

    # ---------- 交互模式 ----------
    if cfg.warm:
        print(color("预热模型中（首次/换 ctx 后较慢，之后秒级）…", "dim", use_color), flush=True)
        try:
            dt = client.warmup()
            print(color(f"模型就绪（{dt:.1f}s）", "dim", use_color), flush=True)
        except Exception as e:
            print(color(f"[警告] 预热失败: {e}", "yellow", use_color), flush=True)

    import tools as tools_pkg
    _on = toolpolicy.enabled_count(cfg)
    print(color(f"工具 {_on}/{len(tools_pkg.all_tools())} 个启用"
                f"（{toolpolicy.describe(cfg)}），/tools 查看或切换。"
                f"输入 /help 看命令。", "dim", use_color), flush=True)

    pending_images: list[str] = []   # base64，随下一条消息附上
    pending_img_paths: list[str] = []  # 与 pending_images 一一对应的相对路径（取消用）
    pending_files: list[str] = []    # 文件路径，随下一条消息注入文本让模型自己读

    # ---- 行内选择面板（对齐 WebUI：/ 指令与技能、@ 上下文文件）----
    from agent.palette import _enable_vt
    from agent.palette import read_line as palette_read_line
    palette_on = (not args.no_palette) and os.name == "nt" and sys.stdin.isatty()
    if palette_on and not _enable_vt():
        palette_on = False
        print(color("[提示] 当前控制台不支持 ANSI 光标控制，/ 与 @ 面板已关闭"
                    "（普通输入照常可用）", "yellow", use_color), flush=True)
    file_cache: dict = {"ws": None, "t": 0.0, "files": []}

    def palette_files() -> list[str]:
        """工作空间文件清单（给 @ 面板前缀匹配），5 秒内复用缓存。"""
        now = time.monotonic()
        ws = ctx.workspace
        if file_cache["ws"] != ws or now - file_cache["t"] > 5:
            try:
                file_cache["files"] = find_context_files(ws, "", limit=PALETTE_FILES)
            except Exception:
                file_cache["files"] = []
            file_cache["ws"], file_cache["t"] = ws, now
        return file_cache["files"]

    def palette_skills() -> list[tuple[str, str, str]]:
        try:
            from tools.skill import list_skills
            return [(s["name"], s["display"], s.get("desc", ""))
                    for s in list_skills(str(ctx.workspace))]
        except Exception:
            return []

    def sync_attach(paths: list[str]) -> None:
        """以面板返回的完整待发列表为准：新选的读进来，被取消的移除。

        面板里再选一次已附加的文件 = 取消（选错了能反悔），所以这里必须做双向同步，
        不能只做追加。
        """
        for rel in paths:
            if rel in pending_files or rel in pending_img_paths:
                continue
            p = ctx.workspace / rel
            try:
                if p.suffix.lower() in IMG_EXT:
                    pending_img_paths.append(rel)
                    pending_images.append(base64.b64encode(p.read_bytes()).decode("ascii"))
                    print(color(f"已附加图片 {rel}（待发 {len(pending_images)} 张）",
                                "dim", use_color), flush=True)
                else:
                    pending_files.append(rel)
                    print(color(f"已附加文件 {rel}（待发 {len(pending_files)} 个）",
                                "dim", use_color), flush=True)
            except OSError as e:
                print(color(f"[警告] 读取 {rel} 失败: {e}", "yellow", use_color), flush=True)
        for rel in list(pending_files):
            if rel not in paths:
                pending_files.remove(rel)
                print(color(f"已取消附加 {rel}（待发 {len(pending_files)} 个）",
                            "dim", use_color), flush=True)
        for rel in list(pending_img_paths):
            if rel not in paths:
                i = pending_img_paths.index(rel)
                pending_img_paths.pop(i)
                pending_images.pop(i)
                print(color(f"已取消附加 {rel}（待发 {len(pending_images)} 张图）",
                            "dim", use_color), flush=True)

    def pick_file(cands: list[str]) -> str | None:
        """@引用多个命中时交互选一个（回车跳过）。"""
        print("匹配到多个文件:", flush=True)
        for i, c in enumerate(cands, 1):
            print(f"  {i}) {c}", flush=True)
        try:
            ans = input("选编号（回车跳过）: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None
        return cands[int(ans) - 1] if ans.isdigit() and 1 <= int(ans) <= len(cands) else None

    def do_paste() -> None:
        """抓剪贴板：文件→挂路径（模型自己读），截图→挂图（视觉）。"""
        try:
            from agent.clipboard import grab_any
            files, img_path = grab_any(workspace)
        except Exception as e:
            print(color(f"[错误] {e}", "red", use_color), flush=True)
            return
        if files:
            pending_files.extend(str(f) for f in files)
            names = ", ".join(f.name for f in files)
            print(f"已附加剪贴板文件 {len(files)} 个: {names}（待发 {len(pending_files)} 个文件）",
                  flush=True)
        if img_path:
            pending_img_paths.append(str(img_path))
            pending_images.append(base64.b64encode(img_path.read_bytes()).decode("ascii"))
            print(f"已附加剪贴板截图 → 缓存 {img_path}（待发 {len(pending_images)} 张）", flush=True)
        if not files and not img_path:
            print("剪贴板里没有图片或文件（截图后按 Alt+V / /paste）", flush=True)

    # 全局热键 Alt+V：随时把剪贴板内容带进对话（需 Windows）
    hotkey = None
    if not args.no_hotkey:
        try:
            from agent import hotkeys as hk
            if hk.is_supported():
                hotkey = hk.HotkeyListener(do_paste)
                if hotkey.start():
                    print(color(f"全局热键 {hk.hotkey_label()}: 剪贴板图片/文件直接进对话"
                                f"（--no-hotkey 关闭）", "dim", use_color), flush=True)
                else:
                    print(color("[警告] 热键注册失败（可能被其它程序占用），可用 /paste 代替",
                                "yellow", use_color), flush=True)
                    hotkey = None
        except Exception as e:
            print(color(f"[警告] 热键不可用: {e}", "yellow", use_color), flush=True)

    def do_skill(name: str, question: str) -> str | None:
        """加载技能进对话历史。返回要继续发给模型的文本；None = 本命令到此为止。"""
        r = tools_pkg.dispatch("skill", {"name": name} if name else {}, ctx)
        if not name or r.text.startswith("[错误]"):
            print(r.text, flush=True)   # 列表 / 错误直接打印
            return None
        history.append({
            "role": "user",
            "content": f"[技能指令已加载，后续回答请严格遵循此技能]\n{r.text}",
        })
        print(f"技能 {name} 已注入对话（{len(r.text)} 字符）", flush=True)
        if question:
            return question
        return "看一下我附带的内容" if (pending_images or pending_files) else None

    while True:
        try:
            if palette_on:
                res = palette_read_line(color("\n你> ", "bold", use_color),
                                        palette_files(), palette_skills(), use_color,
                                        attached=pending_files + pending_img_paths)
                line = res.text.strip()
                sync_attach(res.picked)      # 双向同步：新选的加上，取消的去掉
            else:
                line = input(color("\n你> ", "bold", use_color)).strip()
        except (EOFError, KeyboardInterrupt):
            print(flush=True)
            break
        if not line:
            if pending_images or pending_files:
                # 有待发附件时只提示、不代发（否则刚选完文件一回车消息就飞出去了）
                print(color(f"  待发 {len(pending_images)} 张图 / {len(pending_files)} 个文件"
                            " —— 接着写你的问题再回车；空行按退格逐个移除附件，"
                            "@ 可增删", "dim", use_color), flush=True)
                continue
            continue

        forward: str | None = None
        if line == "@":
            # 对齐 WebUI 的 @ 面板：列工作空间文件（模糊搜: @readme 会随消息发，这里只浏览）
            fs = find_context_files(workspace, "")
            print("工作空间文件（@名称 附进消息，最多显示 20 个）:", flush=True)
            for f in fs:
                print(f"  {f}", flush=True)
            if not fs:
                print("  （空）", flush=True)
            continue
        if line.startswith("/"):
            parts = line.split(maxsplit=1)
            cmd, arg = parts[0].lower(), (parts[1].strip() if len(parts) > 1 else "")
            if cmd in ("/exit", "/quit", "/q"):
                break
            elif cmd == "/":
                # 指令 + 技能一览（对齐 WebUI 的 / 面板）
                print(HELP_TEXT, flush=True)
                r = tools_pkg.dispatch("skill", {}, ctx)
                print("\n" + r.text, flush=True)
            elif cmd == "/help":
                print(HELP_TEXT, flush=True)
            elif cmd == "/img":
                p = Path(arg).expanduser()
                if not p.exists():
                    print(color(f"图片不存在: {p}", "red", use_color), flush=True)
                else:
                    pending_img_paths.append(str(p))
                    pending_images.append(base64.b64encode(p.read_bytes()).decode("ascii"))
                    print(f"已附加图片 {p}（待发 {len(pending_images)} 张）", flush=True)
            elif cmd == "/paste":
                do_paste()
            elif cmd == "/think":
                key = arg.lower() or "off"
                if key not in THINK_VALUES:
                    print("可选: off|on|low|medium|high|max|auto", flush=True)
                else:
                    cfg.think = key
                    client.think = THINK_VALUES[key]
                    print(f"think = {key}", flush=True)
            elif cmd == "/model":
                if arg:
                    old = client.model
                    released = client.switch_model(arg)
                    if old == arg:
                        print(f"模型已经是 {arg}", flush=True)
                    else:
                        note = "旧模型已从显存卸掉" if released else "旧模型没占着（无需卸载）"
                        print(f"模型 {old} → {arg}，{note}", flush=True)
                        print(color(" 首次调用要加载新模型，可能要等一会儿",
                                    "dim", use_color), flush=True)
                else:
                    print(f"当前模型: {client.model}", flush=True)
            elif cmd == "/ctx":
                try:
                    n = int(arg)
                    cfg.num_ctx = n if n > 0 else None
                    client.num_ctx = cfg.num_ctx
                    print(f"num_ctx = {cfg.num_ctx or '服务端默认'}（下次调用生效，可能触发重载）",
                          flush=True)
                except ValueError:
                    print("用法: /ctx 65536（0=服务端默认）", flush=True)
            elif cmd == "/repeat":
                from agent.config import parse_repeat_last_n, parse_repeat_penalty
                parts = arg.split()
                if not parts or parts[0].lower() in ("off", "default"):
                    cfg.repeat_penalty = None
                    cfg.repeat_last_n = None
                    client.repeat_penalty = None
                    client.repeat_last_n = None
                    print("repeat_penalty / repeat_last_n = 服务端默认（不下发）", flush=True)
                else:
                    p = parse_repeat_penalty(parts[0])
                    n = parse_repeat_last_n(parts[1]) if len(parts) > 1 else cfg.repeat_last_n
                    if p is None or (len(parts) > 1 and n is None):
                        print("用法: /repeat 1.1 [256]；/repeat off = 恢复服务端默认。"
                              "repeat_last_n 可填 0（禁用）或 -1（整个上下文）", flush=True)
                    else:
                        cfg.repeat_penalty = p
                        cfg.repeat_last_n = n
                        client.repeat_penalty = p
                        client.repeat_last_n = n
                        print(f"repeat_penalty = {p}，repeat_last_n = "
                              f"{n if n is not None else '服务端默认'}（下次请求生效，不会触发模型重载）",
                              flush=True)
            elif cmd == "/ws":
                if not arg:
                    print(f"工作空间: {ctx.workspace}", flush=True)
                else:
                    p = Path(arg).expanduser().resolve()
                    if not p.is_dir():
                        print(f"[错误] 不是目录: {p}", flush=True)
                    else:
                        ctx.workspace = p
                        history = new_history(str(p), cfg)
                        print(f"工作空间 = {p}（历史已重置）", flush=True)
            elif cmd == "/reset":
                history = new_history(str(workspace), cfg)
                print("历史已清空", flush=True)
            elif cmd == "/tools":
                tools_command(arg, cfg, history, workspace, use_color, ctx)
            elif cmd == "/skill":
                name, _, question = arg.partition(" ")
                forward = do_skill(name.strip(), question.strip())
            elif cmd == "/save":
                if not arg:
                    print("用法: /save chat.json", flush=True)
                else:
                    Path(arg).write_text(
                        json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
                    print(f"已保存到 {arg}", flush=True)
            elif cmd == "/load":
                if not arg or not Path(arg).exists():
                    print("用法: /load chat.json（文件需存在）", flush=True)
                else:
                    history = json.loads(Path(arg).read_text(encoding="utf-8"))
                    print(f"已载入 {arg}（{len(history)} 条消息）", flush=True)
            else:
                # /<技能名> [问题] 快捷调用技能（对齐 WebUI 面板选中技能）
                sks = _match_skills(workspace, cmd.lstrip("/"))
                if len(sks) == 1:
                    forward = do_skill(sks[0]["name"], arg)
                elif len(sks) > 1:
                    print(f"匹配到多个技能: {', '.join(s['name'] for s in sks)}"
                          f"，请用 /skill <名称>", flush=True)
                else:
                    print("未知命令，/help 看帮助（输入 / 看指令与技能一览）", flush=True)
            if forward is None:
                continue
            line = forward

        # ---- 消息里的 @ 上下文引用 → 附文件（图片→视觉，其它→模型自己读）----
        line, at_imgs, at_files = resolve_at_refs(line, workspace, use_color, picker=pick_file)
        imgs = (pending_images + at_imgs) or None
        pending_images = []
        pending_img_paths = []
        send_text = line or "看一下我附带的内容"
        all_files = pending_files + at_files
        if all_files:
            listing = "\n".join(f"- {p}" for p in all_files)
            send_text += (f"\n\n[我附带的文件：短的用 read_file 读；很长（几十万字/几百页）的必须用 read_long_doc 分段精读]\n{listing}")
            pending_files = []
        try:
            run_turn(client, cfg, ctx, history, send_text, imgs)
        except KeyboardInterrupt:
            # 兜底：Ctrl+C 连按/落在收尾间隙时不退出，回到对话
            print(color("\n[⏹ 已打断] 回到对话（提示符处 Ctrl+C 或 /exit 退出）",
                        "yellow", use_color), flush=True)
        except Exception as e:
            # 单轮失败不该崩掉整个会话：说清原因、历史补占位，继续对话
            print(color(f"\n[错误] {type(e).__name__}: {e}", "red", use_color), flush=True)
            if history and history[-1].get("role") == "user":
                history.append({"role": "assistant",
                                "content": f"[本轮执行出错，未完成：{e}]"})
            print(color("  已回到对话，可以直接继续问或换个说法/换工具再试",
                        "dim", use_color), flush=True)

    if hotkey:
        hotkey.stop()
    print("再见。", flush=True)
