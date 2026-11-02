"""browse_url — Playwright 打开网页提取正文。依赖可选：pip install playwright。"""
from __future__ import annotations

import re

from agent.registry import ToolContext, ToolResult, tool

# 浏览器单例懒加载（整轮对话复用，避免每次冷启动）
_PW: dict = {"playwright": None, "browser": None, "page": None}

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


def _pw_page():
    if _PW["page"] is not None:
        return _PW["page"]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("未安装 playwright。请执行: pip install playwright && playwright install chromium")
    _PW["playwright"] = sync_playwright().start()
    # --disable-gpu*：4GB 显卡还要喂本地模型，浏览器再抢显存就会一起崩
    # （实测 web_chat 报 Target crashed，就是显存/内存被两边挤爆）
    _PW["browser"] = _PW["playwright"].chromium.launch(
        headless=True,
        args=[
            "--disable-gpu",
            "--disable-gpu-compositing",
            "--disable-software-rasterizer",
            "--disable-dev-shm-usage",
        ],
    )
    _PW["page"] = _PW["browser"].new_page(
        user_agent=USER_AGENT, viewport={"width": 1280, "height": 900})
    return _PW["page"]


def pw_new_page():
    """新开一个独立页面（调用方负责 page.close()），避免共享页状态串扰。"""
    _pw_page()  # 确保浏览器已启动
    return _PW["browser"].new_page(
        user_agent=USER_AGENT, viewport={"width": 1280, "height": 900})


def pw_close() -> None:
    """进程退出前调用，释放浏览器。"""
    for key in ("page", "browser", "playwright"):
        obj = _PW.get(key)
        if obj is not None:
            try:
                obj.stop() if key == "playwright" else obj.close()
            except Exception:
                pass
            _PW[key] = None


@tool(
    name="browse_url",
    description="用 Playwright 打开网页并提取正文文本。读文档、文章、链接内容用这个。",
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "http/https 链接"},
            "max_chars": {"type": "integer", "description": "正文最多返回字符数，默认 12000"},
        },
        "required": ["url"],
    },
    confirm=True,
)
def browse_url(ctx: ToolContext, url: str, max_chars: int = 12000) -> ToolResult:
    if not re.match(r"^https?://", url or ""):
        return ToolResult("[错误] 只支持 http/https 链接")
    try:
        page = _pw_page()
    except RuntimeError as e:
        return ToolResult(f"[错误] {e}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(500)  # 等首屏 JS
        title = page.title()
        text = page.evaluate("""() => {
            document.querySelectorAll('script,style,noscript,svg,iframe,template')
                .forEach(e => e.remove());
            return document.body ? document.body.innerText : '';
        }""")
    except Exception as e:
        pw_close()  # 页面崩了重启浏览器，下次调用可用
        return ToolResult(f"[错误] 打开失败 {url}: {type(e).__name__}: {str(e)[:300]}")
    text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    max_chars = int(max_chars or 12000)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n…[已截断，原文 {len(text)} 字符]"
    return ToolResult(f"[网页] {url}\n[标题] {title}\n\n{text}")
