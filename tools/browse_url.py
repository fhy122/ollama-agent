"""browse_url — Playwright 打开网页提取正文。依赖可选：pip install playwright。

线程模型（关键，别改坏）：Playwright 的 sync 对象**绑定创建它的那个线程**，
换线程碰它就抛 `Cannot switch to a different thread`；而它所在线程一退出，
底层连接就断了，旧对象还留在模块级字典里 → 下一次调用报
`TargetClosedError: Playwright connection closed`（0 秒失败）。

WebUI 是 `ThreadingHTTPServer`（每请求一个线程），所以只要把 driver 挂在
「调用线程」上，第二轮对话必然崩。因此这里所有浏览器操作都投递到一个
**常驻专用线程**（`run_on_pw`）执行，页面/浏览器对象绝不跨线程传递。
每个工具调用只投递一次「在 worker 里跑完整流程」的闭包，别拿 page 回外面用。
"""
from __future__ import annotations

import os
import queue
import re
import subprocess
import threading
import time
import urllib.request

from agent.registry import ToolContext, ToolResult, tool

# Playwright sync API 一个进程只能有一个 event loop（实测第二次 start() 会抛
# "using Sync API inside asyncio loop"）。auto 模式同一轮要先 guest 后 CDP，
# 因此全模块共享同一个 sync_playwright 实例，guest 的 launch 与 CDP 的
# connect_over_cdp 都从这一个 driver 出。
_DRIVER: dict = {"pw": None}

# 常驻 worker 线程：driver / browser / page 全部只在它里面创建和使用。
# lock 只保护「起线程」这一小段，避免两个请求同时起两个 worker。
_WORKER: dict = {"thread": None, "queue": None, "tid": None,
                 "lock": threading.RLock()}

# guest 无登录浏览器单例（懒加载，整轮复用）
_PW: dict = {"browser": None, "page": None}

# CDP 接管浏览器：绝不能在退出时 close 掉用户的窗口，只 disconnect。
_CDP: dict = {"browser": None, "endpoint": None}

# 默认调试端口，可用环境变量 OLLAMA_AGENT_CDP 覆盖（如换端口/远程 CDP）
CDP_ENDPOINT_ENV = "OLLAMA_AGENT_CDP"
CDP_ENDPOINT_DEFAULT = "http://127.0.0.1:9222"

# agent 自动启动浏览器用的固定独立 profile（不用 %TEMP% 随机目录）：
# 登录态持久化在这里，用户只需登录一次，之后所有调用自动复用会话。
# 绝不用用户日常 profile——避开 Edge 136+ 对默认 profile 的调试限制。
CDP_PROFILE_ENV = "OLLAMA_AGENT_CDP_PROFILE"
CDP_PROFILE_DEFAULT = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")),
    "ollama-agent", "cdp-profile")

# 按优先级探测本机 Chromium 系浏览器（Edge 优先，本机常只有 Edge）
_BROWSER_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
)

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


def _cdp_alive(endpoint: str, timeout: float = 1.0) -> bool:
    """探测 CDP 端点是否活着（比 connect 轻，不建 Playwright 连接）。"""
    try:
        with urllib.request.urlopen(endpoint.rstrip("/") + "/json/version",
                                    timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _find_browser() -> str | None:
    """按优先级找本机第一个存在的 Chromium 系浏览器可执行文件。"""
    for path in _BROWSER_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def _auto_start_cdp_browser(endpoint: str, *, wait: float = 15.0) -> None:
    """agent 自己以独立固定 profile 启动带 --remote-debugging-port 的浏览器。

    DETACHED_PROCESS：脱离 agent 进程，对话结束也不会带走浏览器窗口。
    固定 profile：登录态持久化，用户只登一次，之后自动复用。
    端点已活则跳过（幂等）。失败抛 RuntimeError，由调用方转成 ToolResult。
    """
    if _cdp_alive(endpoint):
        return
    exe = _find_browser()
    if not exe:
        raise RuntimeError("未找到 Chrome 或 Edge 浏览器，无法自动启动调试会话")
    profile = os.environ.get(CDP_PROFILE_ENV, CDP_PROFILE_DEFAULT)
    os.makedirs(profile, exist_ok=True)
    # 从 endpoint 解析端口（http://127.0.0.1:9222 -> 9222）
    port = endpoint.rstrip("/").rsplit(":", 1)[-1]
    args = [exe, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
            "--no-first-run", "--no-default-browser-check", "about:blank"]
    kwargs = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.CREATE_NEW_PROCESS_GROUP
                                   | subprocess.DETACHED_PROCESS)
    try:
        subprocess.Popen(args, **kwargs)
    except Exception as e:
        raise RuntimeError(f"自动启动浏览器失败: {type(e).__name__}: {e}")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if _cdp_alive(endpoint):
            return
        time.sleep(0.3)
    raise RuntimeError(f"浏览器已启动但 {endpoint} 在 {wait:.0f}s 内未就绪")


def _worker_loop(q: "queue.Queue") -> None:
    """常驻线程主体：串行取出闭包执行，异常原样回传给提交方。"""
    _WORKER["tid"] = threading.get_ident()
    while True:
        job = q.get()
        if job is None:                     # 退出信号
            return
        fn, box, done = job
        try:
            box["ok"] = fn()
        except BaseException as e:          # noqa: BLE001  原样透传给调用方
            box["err"] = e
        finally:
            _WORKER["tid"] = threading.get_ident()
            done.set()


def run_on_pw(fn, timeout: float = 180.0):
    """在常驻 Playwright 线程里执行 fn 并返回结果（跨线程安全的唯一入口）。

    已经在 worker 线程里（嵌套调用）就直接执行，避免自己等自己死锁。
    超时抛 TimeoutError：worker 卡死时宁可报错，也不让整个对话挂住。
    """
    with _WORKER["lock"]:
        th = _WORKER["thread"]
        if th is None or not th.is_alive():
            q: queue.Queue = queue.Queue()
            th = threading.Thread(target=_worker_loop, args=(q,), daemon=True,
                                  name="playwright-worker")
            th.start()
            _WORKER["queue"] = q
            _WORKER["thread"] = th
        q = _WORKER["queue"]
    if threading.get_ident() == _WORKER["tid"]:
        return fn()
    box: dict = {}
    done = threading.Event()
    q.put((fn, box, done))
    if not done.wait(timeout):
        raise TimeoutError(f"浏览器操作超过 {timeout:.0f}s 未返回")
    if "err" in box:
        raise box["err"]
    return box["ok"]


def _driver():
    """全模块唯一的 sync_playwright 实例（懒加载）。**只能在 worker 线程里调。**"""
    if _DRIVER["pw"] is None:
        from playwright.sync_api import sync_playwright
        try:
            _DRIVER["pw"] = sync_playwright().start()
        except Exception as e:
            raise RuntimeError(
                f"Playwright 启动失败（{type(e).__name__}: {str(e)[:160]}）。"
                "请确认未安装冲突的 asyncio 驱动。")
    return _DRIVER["pw"]


def _reset_pw() -> None:
    """丢弃全部浏览器状态（浏览器崩了/连接断了才调）。**只能在 worker 线程里调。**"""
    for key in ("page", "browser"):
        obj = _PW.get(key)
        _PW[key] = None
        if obj is not None:
            try:
                obj.close()
            except Exception:
                pass
    cdp_browser = _CDP.get("browser")
    _CDP["browser"] = None
    if cdp_browser is not None:
        try:
            (getattr(cdp_browser, "disconnect", None) or (lambda: None))()
        except Exception:
            pass
    pw = _DRIVER.get("pw")
    _DRIVER["pw"] = None
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass


# 连接已失效/对象跨了线程的报错特征 —— 命中就重建 driver 重试一次，
# 而不是把这句底层异常直接甩给模型当"打不开网页"。
_STALE_SIGNALS = (
    "playwright connection closed",
    "targetclosederror",
    "cannot switch to a different thread",
    "has been closed",
    "browser has been closed",
    "target page, context or browser has been closed",
)


def _is_stale(e: BaseException) -> bool:
    msg = f"{type(e).__name__}: {e}".lower()
    return any(sig in msg for sig in _STALE_SIGNALS)


def _cdp_page():
    """连到 CDP 浏览器开新页；端点没起就由 agent 自动启动（无需人工执行命令）。

    **只能在 worker 线程里调**（返回的 page 不能带出去）。
    """
    endpoint = os.environ.get(CDP_ENDPOINT_ENV, CDP_ENDPOINT_DEFAULT).strip()
    _auto_start_cdp_browser(endpoint)
    pw = _driver()
    if _CDP["browser"] is None:
        _CDP["browser"] = pw.chromium.connect_over_cdp(endpoint)
        _CDP["endpoint"] = endpoint
    elif _CDP["endpoint"] != endpoint:
        # 端点变了就重连。注意：绝不对用户浏览器调 close()（部分 Playwright
        # 版本会真的终止进程），有 disconnect 就只用 disconnect。
        old = _CDP["browser"]
        _CDP["browser"] = pw.chromium.connect_over_cdp(endpoint)
        _CDP["endpoint"] = endpoint
        try:
            (getattr(old, "disconnect", None) or (lambda: None))()
        except Exception:
            pass
    contexts = _CDP["browser"].contexts
    if not contexts:
        raise RuntimeError("已连接但浏览器没有任何上下文")
    return contexts[0].new_page()


def _launch():
    """确保 guest 无登录浏览器已启动。**只能在 worker 线程里调。**"""
    if _PW["browser"] is not None:
        return _PW["browser"]
    try:
        pw = _driver()
    except ImportError:
        raise RuntimeError(
            "未安装 playwright。请执行: pip install playwright && playwright install chromium")
    # --disable-gpu*：4GB 显卡还要喂本地模型，浏览器再抢显存就会一起崩
    # （实测 web_chat 报 Target crashed，就是显存/内存被两边挤爆）
    _PW["browser"] = pw.chromium.launch(
        headless=True,
        args=[
            "--disable-gpu",
            "--disable-gpu-compositing",
            "--disable-software-rasterizer",
            "--disable-dev-shm-usage",
        ],
    )
    return _PW["browser"]


def _new_page():
    """新开一页（调用方负责 close）。**只能在 worker 线程里调。**"""
    return _launch().new_page(
        user_agent=USER_AGENT, viewport={"width": 1280, "height": 900})


def run_with_page(fn, *, via_cdp: bool = False, timeout: float = 180.0):
    """在 worker 线程里新开一页交给 fn(page)，用完自动关页，返回值原样带回。

    这是给 web_search / web_screenshot 用的便捷入口：**page 绝不能逃出 fn**。
    拿 page 到别的线程再操作会抛 `Cannot switch to a different thread`。
    """
    def job():
        page = _cdp_page() if via_cdp else _new_page()
        try:
            return fn(page)
        finally:
            try:
                page.close()
            except Exception:
                pass

    try:
        return run_on_pw(job, timeout=timeout)
    except Exception as e:
        if not _is_stale(e):
            raise
        # 连接已死（线程换了 / 浏览器崩了）：重建后重试一次
        try:
            run_on_pw(_reset_pw, timeout=30)
        except Exception:
            pass
        return run_on_pw(job, timeout=timeout)


def pw_close() -> None:
    """释放浏览器。CDP 只 disconnect，绝不 close 用户窗口。可在任意线程调。"""
    try:
        run_on_pw(_reset_pw, timeout=30)
    except Exception:
        pass          # 退出路径：清不掉也不该抛


def _fetch(url: str, *, via_cdp: bool) -> tuple[str, str]:
    """取一页 (title, text)。via_cdp=走 agent 自动启动的浏览器。"""
    def job(page):
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(500)  # 等首屏 JS
        return _read_page(page)

    return run_with_page(job, via_cdp=via_cdp)


@tool(
    name="browse_url",
    description=(
        "用 Playwright 打开网页并提取正文文本。读文档、文章、链接内容用这个。"
        "默认 identity=auto：先用无登录浏览器，遇到反爬/登录墙时由 agent 自动启动"
        "带调试端口的浏览器并接管会话重试，无需用户执行任何命令；只有扫码登录需本人操作，"
        "且窗口由 agent 自动开好。guest=只用无登录浏览器；user=直接用浏览器会话。"
        "只陈述本轮实际读到的正文，被拦截就明说，不编造内容。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "http/https 链接"},
            "max_chars": {"type": "integer", "description": "正文最多返回字符数；不传则不在此截断，超长由 spill 整文落盘"},
            "identity": {"type": "string", "enum": ["auto", "guest", "user"],
                         "description": "auto=自动(默认，遇反爬自动起浏览器会话);guest=仅无登录浏览器;user=直接用浏览器会话"},
        },
        "required": ["url"],
    },
    confirm=True,
)
def browse_url(ctx: ToolContext, url: str, max_chars: int | None = None,
               identity: str = "auto") -> ToolResult:
    if not re.match(r"^https?://", url or ""):
        return ToolResult("[错误] 只支持 http/https 链接")
    if identity not in ("auto", "guest", "user"):
        return ToolResult(f"[错误] identity 只能是 auto/guest/user，收到: {identity}")

    # auto（默认）：先用 guest 无登录浏览器；被拦截则由 agent 自动启动带 CDP 的
    # 浏览器并接管会话重试——全程不需要人执行任何命令。只有扫码登录是人做的。
    use_cdp = identity == "user"
    last_hit = ""
    last_text = ""
    last_title = ""
    for attempt in ("guest", "cdp") if identity == "auto" else ((identity,)):
        via_cdp = attempt == "cdp" or identity == "user"
        try:
            title, text = _fetch(url, via_cdp=via_cdp)
        except RuntimeError as e:
            # 自动启动/连接失败：guest 失败直接报；cdp 失败说明自动接管没成功
            if via_cdp:
                return ToolResult(
                    f"[错误] agent 自动启动浏览器会话失败: {e}\n"
                    f"（可用环境变量 {CDP_PROFILE_ENV} 指定 profile，"
                    f"{CDP_ENDPOINT_ENV} 指定端点）")
            return ToolResult(f"[错误] {e}")
        except Exception as e:
            return ToolResult(f"[错误] 打开失败 {url}: {type(e).__name__}: {str(e)[:300]}")

        text = re.sub(r"\n{3,}", "\n\n", _normalize_text(text or "")).strip()
        hit = _detect_block(title, text)
        if not hit:
            return ToolResult(
                f"[网页] {url}\n[标题] {title}\n[identity] {attempt}\n\n{_clip(text, max_chars)}")

        last_hit, last_text, last_title = hit, text, title
        if via_cdp:
            # 自动启动的浏览器仍被拦 = 该站未登录。窗口 agent 已开好并停在目标页，
            # 人工只需扫码/登录（唯一 human-only 步骤），无需执行任何命令。
            return ToolResult(
                f"[需要登录] agent 已自动打开浏览器并访问目标站，但会话未登录，"
                f"被拦截（{hit}）。\n"
                "请在刚弹出的浏览器窗口里完成登录（扫码或账号均可）；"
                "登录一次后会持久化，之后无需再登。登录完告诉我，我重新读取。\n"
                "这是唯一需要你本人操作的步骤（人工验证），无需执行任何命令。\n"
                f"[已验证片段]\n{text[:300]}")

    # auto 流程 guest 被拦但 cdp 分支未执行到（理论上不会），诚实报卡点
    return ToolResult(
        f"[已拦截] {url}\n[标题] {last_title}\n[拦截特征] {last_hit}\n"
        f"本轮只观察到反爬响应（{len(last_text)} 字符），不编造正文。"
        f"\n[已验证片段]\n{last_text[:300]}")


def _clip(text: str, max_chars: int | None) -> str:
    """模型显式要求小窗口才破坏性截断；默认整文交由 dispatch spill 落盘。"""
    if max_chars:
        max_chars = int(max_chars)
        if len(text) > max_chars:
            return text[:max_chars] + f"\n…[已截断，原文 {len(text)} 字符]"
    return text
