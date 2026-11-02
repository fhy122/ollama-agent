"""「清空当前对话」的界面回归测试（Playwright，离线）。

盯住老板踩过的坑：右上角一颗「清空」，点完看着像把所有对话都清了。约定：
  * 按钮只出现在侧栏「本次会话」区块，不在顶栏右上角（那是全局操作的位置）；
  * 点它先弹确认，确认文案要写明「只删这一条、其他不受影响」；
  * 确认后只删当前这条（列表少一条），回到空白欢迎页；取消则什么都不动。

运行：python tests/test_ui_reset.py
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


def _seed(store, n=3):
    now = time.time()
    for i in range(n):
        d = store.create(workspace=str(Path.cwd()), model="test")
        d["title"] = f"第 {i + 1} 条对话"
        d["turns"] = [{"role": "user", "text": f"问题{i + 1}"},
                      {"role": "assistant", "events": [{"type": "text", "text": f"回答{i + 1}"}]}]
        d["updated"] = now - i * 60
        store.save(d)


def _snap(page):
    return page.evaluate(
        """() => ({
          n: document.querySelectorAll('#session-list .ss-item').length,
          msgs: document.querySelectorAll('#messages .msg').length,
          active: document.querySelector('#session-list .ss-item.active')?.innerText || '',
          welcome: getComputedStyle(document.getElementById('welcome')).display !== 'none',
          topBtn: !!document.querySelector('#topbar #btn-reset'),
          sideBtn: !!document.querySelector('#sidebar #btn-reset'),
        })"""
    )


def main():
    httpd, port, app = _serve()
    base = f"http://127.0.0.1:{port}/"
    _seed(app.session.store)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1320, "height": 900})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(base)
                page.wait_for_timeout(500)

                st = _snap(page)
                assert st["sideBtn"] and not st["topBtn"], f"清空按钮该在侧栏，不在顶栏: {st}"
                assert st["n"] == 3, st

                # 1) 取消确认：什么都不动
                page.click("#session-list .ss-item .ss-main")
                page.wait_for_timeout(600)
                st = _snap(page)
                assert st["msgs"] >= 2 and st["active"], st
                before = _snap(page)
                page.click("#btn-reset")
                page.wait_for_timeout(400)
                assert page.is_visible("#yesno-modal"), "点清空应先弹确认框"
                assert "不受影响" in page.inner_text("#yn-body"), \
                    f"确认文案要写明影响范围: {page.inner_text('#yn-body')!r}"
                page.click("#yn-cancel")
                page.wait_for_timeout(500)
                after = _snap(page)
                assert after["n"] == before["n"] == 3 and after["msgs"] == before["msgs"], \
                    f"取消后不该删任何东西: {before} -> {after}"
                assert not page.is_visible("#yesno-modal")

                # 2) 确认：只删当前这条
                page.click("#btn-reset")
                page.wait_for_timeout(400)
                page.click("#yn-ok")
                page.wait_for_timeout(800)
                st = _snap(page)
                assert st["n"] == 2, f"三条只该少一条: {st}"
                assert st["msgs"] == 0 and st["welcome"], f"清空后该回欢迎页: {st}"
                assert not st["active"], "清空后当前不该再高亮在旧对话上"
                assert len(list(app.session.store.root.glob("*.json"))) == 2, "磁盘上也只该留 2 个文件"

                # 3) 空对话再点清空：只提示，不发请求
                page.click("#btn-reset")
                page.wait_for_timeout(500)
                assert _snap(page)["n"] == 2, "空对话点清空不该影响列表"
                toast = page.inner_text("#toasts")
                assert "本来就是空" in toast, f"应提示当前是空的: {toast!r}"

                assert not errors, errors
                print("PASS: 清空按钮在侧栏 + 确认写明影响范围 + 取消不动 + 只删当前这条 + 空对话提示")
            finally:
                browser.close()
    finally:
        httpd.shutdown()
    print("PASS: 清空对话全过")


if __name__ == "__main__":
    main()
