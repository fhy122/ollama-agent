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

# CDP 接管浏览器：绝不能 close 用户的窗口，只清引用 + pw.stop() 断连接。
# 注意：Playwright Python 的 Browser 没有 disconnect()，别再假装调用它。
_CDP: dict = {"browser": None, "endpoint": None, "page": None}

# CDP 端点活着（/json/version 200）不等于已有可用 context/page target：
# 用户关掉最后一个窗口、后台进程残留、启动未稳时 contexts 都可能为空。
# 先轮询等它就绪，再兜底新建，而不是立刻抛"没有任何上下文"。
_CDP_CTX_WAIT_TRIES = 10
_CDP_CTX_WAIT_INTERVAL = 0.3


def _cdp_browser_alive(browser) -> bool:
    """CDP 连接活体判据。Playwright Python 的 Browser 无 disconnect()，
    is_connected() 是唯一信号；失效/异常对象一律判死，交给上层重建。"""
    try:
        return browser is not None and bool(browser.is_connected())
    except Exception:
        return False

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
    _CDP["page"] = None            # 常驻页随 driver 一起作废
    if cdp_browser is not None:
        _disconnect_cdp(cdp_browser)
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


def _disconnect_cdp(browser) -> None:
    """断开 CDP 连接但绝不关用户窗口。

    Playwright Python 的 Browser 没有 disconnect()：实测 `hasattr(b,'disconnect')`
    为 False，close() 又会真的终止用户进程。唯一安全的释放方式是通过
    `_reset_pw()` 停掉整个 driver（连接随之断开）。这里只对确实存在的
    disconnect 做兼容调用，其余情况什么都不做，交给 driver 层回收。
    """
    fn = getattr(browser, "disconnect", None)
    if callable(fn):
        try:
            fn()
        except Exception:
            pass


def _pick_cdp_context(browser):
    """从已连接的 CDP 浏览器里挑一个可开新页的 context。

    实测坑：CDP 端点 `/json/version` 返回 200（ agent 认为浏览器已就绪），但
    `browser.contexts` 可能仍为空 —— 用户关掉最后一个窗口只剩后台进程、或
    刚启动 target 还没注册，此时 0.03s 就抛"没有任何上下文"，页面根本没打开。
    策略：轮询等 target 注册 → 仍空则 new_context() 兜底（CDP 连接的浏览器
    允许新建独立 context，不会碰用户已有窗口/登录态）。
    """
    contexts = list(browser.contexts or [])
    if not contexts:
        for _ in range(_CDP_CTX_WAIT_TRIES):
            time.sleep(_CDP_CTX_WAIT_INTERVAL)
            contexts = list(browser.contexts or [])
            if contexts:
                break
    if not contexts:
        # 浏览器活着但一个 context 都没有：新建独立 context 兜底。
        # new_context 在 CDP 连接下创建的是隔离会话，不动用户默认 context。
        contexts = [browser.new_context()]
    return contexts[0]


def _cdp_browser():
    """返回可用的 CDP browser 连接；端点没起就自动启动（无需人工执行命令）。

    连接失效 / 端点变化时重建。is_connected() 偶有假阳性（实测关页后进程已退
    它仍返回 True），所以拿 context 时由 _pick_cdp_context 再兜底。
    **只能在 worker 线程里调。**
    """
    endpoint = os.environ.get(CDP_ENDPOINT_ENV, CDP_ENDPOINT_DEFAULT).strip()
    _auto_start_cdp_browser(endpoint)
    pw = _driver()
    if (not _cdp_browser_alive(_CDP["browser"])
            or _CDP["endpoint"] != endpoint):
        old = _CDP["browser"]
        _CDP["browser"] = pw.chromium.connect_over_cdp(endpoint)
        _CDP["endpoint"] = endpoint
        _CDP["page"] = None          # 换了连接，旧常驻页作废
        _disconnect_cdp(old)
    return _CDP["browser"]


def _cdp_page():
    """连到 CDP 浏览器**新开一页**（用完由调用方关）。

    **只能在 worker 线程里调**（返回的 page 不能带出去）。
    """
    return _pick_cdp_context(_cdp_browser()).new_page()


def _cdp_sticky_page():
    """返回 CDP 浏览器的**常驻页**：复用同一页 navigate，读完不关。

    identity=user 的语义是"接管会话、停在当前页给人看/扫码"，所以目标页必须
    留在浏览器里。若每次 new_page + close，用户只会看到页面一闪而过、最后
    剩 agent 启动时自带的 about:blank（实测）。常驻页解决了这个"跳过去又
    退出来"的问题，也避免反复调用累积标签页。
    **只能在 worker 线程里调**（page 不能带出 worker）。
    """
    page = _CDP.get("page")
    if page is not None:
        try:
            if not page.is_closed():
                _bring_to_front(page)
                return page
        except Exception:
            pass
        _CDP["page"] = None
    ctx = _pick_cdp_context(_cdp_browser())
    # 优先复用浏览器里已有的 about:blank（agent 启动时的占位页），而不是
    # 再 new_page —— 否则用户会看到"目标页 + 一个空标签页"两个页。
    for existing in ctx.pages:
        try:
            if (not existing.is_closed()
                    and existing.url in ("about:blank", "")):
                _CDP["page"] = existing
                _bring_to_front(existing)
                return existing
        except Exception:
            continue
    _CDP["page"] = ctx.new_page()
    _bring_to_front(_CDP["page"])
    return _CDP["page"]


def _bring_to_front(page) -> None:
    """把 CDP 页所在窗口提到前面，方便用户看到登录/扫码页。失败不影响读取。"""
    try:
        page.bring_to_front()
    except Exception:
        pass


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


def run_with_page(fn, *, via_cdp: bool = False, timeout: float = 180.0,
                  keep_page: bool = False):
    """在 worker 线程里开一页交给 fn(page)，返回值原样带回。

    默认用完关页（guest 的 web_search / web_screenshot 走这条，防泄漏）。
    keep_page=True 时保留页面不关——给 identity=user 的常驻页用，页面要停在
    目标 URL 给人看/扫码。**page 绝不能逃出 fn**：拿到别的线程操作会抛
    `Cannot switch to a different thread`。
    """
    def job():
        # keep_page + via_cdp = 常驻页复用（identity=user，页面要停在目标 URL）；
        # 其余走一次性新页。三条路径都在 worker 里开页，page 不带出 fn。
        if keep_page and via_cdp:
            page = _cdp_sticky_page()
        elif via_cdp:
            page = _cdp_page()
        else:
            page = _new_page()
        try:
            return fn(page)
        finally:
            if not keep_page:
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

    # user/CDP：keep_page=True 复用常驻页并保留，页面停在目标 URL（见
    # _cdp_sticky_page）；guest：一次性新页读完即关。两者都保留 _stale 重建重试。
    return run_with_page(job, via_cdp=via_cdp, keep_page=via_cdp)


@tool(
    name="browse_url",
    description=(
        "用 Playwright 打开网页并读取正文文本，原样返回标题+URL+正文。读文档、文章、"
        "链接内容用这个。工具只负责读，原样交回内容，不判断读回来的东西是不是你要的。"
        "你自己看正文：若读到的内容与预期不符——登录页、人机验证、需要点确认/同意、"
        "验证码、欢迎页等任何需人工操作的拦截——按下面流程处理，全程由你主动调用工具，"
        "不要把工具名或参数(如 identity=user)写进给用户的说明里、更不要让用户自己调用工具："
        "① 你直接调用 browse_url(url, identity='user')，会在用户桌面弹出并置顶浏览器窗口、"
        "停在目标页；② 然后告诉用户：窗口已打开，请在窗口里完成登录/验证/点击，好了告诉我；"
        "③ 用户说好后，你再调用 browse_url(url, identity='user') 读一次，用新正文回答。"
        "identity: guest=无登录匿名浏览器；user=用已打开的浏览器会话（登录态/操作态都"
        "复用，页面停在目标页，便于人工操作）；auto(默认)=先 guest，读取抛错再用 user。"
        "不编造内容——读到的就是原样正文。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "http/https 链接"},
            "max_chars": {"type": "integer", "description": "正文最多返回字符数；不传则不在此截断，超长由 spill 整文落盘"},
            "identity": {"type": "string", "enum": ["auto", "guest", "user"],
                         "description": "guest=无登录浏览器;user=用已登录浏览器会话;auto=先guest失败再user"},
        },
        "required": ["url"],
    },
    confirm=True,
)
def browse_url(ctx: ToolContext, url: str, max_chars: int | None = None,
               identity: str = "auto") -> ToolResult:
    """打开网页、读正文、原样返回。不做任何"这是什么类型的障碍"的判断 —— 登录页、
    人机验证、需点击确认等任何异常内容都原样交回，由读结果的模型自己看内容判断。
    identity 只决定用哪个浏览器读（机械策略）：guest=匿名 headless；user=已打开的
    浏览器会话（CDP 常驻页，复用登录态/操作态）；auto=先 guest，技术上读失败再退到
    user。读到异常内容不算"失败"，会原样返回，由模型决定下一步。
    """
    if not re.match(r"^https?://", url or ""):
        return ToolResult("[错误] 只支持 http/https 链接")
    if identity not in ("auto", "guest", "user"):
        return ToolResult(f"[错误] identity 只能是 auto/guest/user，收到: {identity}")

    # 机械策略：auto 先 guest，读抛异常再退 user。不判断内容语义（登录/拦截与否
    # 一律原样返回），只读失败（异常）才换浏览器。
    attempts = ("guest", "user") if identity == "auto" else (identity,)
    last_err = ""
    for which in attempts:
        via_cdp = which == "user"
        try:
            title, text = _fetch(url, via_cdp=via_cdp)
        except RuntimeError as e:
            last_err = f"agent 自动启动浏览器会话失败: {e}"
            if via_cdp:
                return ToolResult(
                    f"[错误] {last_err}\n（可用环境变量 {CDP_PROFILE_ENV} 指定 profile，"
                    f"{CDP_ENDPOINT_ENV} 指定端点）")
            continue          # guest 读失败，auto 退到 user 再试
        except Exception as e:
            last_err = f"打开失败 {url}: {type(e).__name__}: {str(e)[:300]}"
            if via_cdp:
                return ToolResult(f"[错误] {last_err}")
            continue
        text = re.sub(r"\n{3,}", "\n\n", _normalize_text(text or "")).strip()
        return ToolResult(
            f"[网页] {url}\n[标题] {title}\n[identity] {which}\n\n{_clip(text, max_chars)}")

    return ToolResult(f"[错误] {last_err or '未能读取页面'}")


def _clip(text: str, max_chars: int | None) -> str:
    """模型显式要求小窗口才破坏性截断；默认整文交由 dispatch spill 落盘。"""
    if max_chars:
        max_chars = int(max_chars)
        if len(text) > max_chars:
            return text[:max_chars] + f"\n…[已截断，原文 {len(text)} 字符]"
    return text
