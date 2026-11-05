"""「长工具调用期间有东西可看」的界面回归测试（Playwright，离线）。

盯住老板踩过的坑：一段对话说完「好的！现在更新数据模型并初始化数据」，然后
画面完全静止好几分钟，接着 write_file 卡片突然冒出来。查下来不是卡死 ——
Ollama 把 tool_calls 攒成整块才发，参数生成那几十上百秒里流上一个字节都没有。

约定：
  * 后端按秒推 {"type":"thinking","elapsed":..,"chars":..}；
  * 前端渲染成一枚「正在生成（已等 Ns，已生成 M 字符）」的提示条，
    **就地更新**而不是每次新加一行（否则 200 秒能堆几百个元素）；
  * 任何真实事件（正文/工具/统计/步骤）一到，提示条立刻收掉，
    不能一直挂在回合末尾让人以为还在等；
  * stats 要标出「第 N 步」，否则多步回合里几个 token 行长得一模一样。

运行：python tests/test_ui_thinking.py
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


def _snap(page):
    return page.evaluate(
        """() => {
          const waits = [...document.querySelectorAll('#messages .gen-wait')];
          return {
            n: waits.length,
            txt: waits.length ? waits[waits.length - 1].innerText : '',
            metas: [...document.querySelectorAll('#messages .meta')].map(e => e.innerText),
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
                page = browser.new_page(viewport={"width": 1320, "height": 900})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(base)
                page.wait_for_timeout(500)

                # 造一个回合，然后手工喂事件（等价于流上真的收到了）
                page.evaluate("window.__t = newAssistantTurn()")
                page.evaluate("""() => {
                  applyEvent(window.__t, {type:'step', n:1});
                  applyEvent(window.__t, {type:'text', text:'好的！烟台五区确认为…现在更新数据模型并初始化数据'});
                }""")

                # 1) 心跳出现，且文案带秒数
                page.evaluate("applyEvent(window.__t, {type:'thinking', elapsed:7, chars:0})")
                st = _snap(page)
                assert st["n"] == 1, f"应出现一枚等待提示条: {st}"
                assert "7" in st["txt"] and "已等" in st["txt"], f"文案应带已等秒数: {st['txt']!r}"

                # 2) 再来心跳：**就地更新**，元素数不涨
                for i in range(20):
                    page.evaluate(
                        "applyEvent(window.__t, {type:'thinking', elapsed:%d, chars:%d})"
                        % (8 + i, 1200 + i * 300))
                st = _snap(page)
                assert st["n"] == 1, f"心跳必须就地更新，不能每来一条加一个元素: {st}"
                assert "27" in st["txt"], f"应更新到最新秒数: {st['txt']!r}"
                assert "6900" in st["txt"], f"应显示已生成字符数: {st['txt']!r}"

                # 3) 真实事件一到，提示条收掉
                page.evaluate("applyEvent(window.__t, {type:'tool_call', name:'write_file', args:{path:'a.py'}})")
                st = _snap(page)
                assert st["n"] == 0, f"工具卡片出现后等待条该收掉: {st}"

                # 4) 步骤之间也要收（tool_result → 下一步生成 → 心跳 → 正文）
                page.evaluate("applyEvent(window.__t, {type:'thinking', elapsed:2, chars:0})")
                assert _snap(page)["n"] == 1, "工具执行后又该出现等待条"
                page.evaluate("applyEvent(window.__t, {type:'step', n:2})")
                assert _snap(page)["n"] == 0, "进入下一步时应收掉上一轮的等待条"

                # 5) stats 标「第 N 步」
                page.evaluate("""() => {
                  applyEvent(window.__t, {type:'stats', step:2, prompt_tokens:13652,
                                          gen_tokens:900, duration:95});
                }""")
                st = _snap(page)
                assert st["metas"], f"应出现统计行: {st}"
                last = st["metas"][-1]
                assert "第 2 步" in last, f"多步回合的统计要标第几步: {last!r}"
                assert "95" in last and "13652" in last, last

                # 5b) 累计 usage 行：本回合增量 + 会话累计 + 缺失标注
                page.evaluate("""() => {
                  applyEvent(window.__t, {type:'usage',
                    turn:{requests:3, prompt_tokens:2000, gen_tokens:200,
                          total_tokens:2200, missing_usage:1, by_source:{}},
                    total:{requests:9, prompt_tokens:9000, gen_tokens:900,
                           total_tokens:9900}});
                }""")
                st = _snap(page)
                last = st["metas"][-1]
                assert "本回合累计 3 次调用" in last, last
                assert "2200" in last and "9900" in last, last
                assert "1 次未返回 usage" in last, last

                # 6) 回合结束：有残留等待条也必须清掉
                page.evaluate("applyEvent(window.__t, {type:'thinking', elapsed:3, chars:0})")
                page.evaluate("window.__t.finalize()")
                st = _snap(page)
                assert st["n"] == 0, f"回合结束后不该还挂着等待条: {st}"

                assert not errors, errors
                print("PASS: 心跳提示条 + stats 标第 N 步 + 累计 usage 行 + 收尾清理")
            finally:
                browser.close()
    finally:
        httpd.shutdown()
    print("PASS: 长工具调用可见性全过")


if __name__ == "__main__":
    main()
