"""web_search — 联网搜索，多引擎并行 + 本地模型综合（元宝式效果）。

路线（全并行，尽量走纯 HTTP，浏览器只在“百度 AI 摘要”一路用）：
  1. Bing 常规结果（纯 HTTP 解析，快）：cn.bing.com 的 li.b_algo 结果块，质量较高。
     匿名会话会随机发“词典兜底页”/“首词劫持页”——命中降级自动重试翻到好页。
  2. 百度 AI 总结（Playwright 渲染）：一次搜索即得“AI 总结 N 篇结果”的聚合答案+引用来源；
     卡片间歇出现，带自动展开。同时取百度常规结果作补充。
  3. 360 搜索（纯 HTTP，零浏览器，快）：www.so.com，国内可达、来源互补，补充第三路覆盖。

综合（元宝效果的关键）：
  抓完全部来源后，调用 ctx.client（本地模型）把来源综合成一段带 [n] 引用的中文答案，
  放在最前；下方附“检索来源清单”（编号 + 标题 + 链接 + 摘要）供核验。
  这一步可关（synthesize=false）——关掉就只返回来源清单，由主模型综合，省一次本地推理。

engine 参数：auto/both(默认,三引擎全量) | baidu | bing | so(=360) | 360
"""
from __future__ import annotations

import html
import http.cookiejar
import re
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from agent.registry import ToolContext, ToolResult, tool
from tools.browse_url import _pw_page, pw_new_page  # 复用浏览器单例

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

BING_URL = "https://cn.bing.com/search?q={q}&setmkt=zh-CN&setlang=zh-hans"
BAIDU_URL = "https://www.baidu.com/s?ie=UTF-8&wd={q}"
SO360_URL = "https://www.so.com/s?q={q}"

# 百度 AI 卡片识别标记（任一命中即认为是 AI 总结卡）
_BAIDU_AI_JS = """() => {
    const out = {ai: null, results: []};
    const markerRe = /AI总结\\d*篇结果生成|均由AI生成|体验百度AI/;
    // —— AI 总结卡：按文本标记找（cosc-card 类名会被用于多种卡片，不能只看类名）——
    const cards = Array.from(document.querySelectorAll('div')).filter(d => {
        return d.classList && d.classList.contains('cosc-card') && markerRe.test(d.innerText || '');
    });
    cards.sort((a, b) => (b.innerText || '').length - (a.innerText || '').length);
    const card = cards[0];
    if (card) {
        const contentEl = card.querySelector('.cosc-card-content')
            || card.querySelector('[class*="content-container"]') || card;
        let text = contentEl.innerText || '';
        text = text.replace(/长按识别[\\s\\S]*$/, '')
                   .replace(/以上内容均由AI生成[\\s\\S]*$/, '')
                   .replace(/AI总结\\d*篇结果生成[\\s\\S]*$/, '')
                   .replace(/\\n?收起\\s*$/, '')
                   .replace(/[\\u200b-\\u200f\\u2028\\u2029]/g, '')
                   .trim();
        const meta = ((card.innerText || '').match(/AI总结\\d*篇结果生成/) || [''])[0];
        // 引用来源：百度常用 javascript: 假 href + data-url 等属性，全部兜住
        const links = [];
        for (const el of card.querySelectorAll('a, [data-url], [data-link], [data-href]')) {
            const u = el.getAttribute('href') || el.getAttribute('data-url')
                || el.getAttribute('data-link') || el.getAttribute('data-href') || '';
            const m = u.match(/https?:\\/\\/[^"'\\s)]+/);
            if (!m) continue;
            const real = m[0];
            if (/baidu\\.com/.test(real)) continue;
            if (links.some(x => x.u === real)) continue;
            links.push({
                t: ((el.innerText || '').trim() || el.title || `来源${links.length + 1}`).slice(0, 80),
                u: real,
            });
        }
        out.ai = {text: text.slice(0, 6000), meta, links: links.slice(0, 12)};
    }
    // —— 自然结果 ——
    for (const d of document.querySelectorAll('div.result.c-container, div[class*="c-container"]')) {
        const a = d.querySelector('h3 a');
        if (!a || !a.href) continue;
        const t = (d.innerText || '');
        out.results.push({
            title: (a.innerText || '').trim().slice(0, 120),
            href: a.href,
            snip: t.slice(0, 300).trim(),
        });
        if (out.results.length >= 15) break;
    }
    return out;
}"""


def _clean(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


# ---------------- Bing（纯 HTTP） ----------------

# Bing 对匿名会话按概率发降级页——两种形态：
#   A. 词典兜底页：词典/百科类标题顶格（"本地（汉语词语）_百度百科"…）
#   B. 首词劫持页：结果全被 query 第一个词带跑（"CPU GPU 跑本地大模型谁快"→CPU天梯图…）
# 真实登录浏览器不受影响。识别后换轮重试。
_DICT_PAT = re.compile(
    r"是什么意思|汉语词语|汉语文字|_百度百科|剑桥词典|漢語|詞典|翻译_音标|音标_读音|_维基词典")

# 模块级会话：跨调用共享 Cookie（MUID/SRCHUID 等），重试像同一访客连续刷新
_BING_JAR = http.cookiejar.CookieJar()
_BING_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_BING_JAR))
_BING_PRIMED = False


def _query_terms(query: str) -> set[str]:
    """query 关键词集合（英文词 + 中文 2-gram），去掉首词锚点。"""
    terms: set[str] = set()
    for m in re.finditer(r"[a-z0-9]{2,}|[一-鿿]{2,}", query.lower()):
        tok = m.group(0)
        if tok.isascii():
            terms.add(tok)
        else:
            for i in range(len(tok) - 1):
                terms.add(tok[i:i + 2])
    # 首词锚点（query 第一个词/第一个 bigram）不算命中——防首词劫持蒙混过关
    first = re.search(r"[a-z0-9]{2,}|[一-鿿]{2,}", query.lower())
    if first:
        tok = first.group(0)
        terms.discard(tok if tok.isascii() else tok[:2])
    return terms


def _bing_is_junk(query: str, results: list) -> bool:
    """降级页判定：词典顶格（形态A）或结果与 query 关键词零相关（形态B）。

    形态B 只对含中文的 query 生效——英文结果标题常不含字面关键词（品牌页/同义改写），
    硬套相关性判定会产生误报。
    """
    if not results or len(results) < 3:
        return False
    # 形态 A：词典/百科标题顶格
    hits = sum(1 for t, _u, _s in results[:5] if _DICT_PAT.search(t or ""))
    if hits >= 3:
        return True
    # 形态 B：前 5 条里 ≥3 条不含 query 任何非首词关键词（仅含中文的 query）
    if len(re.findall(r"[一-鿿]", query)) < 2:
        return False
    terms = _query_terms(query)
    if not terms:
        return False
    bad = 0
    for t, u, s in results[:5]:
        text = f"{t or ''} {s or ''}".lower()
        if not any(term in text for term in terms):
            bad += 1
    return bad >= 3


def _fetch_bing_once(query: str, max_results: int) -> list[tuple[str, str, str]]:
    """单次抓取 cn.bing.com 的 li.b_algo 结果块。"""
    global _BING_PRIMED
    if not _BING_PRIMED:
        # 首次先逛一圈首页养 Cookie（MUID/SRCHUID），让后续重试像同一访客连续刷新
        _BING_PRIMED = True
        try:
            _BING_OPENER.open(
                urllib.request.Request("https://cn.bing.com/", headers={"User-Agent": UA}),
                timeout=15).read()
        except Exception:
            pass

    url = BING_URL.format(q=urllib.parse.quote_plus(query))
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    with _BING_OPENER.open(req, timeout=20) as resp:
        page_html = resp.read().decode("utf-8", "replace")

    blocks = re.findall(r'<li class="b_algo".*?</li>', page_html, re.S)
    results = []
    for b in blocks[:max_results]:
        m = re.search(r'<h2[^>]*><a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not m:
            continue
        href = html.unescape(m.group(1))
        if not href.startswith("http"):
            continue
        p = re.search(r'<p[^>]*>(.*?)</p>', b, re.S)
        snip = _clean(p.group(1))[:300] if p else ""
        results.append((_clean(m.group(2)), href, snip))
    return results


def _search_bing(query: str, max_results: int, attempts: int = 3) -> list[tuple[str, str, str]]:
    """带降级重试的 Bing 搜索：命中降级页（词典兜底/首词劫持）就重试，翻到正常页为止。

    实测同一 query 在匿名会话下时好时坏（随机降级），重试 2~3 次基本能拿到正常结果。
    """
    last: list[tuple[str, str, str]] = []
    for i in range(attempts):
        try:
            results = _fetch_bing_once(query, max_results)
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(1.0 + i * 0.8)
            continue
        if results:
            last = results
            if not _bing_is_junk(query, results):
                return results
        if i < attempts - 1:
            time.sleep(1.0 + i * 0.8)  # 退避，像真人刷新
    return last


# ---------------- 360 搜索（纯 HTTP，零浏览器） ----------------

_SO360_JAR = http.cookiejar.CookieJar()
_SO360_OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(_SO360_JAR))
_SO360_PRIMED = False


def _resolve_so360_link(url: str) -> str:
    """so.com/link?m= 跳转链接 → 真实 URL。

    360 这类链接返回 200 + JS 跳转（window.location.replace("真实URL")），并非 HTTP 302，
    所以要从返回体提取真实地址；提取失败/异常则保留原链。
    """
    if "so.com/link" not in url:
        return url
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Referer": "https://www.so.com/"})
        with urllib.request.urlopen(req, timeout=6) as r:
            body = r.read(2000).decode("utf-8", "replace")
        m = re.search(r'window\.location(?:\.replace)?\s*\(\s*"([^"]+)"\s*\)', body)
        if not m:
            m = re.search(r'content="0;URL=[\'"]?([^\'"]+)', body)
        if m:
            real = m.group(1)
            if real.startswith("http"):
                return real
    except Exception:
        pass
    return url


def _search_360(query: str, max_results: int) -> list[tuple[str, str, str]]:
    """纯 HTTP 抓 www.so.com（360 搜索）。国内可达、零浏览器、快，补充第三路来源。

    结果块 <li class="res-list">（内含内联 <style> 噪音需剥离）；
    标题外链为 https://www.so.com/link?m=... 跳转，解析后换成真实地址（失败回退原链），
    摘要优先取 res-desc，否则用块内可见文本兜底。
    """
    global _SO360_PRIMED
    if not _SO360_PRIMED:
        _SO360_PRIMED = True
        try:    # 先逛首页养 cookie
            _SO360_OPENER.open(
                urllib.request.Request("https://www.so.com/",
                                      headers={"User-Agent": UA}), timeout=15).read()
        except Exception:
            pass

    url = SO360_URL.format(q=urllib.parse.quote_plus(query))
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    with _SO360_OPENER.open(req, timeout=15) as resp:
        page_html = resp.read().decode("utf-8", "replace")

    # 剥离内联 style 噪音，按结果块解析
    page_html = re.sub(r"<style[^>]*>.*?</style>", "", page_html, flags=re.S)
    blocks = re.findall(r'<li class="res-list".*?</li>', page_html, re.S)
    out: list[tuple[str, str, str]] = []
    for b in blocks:
        m = re.search(
            r'<a[^>]*href="(https?://www\.so\.com/link\?m=[^"]+)"[^>]*>(.*?)</a>', b, re.S)
        if not m:
            continue
        href = _resolve_so360_link(m.group(1))  # 跳转解析为真实地址
        title = _clean(m.group(2))
        if not title:
            continue
        dm = re.search(r'class="[^"]*res-desc[^"]*"[^>]*>(.*?)</(?:div|p|span)>', b, re.S)
        snip = _clean(dm.group(1)) if dm else ""
        if not snip:
            bt = _clean(b).replace(title, "", 1)
            snip = re.sub(r"^(AI正在分析|导航|设置|反馈)[^\n]*", "", bt).strip()
        # 清理摘要里的「日期 - 」前缀（360 在摘要前塞了来源日期）
        snip = re.sub(r"^\s*\d{4}年\d{1,2}月\d{1,2}日\s*[–-]\s*", "", snip).strip()
        out.append((title, href, snip[:300]))
        if len(out) >= max_results:
            break
    return out


# ---------------- 百度（Playwright：AI 总结 + 结果） ----------------

def _resolve_baidu_link(url: str) -> str:
    """baidu.com/link?url= 跳转链接 → 真实 URL（失败保留原链）。"""
    if "baidu.com/link" not in url:
        return url
    try:
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=8) as r:
            return r.geturl() or url
    except Exception:
        return url


# 展开 AI 卡折叠区（默认只显示约 44%；按钮文本可能是"展开剩余56%结果"或"展开更多"）
_EXPAND_JS = """() => {
    const markerRe = /AI总结\\d*篇结果生成|均由AI生成|体验百度AI/;
    const cards = Array.from(document.querySelectorAll('div')).filter(d =>
        d.classList && d.classList.contains('cosc-card') && markerRe.test(d.innerText || ''));
    cards.sort((a, b) => (b.innerText || '').length - (a.innerText || '').length);
    const card = cards[0];
    if (!card) return false;
    // 只在 AI 卡内/紧邻父级找开关，避免误点页面其它折叠区（问答卡等）
    let btn = card.querySelector('[class*="has-fold-button"]')
        || card.querySelector('[class*="fold-switch"]')
        || (card.parentElement && card.parentElement.querySelector('[class*="has-fold-button"]'))
        || (card.parentElement && card.parentElement.querySelector('[class*="fold-switch"]'));
    if (!btn) {
        for (const el of card.querySelectorAll('*')) {
            const t = (el.innerText || '').trim();
            if (/^展开/.test(t) && t.length < 30 && el.children.length <= 2) {
                btn = el;
                break;
            }
        }
    }
    if (!btn) return false;
    btn.click();
    return true;
}"""


def _search_baidu(query: str, max_results: int, attempts: int = 2) -> dict:
    """Playwright 渲染百度 SERP。返回 {"ai": {...}|None, "results": [(t,u,s)]}

    注意：百度 AI 总结卡是灰度实验（baikan-pc-experiment），随机出现；
    每次用独立 page 防状态串扰，缺席时重试 attempts 轮。
    """
    url = BAIDU_URL.format(q=urllib.parse.quote_plus(query))
    data: dict = {"ai": None, "results": []}

    for attempt in range(attempts):
        page = pw_new_page()  # 独立页面，用完即关
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            # 轻微滚动触发懒渲染
            try:
                page.mouse.wheel(0, 1200)
                page.wait_for_timeout(1500)
                page.mouse.wheel(0, -1200)
            except Exception:
                pass
            page.wait_for_timeout(5000)
            data = page.evaluate(_BAIDU_AI_JS) or data

            if data.get("ai"):
                # 有 AI 卡：真实鼠标点击"展开/更多"（合成 click 会被前端忽略）后重抓
                try:
                    import re as _re
                    fold = (page.locator('div[class*="cosc-card"]')
                            .filter(has_text=_re.compile("均由AI生成|AI总结|体验百度AI"))
                            .locator('[class*="has-fold-button"], [class*="fold-switch"]').first)
                    if fold.count():
                        fold.click(timeout=2000)
                        page.wait_for_timeout(2500)
                        data = page.evaluate(_BAIDU_AI_JS) or data
                except Exception:
                    pass
                break
            if not data.get("results"):
                page.wait_for_timeout(2500)  # 渲染慢的兜底
                data = page.evaluate(_BAIDU_AI_JS) or data
            if data.get("ai") or data.get("results"):
                break
        finally:
            try:
                page.close()
            except Exception:
                pass

    ai = data.get("ai")
    if ai and ai.get("links"):
        ai["links"] = [
            {"t": x["t"], "u": _resolve_baidu_link(x["u"])} for x in ai["links"][:12]
        ]

    results = []
    for r in data.get("results", [])[:max_results]:
        results.append((r["title"], _resolve_baidu_link(r["href"]), r["snip"]))
    return {"ai": ai, "results": results}


# ---------------- 组装 ----------------

def _fmt_results(tag: str, results: list[tuple[str, str, str]]) -> str:
    if not results:
        return ""
    lines = [f"[搜索结果 · {tag}]"]
    for i, (t, u, s) in enumerate(results, 1):
        lines.append(f"{i}. {t}\n   {u}\n   {s}")
    return "\n".join(lines)


def _fmt_ai(ai: dict) -> str:
    lines = [f"[AI聚合摘要 · 百度AI总结（{ai.get('meta') or '多篇结果聚合'}）]"]
    lines.append(ai.get("text", ""))
    if ai.get("links"):
        lines.append("[摘要引用来源]")
        for i, x in enumerate(ai["links"], 1):
            lines.append(f"{i}. {x['t']}  {x['u']}")
    return "\n".join(lines)


# 重试后仍降级时的兜底提示（判定同 _bing_is_junk）
def _looks_degraded(query: str, results: list) -> bool:
    return _bing_is_junk(query, results)


def _url_key(u: str) -> str:
    """URL 主干（去 fragment/utm 等/尾斜杠），用于跨引擎去重。

    顺序：先去 fragment → 去协议头 → 剔除 utm/from/wd/eqid 追踪参数 → 最后去尾斜杠。
    尾斜杠必须放在参数剔除之后，否则 ?utm=1 被吃掉后残留的斜杠会让同类 URL 判为不同。
    """
    u = (u or "").split("#")[0]
    u = re.sub(r"^https?://(www\.)?", "", u)
    u = re.sub(r"[?&](utm_\w+|from|wd|eqid)=\w+", "", u)
    return u.rstrip("/")


def _synthesize(query: str, master: list[dict], ctx: ToolContext) -> str | None:
    """用本地模型把来源综合成带 [n] 引用的中文答案。失败/无客户端返回 None。"""
    if ctx.client is None:
        return None
    # 只喂前 10 条 + 摘要截 200 字，控制上下文与耗时
    lines = []
    for i, s in enumerate(master[:10], 1):
        lines.append(f"[{i}] {s['title']}\nURL: {s['url']}\n摘要: {s['snippet'][:200]}")
    src_text = "\n\n".join(lines)
    prompt = (
        f"你是搜索综合助手。用户问题：{query}\n\n"
        f"以下是检索到的来源（编号对应引用，可能来自不同搜索引擎）：\n{src_text}\n\n"
        "要求：\n"
        "1. 用简体中文直接回答，先给结论、再分点展开，条理清晰；\n"
        "2. 在每条关键事实后用 [n] 标注其来源编号（n 对应上方来源编号）；\n"
        "3. 只基于上述来源作答，不得编造来源以外的信息或来源链接；"
        "来源不足以回答时，明确说明“现有来源不足以确认”；\n"
        "4. 不要重复罗列来源链接（链接会在下方单独给出）；不要输出“根据来源”之类的废话开头。"
    )
    try:
        resp = ctx.client.chat(
            [{"role": "user", "content": prompt}], tools=None, cancel=ctx.cancel)
        if resp.get("error") or resp.get("cancelled"):
            return None
        return (resp.get("content") or "").strip() or None
    except Exception:
        return None


@tool(
    name="web_search",
    description=(
        "联网搜索（多引擎并行：Bing 结果 + 百度AI总结/百度结果 + 360搜索结果），"
        "并用本地模型把来源综合成【带 [n] 引用的中文答案】放最前，下方附编号来源清单（标题+链接+摘要）供核验。"
        "效果类似 AI 搜索（元宝式）：搜得多、快、有效信息密度高。"
        "默认 synthesize=true 会多花一次本地推理做综合；想更快可设 synthesize=false，"
        "则返回来源清单，由你基于来源综合并标注引用。"
        "engine: auto/both(默认,三引擎) | baidu | bing | so(=360) | 360。"
        "查最新信息、事实类问题先用这个；需要原文细节再 browse_url 对应链接。"),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词，可用中英文"},
            "max_results": {"type": "integer", "description": "每个引擎常规结果最多条数，默认 8"},
            "engine": {
                "type": "string",
                "description": "搜索通道：auto/both(默认,三引擎全量) | baidu | bing | so(=360) | 360",
                "enum": ["auto", "both", "baidu", "bing", "so", "360"],
            },
            "synthesize": {
                "type": "boolean",
                "description": "是否用本地模型综合成带引用的答案（元宝效果）。true(默认)=综合；false=只返回来源清单，更快",
            },
        },
        "required": ["query"],
    },
    confirm=True,
)
def web_search(ctx: ToolContext, query: str, max_results: int = 8,
               engine: str = "auto", synthesize: bool = True) -> ToolResult:
    max_results = max(1, int(max_results or 8))
    engine = (engine or "auto").lower()
    if engine == "both":
        engine = "auto"
    if engine == "360":
        engine = "so"
    want_bing = engine in ("auto", "bing")
    want_baidu = engine in ("auto", "baidu")
    want_360 = engine in ("auto", "so")

    sections: list[str] = []
    errors: list[str] = []

    # —— 并行抓取：Bing / 360 走后台线程（纯 HTTP），百度留主线程（Playwright 线程亲和）——
    futures: list = []
    pool = ThreadPoolExecutor(max_workers=2)
    bing_results: list[tuple[str, str, str]] = []
    so360_results: list[tuple[str, str, str]] = []
    if want_bing:
        futures.append(("bing", pool.submit(_search_bing, query, max_results)))
    if want_360:
        futures.append(("360", pool.submit(_search_360, query, max_results)))

    baidu_ai = None
    baidu_results: list[tuple[str, str, str]] = []
    if want_baidu:
        try:
            bd = _search_baidu(query, max_results)
            baidu_ai, baidu_results = bd.get("ai"), bd.get("results", [])
        except RuntimeError as e:  # playwright 未安装
            errors.append(str(e))
        except Exception as e:
            errors.append(f"百度: {type(e).__name__}: {str(e)[:120]}")

    for name, fut in futures:
        try:
            res = fut.result(timeout=60)
        except Exception as e:
            errors.append(f"{name.upper()}: {type(e).__name__}: {str(e)[:120]}")
            res = []
        if name == "bing":
            bing_results = res
        else:
            so360_results = res
    pool.shutdown(wait=False)

    # —— 跨引擎去重，拼成统一来源清单（编号即引用编号）——
    master: list[dict] = []
    seen: set[str] = set()
    for eng, results in (("Bing", bing_results),
                         ("百度", baidu_results),
                         ("360", so360_results)):
        for (t, u, s) in results:
            k = _url_key(u)
            if k and k in seen:
                continue
            if k:
                seen.add(k)
            master.append({"engine": eng, "title": t, "url": u, "snippet": s})

    if not master and not baidu_ai:
        msg = f"[搜索] 「{query}」全部通道失败" + (f"：{'; '.join(errors)}" if errors else "")
        return ToolResult(msg + "。不要编造结果，如实告知用户搜索失败。")

    # —— 综合（元宝效果核心）——
    synth = None
    if synthesize and master:
        synth = _synthesize(query, master, ctx)

    # —— 输出：综合答案（最前）→ 来源清单 → 百度AI卡(若有) → 提示 ——
    if synth:
        sections.append(
            f"[AI 综合回答 · 本地模型综合自 {len(master)} 个来源]\n{synth}")
    elif synthesize and ctx.client is None:
        sections.append(
            "[提示] 当前环境无可用模型客户端，跳过综合；已返回来源清单，请基于来源综合并标注引用。")
    elif not synthesize:
        sections.append(
            "[提示] 未启用综合(synthesize=false)，已返回来源清单，请基于来源综合并标注引用。")

    src_lines = [f"[检索来源 · 共 {len(master)} 条]"]
    for i, s in enumerate(master, 1):
        src_lines.append(
            f"[{i}] ({s['engine']}) {s['title']}\n    {s['url']}\n    {s['snippet']}")
    sections.append("\n".join(src_lines))

    if baidu_ai:
        sec = _fmt_ai(baidu_ai)
        if synth is None:
            sec = ("[提示: 百度AI总结卡已附在下方来源清单之外，可优先参考]\n" + sec)
        sections.append(sec)
    elif bing_results and _looks_degraded(query, bing_results):
        sections.append(
            "[提示: Bing 命中匿名降级页（词典/百科顶格或结果跑题，自动重试仍未翻到好页），"
            "此路质量差仅供参考，请优先看上方综合与来源清单]")

    sections.append(
        "提示: 综合回答由本地模型基于上述来源生成，引用编号对应下方来源清单；"
        "来源不足处模型会说明。核实重要事实请点开对应链接，需要原文细节再 browse_url。")
    if errors:
        sections.append(f"[通道降级] {'; '.join(errors)}")
    return ToolResult("\n".join(sections))
