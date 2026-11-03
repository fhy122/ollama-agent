"""工作空间文件列表的位置与行为回归测试（Playwright，离线）。

盯住的是一次真改动：「工作空间文件」原本塞在设置抽屉里 —— 它不属于「设置」，
设置管的是模型后端参数，文件列表是浏览工作空间内容的入口。现在搬到侧栏。

判据（每条都能跑出结果）：
  1. 位置：列表在侧栏、不在设置抽屉；设置抽屉里不留这块 DOM；
  2. 内容：列出工作空间文件，且能点开预览（点击链路没被搬家打断）；
  3. 刷新：侧栏「刷新」按钮真的重扫（扫描前新建的文件出现）；
  4. 边界：工作空间为空时给的是人话（不是空白、不是「加载中…」卡住）；
  5. 收起：侧栏收成窄轨时整块隐藏，不撑破 rail。

运行：python tests/test_ui_ws_files.py
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


def _serve(workspace: Path):
    from http.server import ThreadingHTTPServer

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    app = WebUIApp(AgentConfig(use_color=False, warm=False, workspace=str(workspace)), port=port)
    app.start_hotkey = lambda: False
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def _probe(page):
    return page.evaluate(
        """() => {
          const box = document.getElementById('ws-files');
          const sb = document.getElementById('sidebar');
          const st = document.getElementById('settings');
          return {
            exists: !!box,
            inSidebar: !!(box && sb.contains(box)),
            inSettings: !!(box && st.contains(box)),
            n: box ? box.querySelectorAll('.ws-item').length : 0,
            names: box ? [...box.querySelectorAll('.ws-item .nm')].map(e => e.textContent) : [],
            empty: box ? (box.querySelector('.sb-empty')?.textContent || '') : '',
            visible: !!(box && box.getBoundingClientRect().height > 0),
          };
        }"""
    )


def _write(ws: Path, name: str, body: str = "x"):
    p = ws / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


def main():
    # 隔离的工作空间：不碰用户真实工作目录（禁止清单 8）
    ws = Path(tempfile.mkdtemp(prefix="oa-ws-"))
    _write(ws, "README.md", "# demo\n")
    _write(ws, "notes/todo.md", "- a\n")
    _write(ws, "data.csv", "a,b\n1,2\n")

    httpd, port = _serve(ws)
    base = f"http://127.0.0.1:{port}/"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 900})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(base)
                page.wait_for_timeout(600)

                # 1. 位置：在侧栏、不在设置
                st = _probe(page)
                assert st["exists"], "工作空间文件列表应存在"
                assert st["inSidebar"], f"应在侧栏里: {st}"
                assert not st["inSettings"], f"设置抽屉里不该再有这块: {st}"

                # 2. 内容：三个文件都在
                assert st["n"] == 3, f"应列出 3 个工作空间文件: {st}"
                assert "README.md" in st["names"] and "todo.md" in st["names"], st

                # 3. 点开预览：搬家后点击链路仍通
                page.click("#ws-files .ws-item[data-path='README.md']")
                page.wait_for_timeout(500)
                pv = page.evaluate(
                    """() => ({
                      open: !document.getElementById('preview-modal').classList.contains('hidden'),
                      name: document.getElementById('pv-name').textContent,
                      body: document.getElementById('pv-body').innerText || '',
                    })"""
                )
                assert pv["open"] and pv["name"] == "README.md", f"点应打开预览: {pv}"
                assert "demo" in pv["body"], f"预览应显示文件内容: {pv}"
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)

                # 4. 刷新：新建文件后点刷新应出现（证明按钮绑的还是活的）
                _write(ws, "after_refresh.txt", "hi")
                page.click("#btn-refresh-files")
                page.wait_for_timeout(700)
                st = _probe(page)
                assert st["n"] == 4 and "after_refresh.txt" in st["names"], \
                    f"刷新后应扫到新文件: {st}"

                # 5. 收起侧栏：整块跟着隐藏，不撑破窄轨
                page.click("#btn-sidebar-toggle")
                page.wait_for_timeout(350)
                st = _probe(page)
                assert not st["visible"], f"侧栏收起时文件列表应隐藏: {st}"
                w = page.evaluate("() => Math.round(document.getElementById('sidebar').getBoundingClientRect().width)")
                assert w < 90, f"收起后侧栏应是窄轨: {w}"
                page.click("#btn-sidebar-toggle")
                page.wait_for_timeout(350)

                # 6. 设置抽屉里不再有这块（人不会在「设置」里看见文件列表）
                page.click("#btn-settings")
                page.wait_for_timeout(400)
                assert not _probe(page)["inSettings"], "打开设置抽屉也不该出现工作空间文件"
                page.keyboard.press("Escape")
                page.wait_for_timeout(250)

                assert not errors, f"不应有 JS 报错: {errors}"
                print("PASS: 位置（侧栏/非设置）+ 内容 + 预览 + 刷新 + 收起隐藏")
                page.close()

                # 7. 边界：空工作空间要有指路文案，不是卡在「加载中…」
                empty_ws = Path(tempfile.mkdtemp(prefix="oa-ws-empty-"))
                httpd2, port2 = _serve(empty_ws)
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 900})
                    page.goto(f"http://127.0.0.1:{port2}/")
                    page.wait_for_timeout(700)
                    st = _probe(page)
                    assert st["n"] == 0, f"空工作空间不该有文件项: {st}"
                    assert st["empty"] and "加载中" not in st["empty"], st
                    assert "没有文件" in st["empty"], f"空态应说清是没文件: {st['empty']!r}"
                    print(f"PASS: 空工作空间文案 = {st['empty']!r}")
                    page.close()
                finally:
                    httpd2.shutdown()
            finally:
                browser.close()
    finally:
        httpd.shutdown()
    print("PASS: 工作空间文件搬进侧栏 全过")


if __name__ == "__main__":
    main()
