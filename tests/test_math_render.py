"""数学公式渲染回归测试（Playwright，离线、不联网）。

盯住的需求：回答里的 `$P(A) = \\frac{A}{10}$` 要显示成排版公式，
而不是一串带着反斜杠的源码文本。

判据（正常 / 错误 / 边界）：
  1. KaTeX 由本地 /static/vendor/katex/ 提供，字体 MIME 正确（断网也能用）；
  2. 行内 $...$ 渲染出 .katex 与分数结构 .mfrac，无渲染错误；
  3. 显示公式 $$...$$ / \\[...\\] 独占 .md-math-block，不被 <p> 包住；
  4. 代码块、行内代码、货币金额里的 $ 不被当公式；
  5. 流式输出中未闭合的 $ 不出错、不渲染、原文还在，补齐后能变成公式；
  6. 原有 Markdown（粗体 / 链接 / 表格 / 代码块）没被改坏；
  7. 页面无 JS 报错、本地静态资源无 404。

运行：python tests/test_math_render.py
"""
from __future__ import annotations

import os
import re
import socket
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = Path(tempfile.mkdtemp(prefix="oa-ui-math-"))
os.environ["OLLAMA_AGENT_SESSIONS"] = str(_TMP / "sessions")
os.environ["OLLAMA_AGENT_CONFIG"] = str(_TMP / "config.json")

from playwright.sync_api import sync_playwright

from agent.config import AgentConfig
from webui.server import WebUIApp, make_handler


def _serve(workspace: Path):
    from http.server import ThreadingHTTPServer

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    app = WebUIApp(AgentConfig(use_color=False, warm=False,
                               workspace=str(workspace)), port=port)
    app.start_hotkey = lambda: False
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port, app


# 把渲染结果挂到真实 DOM 上再查：KaTeX 的字体、样式、标签结构都要真生效
_RENDER = """(src) => {
  const html = renderMarkdown(src);
  const box = document.createElement('div');
  box.className = 'bubble';
  box.innerHTML = html;
  document.body.appendChild(box);
  const r = {
    html,
    katex: box.querySelectorAll('.katex').length,
    mfrac: box.querySelectorAll('.mfrac').length,
    err: box.querySelectorAll('.katex-error').length,
    blocks: box.querySelectorAll('.md-math-block').length,
    blockInP: box.querySelectorAll('p .md-math-block').length,
    codeCards: box.querySelectorAll('.code-card').length,
    strong: box.querySelectorAll('strong').length,
    links: box.querySelectorAll('a').length,
    tables: box.querySelectorAll('table.md-table').length,
    text: box.textContent,
  };
  box.remove();
  return r;
}"""


def _render(page, src):
    return page.evaluate(_RENDER, src)


def main():
    ws = Path(tempfile.mkdtemp(prefix="oa-ws-math-"))
    httpd, port, app = _serve(ws)
    base = f"http://127.0.0.1:{port}/"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                errors, bad = [], []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.on("response", lambda r: bad.append((r.url, r.status))
                        if "/static/vendor/katex/" in r.url and r.status >= 400 else None)
                page.goto(base)
                page.wait_for_timeout(700)

                # 1. 本地 KaTeX 真加载了，字体路径与 MIME 都对（离线可用）
                ok = page.evaluate("() => typeof katex !== 'undefined' && "
                                   "typeof katex.renderToString === 'function'")
                assert ok, "本地 KaTeX 没加载：检查 index.html 的 script 顺序"
                f = page.request.get(base + "static/vendor/katex/fonts/KaTeX_Main-Regular.woff2")
                assert f.status == 200, f"字体 404: {f.status}"
                assert "woff2" in f.headers.get("content-type", ""), \
                    f"字体 MIME 不对: {f.headers.get('content-type')}"
                print(f"  ok  本地 KaTeX 就绪，字体 200 {f.headers.get('content-type')}")

                # 2. 用户报的那条公式
                r = _render(page, r"概率是 $P(A) = \frac{A}{10}$，其中 A 是点数。")
                assert r["katex"] == 1, f"行内公式没渲染: {r}"
                assert r["mfrac"] >= 1, f"分数结构没出来: {r['html'][:200]}"
                assert r["err"] == 0, f"KaTeX 报了渲染错误: {r['html'][:200]}"
                # KaTeX 默认输出 htmlAndMathml：<annotation> 里会原样保留 LaTeX
                # 源码（不可见，供读屏/复制用）。所以「HTML 里出现 \frac」不等于
                # 没渲染 —— 去掉 annotation 后的可见部分才是判据。
                visible = re.sub(r"<annotation[^>]*>[\s\S]*?</annotation>", "", r["html"])
                assert r"\frac" not in visible, "可见部分不该残留反斜杠源码"
                print("  ok  $P(A) = \\frac{A}{10}$ → .katex + .mfrac，无错误")

                # 3. 显示公式独占一块，不被 <p> 包住
                r = _render(page, "前文\n\n$$\\frac{a}{b} = c$$\n\n后文")
                assert r["blocks"] == 1 and r["blockInP"] == 0, r
                assert r["mfrac"] >= 1 and r["err"] == 0, r
                r2 = _render(page, r"展示：\[\sum_{i=1}^{n} i\] 与行内 \(x^2\)")
                assert r2["katex"] == 2 and r2["blocks"] == 1, r2
                assert r2["err"] == 0, r2
                print("  ok  $$...$$ / \\[...\\] 块级，\\(...\\) 行内")

                # 4. 不该渲染的地方
                r = _render(page, "```python\ns = '$x^2$'\n```")
                assert r["codeCards"] == 1 and r["katex"] == 0, r
                assert "$x^2$" in r["text"], r
                r = _render(page, "用 `$x^2$` 表示源码，而 $y^2$ 是公式。")
                assert "<code>$x^2$</code>" in r["html"] and r["katex"] == 1, r
                r = _render(page, "价格 $5 和 $10 不是公式。")
                assert r["katex"] == 0 and "$5" in r["text"], r
                print("  ok  代码块 / 行内代码 / 货币金额里的 $ 未被误判")

                # 5. 流式：半截公式不闪错，补齐后立刻成为公式
                half = r"概率是 $P(A) = \fra"
                r = _render(page, half)
                assert r["katex"] == 0 and r["err"] == 0, r
                assert r"\fra" in r["text"], r
                full = _render(page, r"概率是 $P(A) = \frac{A}{10}$")
                assert full["katex"] == 1 and full["mfrac"] >= 1, full
                print("  ok  流式未闭合保持原文，闭合后渲染成公式")

                # 6. 既有 Markdown 没被占位符改坏
                r = _render(page, "**粗体** 和 https://example.com\n\n"
                                  "| a | b |\n|---|---|\n| 1 | 2 |")
                assert r["strong"] == 1 and r["links"] == 1 and r["tables"] == 1, r
                print("  ok  粗体 / 链接 / 表格照常")

                page.wait_for_timeout(400)   # 等字体请求发起
                assert not bad, f"本地 KaTeX 资源 404: {bad}"
                assert not errors, f"页面 JS 报错: {errors}"
                print("PASS")
            finally:
                browser.close()
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
