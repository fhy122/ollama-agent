# 第 6 章 · 命令行成品

**本章产出**：`agent/cli.py`（约 300 行）+ `main.py` 退休。到这里它已经是个能日常用的工具了。

---

## 一、装配顺序很重要

`main()` 里前 40 行只是"把一个 agent 装起来"。顺序错了会出现很隐蔽的问题，所以按依赖顺序来：

```
1. argparse  →  AgentConfig          （所有可调项的入口）
2. 校验 workspace 是不是目录          （错了立刻退，别等到第一次读文件）
3. OllamaClient(cfg)                 （读 cfg 派生自己的字段）
4. ToolContext(workspace, confirm)   （工具的运行环境）
5. new_history(workspace)            （system prompt + 工作空间说明）
6. 检查模型是否存在 → 预热 → REPL
```

**第 3 步有个坑**：`OllamaClient.__init__` 会把 `cfg.model`、`cfg.think_value` 拷到自己身上。之后改 `cfg.model` **不会**影响已创建的 client。这就是为什么切换模型必须同时改两边：

```python
cfg.model = new_model
client.model = cfg.model      # 少这一行，/model 看起来生效了其实没有
```

---

## 二、`agent/cli.py`

```python
"""命令行入口：参数解析 + 交互 REPL。"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from pathlib import Path

from agent.client import OllamaClient
from agent.config import (DEFAULT_CTX, DEFAULT_HOST, DEFAULT_MODEL,
                          MAX_TOOL_STEPS, THINK_VALUES, AgentConfig)
from agent.console import color, setup_stdio
from agent.loop import new_history, run_turn
from agent.registry import ToolContext

HELP_TEXT = """命令:
  /                 列出已注册工具与技能
  /img <路径>       给下一条消息附图片（可连续多次）
  /think <模式>     off|on|low|medium|high|max|auto
  /model <模型名>   切换模型
  /ctx <整数>       设置 num_ctx（0 = 服务端默认）。改值会触发模型重载
  /ws [目录]        查看 / 切换工作空间（相对路径的根）
  /reset            清空对话历史
  /tools            列出已注册工具
  /skill <名称> [问题]  加载技能进对话；带问题则加载后直接提问
  /save <文件>      保存对话历史为 JSON
  /load <文件>      读入对话历史
  /help             本帮助
  /exit             退出
@ 上下文： "总结 @report.md 的要点"  —— 唯一命中自动附上
普通输入即对话。"""

# @ 前不接拉丁字母/数字/下划线：拦住 foo@bar.com，但允许 "总结@报告" 中文紧邻
AT_RE = re.compile(r"(?<![A-Za-z0-9_])@([^\s@]+)")
IMG_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", ".workbuddy"}


# ---------- @ 引用 ----------

def find_context_files(ws: Path, q: str, limit: int = 20) -> list[str]:
    """工作空间文件模糊搜索，返回相对路径列表。"""
    q = (q or "").strip().lower().replace("\\", "/")
    out: list[str] = []
    for p in sorted(ws.rglob("*")):
        if len(out) >= limit:
            break
        if not p.is_file() or any(part in SKIP_DIRS for part in p.parts):
            continue
        rel = p.relative_to(ws).as_posix()
        if q in rel.lower():
            out.append(rel)
    return out


def resolve_at_refs(text: str, ws: Path, use_color: bool = False
                    ) -> tuple[str, list[str], list[str]]:
    """把 @xxx 处理成 (新文本, 图片 base64 列表, 文件路径列表)。

    唯一命中：替换成真实路径；图片额外读成 base64 附上。
    多命中 / 没命中：原文不动，交给用户自己说清楚。
    """
    images: list[str] = []
    files: list[str] = []

    def repl(m: re.Match) -> str:
        q = m.group(1)
        cands = find_context_files(ws, q)
        if len(cands) != 1:
            if len(cands) > 1:
                print(color(f"[提示] @{q} 命中 {len(cands)} 个：{', '.join(cands[:5])}",
                            "yellow", use_color), flush=True)
            return m.group(0)
        rel = cands[0]
        p = ws / rel
        if p.suffix.lower() in IMG_EXT:
            images.append(base64.b64encode(p.read_bytes()).decode("ascii"))
            return f"[图片 {rel}]"
        files.append(rel)
        return rel

    return AT_RE.sub(repl, text), images, files


# ---------- 参数 ----------

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="agent", description="本地 Ollama agent")
    ap.add_argument("prompt", nargs="*", help="一次性提问；省略则进入交互模式")
    ap.add_argument("-m", "--model", default=DEFAULT_MODEL)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--think", default="off", choices=list(THINK_VALUES))
    ap.add_argument("--ctx", type=int, default=DEFAULT_CTX,
                    help="num_ctx，0 表示不传（用服务端默认）")
    ap.add_argument("--temp", type=float, default=0.7)
    ap.add_argument("--max-steps", type=int, default=MAX_TOOL_STEPS)
    ap.add_argument("-w", "--workspace", default="")
    ap.add_argument("--confirm", action="store_true",
                    help="写/改/删文件、跑命令、联网前需确认")
    ap.add_argument("--no-tools", action="store_true")
    ap.add_argument("--no-color", action="store_true")
    ap.add_argument("--hide-think", action="store_true")
    ap.add_argument("--no-warm", action="store_true")
    ap.add_argument("--img", action="append", default=[], help="附图片，可多次")
    return ap


def make_context(cfg: AgentConfig, workspace: Path) -> ToolContext:
    def ask(what: str) -> bool:
        ans = input(color(f"? 允许执行 {what} ? [y/N] ", "yellow",
                          cfg.use_color)).strip().lower()
        return ans in ("y", "yes")

    return ToolContext(confirm=cfg.confirm, use_color=cfg.use_color,
                       ask=ask, workspace=workspace)


def load_images(paths: list[str], use_color: bool) -> list[str]:
    out = []
    for p in paths:
        path = Path(p).expanduser()
        if path.exists():
            out.append(base64.b64encode(path.read_bytes()).decode("ascii"))
        else:
            print(color(f"[警告] 图片不存在: {p}", "yellow", use_color), flush=True)
    return out


# ---------- 主流程 ----------

def main(argv: list[str] | None = None) -> None:
    setup_stdio()
    args = build_parser().parse_args(argv)
    use_color = not args.no_color and sys.stdout.isatty()

    cfg = AgentConfig(
        host=args.host, model=args.model, think=args.think,
        num_ctx=(args.ctx if args.ctx > 0 else None),
        temperature=args.temp, max_steps=args.max_steps,
        confirm=args.confirm, use_color=use_color,
        show_think=not args.hide_think, no_tools=args.no_tools,
        warm=not args.no_warm,
        workspace=str(Path(args.workspace).expanduser().resolve())
                  if args.workspace else "",
    )

    workspace = Path(cfg.workspace) if cfg.workspace else Path.cwd()
    if not workspace.is_dir():
        print(color(f"[错误] 工作空间不是目录: {workspace}", "red", use_color), flush=True)
        raise SystemExit(1)

    client = OllamaClient(cfg)
    ctx = make_context(cfg, workspace)
    history = new_history(str(workspace))

    print(color(f"Ollama Agent | 模型 {cfg.model} | think={cfg.think} "
                f"| ctx={cfg.num_ctx or '服务端默认'}", "bold", use_color), flush=True)
    print(color(f"工作空间: {workspace}", "dim", use_color), flush=True)

    try:
        models = client.list_models()
        if cfg.model not in models:
            print(color(f"[警告] 本地未发现 {cfg.model}，已装: "
                        f"{', '.join(models[:10])}", "yellow", use_color), flush=True)
    except Exception as e:
        print(color(f"[警告] 无法列出模型: {e}", "yellow", use_color), flush=True)

    # ---------- 单次模式 ----------
    if args.prompt:
        imgs = load_images(args.img, use_color)
        text, at_imgs, at_files = resolve_at_refs(" ".join(args.prompt),
                                                  workspace, use_color)
        imgs += at_imgs
        send_text = text or "看一下我附带的内容"
        if at_files:
            listing = "\n".join(f"- {p}" for p in at_files)
            send_text += f"\n\n[我附带的文件，请按需用 read_file 读取]\n{listing}"
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
        print(color("预热模型中…", "dim", use_color), flush=True)
        try:
            print(color(f"模型就绪（{client.warmup():.1f}s）", "dim", use_color), flush=True)
        except Exception as e:
            print(color(f"[警告] 预热失败: {e}", "yellow", use_color), flush=True)

    import tools as tools_pkg
    print(color(f"已注册工具 {len(tools_pkg.all_specs())} 个，输入 /help 看命令。",
                "dim", use_color), flush=True)

    pending_images: list[str] = []

    while True:
        try:
            line = input(color("\n你> ", "bold", use_color)).strip()
        except (EOFError, KeyboardInterrupt):
            print(flush=True)
            break
        if not line:
            continue

        # ---- 斜杠命令 ----
        if line.startswith("/"):
            cmd, _, rest = line.partition(" ")
            rest = rest.strip()
            followup = ""          # /skill 带了问题时，这里放"加载后要问什么"

            if cmd in ("/exit", "/quit"):
                break
            elif cmd in ("/help", "/"):
                print(HELP_TEXT, flush=True)
            elif cmd == "/tools":
                for t in tools_pkg.all_tools():
                    mark = " [需确认]" if t.confirm else ""
                    print(f"  {t.name}{mark}\n      {t.description[:80]}", flush=True)
            elif cmd == "/think":
                if rest in THINK_VALUES:
                    cfg.think = rest
                    client.think = cfg.think_value
                    print(f"think = {rest}", flush=True)
                else:
                    print(f"可选: {' | '.join(THINK_VALUES)}", flush=True)
            elif cmd == "/model":
                if rest:
                    cfg.model = client.model = rest
                    print(f"模型切到 {rest}", flush=True)
                else:
                    print(f"当前模型 {cfg.model}", flush=True)
            elif cmd == "/ctx":
                try:
                    n = int(rest)
                    cfg.num_ctx = client.num_ctx = (n if n > 0 else None)
                    print(f"num_ctx = {cfg.num_ctx or '服务端默认'}（下次请求会重载模型）",
                          flush=True)
                except ValueError:
                    print(f"当前 num_ctx = {cfg.num_ctx}", flush=True)
            elif cmd == "/ws":
                if rest:
                    p = Path(rest).expanduser().resolve()
                    if p.is_dir():
                        ctx.workspace = workspace = p
                        cfg.workspace = str(p)
                        print(f"工作空间 → {p}", flush=True)
                    else:
                        print(f"[错误] 不是目录: {p}", flush=True)
                else:
                    print(f"工作空间: {ctx.workspace}", flush=True)
            elif cmd == "/reset":
                history[:] = new_history(str(workspace))
                print("历史已清空", flush=True)
            elif cmd == "/img":
                imgs = load_images([rest], use_color)
                pending_images += imgs
                print(f"已附 {len(imgs)} 张图，随下条消息发送", flush=True)
            elif cmd == "/skill":
                name, _, question = rest.partition(" ")
                res = tools_pkg.dispatch("skill", {"name": name}, ctx)
                if res.text.startswith(("[错误]", "[提示]")):
                    print(res.text, flush=True)
                else:
                    history.append({"role": "user",
                                    "content": f"[技能指令已加载，后续回答请严格遵循此技能]\n"
                                               f"{res.text}"})
                    print(f"技能 {name} 已加载（{len(res.text)} 字符）", flush=True)
                    if question.strip():
                        followup = question.strip()
            elif cmd == "/save":
                Path(rest).write_text(json.dumps(history, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
                print(f"已保存 {len(history)} 条消息到 {rest}", flush=True)
                continue
            elif cmd == "/load":
                history[:] = json.loads(Path(rest).read_text(encoding="utf-8"))
                print(f"已载入 {len(history)} 条消息", flush=True)
                continue
            elif cmd != "/":
                print(f"[错误] 未知命令 {cmd}，/help 查看", flush=True)

            if followup:
                line = followup     # 技能已加载 → 继续往下当普通提问
            else:
                continue            # 普通命令处理完就回输入提示

        # ---- 普通对话（含 @ 引用）----
        text, at_imgs, at_files = resolve_at_refs(line, workspace, use_color)
        imgs = pending_images + at_imgs
        pending_images = []
        send_text = text or "看一下我附带的内容"
        if at_files:
            listing = "\n".join(f"- {p}" for p in at_files)
            send_text += f"\n\n[我附带的文件，请按需用 read_file 读取]\n{listing}"

        try:
            run_turn(client, cfg, ctx, history, send_text, imgs or None)
        except KeyboardInterrupt:
            print(color("\n[⏹ 已打断]", "yellow", use_color), flush=True)
        except Exception as e:
            print(color(f"[错误] {type(e).__name__}: {e}", "red", use_color), flush=True)

    print("再见。", flush=True)


if __name__ == "__main__":
    main()
```

> `/skill` 那段有个小技巧：带问题时把问题存进 `followup`，命令块结束时**不 continue**，直接落进下面的普通对话逻辑。这样 `/skill pdf 帮我读 a.pdf` 就是"先加载技能，再提问"一条命令搞定。

再加一个 `agent/__main__.py`，让 `python -m agent` 生效：

```python
from agent.cli import main

main()
```

---

## 三、为什么 `@` 引用值得单独实现

用 LLM 的人都会遇到这个摩擦：文件明明在工作空间里，模型却老是猜路径。原因是它**不知道有哪些文件**（列目录要花一次工具调用，模型常常懒得做）。

`@` 引用解决的就是这个：用户在输入里点一下，路径就精确注入。

实现有三个要点：

**① 正则要排除邮箱和 @ 用户名**

```python
AT_RE = re.compile(r"(?<![A-Za-z0-9_])@([^\s@]+)")
```

`(?<![A-Za-z0-9_])` 是负向后顾：`foo@bar.com` 里的 `@` 前面是 `o`，不匹配；`总结@报告` 里的 `@` 前面是中文，匹配。这个细节不做，用户粘贴一个邮箱就触发文件搜索。

**② 只在唯一命中时自动处理**

命中 3 个文件时自动选第一个是灾难（用户以为附了 A，其实附了 B）。多命中就打印候选、**原文不动**，让用户补几个字。

**③ 图片和文件走两条路**

| 类型 | 处理 | 为什么 |
|---|---|---|
| 图片 | 读成 base64 → `images` 数组 | 模型能直接"看"，不用调工具 |
| 其它文件 | 只把**路径**塞进文本 | 强制模型用 `read_file`，这样文件内容走工具结果，可追溯、可截断 |

如果所有文件都 base64 塞进去，一个 5000 行的 CSV 能瞬间吃满上下文 —— 而 `read_file` 有行窗口，能翻页。

---

## 四、确认闸：`--confirm`

`make_context` 里那个 `ask` 就是全部实现：

```python
def ask(what: str) -> bool:
    ans = input(color(f"? 允许执行 {what} ? [y/N] ", "yellow", cfg.use_color))
    return ans == "y"
```

**默认拒绝**（`[y/N]` 大写 N）很重要：用户直接回车 = 不许执行。写文件、删文件这种操作，默认值应该是安全的那一侧。

触发条件是**工具自己声明**的：

```python
@tool(..., confirm=True)     # 只有这一类工具会走 approve()
```

真实仓库里 15 个工具中有 7 个带 `confirm=True`：`write_file` `edit_file` `delete_file` `run_command` `web_search` `browse_url` `web_chat`。规律是：**会改变外部状态、或会花用户的钱/带宽**的都要确认；只读的不要（否则每读一个文件弹一次，没法用）。

---

## 五、Ctrl+C 有两个位置

| 时机 | 谁接住 | 结果 |
|---|---|---|
| 模型正在生成 | `client.chat` 里的 `except KeyboardInterrupt` | `cancelled=True`，部分文本入历史，界面回到输入提示 |
| 工具正在执行 / 两步之间 | `run_turn` 的 `except KeyboardInterrupt` | `pad_unanswered_tool_calls()` 补齐，回到输入提示 |
| 在输入提示处 | `main` 的 `input()` | 退出程序 |

三层拦截缺一层就会"按一次 Ctrl+C 整个程序没了"。这也是为什么 `run_turn` 里要 `try/except KeyboardInterrupt` 而不是让它冒泡。

---

## 六、关于行内面板（palette）

仓库里有 445 行的 `agent/palette.py`，实现 `/` 和 `@` 的**行内候选面板**（按 `/` 弹出命令列表，方向键选，Tab 补全）。本章有意不实现它，原因值得一说：

- 它需要 Win32 的 raw console 模式（`SetConsoleMode` 去掉 `ENABLE_LINE_INPUT`）、ANSI 光标控制、自己处理退格和方向键
- 平台强绑定（Windows only），且只在真 TTY 下工作
- **它不产生任何新能力**，只是把"打命令"变成"选命令"

写产品时值得做，学架构时是噪音。想看实现，第 11 章给了入口。你现在有 `/help` 和 `@` 的文本版，功能完全等价。

---

## 七、验证清单

```bash
# 1. 启动
python -m agent -w .
# 期望：打印模型/工作空间/已装模型检查/预热耗时/工具数

# 2. 命令
你> /tools          # 列出 2 个（本章）到 15 个（完整版）
你> /ws             # 当前工作空间
你> /ws D:/projectspace    # 切换
你> /think auto
你> /ctx 0          # 服务端默认

# 3. @ 引用
你> 总结 @README.md 的要点
# 期望：先看到 [文本] README.md ... 的工具结果

# 4. 确认闸
python -m agent --confirm "把 hello 写进 /tmp/t.txt"
# 期望：? 允许执行 write_file({"path": ...}) ? [y/N]

# 5. 单次模式（适合脚本/管道）
python -m agent "当前目录有几个 py 文件" | cat

# 6. Ctrl+C：生成中途按一次，应回到输入提示而不是退出
```

---

## 八、踩过的坑

| 现象 | 原因 | 解决 |
|---|---|---|
| 输出重定向到文件后全是乱码 | 没 `setup_stdio()` | 已在 `main` 第一行调 |
| 管道模式下带一堆 `\033[36m` | `use_color` 只看 `--no-color`，没看 `isatty()` | `use_color = not args.no_color and sys.stdout.isatty()` |
| `/model` 切换后还是老模型回答 | 只改了 `cfg.model` | 同时改 `client.model` |
| `/ctx` 改完第一次特别慢 | 模型重载 | 提示用户，别以为卡死 |
| `@图片` 之后模型说"没看到图" | 图片没随消息发（`pending_images` 忘了传） | 检查 `run_turn(..., imgs or None)` |
| 单次模式 `KeyboardInterrupt` 退出码不对 | 应 `SystemExit(130)`（shell 惯例） | 已处理 |
| 输入中文文件名时 `@` 匹配不到 | `rglob` 路径分隔符是 `\`，比较时没归一 | `rel.replace("\\", "/")` 或 `as_posix()` |

---

## 九、过关自测

1. 为什么 `OllamaClient` 要把 `cfg.model` 拷一份？这算不算设计缺陷？
2. `@` 正则里的 `(?<![A-Za-z0-9_])` 去掉会怎样？
3. 为什么确认闸的默认是"拒绝"而不是"允许"？
4. `/reset` 之后 `client` 需要重建吗？
5. 想把 `/ws` 的切换**持久化**（下次启动还在），改哪里？

<details>
<summary>答案要点</summary>

1. 因为一次请求过程中 `cfg` 可能被 REPL 改（另一个线程/另一条命令），而单次 HTTP 往返内参数必须稳定。算轻度重复，换来的是一致性；只要记得同时改两边。
2. `foo@bar.com` 会被当成 `@bar.com` 触发文件搜索，并且可能把 `.com` 当路径。还会误伤代码里的 `@decorator`（这个倒是也该拦住）。
3. 安全侧默认：无人值守（超时、脚本、误触回车）时宁可拒绝。这条原则在 WebUI 的 `ConfirmGate` 里更明显 —— 300 秒超时 = 拒绝。
4. 不需要。`client` 只持有 host/model/参数这类"连接配置"，历史在 `history` 里，跟 client 无关。
5. 加一个 `~/.ollama-agent.json` 存 `{"workspace": "...", "model": "..."}`，启动时读、`/ws` 时写。注意：配置文件里只存"用户偏好"，**不要存 confirm 开关**（安全设置不该被静默继承）。

</details>

---

下一章：同一个内核，加一个浏览器前端 → [第 7 章](07-webui.md)
