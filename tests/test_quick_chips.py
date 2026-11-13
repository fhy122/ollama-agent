"""输入框下方快捷入口的 UI 冒烟（Playwright，离线、不联网）。

运行：python tests/test_quick_chips.py

判据（写功能的同时写「怎么才算对」）：
  1. 三个 chip 都在，模型 chip 的文字来自 /api/config 的当前生效模型；
  2. 点模型 chip 弹出的浮层里，模型列表 / 思考模式 / 上下文窗口 /
     「配置自定义模型」入口齐全；
  3. 权限开关切换后后端 cfg.confirm 真的翻转，chip 文字跟着变；
  4. 工作空间应用后后端 workspace 真的变；
  5. Esc 能关闭浮层，且整轮没有 JS 运行时错误；
  6. 走真实 HTTP 切换模型 → 落盘 → 用同一配置文件新建一个 WebUIApp
     （模拟重启）→ 页面上模型 chip 显示的是上次选中的那个，不是启动默认值；
  7. 工具 chip 切「只对话」→ 后端真的只下发四个工具 schema；
  8. 单独取消一个勾选 → 自动切「自定义」，后端白名单同步；
  9. 偏好抽屉里的工具清单与当前策略一致；
 10. 重启后工具策略仍在。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = Path(tempfile.mkdtemp(prefix="oa-ui-quick-"))
os.environ["OLLAMA_AGENT_SESSIONS"] = str(_TMP / "sessions")
os.environ["OLLAMA_AGENT_CONFIG"] = str(_TMP / "config.json")

from playwright.sync_api import sync_playwright  # noqa: E402

import tools  # noqa: E402,F401  触发工具注册（下面按注册表算期望值）
from agent import toolpolicy  # noqa: E402
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
    return httpd, port, app


def _post(port: int, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def main():
    workspace = _TMP / "ws"
    workspace.mkdir(exist_ok=True)
    cfg_path = Path(os.environ["OLLAMA_AGENT_CONFIG"])
    httpd1, port1, app1 = _serve(workspace)
    httpd2 = None
    httpd3 = None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            js_errors: list[str] = []
            page.on("pageerror", lambda e: js_errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port1}/")
            page.wait_for_selector("#quick-model-name")
            page.wait_for_function(
                "document.getElementById('quick-model-name').textContent.trim() !== '…'")

            # 1. 三个 chip 存在，模型名来自后端而不是写死的常量
            for cid in ("quick-model", "quick-perm", "quick-ws"):
                assert page.locator("#" + cid).count() == 1, cid
            live = page.evaluate(
                "async () => (await (await fetch('/api/config')).json()).model")
            shown = page.inner_text("#quick-model-name").strip()
            assert shown == live, (shown, live)
            print(f"  ok  chip 显示后端生效模型：{shown}")

            # 2. 模型浮层结构齐全
            page.click("#quick-model")
            page.wait_for_selector("#quick-pop:not(.hidden)")
            assert page.locator("#quick-pop .qp-list").count() == 1
            assert page.locator('#quick-pop select[data-qp="think"]').count() == 1
            assert page.locator('#quick-pop select[data-qp="ctx"]').count() == 1
            assert page.locator('#quick-pop [data-qp="new-model"]').count() == 1
            print("  ok  模型浮层：模型列表 + 思考模式 + 上下文窗口 + 自定义入口")

            # 2b. 浮层里点一下模型行 → 后端真的切过去（不是只改了界面）。
            # 本机不一定装了 Ollama，直接往 state.models 注入一份清单，
            # 验的是「点击 → /api/config → switch_model」这条链路本身。
            page.evaluate("""() => {
              state.models = {
                active: 'ollama-local', profiles: [],
                builtin: { active: true, model: state.config.model,
                           models: ['fake-a:1b', 'fake-b:2b'],
                           stale: false, error: '' }
              };
              renderQuick();
            }""")
            page.click('[data-qp="pick-ollama"][data-name="fake-a:1b"]')
            page.wait_for_function(
                "document.getElementById('quick-model-name').textContent.trim() === 'fake-a:1b'")
            assert app1.session.cfg.model == "fake-a:1b", \
                f"浮层点击没切到后端：{app1.session.cfg.model!r}"
            print("  ok  浮层点击一键切换：后端 cfg.model = fake-a:1b")

            # 5. 重新打开再 Esc 关闭
            page.click("#quick-model")
            page.wait_for_selector("#quick-pop:not(.hidden)")
            page.keyboard.press("Escape")
            page.wait_for_function(
                "document.getElementById('quick-pop').classList.contains('hidden')")
            print("  ok  Esc 关闭浮层")

            # 3. 权限开关真的改到后端
            page.click("#quick-perm")
            page.wait_for_selector('#quick-pop [data-qp="perm-full"]')
            before = app1.session.confirm
            page.click('#quick-pop [data-qp="perm-full"]')
            page.wait_for_function(
                "document.getElementById('quick-perm-label').textContent.includes('完全访问')"
                if before else
                "document.getElementById('quick-perm-label').textContent.includes('默认权限')")
            assert app1.session.confirm != before, "confirm 没被后端真的改掉"
            print(f"  ok  权限开关：confirm {before} → {app1.session.confirm}")

            # 4. 工作空间真的改到后端
            ws2 = _TMP / "ws2"
            ws2.mkdir(exist_ok=True)
            page.click("#quick-ws")
            page.fill("#qp-ws-input", str(ws2))
            page.click('[data-qp="ws-apply"]')
            page.wait_for_function(
                "document.getElementById('quick-ws-label').textContent.includes('ws2')")
            assert Path(app1.session.workspace).resolve() == ws2.resolve()
            print(f"  ok  工作空间：{app1.session.workspace}")
            page.keyboard.press("Escape")

            # 6. 真实 HTTP 切模型 → 落盘 → 模拟重启
            chosen = "my-model:7b"
            _post(port1, "/api/config", {"model": chosen})
            raw = json.loads(cfg_path.read_text(encoding="utf-8"))
            assert raw.get("builtin_model") == chosen, raw
            print(f"  ok  切换已落盘：builtin_model={chosen}")

            httpd2, port2, app2 = _serve(workspace)
            assert app2.session.cfg.model == chosen, \
                f"重启后 cfg.model={app2.session.cfg.model!r}，应为 {chosen}"
            page2 = browser.new_page()
            page2.on("pageerror", lambda e: js_errors.append(str(e)))
            page2.goto(f"http://127.0.0.1:{port2}/")
            page2.wait_for_function(
                "document.getElementById('quick-model-name').textContent.trim() === 'my-model:7b'")
            print("  ok  重启后页面显示上次选中的模型（不是启动默认值）")

            # 7. 工具 chip：切到「只对话」→ 后端真的只下发四件
            assert page2.locator("#quick-tools").count() == 1
            page2.click("#quick-tools")
            page2.wait_for_selector("#quick-pop:not(.hidden)")
            for mode in ("all", "chat", "custom", "none"):
                assert page2.locator(
                    f'[data-qp="tool-mode"][data-mode="{mode}"]').count() == 1, mode
            page2.click('[data-qp="tool-mode"][data-mode="chat"]')
            page2.wait_for_function(
                "document.getElementById('quick-tools-label').textContent.trim() === '只对话'")
            assert app2.session.cfg.tool_mode == "chat", app2.session.cfg.tool_mode
            specs = toolpolicy.specs_for(app2.session.cfg)
            names = {s["function"]["name"] for s in specs}
            assert names == set(toolpolicy.CHAT_TOOLS), names
            print(f"  ok  只对话：后端下发 {len(names)} 个工具 {sorted(names)}")

            # 8. 单独勾一个 = 自动切自定义模式
            page2.wait_for_selector('#quick-pop input[data-name="browse_url"]')
            page2.uncheck('#quick-pop input[data-name="browse_url"]')
            page2.wait_for_function(
                "document.getElementById('quick-tools-label').textContent.includes('自定义')")
            assert app2.session.cfg.tool_mode == "custom"
            assert "browse_url" not in app2.session.cfg.tools_enabled
            print(f"  ok  取消勾选 → 自定义，白名单 {app2.session.cfg.tools_enabled}")

            # 9. 偏好抽屉里的清单与当前策略一致（同源，不另维护一份）
            page2.keyboard.press("Escape")
            page2.click("#btn-settings")
            page2.click('.set-tab[data-tab="prefs"]')
            page2.wait_for_selector("#set-tool-list input[data-qp='tool-toggle']")
            assert page2.input_value("#set-tool-mode") == "custom"
            boxes = page2.locator("#set-tool-list input[data-qp='tool-toggle']").count()
            all_tools_n = len(tools.all_tools())
            assert boxes == all_tools_n, (boxes, all_tools_n)
            print(f"  ok  偏好抽屉：{boxes} 个工具开关，模式=custom")

            # 10. 重启后策略仍在
            httpd3, port3, app3 = _serve(workspace)
            assert app3.session.cfg.tool_mode == "custom", app3.session.cfg.tool_mode
            assert "browse_url" not in app3.session.cfg.tools_enabled
            page3 = browser.new_page()
            page3.on("pageerror", lambda e: js_errors.append(str(e)))
            page3.goto(f"http://127.0.0.1:{port3}/")
            page3.wait_for_function(
                "document.getElementById('quick-tools-label').textContent.includes('自定义')")
            print("  ok  重启后仍显示上次的工具策略")

            assert not js_errors, f"页面有 JS 运行时错误：{js_errors}"
            print("  ok  无 JS 运行时错误")
            browser.close()
        print("PASS: 快捷入口 + 模型选择持久化 + 工具策略 全部通过")
    finally:
        httpd1.shutdown()
        for h in (httpd2, httpd3):
            if h:
                h.shutdown()


if __name__ == "__main__":
    main()
