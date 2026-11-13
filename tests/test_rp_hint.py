"""repeat_penalty 分档提示的 UI 冒烟（Playwright，离线、不联网）。

运行：python tests/test_rp_hint.py

判据：
  1. 偏好的输入框下面有一块提示，初始文字来自当前配置（留空 → 说明「不下发」）；
  2. 边输边变：1.1 → 安全（ok 色）、1.2 → 警戒（warn 色）、1.5 → 危险（danger 色），
     而且 1.5 的文案点名「格式没了 / 只有一半」这个真实症状，人能对上号；
  3. 1.0 单独说明「= 关闭惩罚」，不跟安全档混为一谈；
  4. 非法值（0、负、非数字）只说「不会下发」，不误判为危险档；
  5. 自定义模型表单里：provider=ollama 才给分档提示，openai 兼容直接说「不会下发」；
  6. 整轮没有 JS 运行时错误。
"""
from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = Path(tempfile.mkdtemp(prefix="oa-ui-rp-"))
os.environ["OLLAMA_AGENT_SESSIONS"] = str(_TMP / "sessions")
os.environ["OLLAMA_AGENT_CONFIG"] = str(_TMP / "config.json")

from playwright.sync_api import sync_playwright  # noqa: E402

from agent.config import AgentConfig  # noqa: E402
from webui.server import WebUIApp, make_handler  # noqa: E402

_fails: list[str] = []


def check(cond: bool, label: str) -> None:
    if cond:
        print(f"  ok  {label}")
    else:
        print(f"  FAIL {label}")
        _fails.append(label)


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


def main() -> int:
    httpd, port, _app = _serve(_TMP)
    errors: list[str] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 860})
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"http://127.0.0.1:{port}/")
            page.wait_for_selector("#quick-model")

            page.click("#btn-settings")
            page.click('.set-tab[data-tab="prefs"]')
            page.wait_for_selector("#set-rp-hint")

            JS = """el => {
                const cl = el.classList;
                return {text: el.textContent, cls: el.className,
                        tier: cl.contains('danger') ? 'danger'
                              : cl.contains('warn') ? 'warn'
                              : cl.contains('ok') ? 'ok' : ''};
            }"""
            hint = lambda: page.eval_on_selector("#set-rp-hint", JS)

            # 1. 初始：留空 → 说明不下发
            h = hint()
            check("不下发" in h["text"], "留空时说明「不下发」")
            check(h["tier"] == "", f"留空是常态色，不抢注意力（实际 {h['tier'] or '无'}）")

            # 2. 逐档输入，看文字与颜色
            cases = [
                ("1.05", "ok", "实测"),
                ("1.1", "ok", "完好"),
                ("1.2", "warn", "字面"),
                ("1.5", "danger", "只有一半"),
            ]
            for value, cls, needle in cases:
                page.fill("#set-repeat-penalty", "")
                page.type("#set-repeat-penalty", value)
                page.wait_for_timeout(120)
                h = hint()
                check(h["tier"] == cls, f"{value} → {cls} 档（实际 {h['tier'] or '无'}）")
                check(needle in h["text"], f"{value} 文案包含「{needle}」")
                if value == "1.5":
                    check("退回" in h["text"], "1.5 给出可执行的退路（退回 1.05–1.1）")

            # 3. 1.0 是「关闭惩罚」，不能算安全档
            page.fill("#set-repeat-penalty", "")
            page.type("#set-repeat-penalty", "1.0")
            page.wait_for_timeout(120)
            h = hint()
            check("关闭" in h["text"], "1.0 说明是关闭惩罚")
            check(h["tier"] == "", f"1.0 不冒充安全档（实际 {h['tier'] or '无'}）")


            # 4. 非法值
            for bad in ("0", "-1"):
                page.fill("#set-repeat-penalty", "")
                page.type("#set-repeat-penalty", bad)
                page.wait_for_timeout(120)
                h = hint()
                check("不会下发" in h["text"] or "不下发" in h["text"],
                      f"非法值 {bad} 只说不会下发")
                check(h["tier"] != "danger", f"非法值 {bad} 不误判成危险档")

            # 5. 自定义模型表单：provider 决定有没有分档提示
            page.click('.set-tab[data-tab="models"]')
            page.click("#btn-new-model")
            page.wait_for_selector("#mf-rp-hint")
            page.select_option("#mf-provider", "openai")
            page.wait_for_timeout(120)
            h = page.eval_on_selector("#mf-rp-hint", JS)
            check("不会下发" in h["text"], "openai 兼容：直接说不会下发")
            check(h["tier"] == "", "openai 兼容不给分档色")

            page.select_option("#mf-provider", "ollama")
            page.fill("#mf-repeat-penalty", "")
            page.type("#mf-repeat-penalty", "1.5")
            page.wait_for_timeout(120)
            h = page.eval_on_selector("#mf-rp-hint", JS)
            check(h["tier"] == "danger", "自定义模型表单里 1.5 同样是危险档")
            check("只有一半" in h["text"], "自定义模型表单里的文案与偏好一致")

            check(not errors, f"整轮无 JS 运行时错误（{errors[:1]}）")
            browser.close()
    finally:
        httpd.shutdown()

    print()
    if _fails:
        print(f"{len(_fails)} 项未通过：")
        for f in _fails:
            print("  -", f)
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
