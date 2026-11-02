"""系统剪贴板图片读取（Windows，零第三方依赖）。

截图（Win+Shift+S、QQ/微信截图等）通常把位图放剪贴板（CF_DIB/CF_PNG），
本模块用 ctypes 调 Win32 API 抓出来，用标准库 zlib 编码成 PNG。

对外：
    grab_image_png() -> bytes           # PNG 字节；无图/失败抛 ClipboardError
    grab_and_cache(workspace) -> Path   # 抓图并缓存到 {workspace}/.clip_cache/
"""
from __future__ import annotations

import ctypes
import struct
import sys
import time
import zlib
from datetime import datetime
from pathlib import Path

CACHE_DIRNAME = ".clip_cache"

# Win32 常量
_CF_DIB = 8
_CF_HDROP = 15
_GMEM_MOVEABLE = 0x0002
_BI_RGB = 0
_BI_BITFIELDS = 3


class ClipboardError(RuntimeError):
    """剪贴板不可用 / 无图 / 格式不支持。"""


# ---------------- PNG 编码（纯标准库） ----------------

def png_encode(width: int, height: int, rgba: bytes) -> bytes:
    """把 RGBA 位图（每行连续 width*4 字节，自上而下）编码为 PNG 字节。"""
    if len(rgba) != width * height * 4:
        raise ValueError(f"RGBA 长度不匹配: {len(rgba)} != {width * height * 4}")

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    stride = width * 4
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter: None
        raw += rgba[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 6))
            + chunk(b"IEND", b""))


def _png_size(png: bytes) -> tuple[int, int]:
    w, h = struct.unpack(">II", png[16:24])
    return w, h


# ---------------- DIB → RGBA ----------------

def dib_to_rgba(dib: bytes) -> tuple[int, int, bytes]:
    """解析 CF_DIB 位图 → (width, height, rgba 自上而下)。"""
    if len(dib) < 40:
        raise ClipboardError("剪贴板位图数据不完整")
    bi_size, width, height, planes, bpp, comp = struct.unpack_from("<IiiHHI", dib, 0)
    if width <= 0 or height == 0:
        raise ClipboardError(f"非法位图尺寸: {width}x{height}")
    top_down = height < 0
    abs_h = abs(height)
    if bpp not in (24, 32):
        raise ClipboardError(f"暂不支持 {bpp} 位深剪贴板位图（仅 24/32 位）")

    row_stride = ((width * bpp + 31) // 32) * 4
    need = row_stride * abs_h

    # 像素数据起点：常规在 bi_size 后；BI_BITFIELDS(且 biSize=40) 时掩码占 12/16 字节
    candidates = [bi_size]
    if comp == _BI_BITFIELDS and bi_size == 40:
        candidates = [52, 56, 40]  # 40+12(3掩码) / 40+16(4掩码) / 兼容罕见情况
    pixel_off = next((o for o in candidates if o + need <= len(dib)), None)
    if pixel_off is None:
        raise ClipboardError("剪贴板位图数据长度不足")

    # 通道掩码（默认 BGRA 在内存中的常见布局）
    rmask, gmask, bmask = 0x00FF0000, 0x0000FF00, 0x000000FF
    if comp == _BI_BITFIELDS and bi_size == 40 and pixel_off >= 52:
        rmask, gmask, bmask = struct.unpack_from("<III", dib, 40)

    def expand(val: int, mask: int) -> int:
        if not mask:
            return 0
        shift = (mask & -mask).bit_length() - 1
        bits = (mask >> shift).bit_length()
        v = (val & mask) >> shift
        if bits < 8:
            v = v * 255 // ((1 << bits) - 1)
        return min(v, 255)

    rgba = bytearray(width * abs_h * 4)
    std_bgrra = (bpp == 32 and (comp == _BI_RGB
                                or (rmask, gmask, bmask) == (0x00FF0000, 0x0000FF00, 0x000000FF)))
    for row in range(abs_h):
        src_row = (abs_h - 1 - row) if not top_down else row
        off = pixel_off + src_row * row_stride
        dst = row * width * 4
        if std_bgrra:
            # 内存序 B,G,R,A/X → 直接切片换位（快路径；切片限定在本行内）
            seg = dib[off:off + width * 4]
            end = dst + width * 4
            rgba[dst:end:4] = seg[2::4]
            rgba[dst + 1:end:4] = seg[1::4]
            rgba[dst + 2:end:4] = seg[0::4]
            rgba[dst + 3:end:4] = b"\xff" * width
        elif bpp == 32:
            for x in range(width):
                val = int.from_bytes(dib[off + x * 4:off + x * 4 + 4], "little")
                i = dst + x * 4
                rgba[i] = expand(val, rmask)
                rgba[i + 1] = expand(val, gmask)
                rgba[i + 2] = expand(val, bmask)
                rgba[i + 3] = 255
        else:  # 24bpp：内存序 B,G,R
            for x in range(width):
                b = off + x * 3
                i = dst + x * 4
                rgba[i] = dib[b + 2]
                rgba[i + 1] = dib[b + 1]
                rgba[i + 2] = dib[b]
                rgba[i + 3] = 255
    return width, abs_h, bytes(rgba)


# ---------------- Windows 抓取 ----------------

def _win32() -> tuple:
    if not sys.platform.startswith("win"):
        raise ClipboardError(f"当前平台 {sys.platform} 暂只支持 Windows 剪贴板取图")
    u32 = ctypes.windll.user32
    k32 = ctypes.windll.kernel32
    u32.OpenClipboard.argtypes = [ctypes.c_void_p]
    u32.OpenClipboard.restype = ctypes.c_int
    u32.GetClipboardData.argtypes = [ctypes.c_uint]
    u32.GetClipboardData.restype = ctypes.c_void_p
    u32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
    u32.IsClipboardFormatAvailable.restype = ctypes.c_int
    u32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
    u32.RegisterClipboardFormatW.restype = ctypes.c_uint
    u32.CloseClipboard.argtypes = []
    u32.CloseClipboard.restype = ctypes.c_int
    u32.EmptyClipboard.argtypes = []
    u32.EmptyClipboard.restype = ctypes.c_int
    u32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p
    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalLock.restype = ctypes.c_void_p
    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.restype = ctypes.c_int
    k32.GlobalSize.argtypes = [ctypes.c_void_p]
    k32.GlobalSize.restype = ctypes.c_size_t
    k32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k32.GlobalAlloc.restype = ctypes.c_void_p
    return u32, k32


def _open_clipboard(u32) -> bool:
    for _ in range(12):
        if u32.OpenClipboard(None):
            return True
        time.sleep(0.05)
    return False


def grab_image_png() -> bytes:
    """从系统剪贴板取图片 → PNG 字节。"""
    u32, k32 = _win32()
    if not _open_clipboard(u32):
        raise ClipboardError("剪贴板被其它程序占用，请稍后再试")
    try:
        # 1) 部分截图工具直接放 PNG 格式
        fmt_png = u32.RegisterClipboardFormatW("PNG")
        if fmt_png and u32.IsClipboardFormatAvailable(fmt_png):
            h = u32.GetClipboardData(fmt_png)
            if h:
                ptr = k32.GlobalLock(h)
                try:
                    size = k32.GlobalSize(h)
                    if ptr and size > 8:
                        data = ctypes.string_at(ptr, size)
                        if data[:8] == b"\x89PNG\r\n\x1a\n":
                            return data
                finally:
                    k32.GlobalUnlock(h)

        # 2) 标准 CF_DIB
        if not u32.IsClipboardFormatAvailable(_CF_DIB):
            raise ClipboardError("剪贴板里没有图片（先截图或复制图片再试）")
        h = u32.GetClipboardData(_CF_DIB)
        if not h:
            raise ClipboardError("读取剪贴板图片句柄失败")
        ptr = k32.GlobalLock(h)
        if not ptr:
            raise ClipboardError("锁定剪贴板内存失败")
        try:
            size = k32.GlobalSize(h)
            dib = ctypes.string_at(ptr, size)
        finally:
            k32.GlobalUnlock(h)
        w, hh, rgba = dib_to_rgba(dib)
        return png_encode(w, hh, rgba)
    finally:
        u32.CloseClipboard()


def grab_files() -> list[Path]:
    """从剪贴板取复制的文件列表（Explorer Ctrl+C，CF_HDROP）。

    返回绝对路径列表；剪贴板里没有文件时返回 []；失败抛 ClipboardError。
    """
    u32, k32 = _win32()
    shell32 = ctypes.windll.shell32
    shell32.DragQueryFileW.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                       ctypes.c_wchar_p, ctypes.c_uint]
    shell32.DragQueryFileW.restype = ctypes.c_uint
    if not _open_clipboard(u32):
        raise ClipboardError("剪贴板被其它程序占用，请稍后再试")
    try:
        if not u32.IsClipboardFormatAvailable(_CF_HDROP):
            return []
        h = u32.GetClipboardData(_CF_HDROP)
        if not h:
            return []
        # HDROP 可直接传给 DragQueryFile（句柄即内存指针）
        count = shell32.DragQueryFileW(h, 0xFFFFFFFF, None, 0)
        out: list[Path] = []
        for i in range(count):
            n = shell32.DragQueryFileW(h, i, None, 0)  # 取长度（不含 NUL）
            buf = ctypes.create_unicode_buffer(n + 1)
            shell32.DragQueryFileW(h, i, buf, n + 1)
            if buf.value:
                out.append(Path(buf.value))
        return out
    finally:
        u32.CloseClipboard()


def grab_and_cache(workspace: Path) -> Path:
    """抓剪贴板图片并缓存到 {workspace}/.clip_cache/clip_时间戳.png，返回缓存路径。"""
    png = grab_image_png()
    cache = Path(workspace) / CACHE_DIRNAME
    cache.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    p = cache / f"clip_{stamp}.png"
    n = 1
    while p.exists():
        p = cache / f"clip_{stamp}_{n}.png"
        n += 1
    p.write_bytes(png)
    return p


def grab_any(workspace: Path) -> tuple[list[Path], Path | None]:
    """统一抓剪贴板：优先文件（Ctrl+C），其次图片（截图）。

    返回 (copied_files, cached_image_or_None)。
    两者都没有时 ([], None)；剪贴板被占抛 ClipboardError。
    """
    files = grab_files()
    if files:
        return files, None
    try:
        return [], grab_and_cache(workspace)
    except ClipboardError:
        return [], None


# ---------------- 测试辅助（写剪贴板，供自测用） ----------------

def _set_dib_for_test(width: int, height: int, rgba: bytes) -> None:
    """把 RGBA 写成 CF_DIB 放进剪贴板（仅自测用）。"""
    u32, k32 = _win32()
    row_stride = ((width * 32 + 31) // 32) * 4
    header = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 32, _BI_RGB,
                         row_stride * height, 0, 0, 0, 0)
    body = bytearray(row_stride * height)
    for row in range(height):
        src_row = height - 1 - row  # 自下而上
        s = src_row * width * 4
        d = row * row_stride
        for x in range(width):
            i = s + x * 4
            body[d + x * 4:d + x * 4 + 4] = bytes((rgba[i + 2], rgba[i + 1], rgba[i], 255))
    dib = header + bytes(body)

    if not _open_clipboard(u32):
        raise ClipboardError("剪贴板被占用，无法写入测试图")
    try:
        u32.EmptyClipboard()
        h = k32.GlobalAlloc(_GMEM_MOVEABLE, len(dib))
        if not h:
            raise ClipboardError("GlobalAlloc 失败")
        ptr = k32.GlobalLock(h)
        if not ptr:
            raise ClipboardError("GlobalLock 失败")
        ctypes.memmove(ptr, dib, len(dib))
        k32.GlobalUnlock(h)
        if not u32.SetClipboardData(_CF_DIB, h):
            raise ClipboardError("SetClipboardData 失败")
        # 成功后句柄归系统所有，不释放
    finally:
        u32.CloseClipboard()


def _set_files_for_test(paths: list[str]) -> None:
    """把文件列表写成 CF_HDROP 放进剪贴板（仅自测用，模拟 Explorer Ctrl+C）。"""
    u32, k32 = _win32()
    # DROPFILES 头(20 字节) + 宽字符双 NUL 结尾的路径列表
    import struct as _st
    list_w = "".join(p + "\0" for p in paths) + "\0"
    blob = _st.pack("<IiiII", 20, 0, 0, 0, 1) + list_w.encode("utf-16-le")
    if not _open_clipboard(u32):
        raise ClipboardError("剪贴板被占用，无法写入测试文件列表")
    try:
        u32.EmptyClipboard()
        h = k32.GlobalAlloc(_GMEM_MOVEABLE, len(blob))
        if not h:
            raise ClipboardError("GlobalAlloc 失败")
        ptr = k32.GlobalLock(h)
        if not ptr:
            raise ClipboardError("GlobalLock 失败")
        ctypes.memmove(ptr, blob, len(blob))
        k32.GlobalUnlock(h)
        if not u32.SetClipboardData(_CF_HDROP, h):
            raise ClipboardError("SetClipboardData 失败")
    finally:
        u32.CloseClipboard()
