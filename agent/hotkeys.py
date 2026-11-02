"""全局热键监听（Windows）。

ctypes 调 RegisterHotKey 注册全局热键（默认 Alt+V），专用线程跑消息循环，
WM_HOTKEY 到达时执行回调。零第三方依赖；仅 Windows，其它平台 start() 返回 False。
"""
from __future__ import annotations

import ctypes
import sys
import threading
from typing import Callable

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
VK_V = 0x56

_HOTKEY_ID = 0xB0A1  # 随便挑个不冲突的 id


class _MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("message", ctypes.c_uint),
        ("wParam", ctypes.c_size_t),
        ("lParam", ctypes.c_size_t),
        ("time", ctypes.c_uint),
        ("pt_x", ctypes.c_long),
        ("pt_y", ctypes.c_long),
    ]


def is_supported() -> bool:
    return sys.platform.startswith("win")


class HotkeyListener:
    """全局热键。start() 注册并起消息线程；stop() 注销退出。"""

    def __init__(self, callback: Callable[[], None],
                 modifiers: int = MOD_ALT, vk: int = VK_V,
                 hotkey_id: int = _HOTKEY_ID):
        self.callback = callback
        self.modifiers = modifiers
        self.vk = vk
        self.hotkey_id = hotkey_id
        self._thread: threading.Thread | None = None
        self._tid: int | None = None
        self._ready = threading.Event()
        self._ok = False

    def start(self) -> bool:
        """注册热键并启动监听线程。成功返回 True（失败=平台不支持或快捷键被占）。"""
        if not is_supported():
            return False
        if self._thread and self._thread.is_alive():
            return self._ok
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="hotkey-listener")
        self._thread.start()
        self._ready.wait(timeout=3.0)
        return self._ok

    def stop(self) -> None:
        if self._tid is not None:
            ctypes.windll.user32.PostThreadMessageW(int(self._tid), WM_QUIT, 0, 0)
        if self._thread:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._tid = None

    def _run(self) -> None:
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        self._tid = k32.GetCurrentThreadId()

        if not u32.RegisterHotKey(None, self.hotkey_id, self.modifiers, self.vk):
            self._ok = False
            self._ready.set()
            return
        self._ok = True
        self._ready.set()

        msg = _MSG()
        try:
            while True:
                r = u32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r <= 0:  # WM_QUIT 或错误
                    break
                if msg.message == WM_HOTKEY and msg.wParam == self.hotkey_id:
                    try:
                        self.callback()
                    except Exception:  # 回调异常不能带走消息循环
                        pass
        finally:
            u32.UnregisterHotKey(None, self.hotkey_id)


def hotkey_label(modifiers: int = MOD_ALT, vk: int = VK_V) -> str:
    parts = []
    if modifiers & MOD_CONTROL:
        parts.append("Ctrl")
    if modifiers & MOD_SHIFT:
        parts.append("Shift")
    if modifiers & MOD_ALT:
        parts.append("Alt")
    parts.append(chr(vk).upper() if 0x30 <= vk <= 0x5A else f"Vk{vk:X}")
    return "+".join(parts)
