"""侧栏「工作空间文件」目录树的 UI 冒烟（Playwright，离线）。

运行：python tests/test_file_tree_ui.py

判据：
  1. 根层目录显示为可展开行（.ws-dir），根层文件才直接平铺；
  2. 跨目录同名文件各有各的位置（截图里的痛点：8 个 flat 列表分不清谁是谁）；
  3. 点目录 → 展开并拉出子项，caret/文件夹图标变展开态，缩进比父级深；
  4. 再点一次 → 收起，子项隐藏（DOM 保留，不重新请求）；
  5. 「刷新」保留已展开的目录（不能一刷新就全部折叠）；
  6. 点树里的文件 → 预览弹窗打开的是那个目录下的那份（不是同名另一份）；
  7. 忽略目录（.git 等）不出现在树里；
  8. 悬停文件行出现「+」，点它把文件挂进上下文（chip 出现、不弹预览、重复添加去重）；
  9. 预览弹窗里的「加入上下文」同样生效；
 10. 整轮无 JS 运行时错误。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = Path(tempfile.mkdtemp(prefix="oa-ui-tree-"))
os.environ["OLLAMA_AGENT_SESSIONS"] = str(_TMP / "sessions")
os.environ["OLLAMA_AGENT_CONFIG"] = str(_TMP / "config.json")

from playwright.sync_api import sync_playwright  # noqa: E402

from agent.config import AgentConfig  # noqa: E402
from webui.server import WebUIApp, make_handler  # noqa: E402


def _serve(workspace: Path):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    app = WebUIApp(AgentConfig(use_color=False, warm=False,
                               workspace=str(workspace)), port=port)
    app.start_hotkey = lambda: False
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, port


def _build_ws(root: Path) -> Path:
    """三个目录各有一个 parser.py —— 同名文件必须有可视的归属。"""
    ws = root / "ws"
    (ws / "alpha" / "deep").mkdir(parents=True)
    (ws / "beta").mkdir()
    (ws / "alpha" / "parser.py").write_text("ALPHA_PARSER", encoding="utf-8")
    (ws / "alpha" / "deep" / "parser.py").write_text("DEEP_PARSER", encoding="utf-8")
    (ws / "beta" / "parser.py").write_text("BETA_PARSER", encoding="utf-8")
    (ws / "README.md").write_text("# root readme", encoding="utf-8")
    (ws / ".git").mkdir()
    (ws / ".git" / "config").write_text("[core]", encoding="utf-8")
    (ws / "__pycache__").mkdir()
    (ws / "__pycache__" / "x.pyc").write_text("x", encoding="utf-8")
    return ws


def main():
    ws = _build_ws(_TMP)
    httpd, port = _serve(ws)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 860})
            js_errors: list[str] = []
            page.on("pageerror", lambda e: js_errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/")
            page.wait_for_selector("#ws-files .ws-dir")

            def rows():
                return page.evaluate("""() => [...document.querySelectorAll('#ws-files .ws-dir')]
                  .map(e => ({dir: e.dataset.dir, open: e.classList.contains('open'),
                              depth: Number(e.dataset.depth),
                              pad: parseInt(getComputedStyle(e).paddingLeft, 10)}))""")

            def files():
                return page.evaluate("""() => [...document.querySelectorAll('#ws-files .ws-item')]
                  .map(e => ({path: e.dataset.path,
                              name: e.querySelector('.nm').textContent.trim(),
                              pad: parseInt(getComputedStyle(e).paddingLeft, 10)}))""")

            # 1. 根层：两个目录 + README，子目录里的文件不该平铺出来
            root_dirs = {r["dir"] for r in rows()}
            assert root_dirs == {"alpha", "beta"}, root_dirs
            root_files = {f["path"] for f in files()}
            assert root_files == {"README.md"}, root_files
            print(f"  ok  根层只列一层：目录 {sorted(root_dirs)}，文件 {sorted(root_files)}")

            # 2. 忽略目录不出现
            all_text = page.inner_text("#ws-files")
            for ignored in (".git", "__pycache__", "config", "x.pyc"):
                assert ignored not in all_text, f"{ignored} 不该出现在树里"
            print("  ok  .git / __pycache__ 已隐藏")

            # 3. 展开 alpha：子项出现、缩进更深、展开态
            parent_pad = next(r["pad"] for r in rows() if r["dir"] == "alpha")
            page.click('[data-dir="alpha"]')
            page.wait_for_selector('[data-path="alpha/parser.py"]')
            a = next(r for r in rows() if r["dir"] == "alpha")
            assert a["open"], "展开后应带 open 类"
            kids = {f["path"]: f for f in files()}
            assert "alpha/parser.py" in kids and "alpha/deep" in {r["dir"] for r in rows()}
            assert kids["alpha/parser.py"]["pad"] > parent_pad, \
                (kids["alpha/parser.py"]["pad"], parent_pad)
            print(f"  ok  展开 alpha：子项缩进 {parent_pad}px → "
                  f"{kids['alpha/parser.py']['pad']}px")

            # 3b. 再展开一层，三层同名文件各自可辨
            page.click('[data-dir="alpha/deep"]')
            page.wait_for_selector('[data-path="alpha/deep/parser.py"]')
            depths = {r["dir"]: r["depth"] for r in rows()}
            assert depths["alpha"] == 0 and depths["alpha/deep"] == 1, depths
            print("  ok  多层展开：depth 逐层递增")

            # 4. 收起：子项隐藏但 DOM 还在（再展开不再请求）
            page.click('[data-dir="alpha"]')
            page.wait_for_function(
                "() => document.querySelector('[data-kids=\"alpha\"]').hidden === true")
            assert not next(r for r in rows() if r["dir"] == "alpha")["open"]
            assert page.locator('[data-path="alpha/parser.py"]').count() == 1, \
                "收起只隐藏，不该销毁 DOM"
            print("  ok  收起：子项隐藏、DOM 保留")

            # 5. 刷新保留展开状态（先重新展开）
            page.click('[data-dir="alpha"]')
            page.wait_for_selector('[data-path="alpha/parser.py"]')
            page.click("#btn-refresh-files")
            page.wait_for_selector('[data-dir="alpha"].open')
            page.wait_for_selector('[data-path="alpha/parser.py"]')
            print("  ok  刷新后仍保持展开")

            # 6. 点同名文件：打开的是该目录那一份
            page.click('[data-path="alpha/parser.py"]')
            page.wait_for_selector("#preview-modal:not(.hidden)")
            page.wait_for_function(
                "() => document.getElementById('pv-body').textContent.includes('ALPHA_PARSER')")
            assert page.inner_text("#pv-name").strip() == "parser.py"
            body = page.inner_text("#pv-body")
            assert "BETA_PARSER" not in body and "DEEP_PARSER" not in body, body
            print("  ok  点 alpha/parser.py → 预览的是 ALPHA_PARSER")

            page.click("#pv-close")
            page.wait_for_function(
                "() => document.getElementById('preview-modal').classList.contains('hidden')")

            # 7. 悬停「+」把文件加入上下文：只挂 chip，不弹预览
            page.locator('[data-path="alpha/parser.py"]').hover()
            page.click('.ws-add[data-add="alpha/parser.py"]')
            page.wait_for_selector("#attachments .attach")
            assert page.evaluate(
                "() => state.files.some(f => f.path === 'alpha/parser.py')")
            assert page.evaluate(
                "() => document.getElementById('preview-modal').classList.contains('hidden')"), \
                "点「+」不该同时弹出预览"
            print("  ok  悬停「+」→ 已加入上下文")

            # 7b. 再加同一个：去重，不重复挂 chip
            n_before = page.evaluate("() => state.files.length")
            page.click('.ws-add[data-add="alpha/parser.py"]')
            page.wait_for_timeout(200)
            assert page.evaluate("() => state.files.length") == n_before, "同一个文件不该加两次"
            assert page.locator("#attachments .attach").count() == n_before
            print("  ok  重复添加已去重")

            # 7c. 预览弹窗里的「加入上下文」也能加（看完内容直接挂上）
            page.click('[data-dir="beta"]')
            page.wait_for_selector('[data-path="beta/parser.py"]')
            page.click('[data-path="beta/parser.py"]')
            page.wait_for_selector("#preview-modal:not(.hidden)")
            page.wait_for_function(
                "() => document.getElementById('pv-body').textContent.includes('BETA_PARSER')")
            page.click("#pv-add")
            page.wait_for_function(
                "() => state.files.some(f => f.path === 'beta/parser.py')")
            assert page.locator("#attachments .attach").count() == n_before + 1
            page.click("#pv-close")
            print("  ok  预览里「加入上下文」生效")

            assert not js_errors, f"页面有 JS 运行时错误：{js_errors}"
            print("  ok  无 JS 运行时错误")
            browser.close()
        print("PASS: 工作空间目录树 UI 全部通过")
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
