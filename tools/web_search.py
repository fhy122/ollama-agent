"""web_search — 联网搜索，双引擎并行：百度 AI 聚合摘要 + Bing/百度常规结果。

路线：
  1. 百度 AI 总结（Playwright 渲染）：一次搜索即得"AI 总结 N 篇结果"的聚合答案+引用来源，
     模型可直接引用，无需逐条打开网页。headless 可抓；卡片间歇出现，带自动展开。
  2. Bing 常规结果（纯 HTTP 解析，快）：cn.bing.com 的 li.b_algo 结果块，质量较高。
     注意：Bing 对匿名会话会随机发"词典兜底页"（词典/百科顶格）——实测同一 query 时好时坏，
     真实登录浏览器正常。对策：命中降级页自动重试（同会话 Cookie + 退避）翻到好页。
     Bing 的 AI 摘要绑定登录态/灰度，headless 抓不到，放弃。
  3. 百度常规结果：作为补充（质量略逊），与 Bing 去重后并入。

并行：Bing 纯 HTTP 走后台线程，百度 Playwright 留主线程（同步 API 有线程亲和），
总耗时 ≈ 百度一条，Bing 零额外等待。

engine 参数：auto/both(默认,双引擎全量) | baidu | bing
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


def _dedup(baidu_results: list[tuple[str, str, str]],
           bing_results: list[tuple[str, str, str]]) -> list[tuple[str, str, str]]:
    """百度结果里去掉与 Bing 重复的（按 URL 主干比对），减少冗余。"""
    def key(u: str) -> str:
        u = (u or "").split("#")[0].rstrip("/")
        u = re.sub(r"^https?://(www\.)?", "", u)
        return re.sub(r"[?&](utm_\w+|from|wd|eqid)=\w+", "", u)

    seen = {key(u) for _, u, _ in bing_results}
    out = []
    for t, u, s in baidu_results:
        k = key(u)
        if k and k in seen:
            continue
        seen.add(k)
        out.append((t, u, s))
    return out


@tool(
    name="web_search",
    description=(
        "联网搜索（双引擎并行：Bing 结果 + 百度AI总结/百度结果）。"
        "输出顺序：AI聚合摘要（已综合多篇结果并附引用来源，摘要足够可直接回答，无需逐条 browse_url）"
        "→ Bing 结果（质量较高）→ 百度结果（补充）。需要原文细节才用 browse_url。"
        "查最新信息、事实类问题先用这个。"),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词，可用中英文"},
            "max_results": {"type": "integer", "description": "每个引擎常规结果最多条数，默认 6"},
            "engine": {
                "type": "string",
                "description": "搜索通道：auto/both(默认,双引擎全量) | baidu | bing",
                "enum": ["auto", "both", "baidu", "bing"],
            },
        },
        "required": ["query"],
    },
    confirm=True,
)
def web_search(ctx: ToolContext, query: str, max_results: int = 6,
               engine: str = "auto") -> ToolResult:
    max_results = max(1, int(max_results or 6))
    engine = (engine or "auto").lower()
    if engine == "both":
        engine = "auto"
    want_bing = engine in ("auto", "bing")
    want_baidu = engine in ("auto", "baidu")

    sections: list[str] = []
    errors: list[str] = []

    # Bing 纯 HTTP → 后台线程先跑；百度 Playwright 留主线程（线程亲和）
    bing_results: list[tuple[str, str, str]] = []
    bing_future = None
    if want_bing:
        pool = ThreadPoolExecutor(max_workers=1)
        bing_future = pool.submit(_search_bing, query, max_results)

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

    if bing_future is not None:
        try:
            bing_results = bing_future.result(timeout=60)
        except Exception as e:
            errors.append(f"Bing: {type(e).__name__}: {str(e)[:120]}")
        finally:
            pool.shutdown(wait=False)

    # 百度结果与 Bing 去重后作为补充
    baidu_results = _dedup(baidu_results, bing_results)

    if baidu_ai:
        sections.append(_fmt_ai(baidu_ai))
    if bing_results:
        sec = _fmt_results("Bing", bing_results)
        if _looks_degraded(query, bing_results):
            sec = ("[提示: 该查询命中 Bing 匿名降级页（词典/百科顶格或结果跑题，自动重试仍未翻到好页），"
                   "此块质量差仅供参考，请优先看上方 AI 摘要与下方百度结果]\n" + sec)
        sections.append(sec)
    if baidu_results:
        sections.append(_fmt_results("百度（补充）", baidu_results))

    if not sections:
        msg = f"[搜索] 「{query}」全部通道失败" + (f"：{'; '.join(errors)}" if errors else "")
        return ToolResult(msg + "。不要编造结果，如实告知用户搜索失败。")

    sections.append(
        "提示: AI聚合摘要已综合多篇内容并附来源，摘要足够时直接引用（注明来源）即可；"
        "需要原文细节再 browse_url 对应链接。")
    if errors:
        sections.append(f"[通道降级] {'; '.join(errors)}")
    return ToolResult("\n\n".join(sections))
