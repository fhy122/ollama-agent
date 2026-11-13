"""终端行内选择面板：`/` 指令与技能、`@` 上下文文件。

输入 `/` 或 `@` 即在输入行下方弹出候选清单（@ 按前缀匹配文件名/相对路径），
↑↓ 移动选择、Enter 确认、Esc 关闭；选中技能/指令回填输入行，选中文件挂到
下一条消息（对齐 WebUI 的 / 与 @ 面板语义）。

纯逻辑（触发识别 / 前缀过滤 / 按键状态机）与终端 I/O 分离，便于单测。
I/O：Windows 用 msvcrt 逐键读取 + ANSI 重绘；非 Windows 或非 TTY 回退 input()。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field

# ---- / 面板里的指令清单（name 不带斜杠；与 agent/cli.py 的 HELP_TEXT 同步）----
COMMANDS: list[tuple[str, str]] = [
    ("img", "附图片 <路径>"),
    ("paste", "剪贴板进对话"),
    ("think", "思考模式 <off|on|low|medium|high|max|auto>"),
    ("model", "切换模型 <名>"),
    ("ctx", "设置 num_ctx <整数>"),
    ("repeat", "重复惩罚 <penalty> [last_n]；留空=恢复服务端默认"),
    ("ws", "查看/切换工作空间 [目录]"),
    ("reset", "清空对话历史"),
    ("tools", "列出已注册工具"),
    ("skill", "加载技能 <名称> [问题]"),
    ("save", "保存对话 <文件>"),
    ("load", "载入对话 <文件>"),
    ("help", "帮助"),
    ("exit", "退出"),
]

HINT = "↑↓ 选择 · Enter 确认 · Esc 关闭"
HINT_EMPTY = "无匹配（Enter 原样发送 · Esc 关闭）"
MAX_ROWS = 10         # 菜单最多显示条目行数（不含分组标题/提示行）

_AT_END = re.compile(r"(?<![A-Za-z0-9_])@([^\s@]*)$")
_SLASH_LINE = re.compile(r"^/([^\s]*)$")
_SKILL_LINE = re.compile(r"^/skill\s+([^\s]*)$", re.I)   # /skill 后的二级补全（技能名，未跟问题）
_ANSI = re.compile(r"\033\[[0-9;]*m")


# ---------------- 纯逻辑：触发 / 过滤 ----------------

def trigger_token(buf: str) -> tuple[str, int, str] | None:
    """当前输入行的触发态 → (kind, 起点下标, 已输入的过滤词)；无触发返回 None。

    skill：整行形如 `/skill xxx` → (\"skill\", 7, \"xxx\")，打完 /skill 空格后继续补技能名
    slash：整行形如 `/xxx`（无空格）→ (\"slash\", 0, \"xxx\")
    at：行尾 `@xxx` 且 @ 前不接拉丁字母/数字/下划线（拦邮箱）→ (\"at\", @下标, \"xxx\")
    """
    m = _SKILL_LINE.fullmatch(buf)
    if m:
        return ("skill", 7, m.group(1))
    m = _SLASH_LINE.fullmatch(buf)
    if m:
        return ("slash", 0, m.group(1))
    m = _AT_END.search(buf)
    if m:
        return ("at", m.start(), m.group(1))
    return None


def filter_files(files: list[str], token: str) -> list[str]:
    """文件候选：前缀匹配（大小写不敏感）。文件名前缀命中排在相对路径前缀命中之前。"""
    t = (token or "").lower()
    by_base = [f for f in files if f.rsplit("/", 1)[-1].lower().startswith(t)]
    by_path = [f for f in files
               if not f.rsplit("/", 1)[-1].lower().startswith(t) and f.lower().startswith(t)]
    return by_base + by_path


@dataclass
class MenuItem:
    kind: str                    # "cmd" | "skill" | "file"
    label: str                   # 面板显示名
    insert: str                  # 选中后回填进输入行的文本
    hint: str = ""               # 右侧提示
    exact: tuple[str, ...] = ()  # 过滤词与之全等时 Enter 直接发送（不选中回填）
    payload: str = ""            # file=相对路径；skill=技能名


# ---------------- 纯逻辑：状态机 ----------------

@dataclass
class PaletteState:
    files: list[str] = field(default_factory=list)
    skills: list[tuple[str, ...]] = field(default_factory=list)   # (name, display[, desc])
    buf: str = ""
    items: list[MenuItem] = field(default_factory=list)
    sel: int = 0
    menu_active: bool = False
    trigger: tuple[str, int, str] | None = None
    dismissed: tuple[str, int] | None = None      # Esc 关闭的触发身份 (kind, 起点)
    picked: list[str] = field(default_factory=list)   # 选中的文件相对路径

    def _skill_items(self, t: str) -> tuple[list[MenuItem], list[MenuItem]]:
        """技能候选：命中的排前面，其余归「其它技能」（不淘汰，方便浏览挑选）。"""
        matched: list[MenuItem] = []
        rest: list[MenuItem] = []
        for row in self.skills:
            name, display = row[0], row[1]
            desc = (row[2] if len(row) > 2 else "").strip()
            hint = (desc[:60] + ("…" if len(desc) > 60 else "")) or display
            if hint == name:            # 没描述且显示名同原名 → 不重复列一遍
                hint = ""
            nl, dl = name.lower(), display.lower()
            it = MenuItem("skill" if not t or nl.startswith(t) or dl.startswith(t)
                          or t in nl or t in dl else "skill_more",
                          name, f"/skill {name} ", hint,
                          exact=(nl, dl), payload=name)
            (matched if it.kind == "skill" else rest).append(it)
        return matched, rest

    def _build(self, kind: str, token: str) -> list[MenuItem]:
        t = (token or "").lower()
        out: list[MenuItem] = []
        if kind == "slash":
            for name, hint in COMMANDS:
                if name.startswith(t):
                    out.append(MenuItem("cmd", "/" + name, "/" + name + " ",
                                        hint, exact=(name,)))
            # 技能不淘汰：命中的排前面（输入=聚焦排序），其余照常列出供浏览挑选
            matched, rest = self._skill_items(t)
            out.extend(matched)
            out.extend(rest)
        elif kind == "skill":                   # `/skill xxx` 二级补全：只补技能名
            matched, rest = self._skill_items(t)
            out.extend(matched)
            out.extend(rest)
        else:
            for rel in filter_files(self.files, token):
                if rel in self.picked:          # 已附加的单独列在后面，避免重复
                    continue
                base = rel.rsplit("/", 1)[-1]
                out.append(MenuItem("file", rel, rel, "",
                                    exact=(rel.lower(), base.lower()), payload=rel))
            for rel in self.picked:             # ✓ 已附加：始终可见，Enter 取消
                out.append(MenuItem("file", f"✓ {rel}", rel,
                                    "已附加 · Enter 取消", payload=rel))
        return out

    def _refresh(self) -> None:
        trig = trigger_token(self.buf)
        self.trigger = trig
        if trig is None:
            self.items, self.menu_active, self.dismissed = [], False, None
            return
        key = (trig[0], trig[1])
        if self.dismissed == key:
            self.items, self.menu_active = [], False
            return
        self.menu_active = True
        self.items = self._build(trig[0], trig[2])
        self.sel = 0

    def apply(self, key) -> str:
        """处理一个按键。返回 \"continue\" | \"send\"（send=整行交给调用方）。"""
        if isinstance(key, tuple):                      # ("char", ch)
            ch = key[1]
            if ch and (ch >= " " and ch != "\x7f"):     # 忽略 Ctrl+V 之类的控制字符
                self.buf += ch
                self._refresh()
            return "continue"
        if key == "backspace":
            if self.buf:
                self.buf = self.buf[:-1]
                self.dismissed = None                   # 重新编辑触发词 → 菜单恢复
                self._refresh()
            elif self.picked:
                # 输入行为空时退格 = 删最后一个附件（像删字符一样逐个往回删）
                self.picked.pop()
            return "continue"
        if key in ("up", "down"):
            if self.items:
                n = len(self.items)
                self.sel = (self.sel + (-1 if key == "up" else 1)) % n
            return "continue"
        if key == "esc":
            if self.menu_active and self.trigger:
                self.dismissed = (self.trigger[0], self.trigger[1])
                self._refresh()
            return "continue"
        if key == "enter":
            if self.menu_active:
                if not self.items:
                    return "send"
                tok = (self.trigger[2] if self.trigger else "").lower()
                if tok and any(tok in it.exact for it in self.items):
                    return "send"                       # 已输入全名 → 直接发送
                if (not tok and self.sel == 0 and self.trigger
                        and self.trigger[0] == "slash"):
                    return "send"                       # 光打 / 回车 → 列指令清单
                # 其余（含光打 @ 回车）→ 选中当前项：第一个候选也能选上
                self._pick(self.items[self.sel])
                return "continue"
            return "send"
        return "continue"                               # noop / 其它键

    def _pick(self, it: MenuItem) -> None:
        if it.kind == "file":
            # 对齐 WebUI：@token 从输入行移除，文件挂到下一条消息。
            # 已在待发列表里的再选一次 = 取消（选错了能反悔）
            if it.payload in self.picked:
                self.picked.remove(it.payload)
            else:
                self.picked.append(it.payload)
            trig = self.trigger
            assert trig is not None
            self.buf = self.buf[:trig[1]]
        else:
            self.buf = it.insert                        # /reset  → "/reset "
        self.dismissed = None
        self._refresh()

    def _attached_line(self, use_color: bool, width: int = 0) -> str:
        """待发附件汇总行（常驻：菜单收起后也显示，让人随时知道选了哪些）。"""
        from agent.console import color
        parts = []
        for i, p in enumerate(self.picked[:3], 1):
            parts.append(f"[{i}] {p.replace(chr(92), '/').rsplit('/', 1)[-1]}")
        names = "  ".join(parts)
        if len(self.picked) > 3:
            names += f" … 共 {len(self.picked)} 个"
        text = f"  ✓ 已附加 {len(self.picked)} 个：{names}"
        tail = "（空行按退格逐个移除 · 再 @ 可增删）"
        if width and _disp_width(text) + _disp_width(tail) > width:
            tail = ""                                   # 放不下提示就只列附件
        return color(_trunc(text + tail, width) if width else text + tail,
                     "green", use_color)

    # ---- 渲染 ----
    def render_lines(self, use_color: bool = True) -> list[str]:
        from agent.console import color
        width = max(24, shutil.get_terminal_size().columns - 2)
        attached = [self._attached_line(use_color, width)] if self.picked else []
        if not self.menu_active:
            return attached
        if not self.items:
            return attached + [color("  " + HINT_EMPTY, "dim", use_color)]

        # 展示窗口：以 sel 为中心取 MAX_ROWS 条（分组标题不占窗口）
        n = len(self.items)
        if n <= MAX_ROWS:
            lo, hi = 0, n
        else:
            lo = max(0, min(self.sel - MAX_ROWS // 2, n - MAX_ROWS))
            hi = lo + MAX_ROWS
        rows: list[str] = list(attached)
        if self.trigger and self.trigger[0] in ("slash", "skill"):
            for gname, kind in (("指令", "cmd"), ("技能", "skill"),
                                ("其它技能", "skill_more")):
                idxs = [i for i, it in enumerate(self.items) if it.kind == kind]
                vis = [i for i in idxs if lo <= i < hi]
                if not vis:
                    continue
                rows.append(color(f"  {gname}", "dim", use_color))
                for i in vis:
                    rows.append(self._item_line(i, use_color, width))
            if hi < n:
                rows.append(color(f"  … 还有 {n - hi} 项（继续输入可过滤，↑↓ 可滚动）",
                                  "dim", use_color))
        else:
            if lo > 0:
                rows.append(color(f"  … 前 {lo} 项", "dim", use_color))
            for i in range(lo, hi):
                rows.append(self._item_line(i, use_color, width))
            if hi < n:
                rows.append(color(f"  … 还有 {n - hi} 项", "dim", use_color))
        rows.append(color("  " + HINT, "dim", use_color))
        return rows

    def _item_line(self, i: int, use_color: bool, width: int = 0) -> str:
        from agent.console import color
        it = self.items[i]
        mark = "▸ " if i == self.sel else "  "
        text = f"{mark}{it.label}"
        if it.hint:
            text += f"  — {it.hint}"
        text = _trunc(text, width) if width else text      # 防止超宽换行导致重绘错位
        return color(text, "cyan", use_color) if i == self.sel else text


# ---------------- 终端 I/O ----------------

@dataclass
class LineResult:
    text: str
    picked: list[str] = field(default_factory=list)


def _disp_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1
               for c in _ANSI.sub("", s))


def _trunc(s: str, width: int) -> str:
    """按显示宽度截断（CJK 算 2 列），超出补 …，避免菜单行折行破坏重绘。"""
    if _disp_width(s) <= width:
        return s
    out, w = "", 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if w + cw > width - 1:
            return out + "…"
        out += ch
        w += cw
    return out + "…"


def _write(out, s: str) -> None:
    """整帧一次写出（绕过行缓冲，避免逐行刷新造成闪烁）。

    字节直写绕开了 Python 的换行转换，而 Windows 控制台里单独一个 LF
    只下移不回列首，所以这里显式把 \\n 补成 \\r\\n（POSIX 下多一个 \\r 无害）。
    """
    s = s.replace("\r\n", "\n").replace("\n", "\r\n")
    buf = getattr(out, "buffer", None)
    if buf is not None:
        try:
            buf.write(s.encode("utf-8", "replace"))
            buf.flush()
            return
        except Exception:
            pass
    out.write(s)
    out.flush()


def _redraw(out, prompt: str, buf: str, menu_lines: list[str]) -> None:
    """整块重绘：输入行（可含前导 \\n、ANSI）+ 下方菜单行，光标回到输入行末尾。"""
    cols = shutil.get_terminal_size().columns
    plain = _ANSI.sub("", prompt)
    head = plain.rsplit("\n", 1)[-1]
    line_w = _disp_width(head) + _disp_width(buf)
    rows = plain.count("\n") + line_w // cols + 1   # 从写入起点算的占用行数
    h = len(menu_lines)
    up = rows - 1 + h
    parts = [f"\033[{up}A"] if up else []
    parts += ["\r\033[J", prompt, buf]
    if h:
        parts += ["\n", "\n".join(menu_lines)]
        col = line_w % cols
        parts.append(f"\033[{h}A\r")
        if col:
            parts.append(f"\033[{col}C")
    _write(out, "".join(parts))


def _win_get_key():
    """msvcrt 逐键 → 统一按键名 / ("char", ch)。"""
    import msvcrt
    ch = msvcrt.getwch()
    if ch in ("\x00", "\xe0"):
        ch2 = msvcrt.getwch()
        return {"H": "up", "P": "down"}.get(ch2, "noop")
    if ch in ("\r", "\n"):
        return "enter"
    if ch == "\x08":
        return "backspace"
    if ch == "\x1b":
        return "esc"
    if ch == "\x03":
        return "interrupt"
    if ch in ("\x04", "\x1a"):
        return "eof"
    return ("char", ch)


def _read_line_raw(prompt: str, files: list[str], skills: list[tuple[str, ...]],
                   use_color: bool, get_key, out,
                   attached: list[str] | None = None) -> LineResult:
    st = PaletteState(files=files, skills=skills, picked=list(attached or []))

    def draw() -> None:
        _redraw(out, prompt, st.buf, st.render_lines(use_color))

    try:
        draw()
        while True:
            key = get_key()
            if key == "noop":
                continue
            if key == "interrupt":
                raise KeyboardInterrupt
            if key == "eof":
                raise EOFError
            if st.apply(key) == "send":
                _redraw(out, prompt, st.buf, [])
                out.write("\n")
                out.flush()
                return LineResult(st.buf, list(st.picked))
            draw()
    except BaseException:
        try:
            _redraw(out, prompt, st.buf, [])
        except Exception:
            pass
        raise


def _enable_vt() -> bool:
    """确保当前控制台支持 ANSI 转义（Windows 老 conhost 默认关闭）。"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)                     # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not k.GetConsoleMode(h, ctypes.byref(mode)):
            return False
        if mode.value & 0x0004:                     # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            return True
        return bool(k.SetConsoleMode(h, mode.value | 0x0004))
    except Exception:
        return False


def read_line(prompt: str, files: list[str], skills: list[tuple[str, ...]],
              use_color: bool = True, get_key=None, out=None,
              attached: list[str] | None = None) -> LineResult:
    """读一行输入（带 / 与 @ 行内面板）。

    attached：当前已挂载到下一条消息的文件（相对路径），面板里显示为 ✓ 可回车取消；
    返回值 picked 是**完整待发列表**（初始值 ∪ 新选 − 取消），调用方可据此同步。
    非 Windows / stdin 非 TTY（管道）/ 控制台不支持 ANSI → 回退 input()，无面板。
    任何底层异常也回退 input()，保证 REPL 不被面板拖死。
    KeyboardInterrupt / EOFError 语义与 input() 一致，向上抛。
    """
    keep = list(attached or [])
    if get_key is None and (os.name != "nt" or not sys.stdin.isatty()):
        return LineResult(input(prompt), keep)
    if get_key is None and not _enable_vt():
        return LineResult(input(prompt), keep)
    try:
        return _read_line_raw(prompt, files, skills, use_color,
                              get_key or _win_get_key, out or sys.stdout, keep)
    except (KeyboardInterrupt, EOFError):
        raise
    except Exception:
        return LineResult(input(prompt), keep)
