"""web_screenshot — 把网页/HTML 截成图片，附给视觉模型看。

和 browse_url 的区别：
  browse_url      → 取正文文本（省 context，模型读字）
  web_screenshot  → 出图片（模型看画面：版式/配色/对齐/错位）

浏览器复用 browse_url 里的单例（_pw_page），避免重复冷启动，
也就顺带继承了那套 --disable-gpu 参数——本地模型和浏览器抢显存会一起崩。
"""
from __future__ import annotations

import base64
import re
import tempfile
import time
from pathlib import Path

from agent.registry import ToolContext, ToolResult, tool
from tools.browse_url import pw_close, pw_new_page

OUT_DIR_NAME = "ollama_agent_render"   # 与 render_sheet 共用输出目录

DEFAULT_W, DEFAULT_H = 1280, 900
MIN_W, MAX_W = 320, 2560
MIN_H, MAX_H = 240, 2400

# 整页长图很容易几 MB，base64 再放大 1/3，直接撑爆视觉模型的 context。
# 双保险：像素上截断高度，字节上退化成 JPEG。
MAX_PAGE_H = 5000
MAX_BYTES = 1_200_000
JPEG_QUALITY = 80


def _clamp(value, lo: int, hi: int, default: int) -> int:
    """把外部传进来的尺寸夹到安全区间，脏值落回默认。"""
    try:
        return max(lo, min(int(value), hi))
    except (TypeError, ValueError):
        return default


@tool(
    name="web_screenshot",
    description=(
        "把网页或一段 HTML 截成图片，返回后视觉模型能直接看到页面长什么样。"
        "用于「截个图看看 / 这页面渲染对不对 / UI 有没有错位 / 看看排版效果」这类需要看画面的场景。"
        "只想要文字内容用 browse_url（更省 context）；表格文件用 render_sheet；"
        "本地已存在的图片文件用 load_image。"
        "url 与 html 二选一，都传会报错；full_page=true 截整页长图，默认只截首屏。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "http/https 链接，与 html 二选一"},
            "html": {"type": "string",
                     "description": "直接渲染的 HTML 片段（本地预览/调试用），与 url 二选一"},
            "full_page": {"type": "boolean",
                          "description": "true 截整页长图；默认 false 只截首屏"},
            "width": {"type": "integer",
                      "description": f"视口宽度，默认 {DEFAULT_W}，范围 {MIN_W}-{MAX_W}"},
            "height": {"type": "integer",
                       "description": f"视口高度，默认 {DEFAULT_H}，范围 {MIN_H}-{MAX_H}"},
        },
    },
    confirm=True,
)
def web_screenshot(ctx: ToolContext, url: str = "", html: str = "",
                   full_page: bool = False,
                   width: int = DEFAULT_W, height: int = DEFAULT_H) -> ToolResult:
    url = (url or "").strip()
    html = html or ""
    if not url and not html.strip():
        return ToolResult("[错误] url 和 html 至少要给一个")
    if url and html.strip():
        return ToolResult("[错误] url 与 html 只能二选一，不要同时传")
    if url and not re.match(r"^https?://", url):
        return ToolResult("[错误] url 只支持 http/https 链接")

    w = _clamp(width, MIN_W, MAX_W, DEFAULT_W)
    h = _clamp(height, MIN_H, MAX_H, DEFAULT_H)

    try:
        page = pw_new_page()
    except RuntimeError as e:
        return ToolResult(f"[错误] {e}")

    title = ""
    truncated = False
    fmt = "png"
    try:
        page.set_viewport_size({"width": w, "height": h})
        if url:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(500)          # 等首屏 JS 渲染
        else:
            page.set_content(html, wait_until="load")
        title = page.title()

        shot = {"type": "png"}
        if full_page:
            doc_h = page.evaluate(
                "() => Math.max(document.documentElement.scrollHeight,"
                " document.body ? document.body.scrollHeight : 0)")
            if doc_h and doc_h > MAX_PAGE_H:
                truncated = True
                # clip 必须配 full_page=True：单给 clip 时超出视口的部分会被裁掉，
                # 结果只有一屏高（实测 900px 页面拿去 clip 5000px 仍只出 900x900）
                shot["full_page"] = True
                shot["clip"] = {"x": 0, "y": 0, "width": w, "height": MAX_PAGE_H}
            else:
                shot["full_page"] = True

        raw = page.screenshot(**shot)

        # PNG 太大就退化成 JPEG，宁可损失一点画质也不让 base64 撑爆上下文
        if len(raw) > MAX_BYTES:
            try:
                jpeg_shot = {k: v for k, v in shot.items() if k != "type"}
                jpeg_shot.update({"type": "jpeg", "quality": JPEG_QUALITY})
                raw = page.screenshot(**jpeg_shot)
                fmt = "jpeg"
            except Exception:
                pass                            # JPEG 失败就保留原 PNG
    except Exception as e:
        pw_close()                              # 页面崩了就重启浏览器，下次调用可用
        return ToolResult(
            f"[错误] 截图失败 {url or '(inline html)'}: "
            f"{type(e).__name__}: {str(e)[:300]}")
    finally:
        try:
            page.close()
        except Exception:
            pass

    out_dir = Path(tempfile.gettempdir()) / OUT_DIR_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"shot_{int(time.time() * 1000) % 10 ** 8}.{'jpg' if fmt == 'jpeg' else 'png'}"
    out.write_bytes(raw)
    b64 = base64.b64encode(raw).decode("ascii")

    note = f"（页面过高，只截了前 {MAX_PAGE_H}px）" if truncated else ""
    if truncated:
        dim = f"视口 {w}x{h} · 输出 {w}x{MAX_PAGE_H}（已截断）"
    elif full_page:
        dim = f"视口 {w}x{h} · 整页长图"
    else:
        dim = f"{w}x{h}（首屏）"
    return ToolResult(
        f"[已截图] {url or '(inline html)'}{note}\n"
        f"[标题] {title or '(无)'}\n"
        f"[规格] {dim} · {fmt.upper()} · {len(raw) / 1024:.0f} KB\n"
        f"[保存] {out}\n"
        f"图片已附在本条消息中，直接看画面判断版式/配色/对齐是否正确。",
        images=[b64],
    )
