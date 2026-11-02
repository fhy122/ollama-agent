"""历史对话搜索的界面回归测试（Playwright，离线）。

盯住三件容易坏的事：
  1. 搜索框输入后结果只显示命中的会话，命中位置有字段标签、关键词有高亮；
  2. 多词是「同时包含」，没结果要给人话提示，Esc 能清空回列表；
  3. 点搜索结果能真的打开那条对话并回放（含工具卡片），且搜索态不被刷新冲掉。

运行：python tests/test_ui_search.py
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("OLLAMA_AGENT_SESSIONS", tempfile.mkdtemp(prefix="oa-sessions-"))

from playwright.sync_api import sync_playwright

from agent.config import AgentConfig
from webui.server import WebUIApp, make_handler


def _serve():
    from http.server import ThreadingHTTPServer

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    app = WebUIApp(AgentConfig(use_color=False, warm=False), port=port)
    app.start_hotkey = lambda: False
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port, app


def _seed(store):
    """三条对话：分别把关键词放在标题 / 回答 / 提问里。"""
    now = time.time()
    rows = [
        ("侧栏收起后不能再次展开", [
            {"role": "user", "text": "左侧栏收起后，不能再次展开了"},
            {"role": "assistant", "events": [
                {"type": "text", "text": "改成窄轨方案，同一颗按钮既收起也展开。"},
                {"type": "tool_call", "name": "read_file", "args": {"path": "webui/static/style.css"}},
                {"type": "tool_result", "name": "read_file", "text": "#sb-collapsed 规则"},
            ]},
        ], now - 60),
        ("Excel 第三行数据对不对", [
            {"role": "user", "text": "能打开这张表然后截个屏吗"},
            {"role": "assistant", "events": [
                {"type": "text", "text": "先用 render_sheet 把表格渲染成图。"},
            ]},
        ], now - 7200),
        ("markdown 表格不渲染", [
            {"role": "user", "text": "markdown 语法没有正确渲染，表格和图片都不对"},
            {"role": "assistant", "events": [{"type": "text", "text": "先占位再还原。"}]},
        ], now - 86400 * 3),
    ]
    ids = []
    for title, turns, ts in rows:
        d = store.create(workspace=str(Path.cwd()), model="test")
        d["title"] = title
        d["turns"] = turns
        d["updated"] = ts
        store.save(d)
        ids.append(d["id"])
    return ids


def _probe(page):
    return page.evaluate(
        """() => ({
          box: !!document.querySelector('.sb-search'),
          n: document.querySelectorAll('#session-list .ss-item').length,
          marks: [...document.querySelectorAll('#session-list mark')].map(m => m.textContent),
          fields: [...document.querySelectorAll('#session-list .ss-field')].map(m => m.textContent),
          snips: [...document.querySelectorAll('#session-list .ss-snip')].map(m => m.innerText.trim()),
          empty: document.querySelector('#session-list .sb-empty')?.textContent || '',
          clearShown: !document.getElementById('btn-search-clear').hidden,
          focus: document.activeElement?.id || '',
        })"""
    )


def main():
    httpd, port, app = _serve()
    base = f"http://127.0.0.1:{port}/"
    ids = _seed(app.session.store)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1320, "height": 900})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(base)
                page.wait_for_timeout(500)

                st = _probe(page)
                assert st["box"], "侧栏应有搜索框"
                assert st["n"] == 3, f"初始应显示 3 条历史: {st}"

                # 标题命中：只高亮标题，不再重复一行片段
                page.fill("#session-search", "Excel")
                page.wait_for_timeout(450)
                st = _probe(page)
                assert st["n"] == 1 and st["marks"] == ["Excel"], st
                assert st["snips"] == [] and st["fields"] == [], \
                    f"标题命中时第二行该省掉（标题已高亮，别重复）: {st}"

                # 正文命中：命中位置标出来（回答里）
                page.fill("#session-search", "render_sheet")
                page.wait_for_timeout(450)
                st = _probe(page)
                assert st["n"] == 1 and st["fields"] == ["回答"], st
                assert "render_sheet" in st["snips"][0], st

                # 多词 AND
                page.fill("#session-search", "表格 图片")
                page.wait_for_timeout(450)
                st = _probe(page)
                assert st["n"] == 1 and st["fields"] == ["提问"], st
                page.fill("#session-search", "表格 压根没有")
                page.wait_for_timeout(450)
                st = _probe(page)
                assert st["n"] == 0 and "没有找到" in st["empty"], st

                # Ctrl+K 聚焦 + Esc 清空回列表
                page.click("#messages", position={"x": 30, "y": 30})
                page.keyboard.press("Control+k")
                page.wait_for_timeout(200)
                assert _probe(page)["focus"] == "session-search", "Ctrl+K 应聚焦搜索框"
                page.fill("#session-search", "渲染")
                page.wait_for_timeout(450)
                assert _probe(page)["n"] >= 2, "「渲染」应命中两条"
                page.keyboard.press("Escape")
                page.wait_for_timeout(350)
                st = _probe(page)
                assert st["n"] == 3 and not st["clearShown"], f"Esc 应清空搜索回到全量列表: {st}"

                # 点搜索结果 → 打开并回放
                page.fill("#session-search", "侧栏")
                page.wait_for_timeout(450)
                page.click("#session-list .ss-main")
                page.wait_for_timeout(700)
                st = page.evaluate(
                    """() => ({
                      user: document.querySelectorAll('#messages .msg.user').length,
                      tool: document.querySelectorAll('#messages details.tool').length,
                      stillSearching: document.querySelectorAll('#session-list .ss-result').length,
                      active: document.querySelector('#session-list .ss-item.active')?.innerText || '',
                    })"""
                )
                assert st["user"] == 1 and st["tool"] == 1, f"应回放提问与工具卡片: {st}"
                assert st["stillSearching"] == 1, "打开对话后仍应保持搜索态，不该闪回全量列表"
                assert "侧栏" in st["active"], st

                # 删除后搜索结果同步减少（删除要先在自定义确认框里点头）
                page.hover("#session-list .ss-item")
                page.click("#session-list .ss-del")
                page.wait_for_timeout(400)
                yn = page.inner_text("#yn-body")
                assert "删除后无法恢复" in yn, f"删除也要先确认: {yn!r}"
                page.click("#yn-ok")
                page.wait_for_timeout(700)
                st = _probe(page)
                assert st["n"] == 0 and "没有找到" in st["empty"], f"删掉唯一命中后应无结果: {st}"
                assert not errors, errors
                print("PASS: 搜索（标题/正文命中 + 多词 AND + 高亮 + Ctrl+K + Esc + 打开回放 + 删除同步）")
            finally:
                browser.close()
    finally:
        httpd.shutdown()
    print("PASS: 历史对话搜索全过")


if __name__ == "__main__":
    main()
