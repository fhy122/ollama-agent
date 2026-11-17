"""browse_url — Playwright 打开网页提取正文。依赖可选：pip install playwright。"""
from __future__ import annotations

import os
import re

from agent.registry import ToolContext, ToolResult, tool

# 浏览器单例懒加载（整轮对话复用，避免每次冷启动）
_PW: dict = {"playwright": None, "browser": None, "page": None}

# CDP 接管已登录 Chrome 的独立单例：绝不能和 _PW 混用，也不能在退出时
# close 掉用户的浏览器——那等于替用户关了正在用的窗口。
_CDP: dict = {"pw": None, "browser": None, "endpoint": None}

# 默认调试端口，可用环境变量 OLLAMA_AGENT_CDP 覆盖（如换端口/远程 CDP）
CDP_ENDPOINT_ENV = "OLLAMA_AGENT_CDP"
CDP_ENDPOINT_DEFAULT = "http://127.0.0.1:9222"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# 反爬/验证页特征。知乎 40362 是 HTTP 200 + 一段错误 JSON，goto 不抛异常，
# 若不检测就会被当成"正文"交出去。只在短正文里匹配，避免误伤长文里恰好
# 提到这些词的正常文章。
_BLOCK_SIGNALS = (
    "40362",
    "请求存在异常",
    "暂时限制本次访问",
    "访问受限",
    "请登录",
    "enable javascript and cookies to continue",
    "checking your browser",
    "just a moment",
)
_BLOCK_MAX_CHARS = 1200

# 网页正文里的"不可见空格"：HTML &nbsp; 会被 innerText 解码成 U+00A0，
# 编辑器常显形成 · 或高亮、复制粘贴也容易乱。只折叠空格类字符，
# 不用 unicodedata.normalize("NFKC")——那会把全角字母/上标数字一并改掉，误伤正文。
_INVISIBLE_SPACE_MAP = dict.fromkeys(
    (0x00A0, 0x1680, 0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005,
     0x2006, 0x2007, 0x2008, 0x2009, 0x200A, 0x202F, 0x205F, 0x3000), " ")
_INVISIBLE_ZERO_WIDTH = dict.fromkeys((0x200B, 0xFEFF), None)  # 零宽：直接删
_INVISIBLE_TRANS = {**_INVISIBLE_SPACE_MAP, **_INVISIBLE_ZERO_WIDTH}


def _normalize_text(text: str) -> str:
    """把正文里不可见空格归一成普通空格、零宽字符删除；其余原样保留。"""
    return (text or "").translate(_INVISIBLE_TRANS)


def _detect_block(title: str, text: str) -> str | None:
    """返回命中的拦截特征；正文超过阈值视为正常文章，不做匹配。"""
    body = (text or "").strip()
    if len(body) > _BLOCK_MAX_CHARS:
        return None
    # 大小写不敏感：Cloudflare 页面实际写 "Checking your browser"，
    # 特征表里存的是小写，不归一就会漏。
    hay = (body + "\n" + (title or "")).lower()
    for sig in _BLOCK_SIGNALS:
        if sig.lower() in hay:
            return sig
    return None


_EXTRACT_JS = """() => {
    document.querySelectorAll('script,style,noscript,svg,iframe,template')
        .forEach(e => e.remove());
    return document.body ? document.body.innerText : '';
}"""


def _read_page(page, *, tries: int = 3, settle_ms: int = 1000) -> tuple[str, str]:
    """返回 (title, text)。JS 导航会销毁 evaluate 的执行上下文（实测知乎首屏
    500ms 时必现），遇到就等 settle_ms 重试，而不是把 500ms 拍成一个更大的数。"""
    last: Exception | None = None
    for i in range(tries):
        if i:
            page.wait_for_timeout(settle_ms)
        try:
            return page.title(), page.evaluate(_EXTRACT_JS)
        except Exception as e:
            msg = str(e)
            if ("Execution context was destroyed" in msg
                    or "Cannot find context" in msg
                    or "navigating" in msg.lower()):
                last = e
                continue
            raise
    assert last is not None
    raise last


def _cdp_page():
    """连接用户已启动的带 --remote-debugging-port 的 Chrome，开一个新页。"""
    from playwright.sync_api import sync_playwright
    endpoint = os.environ.get(CDP_ENDPOINT_ENV, CDP_ENDPOINT_DEFAULT).strip()
    if _CDP["browser"] is None:
        _CDP["pw"] = sync_playwright().start()
        _CDP["browser"] = _CDP["pw"].chromium.connect_over_cdp(endpoint)
        _CDP["endpoint"] = endpoint
    elif _CDP["endpoint"] != endpoint:
        # 端点变了就重连。注意：绝不对用户浏览器调 close()（部分 Playwright
        # 版本会真的终止进程），有 disconnect 就只用 disconnect。
        old = _CDP["browser"]
        _CDP["browser"] = _CDP["pw"].chromium.connect_over_cdp(endpoint)
        _CDP["endpoint"] = endpoint
        try:
            (getattr(old, "disconnect", None) or (lambda: None))()
        except Exception:
            pass
    contexts = _CDP["browser"].contexts
    if not contexts:
        raise RuntimeError("已连接但浏览器没有任何上下文")
    return contexts[0].new_page()


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
    # CDP：只断开连接、停掉本地 driver，绝不 close browser（那是用户的 Chrome）
    cdp_browser = _CDP.get("browser")
    if cdp_browser is not None:
        try:
            (getattr(cdp_browser, "disconnect", None) or (lambda: None))()
        except Exception:
            pass
        _CDP["browser"] = None
    cdp_pw = _CDP.get("pw")
    if cdp_pw is not None:
        try:
            cdp_pw.stop()
        except Exception:
            pass
        _CDP["pw"] = None


@tool(
    name="browse_url",
    description=(
        "用 Playwright 打开网页并提取正文文本。读文档、文章、链接内容用这个。"
        "默认 identity=guest 用独立无登录浏览器；遇到反爬/登录墙（返回 [已拦截] 提示）时，"
        "改用 identity=user 接管本机已登录的 Chrome（需先用 --remote-debugging-port=9222 "
        "启动 Chrome 并登录目标站）。只陈述本轮实际读到的正文，被拦截就明说，不编造内容。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "http/https 链接"},
            "max_chars": {"type": "integer", "description": "正文最多返回字符数；不传则不在此截断，超长由 spill 整文落盘"},
            "identity": {"type": "string", "enum": ["guest", "user"],
                         "description": "guest=独立无登录浏览器(默认)；user=接管本机已登录 Chrome(CDP 9222)，用于过登录墙/反爬"},
        },
        "required": ["url"],
    },
    confirm=True,
)
def browse_url(ctx: ToolContext, url: str, max_chars: int | None = None,
               identity: str = "guest") -> ToolResult:
    if not re.match(r"^https?://", url or ""):
        return ToolResult("[错误] 只支持 http/https 链接")
    if identity not in ("guest", "user"):
        return ToolResult(f"[错误] identity 只能是 guest 或 user，收到: {identity}")

    if identity == "user":
        try:
            page = _cdp_page()
        except Exception as e:
            return ToolResult(
                f"[错误] 无法连接已登录 Chrome: {type(e).__name__}: {str(e)[:200]}\n"
                "请先完全关闭 Chrome，再用 --remote-debugging-port=9222 重新启动并登录目标站。\n"
                f"（CDP 端点可用环境变量 {CDP_ENDPOINT_ENV} 覆盖，默认 {CDP_ENDPOINT_DEFAULT}）")
    else:
        try:
            page = _pw_page()
        except RuntimeError as e:
            return ToolResult(f"[错误] {e}")

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(500)  # 等首屏 JS
        title, text = _read_page(page)
    except Exception as e:
        if identity == "user":
            try:
                page.close()
            except Exception:
                pass
        else:
            pw_close()  # 页面崩了重启浏览器，下次调用可用
        return ToolResult(f"[错误] 打开失败 {url}: {type(e).__name__}: {str(e)[:300]}")

    if identity == "user":
        try:
            page.close()   # CDP 新页用完关掉，别在用户窗口堆标签
        except Exception:
            pass

    text = re.sub(r"\n{3,}", "\n\n", _normalize_text(text or "")).strip()
    hit = _detect_block(title, text)
    if hit:
        # 卡住就报卡在哪 —— 绝不把风控 JSON 当正文交出去（对应纪律 1/2/3/5）
        return ToolResult(
            f"[已拦截] {url}\n[标题] {title}\n[拦截特征] {hit}\n\n"
            f"本轮只观察到一段反爬/验证响应（{len(text)} 字符），未取到文章正文。"
            "不编造内容。请二选一：\n"
            "  ① identity=user 接管已登录 Chrome（先 --remote-debugging-port=9222 启动并登录）\n"
            "  ② 你在浏览器里读完，把正文贴给我\n"
            f"\n[已验证片段]\n{text[:300]}")
    if max_chars:                     # 模型显式要求小窗口才在此破坏性截断；
        max_chars = int(max_chars)    # 默认交由 dispatch 的 spill 策略整文落盘，可取回
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n…[已截断，原文 {len(text)} 字符]"
    return ToolResult(f"[网页] {url}\n[标题] {title}\n[identity] {identity}\n\n{text}")
