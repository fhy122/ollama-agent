"""num_ctx 预设档位回归测试（Playwright，离线、不联网）。

盯住的需求：手填 65536 这种数字太费劲 —— 设置面板和模型表单都改成
4K / 8K / … 下拉，但下面这些旧能力一个都不能丢：
  * 任意整数（自定义入口）；
  * 配置里已有的非预设值不能被下拉悄悄吞掉；
  * OpenAI 兼容协议下控件要禁用并说清原因。

判据（正常 / 错误 / 边界）：
  1. 两个控件都是 select，档位齐、K 值标了精确 token 数；
  2. 选 8K 应用后，后端 AgentConfig.num_ctx 真的是 8192（不是只改了界面）；
  3. 自定义 12288 生效；自定义留空时给出带正解的提示，且不改配置；
  4. 非预设值回填为「当前值」项，刷新页面后仍在；
  5. 选服务端默认 → num_ctx=None；
  6. 模型表单在 OpenAI 协议下禁用并显示「不适用」，切回 Ollama 恢复档位；
  7. 模型表单选 8K 保存后，落盘的是精确整数 8192，而不是 "8K"。

运行：python tests/test_ctx_presets.py
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = Path(tempfile.mkdtemp(prefix="oa-ui-ctx-"))
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


def _ctx_ui(page, which="set"):
    return page.evaluate(
        """(which) => {
          const s = document.getElementById(which + '-ctx');
          const c = document.getElementById(which + '-ctx-custom');
          return {
            tag: s.tagName,
            value: s.value,
            selected: s.selectedOptions[0] ? s.selectedOptions[0].textContent.trim() : '',
            options: [...s.options].map(o => ({v: o.value, t: o.textContent.trim(),
                                               extra: o.dataset.extra === '1'})),
            disabled: s.disabled,
            title: s.title,
            customVisible: !c.classList.contains('hidden'),
          };
        }""", which)


def _open_settings(page):
    page.click("#btn-settings")
    page.wait_for_timeout(700)


def main():
    ws = Path(tempfile.mkdtemp(prefix="oa-ws-ctx-"))
    httpd, port, app = _serve(ws)
    base = f"http://127.0.0.1:{port}/"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 950})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(base)
                page.wait_for_timeout(700)
                _open_settings(page)
                page.click('.set-tab[data-tab="prefs"]')

                # 1. 控件类型 + 档位
                ui = _ctx_ui(page)
                assert ui["tag"] == "SELECT", f"num_ctx 应是下拉: {ui}"
                vals = [o["v"] for o in ui["options"]]
                for v in ("0", "4096", "8192", "16384", "32768", "65536",
                          "131072", "custom"):
                    assert v in vals, f"缺档位 {v}: {vals}"
                texts = " ".join(o["t"] for o in ui["options"])
                assert "4K" in texts and "8K" in texts, texts
                assert "4,096" in texts and "服务端默认" in texts, \
                    f"K 要标精确 token 数、要有服务端默认: {texts}"
                print("  ok  档位齐全：服务端默认 / 4K / 8K / 16K / 32K / 64K / 128K / 自定义")

                # 2. 初始回填 64K，顶栏不再裸露 65536
                assert ui["value"] == "65536", ui
                chip = page.inner_text("#chip-ctx").strip()
                assert chip == "ctx 64K", f"顶栏应显示 64K: {chip}"
                print(f"  ok  回填 64K，顶栏 = {chip}")

                # 3. 正常路径：选 8K 真的写进后端配置
                page.select_option("#set-ctx", "8192")
                page.click("#btn-apply")
                page.wait_for_timeout(600)
                assert app.session.cfg.num_ctx == 8192, \
                    f"后端 num_ctx 应为 8192，实际 {app.session.cfg.num_ctx!r}"
                assert page.inner_text("#chip-ctx").strip() == "ctx 8K"
                print("  ok  选 8K → 后端 num_ctx=8192")

                # 4. 自定义任意整数仍可用
                page.select_option("#set-ctx", "custom")
                assert _ctx_ui(page)["customVisible"], "选「自定义」要展开输入框"
                page.fill("#set-ctx-custom", "12288")
                page.click("#btn-apply")
                page.wait_for_timeout(600)
                assert app.session.cfg.num_ctx == 12288, \
                    f"后端 num_ctx 应为 12288，实际 {app.session.cfg.num_ctx!r}"
                print("  ok  自定义 12288 → 后端 num_ctx=12288")

                # 5. 错误路径：自定义留空要有正解，且不能把配置改坏
                page.select_option("#set-ctx", "custom")
                page.fill("#set-ctx-custom", "")
                page.evaluate("() => { document.getElementById('toasts').innerHTML = ''; }")
                page.click("#btn-apply")
                page.wait_for_timeout(400)
                hint = page.locator("#toasts").inner_text()
                assert "12288" in hint and "预设" in hint, f"提示要带正解: {hint}"
                assert "已应用" not in hint, f"非法输入不该提交偏好: {hint}"
                assert app.session.cfg.num_ctx == 12288, "非法输入不能动已有配置"
                print(f"  ok  非法自定义有指路提示: {hint.strip()[:40]}")

                # 6. 边界：非预设值回填，刷新后不丢
                page.reload()
                page.wait_for_timeout(700)
                _open_settings(page)
                ui = _ctx_ui(page)
                assert ui["value"] == "12288", f"非预设值应原样选中: {ui}"
                assert any(o["extra"] and "12K" in o["t"] for o in ui["options"]), ui
                assert "12,288" in ui["selected"], ui
                print(f"  ok  非预设值回填 = {ui['selected']}")

                # 7. 边界：服务端默认
                page.click('.set-tab[data-tab="prefs"]')
                page.select_option("#set-ctx", "0")
                page.click("#btn-apply")
                page.wait_for_timeout(600)
                assert app.session.cfg.num_ctx is None, \
                    f"服务端默认应是不传 num_ctx，实际 {app.session.cfg.num_ctx!r}"
                print("  ok  服务端默认 → 后端 num_ctx=None")

                # 8. 模型表单：OpenAI 协议禁用并说明，切回 Ollama 恢复档位
                page.click('.set-tab[data-tab="models"]')
                page.click("#btn-new-model")
                page.wait_for_timeout(300)
                m = _ctx_ui(page, "mf")
                assert m["tag"] == "SELECT" and m["disabled"], m
                assert m["selected"] == "不适用", f"禁用态应显示「不适用」: {m}"
                assert "Ollama" in m["title"], f"禁用要说明为什么: {m}"
                page.select_option("#mf-provider", "ollama")
                m = _ctx_ui(page, "mf")
                assert not m["disabled"], m
                assert "8192" in [o["v"] for o in m["options"]], m
                print("  ok  模型表单：禁用态「不适用」，切回 Ollama 恢复档位")

                # 9. 模型表单正常路径：选 8K 落盘的是精确 8192，不是 "8K"
                page.fill("#mf-name", "本地测试")
                page.fill("#mf-host", "http://localhost:11434")
                page.fill("#mf-model", "qwen3.5:9b")
                page.select_option("#mf-ctx", "8192")
                page.click("#btn-mf-save")
                page.wait_for_timeout(900)
                disk = json.loads((_TMP / "config.json").read_text(encoding="utf-8"))
                row = (disk.get("profiles") or [])[0]
                assert row["num_ctx"] == 8192, f"模型配置落盘值不对: {row}"
                print("  ok  模型表单选 8K → 落盘 num_ctx=8192")

                assert not errors, f"页面 JS 报错: {errors}"
                print("PASS")
            finally:
                browser.close()
    finally:
        httpd.shutdown()


if __name__ == "__main__":
    main()
