"""真控制台验证 CLI 行内面板：AllocConsole + 注入按键 + 读屏幕缓冲区。

单测只能验状态机，验不了「msvcrt 读键 → ANSI 重绘 → 屏幕实际内容」这条链路。
这里新开一块真控制台，注入 @、前缀字符、↓、Enter，再读回屏幕做断言。
（会短暂弹出一个控制台窗口，跑完自动关闭。部分控制台字体不显示 ▸ 字形，
所以断言只看候选/过滤/最终选中，不依赖字形。）
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time
from ctypes import wintypes as wt

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ_WRITE = 0xC0000000
OPEN_EXISTING = 3
KEY_EVENT = 0x0001
VK_DOWN = 0x28
LEFT_CTRL_PRESSED = 0x0008


class COORD(ctypes.Structure):
    _fields_ = (("X", ctypes.c_int16), ("Y", ctypes.c_int16))


class SMALL_RECT(ctypes.Structure):
    _fields_ = (("Left", ctypes.c_int16), ("Top", ctypes.c_int16),
                ("Right", ctypes.c_int16), ("Bottom", ctypes.c_int16))


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = (("dwSize", COORD), ("dwCursorPosition", COORD),
                ("wAttributes", wt.WORD), ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD))


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = (("bKeyDown", wt.BOOL), ("wRepeatCount", wt.WORD),
                ("wVirtualKeyCode", wt.WORD), ("wVirtualScanCode", wt.WORD),
                ("UnicodeChar", wt.WCHAR), ("dwControlKeyState", wt.DWORD))


class INPUT_RECORD(ctypes.Structure):
    _fields_ = (("EventType", wt.WORD), ("KeyEvent", KEY_EVENT_RECORD))


class Console:
    def __init__(self):
        k32.FreeConsole()
        if not k32.AllocConsole():
            raise OSError(f"AllocConsole 失败 {ctypes.get_last_error()}")
        k32.SetConsoleOutputCP(65001)
        k32.SetConsoleCP(65001)
        self.h_out = self._open("CONOUT$", GENERIC_READ_WRITE)
        self.h_in = self._open("CONIN$", GENERIC_READ_WRITE)
        mode = wt.DWORD()                       # 开 VT（等价 CLI 的 _enable_vt）
        if k32.GetConsoleMode(self.h_out, ctypes.byref(mode)):
            k32.SetConsoleMode(self.h_out, mode.value | 0x0004)
        cols, rows = self.buffer_size()
        os.environ["COLUMNS"] = str(cols)
        os.environ["LINES"] = str(rows)

    @staticmethod
    def _open(name: str, access: int):
        k32.CreateFileW.restype = wt.HANDLE
        h = k32.CreateFileW(name, access, 0x3, None, OPEN_EXISTING, 0, None)
        if not h or h == wt.HANDLE(-1).value:
            raise OSError(f"打开 {name} 失败 {ctypes.get_last_error()}")
        return h

    class _Buf:
        def __init__(self, h):
            self.h = h

        def write(self, data: bytes):
            # Unicode 接口写出，等价 Python 真实 stdout（_WindowsConsoleIO → WriteConsoleW）
            text = data.decode("utf-8", "replace")
            n = wt.DWORD()
            k32.WriteConsoleW(self.h, text, len(text), ctypes.byref(n), None)
            return len(data)

        def flush(self):
            pass

    def stream(self):
        class _Out:
            def __init__(self, h):
                self.buffer = Console._Buf(h)

            def write(self, s: str):
                self.buffer.write(s.encode("utf-8"))

            def flush(self):
                pass
        return _Out(self.h_out)

    def _rec(self, ch: str = "", vk: int = 0, ctrl: int = 0) -> INPUT_RECORD:
        r = INPUT_RECORD()
        r.EventType = KEY_EVENT
        r.KeyEvent.bKeyDown = True
        r.KeyEvent.wRepeatCount = 1
        r.KeyEvent.wVirtualKeyCode = vk
        if ch:
            r.KeyEvent.UnicodeChar = ch
        r.KeyEvent.dwControlKeyState = ctrl
        return r

    def key(self, *records: INPUT_RECORD) -> None:
        arr = (INPUT_RECORD * len(records))(*records)
        n = wt.DWORD()
        k32.WriteConsoleInputW(self.h_in, arr, len(records), ctypes.byref(n))

    def type_text(self, text: str) -> None:
        for ch in text:
            self.key(self._rec(ch))

    def press_down(self) -> None:
        r = self._rec("", VK_DOWN)
        r.KeyEvent.wVirtualScanCode = 0x50      # 真键盘必带扫描码，否则 msvcrt 不认方向键
        self.key(r)

    def press_enter(self) -> None:
        self.key(self._rec("\r", 0x0D))

    def ctrl_c(self) -> None:
        self.key(self._rec("\x03", 0x43, LEFT_CTRL_PRESSED))

    def buffer_size(self) -> tuple[int, int]:
        info = CONSOLE_SCREEN_BUFFER_INFO()
        k32.GetConsoleScreenBufferInfo(self.h_out, ctypes.byref(info))
        return info.dwSize.X, info.dwSize.Y

    def screen(self) -> str:
        """逐行读回屏幕（CJK 占两格，整体按字符数切行会漂移，所以一行一行读）。"""
        cols, rows = self.buffer_size()
        lines: list[str] = []
        buf = ctypes.create_unicode_buffer(cols + 1)
        for y in range(rows):
            n = wt.DWORD()
            k32.ReadConsoleOutputCharacterW(self.h_out, buf, cols, COORD(0, y),
                                            ctypes.byref(n))
            line = buf[: n.value].replace("\x00", "")
            lines.append(line.rstrip())
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def close(self) -> None:
        for h in (self.h_out, self.h_in):
            try:
                k32.CloseHandle(h)
            except Exception:
                pass
        k32.FreeConsole()


def main() -> None:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from agent.palette import _read_line_raw, _win_get_key

    files = ["README.md", "docs/report.md", "docs/report2.md", "报告.md", "assets/logo.png"]
    skills = [("ui-design-system", "UI设计系统")]

    con = Console()
    snaps: dict[str, str] = {}
    err: list[str] = []

    def drive() -> None:
        try:
            time.sleep(0.9)
            snaps["初始"] = con.screen()
            con.type_text("@")
            time.sleep(0.5)
            snaps["敲@"] = con.screen()
            con.type_text("rep")
            time.sleep(0.5)
            snaps["@rep"] = con.screen()
            con.press_down()
            time.sleep(0.4)
            snaps["选中"] = con.screen()
            con.press_enter()
            time.sleep(0.4)
            snaps["选中"] = con.screen()
            con.press_enter()
        except Exception as e:                     # pragma: no cover
            import traceback
            traceback.print_exc()
            err.append(f"{type(e).__name__}: {e}")
            con.ctrl_c()

    threading.Thread(target=drive, daemon=True).start()

    try:
        res = _read_line_raw("\n你> ", files, skills, True, _win_get_key, con.stream())
        for name in ("初始", "敲@", "@rep", "选中"):
            print(f"\n=== {name} ===\n{snaps.get(name, '')}")

        assert not err, f"注入错误: {err}"
        rows0 = snaps["初始"].splitlines()
        assert any("你>" in r for r in rows0), f"提示符未出现:\n{snaps['初始']}"

        rows1 = snaps["敲@"].splitlines()
        assert any("你> @" in r for r in rows1), f"输入行未显示 @:\n{snaps['敲@']}"
        assert any("README.md" in r for r in rows1), f"候选缺失:\n{snaps['敲@']}"
        assert any("docs/report.md" in r for r in rows1), f"候选缺失:\n{snaps['敲@']}"
        assert any("↑↓ 选择" in r for r in rows1), f"提示行缺失:\n{snaps['敲@']}"
        idx_in = [i for i, r in enumerate(rows1) if "你> @" in r][0]
        idx_menu = [i for i, r in enumerate(rows1) if "README.md" in r][0]
        assert idx_menu > idx_in, f"菜单未在输入行下方:\n{snaps['敲@']}"
        print("\nPASS: 真控制台敲 @ 弹出候选菜单（输入行下方逐行排列）")

        rows2 = snaps["@rep"].splitlines()
        assert any("docs/report.md" in r for r in rows2), f"过滤后缺目标:\n{snaps['@rep']}"
        assert not any("README.md" in r for r in rows2), f"过滤未生效:\n{snaps['@rep']}"
        print("PASS: @rep 前缀过滤生效（README.md 已被过滤掉）")

        rows4 = snaps["选中"].splitlines()
        line_in = [r for r in rows4 if "你>" in r][0]
        assert "@" not in line_in, f"@token 未摘除:\n{snaps['选中']}"
        assert not any("↑↓ 选择" in r for r in rows4), f"面板未收起:\n{snaps['选中']}"
        print("PASS: Enter 选中后 @token 摘除、面板收起")

        assert res.picked == ["docs/report2.md"], res.picked
        print(f"PASS: ↓ 移动高亮后选中第 2 项 → 附件 {res.picked}（msvcrt 读键链路通）")

        # ---- 第二轮：/ 面板（指令 + 技能分组、过滤、回填）----
        snaps2: dict[str, str] = {}

        def drive2() -> None:
            try:
                time.sleep(0.9)
                con.type_text("/")
                time.sleep(0.4)
                snaps2["slash"] = con.screen()
                con.type_text("ui")
                time.sleep(0.4)
                snaps2["slash_ui"] = con.screen()
                con.press_enter()
                time.sleep(0.4)
                snaps2["picked"] = con.screen()
                con.press_enter()
            except Exception as e:                 # pragma: no cover
                import traceback
                traceback.print_exc()
                err.append(f"{type(e).__name__}: {e}")
                con.ctrl_c()

        threading.Thread(target=drive2, daemon=True).start()
        res2 = _read_line_raw("\n你> ", files, skills, True, _win_get_key, con.stream())
        for name in ("slash", "slash_ui", "picked"):
            print(f"\n=== {name} ===\n{snaps2.get(name, '')}")

        rows_s = snaps2["slash"].splitlines()
        assert any("指令" in r for r in rows_s), f"/ 面板未出现「指令」分组:\n{snaps2['slash']}"
        assert any("/reset" in r for r in rows_s), f"指令条目缺失:\n{snaps2['slash']}"
        print("PASS: 真控制台敲 / 弹出指令面板")

        rows_u = snaps2["slash_ui"].splitlines()
        assert any("技能" in r for r in rows_u), f"未切到技能分组:\n{snaps2['slash_ui']}"
        assert any("UI设计系统" in r for r in rows_u), f"技能条目缺失:\n{snaps2['slash_ui']}"
        print("PASS: /ui 过滤到技能（显示中文名 UI设计系统）")

        assert res2.text.strip() == "/skill ui-design-system", res2.text
        print(f"PASS: 选中技能回填 {res2.text!r}")
        print("\nALL REAL-CONSOLE TESTS PASS")
    finally:
        con.close()


if __name__ == "__main__":
    if os.name != "nt":
        print("SKIP: 仅 Windows")
        sys.exit(0)
    main()
