"""Generate webui/static/favicon.ico (16/32/48) without third-party deps.

Draws the same design as favicon.svg (rounded square + ◎ ring/dot on a
blue gradient) via supersampled rasterization, encodes each size as a
PNG, and packs them into a ICO container (PNG-in-ICO, Vista+ standard).

Run:  python scripts/make_favicon.py
"""
import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "webui" / "static" / "favicon.ico"
SIZES = (16, 32, 48)
SUB = 3  # 3x3 supersampling per pixel

# brand colors (light-theme accent -> hover accent, same as favicon.svg)
C_TOP = (0x5B, 0x8B, 0xFF)
C_BOT = (0x2E, 0x5C, 0xE0)
WHITE = (255, 255, 255)


def rounded_rect_cov(px, py, s, radius):
    """Coverage (0/1) of point inside a 0..s square with rounded corners."""
    r = radius
    if r <= 0:
        return 1.0
    cx = min(max(px, r), s - r)
    cy = min(max(py, r), s - r)
    dx, dy = px - cx, py - cy
    return 1.0 if dx * dx + dy * dy <= r * r else 0.0


def render(size):
    s = float(size)
    ring_r = s * 17 / 64      # ring centerline radius
    ring_w = s * 5 / 64       # ring stroke width
    dot_r = s * 6.5 / 64      # center dot radius
    corner = s * 14 / 64      # rounded-square corner radius
    step = 1.0 / SUB

    rows = []  # raw RGBA rows
    for y in range(size):
        row = bytearray()
        for x in range(size):
            r = g = b = a = 0.0
            for sy in range(SUB):
                for sx in range(SUB):
                    px = x + (sx + 0.5) * step
                    py = y + (sy + 0.5) * step
                    if not rounded_rect_cov(px, py, s, corner):
                        continue
                    # gradient t over the diagonal
                    t = (px + py) / (2.0 * s)
                    cr = C_TOP[0] + (C_BOT[0] - C_TOP[0]) * t
                    cg = C_TOP[1] + (C_BOT[1] - C_TOP[1]) * t
                    cb = C_TOP[2] + (C_BOT[2] - C_TOP[2]) * t
                    d = math.hypot(px - s / 2, py - s / 2)
                    if abs(d - ring_r) <= ring_w / 2 or d <= dot_r:
                        cr = cg = cb = 255
                    r += cr; g += cg; b += cb; a += 255
            n = SUB * SUB
            row += bytes((int(r / n + 0.5), int(g / n + 0.5),
                          int(b / n + 0.5), int(a / n + 0.5)))
        rows.append(bytes(row))
    return b"".join(rows)


def png_encode(size, raw):
    """Minimal RGBA PNG encoder (zlib + struct only)."""
    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    scan = b"".join(b"\x00" + raw[y * size * 4:(y + 1) * size * 4]
                    for y in range(size))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(scan, 9))
            + chunk(b"IEND", b""))


def pack_ico(pngs):
    """PNG-in-ICO container: ICONDIR + entries + image blobs."""
    count = len(pngs)
    header = struct.pack("<HHH", 0, 1, count)
    entries, offset = b"", 6 + 16 * count
    for size, png in pngs:
        b = 0 if size >= 256 else size  # 0 means 256
        entries += struct.pack("<BBBBHHII", b, b, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    return header + entries + b"".join(png for _, png in pngs)


def main():
    pngs = [(s, png_encode(s, render(s))) for s in SIZES]
    OUT.write_bytes(pack_ico(pngs))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
