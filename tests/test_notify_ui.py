"""桌面通知的 UI 冒烟（Playwright，离线、不调模型）。

运行：python tests/test_notify_ui.py

为什么用桩：自动化里没法真的断言「Windows 右下角弹了个 toast」。这里把
window.Notification 换成记录参数的假类，验的是**我们自己的守卫与内容拼装**：
什么时候该弹、弹什么字、点下去干什么。真机上这一层由浏览器负责，
不用重复测。

判据（写功能的同时写「怎么才算对」）：
  1. 默认关：一条都不弹（安静是默认值，不能装完就弹）；
  2. 开着 + 窗口最小化（hidden）或失焦（hasFocus=false）→ 弹一条，
     标题带会话名，正文是剥掉 markdown 的摘要；
  3. 开着但正在看这个页面 → 不弹（页面内已有 ✦ 徽标，再弹是噪音）；
  4. 免打扰开着 → 不弹；
  5. 同一轮重复触发只弹一次；后台会话的 attention 只在 false→true
     那一下弹，后续轮询不再弹（否则 2.5s 一条，能弹到天荒地老）；
  6. 点通知 → 切到那条会话（发出 /api/sessions/open）；
  7. 两个标签页同时开着 → 同一条只弹一次（跨页去重）；
  8. 权限被拒时开关不会停在「开」，并说清怎么恢复；
  9. 整轮无 JS 运行时错误。
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
_TMP = Path(tempfile.mkdtemp(prefix="oa-ui-notify-"))
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


# 假 Notification：记下构造参数，保留 onclick 供测试手动触发。
# hidden / hasFocus 拆成两个开关，才能分别模拟「最小化」与「只是失焦」。
STUB = r"""
(() => {
  const notes = [];
  class FakeNotification {
    constructor(title, opts) {
      const o = opts || {};
      this.title = title || "";
      this.body = o.body || "";
      this.tag = o.tag || "";
      this.onclick = null;
      this.closed = false;
      notes.push(this);
      window.__notes = notes;
    }
    close() { this.closed = true; }
    static requestPermission() { return Promise.resolve(FakeNotification.permission); }
  }
  FakeNotification.permission = "granted";
  window.Notification = FakeNotification;
  window.__hidden = false;      // 页面是否被隐藏（最小化 / 切标签页）
  window.__focused = true;      // 窗口是否有焦点（切到别的程序 = false）
  Object.defineProperty(document, "hidden", {
    get: () => window.__hidden, configurable: true,
  });
  Object.defineProperty(document, "visibilityState", {
    get: () => (window.__hidden ? "hidden" : "visible"), configurable: true,
  });
  document.hasFocus = () => window.__focused;
})();
"""


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


def _notes(page) -> list[dict]:
    return page.evaluate(
        "() => (window.__notes || []).map(n => ({t: n.title, b: n.body, tag: n.tag}))")


def _clear(page) -> None:
    page.evaluate("() => { if (window.__notes) window.__notes.length = 0; }")


def _away(page, hidden: bool = True, focused: bool = False) -> None:
    """把窗口摆成「没人在看」：最小化 = hidden+失焦，切走窗口 = 只失焦。"""
    page.evaluate("([h, f]) => { window.__hidden = h; window.__focused = f; }",
                  [hidden, focused])


def _on(page) -> None:
    page.evaluate("() => localStorage.setItem('oa.notify', '1')")


def _off(page) -> None:
    page.evaluate("() => localStorage.removeItem('oa.notify')")


def main() -> None:
    httpd, port = _serve(_TMP)
    url = f"http://127.0.0.1:{port}/"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context()
            ctx.add_init_script(STUB)
            page = ctx.new_page()
            js_errors: list[str] = []
            page.on("pageerror", lambda e: js_errors.append(str(e)))
            page.goto(url)
            page.wait_for_selector("#quick-model")

            # ---- 1. 默认关 ----
            _away(page)
            page.evaluate("""() => notifyTurnDone({
                type: 'done', text: '**你好**',
                session: {id: 's1', title: '测试对话'}})""")
            check(len(_notes(page)) == 0, "默认关：一条都不弹")

            # ---- 2. 开着 + 没在看（最小化 / 只失焦 都要弹）----
            _on(page)
            _away(page, hidden=True, focused=False)
            page.evaluate("""() => notifyTurnDone({
                type: 'done', text: '**完成**了，改动见 `a.py`',
                session: {id: 's1', title: '测试对话'}})""")
            n = _notes(page)
            check(len(n) == 1, "最小化时弹一条")
            check(len(n) == 1 and "测试对话" in n[0]["t"], "标题带会话名")
            check(len(n) == 1 and "**" not in n[0]["b"] and "`" not in n[0]["b"],
                  "正文剥掉了 markdown 标记")
            check(len(n) == 1 and "完成" in n[0]["b"], "正文是回复摘要")
            check(len(n) == 1 and n[0]["tag"] == "oa-s1", "tag 按会话归并，不堆一列")

            _clear(page)
            _away(page, hidden=False, focused=False)   # 窗口还在，但焦点在别的程序
            page.evaluate("""() => notifyTurnDone({
                type: 'done', text: '失焦也算',
                session: {id: 's1b', title: '失焦对话'}})""")
            check(len(_notes(page)) == 1, "只是失焦（没最小化）也弹")

            # ---- 3. 正在看 → 不弹 ----
            _clear(page)
            _away(page, hidden=False, focused=True)
            page.evaluate("""() => notifyTurnDone({
                type: 'done', text: '你正看着呢',
                session: {id: 's2', title: '当前对话'}})""")
            check(len(_notes(page)) == 0, "页面可见且有焦点：不弹")

            # ---- 4. 免打扰 ----
            _clear(page)
            _away(page)
            page.evaluate("() => localStorage.setItem('oa.notify.dnd', '1')")
            page.evaluate("""() => notifyTurnDone({
                type: 'done', text: '免打扰期间',
                session: {id: 's3', title: '免打扰'}})""")
            check(len(_notes(page)) == 0, "免打扰开着：不弹")
            page.evaluate("() => localStorage.removeItem('oa.notify.dnd')")

            # ---- 5a. 同一轮重复触发只弹一次 ----
            _clear(page)
            page.evaluate("""() => {
                state.turnSeq = 7;
                const ev = {type: 'done', text: '同一轮',
                            session: {id: 's4', title: '去重'}};
                notifyTurnDone(ev);
                notifyTurnDone(ev);
            }""")
            check(len(_notes(page)) == 1, "同一轮触发两次只弹一条")

            # ---- 5b. attention 只在 false→true 那一下弹 ----
            _clear(page)
            page.evaluate("""() => {
                state.statuses = {};
                const st = {s9: {busy: false, attention: true, rev: 3,
                                 title: '后台对话', preview: '做好了'}};
                notifyAttention(st);
                state.statuses = st;      // 下一轮轮询：attention 仍为 true
                notifyAttention(st);
                notifyAttention(st);
            }""")
            n = _notes(page)
            check(len(n) == 1, "attention 只在翻转时弹一次，后续轮询不再弹")
            check(len(n) == 1 and "后台对话" in n[0]["t"] and "做好了" in n[0]["b"],
                  "后台完成通知带会话名与摘要")

            # ---- 6. 点通知切会话 ----
            reqs: list[str] = []
            page.on("request",
                    lambda r: reqs.append(r.url) if "/api/sessions/open" in r.url else None)
            page.evaluate("""() => {
                state.sid = 'other-sid';
                const last = window.__notes[window.__notes.length - 1];
                last.onclick();
            }""")
            page.wait_for_timeout(400)
            check(any("/api/sessions/open" in u for u in reqs),
                  "点通知 → 请求切到那条会话")

            # ---- 7. 跨标签页去重 ----
            page2 = ctx.new_page()
            page2.goto(url)
            page2.wait_for_selector("#quick-model")
            _clear(page)
            _clear(page2)
            _away(page)
            _away(page2)
            ev = """() => notifyAttention({sX: {busy: false, attention: true,
                        rev: 1, title: '双开', preview: '同一条'}})"""
            page.evaluate(ev)
            page2.evaluate(ev)
            total = len(_notes(page)) + len(_notes(page2))
            check(total == 1, "两个标签页同时开着：同一条只弹一次")
            page2.close()

            # ---- 8. 权限被拒时开关回弹 ----
            # 抽屉默认关着，select 不可见就点不了；先开抽屉切到「偏好」
            page.click("#btn-settings")
            page.click('.set-tab[data-tab="prefs"]')
            page.evaluate("""() => {
                Notification.permission = 'denied';
                localStorage.removeItem('oa.notify');
                renderNotifyPrefs();
            }""")
            page.select_option("#set-notify", "on")
            page.wait_for_timeout(300)
            check(page.eval_on_selector("#set-notify", "el => el.value") == "off",
                  "权限被拒：开关不会停在「开」")
            note_text = page.eval_on_selector("#notify-note", "el => el.textContent")
            check("站点图标" in note_text or "拒绝" in note_text,
                  "提示怎么恢复权限")
            check(page.evaluate("() => localStorage.getItem('oa.notify')") is None,
                  "拒绝后没留下误导性的开关状态")

            # ---- 9. 无 JS 错误 ----
            check(not js_errors, f"无 JS 运行时错误（{js_errors[:2]}）")
            browser.close()
    finally:
        httpd.shutdown()

    if FAILS:
        print(f"\n{len(FAILS)} 项未通过：")
        for f in FAILS:
            print("  -", f)
        raise SystemExit(1)
    print("\n全部通过")


if __name__ == "__main__":
    main()
