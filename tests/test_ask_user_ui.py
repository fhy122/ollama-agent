"""ask_user 弹窗的 UI 冒烟（Playwright，离线、不联网、不调模型）。

运行：python tests/test_ask_user_ui.py

为什么可以直接喂事件：前端处理 confirm 的入口是全局 askConfirm(ev)，
后端只负责把 {type:"confirm", id, question, options, free_text} 推进来。
这里绕过模型直接喂事件，验的是「事件 → 弹窗 → 点选 → 提交格式」这条链路，
不靠模型是否恰好愿意调工具（那会把测试变成概率游戏）。

判据：
  1. kind=choice：标题变「需要你定一下」、问题用正文字体（不是代码框）、
     选项渲染成 N 个按钮、布尔三按钮隐藏、「跳过不答」出现；
  2. 点某个选项 → POST /api/confirm 的 text 正是该选项原文，approved=true；
  3. 自由输入回车 → text 是输入内容；空输入提交 → text 是空串（= 没答复）；
  4. 布尔确认（无 kind）：三按钮显示、选项区隐藏、POST 里**没有** text 字段
     —— 有的话后端会把它当成提问的回答；
  5. 整轮无 JS 运行时错误。
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
_TMP = Path(tempfile.mkdtemp(prefix="oa-ui-ask-"))
os.environ["OLLAMA_AGENT_SESSIONS"] = str(_TMP / "sessions")
os.environ["OLLAMA_AGENT_CONFIG"] = str(_TMP / "config.json")

from playwright.sync_api import sync_playwright  # noqa: E402

from agent.config import AgentConfig  # noqa: E402
from webui.server import WebUIApp, make_handler  # noqa: E402

FAILS: list[str] = []


def check(cond: bool, msg: str) -> None:
    if cond:
        print(f"  ok  {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg}")


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


CHOICE = {
    "type": "confirm",
    "id": "c1",
    "question": "缓存放哪里？",
    "kind": "choice",
    "options": ["A 磁盘缓存：重启不丢，但多写一个文件",
                "B 内存缓存：简单，重启清空"],
    "free_text": True,
}


def main():
    workspace = _TMP / "ws"
    workspace.mkdir(exist_ok=True)
    httpd, port = _serve(workspace)
    sent: list[dict] = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            errs: list[str] = []
            page.on("pageerror", lambda e: errs.append(str(e)))

            # 拦下提交：既拿到 body，也不让后端因 id 不存在而报错
            def on_confirm(route):
                sent.append(route.request.post_data_json or {})
                route.fulfill(status=200, content_type="application/json",
                              body='{"ok": true}')
            page.route("**/api/confirm", on_confirm)

            page.goto(f"http://127.0.0.1:{port}/")
            page.wait_for_selector("#confirm-modal", state="attached")

            # ---- 1. choice 弹窗结构 ----
            page.evaluate("ev => askConfirm(ev)", CHOICE)
            page.wait_for_selector("#confirm-modal:not(.hidden)")
            check(page.inner_text("#confirm-title") == "需要你定一下",
                  "标题切成「需要你定一下」")
            check("prose" in (page.get_attribute("#confirm-q", "class") or ""),
                  "问题用正文字体（不是代码框）")
            check(page.locator(".confirm-opt").count() == 2, "两个选项渲染成按钮")
            check(page.locator("#confirm-actions").is_hidden(), "布尔三按钮隐藏")
            check(page.locator("#confirm-choice-actions").is_visible(), "「跳过不答」出现")
            check(page.locator("#confirm-free").is_visible(), "自由输入框出现")
            print("  ok  choice 弹窗结构齐全")

            # ---- 2. 点选项 → text 是该选项原文 ----
            page.click('.confirm-opt[data-i="1"]')
            page.wait_for_selector("#confirm-modal", state="hidden")
            body = sent[-1]
            check(body.get("text") == CHOICE["options"][1],
                  f"点选项提交的是原文：{body.get('text')!r}")
            check(body.get("id") == "c1" and body.get("approved") is True,
                  f"id / approved 正确：{body}")

            # ---- 3. 自由输入 ----
            page.evaluate("ev => askConfirm(ev)", CHOICE)
            page.wait_for_selector("#confirm-modal:not(.hidden)")
            page.fill("#confirm-text", "C 我自己写：放 Redis")
            page.press("#confirm-text", "Enter")
            page.wait_for_selector("#confirm-modal", state="hidden")
            check(sent[-1].get("text") == "C 我自己写：放 Redis",
                  f"回车提交输入内容：{sent[-1].get('text')!r}")

            page.evaluate("ev => askConfirm(ev)", CHOICE)
            page.wait_for_selector("#confirm-modal:not(.hidden)")
            page.click("#btn-confirm-skip")
            page.wait_for_selector("#confirm-modal", state="hidden")
            check(sent[-1].get("text") == "", "跳过 = 空答复（不是拒绝）")

            # ---- 4. 布尔确认不受影响 ----
            page.evaluate("ev => askConfirm(ev)",
                          {"type": "confirm", "id": "c2", "question": "write_file(x)"})
            page.wait_for_selector("#confirm-modal:not(.hidden)")
            check(page.inner_text("#confirm-title") == "确认执行", "布尔确认标题不变")
            check(page.locator(".confirm-opt").count() == 0, "布尔确认没有选项按钮")
            check(page.locator("#confirm-actions").is_visible(), "布尔三按钮显示")
            page.click("#btn-confirm-yes")
            page.wait_for_selector("#confirm-modal", state="hidden")
            check("text" not in sent[-1],
                  f"布尔确认不带 text 字段：{sent[-1]}")

            # ---- 5. 无运行时错误 ----
            check(not errs, f"无 JS 运行时错误：{errs}")
            browser.close()
    finally:
        httpd.shutdown()

    if FAILS:
        print(f"\nFAIL: {len(FAILS)} 条未通过")
        raise SystemExit(1)
    print("\nPASS: ask_user 弹窗（结构 / 选项 / 自由输入 / 跳过 / 布尔确认回归）")


if __name__ == "__main__":
    main()
