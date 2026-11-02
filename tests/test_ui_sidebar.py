"""侧栏收起 / 展开的界面回归测试（Playwright，离线）。

盯住一个曾经的真 bug：桌面端把侧栏收起来之后，找不到任何入口再展开，
只能刷新页面。现在约定：
  * 桌面端（>=901px）收起后缩成窄轨，箭头按钮原地翻转，再点即展开；
  * 窄屏（<=900px）收起后是抽屉，顶栏汉堡按钮负责展开；
  * Ctrl/Cmd+B 任何时候都能切换。

运行：python tests/test_ui_sidebar.py
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
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
    return httpd, port


def _state(page):
    return page.evaluate(
        """() => {
          const sb = document.getElementById('sidebar');
          const open = document.getElementById('btn-sidebar-open');
          const cs = getComputedStyle(open);
          return {
            collapsed: document.getElementById('shell').classList.contains('sb-collapsed'),
            w: Math.round(sb.getBoundingClientRect().width),
            openBtn: cs.display,
            offscreen: sb.getBoundingClientRect().right <= 1,
          };
        }"""
    )


def main():
    httpd, port = _serve()
    base = f"http://127.0.0.1:{port}/"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                # ---------- 桌面：窄轨往返 ----------
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(base)
                page.wait_for_timeout(400)

                st = _state(page)
                assert not st["collapsed"] and st["w"] > 200, st

                page.click("#btn-sidebar-toggle")
                page.wait_for_timeout(300)
                st = _state(page)
                assert st["collapsed"] and st["w"] < 90, f"收起后应缩成窄轨: {st}"
                assert st["openBtn"] == "none", "桌面端不靠顶栏按钮，靠窄轨里的箭头"

                page.click("#btn-sidebar-toggle")
                page.wait_for_timeout(300)
                st = _state(page)
                assert not st["collapsed"] and st["w"] > 200, f"再点同一颗按钮应能展开: {st}"

                page.keyboard.press("Control+b")
                page.wait_for_timeout(250)
                assert _state(page)["collapsed"], "Ctrl+B 应能收起"
                page.keyboard.press("Control+b")
                page.wait_for_timeout(250)
                assert not _state(page)["collapsed"], "Ctrl+B 应能展开"
                assert not errors, errors
                print("PASS: 桌面端收起成窄轨 + 同一颗按钮展开 + Ctrl+B 切换")
                page.close()

                # ---------- 窄屏：抽屉往返 ----------
                page = browser.new_page(viewport={"width": 800, "height": 900})
                page.goto(base)
                page.wait_for_timeout(400)
                st = _state(page)
                assert st["collapsed"] and st["openBtn"] == "grid", f"窄屏默认收起且顶栏有按钮: {st}"

                page.click("#btn-sidebar-open")
                page.wait_for_timeout(300)
                st = _state(page)
                assert not st["collapsed"] and not st["offscreen"], f"抽屉应滑入: {st}"

                page.click("#btn-sidebar-toggle")
                page.wait_for_timeout(300)
                st = _state(page)
                assert st["collapsed"] and st["offscreen"], f"抽屉应收起: {st}"

                page.click("#btn-sidebar-open")
                page.wait_for_timeout(300)
                st = _state(page)
                assert not st["collapsed"] and not st["offscreen"], f"顶栏按钮应能再打开: {st}"
                print("PASS: 窄屏抽屉收起 / 顶栏按钮展开往返")
                page.close()
            finally:
                browser.close()
    finally:
        httpd.shutdown()
    print("PASS: 侧栏收起展开全过")


if __name__ == "__main__":
    main()
