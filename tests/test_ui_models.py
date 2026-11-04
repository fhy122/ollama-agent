"""模型管理界面回归测试（Playwright，离线，不联网）。

盯住的需求：设置面板要能管两类模型 ——
  1. 内置 Ollama 模型列表（来自 /api/tags；打开设置不自动探测，点「刷新」才拉）；
  2. 自定义模型列表（用户填 /v1 地址 + Key + 模型名，落盘持久化）。
并支持新增 / 修改 / 删除 / 切换。

判据：
  1. 两个标签页存在且能切换，默认停在「模型管理」；
  2. 内置区渲染出本机 Ollama 的模型，且带「当前」标记；
  3. 新建流程：填地址→测连通（失败要给出错因+下一步）→保存→列表出现；
  4. Key 输入框是 password 类型，界面上不出现完整 Key；
  5. 编辑已有条目：表单回填、Key 框留空提示「留空不改」；
  6. 删除：确认后从列表消失；
  7. 偏好页只提交偏好字段，不会把模型配置清掉。

运行：python tests/test_ui_models.py
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
_TMP = Path(tempfile.mkdtemp(prefix="oa-ui-models-"))
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
    return httpd, port


def _open_settings(page):
    page.click("#btn-settings")
    page.wait_for_timeout(700)


def _tabs(page):
    return page.evaluate(
        """() => ({
          tabs: [...document.querySelectorAll('.set-tab')].map(e => e.textContent.trim()),
          active: document.querySelector('.set-tab.active')?.dataset.tab || '',
        })"""
    )


def _models_ui(page):
    return page.evaluate(
        """() => {
          const bi = document.getElementById('builtin-models');
          const cu = document.getElementById('custom-models');
          return {
            builtinRows: bi ? bi.querySelectorAll('.model-row').length : 0,
            builtinNames: bi ? [...bi.querySelectorAll('.m-name')].map(e => e.textContent.trim()) : [],
            builtinActive: bi ? [...bi.querySelectorAll('.model-row.on .m-name')]
                                 .map(e => e.textContent.trim()) : [],
            builtinTags: bi ? [...bi.querySelectorAll('.model-row .m-tag')]
                                 .map(e => e.textContent.trim()) : [],
            customRows: cu ? cu.querySelectorAll('.model-row').length : 0,
            customNames: cu ? [...cu.querySelectorAll('.m-name')].map(e => e.textContent.trim()) : [],
            customActive: cu ? [...cu.querySelectorAll('.model-row.on .m-name')]
                                 .map(e => e.textContent.trim()) : [],
            customEmpty: cu ? (cu.querySelector('.sb-empty')?.textContent || '') : '',
            formOpen: !document.getElementById('model-form').classList.contains('hidden'),
          };
        }"""
    )


def _form_state(page):
    return page.evaluate(
        """() => ({
          title: document.getElementById('model-form-title').textContent.trim(),
          formOpen: !document.getElementById('model-form').classList.contains('hidden'),
          name: document.getElementById('mf-name').value,
          host: document.getElementById('mf-host').value,
          keyType: document.getElementById('mf-key').type,
          keyVal: document.getElementById('mf-key').value,
          keyPh: document.getElementById('mf-key').placeholder,
          model: document.getElementById('mf-model').value,
          note: document.getElementById('mf-note').textContent.trim(),
          delVisible: !document.getElementById('btn-mf-delete').classList.contains('hidden'),
          probe: document.getElementById('mf-probe-result').textContent.trim(),
        })"""
    )


def main():
    ws = Path(tempfile.mkdtemp(prefix="oa-ws-models-"))
    (ws / "a.txt").write_text("x", encoding="utf-8")
    cfg_path = _TMP / "config.json"

    httpd, port = _serve(ws)
    base = f"http://127.0.0.1:{port}/"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 950})
                errors = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(base)
                page.wait_for_timeout(900)
                _open_settings(page)

                # 1. 两个标签页
                t = _tabs(page)
                assert t["tabs"] == ["模型管理", "偏好"], f"标签页不对: {t}"
                assert t["active"] == "models", f"默认应停在模型管理: {t}"

                # 2. 打开设置不自动探测（2026-11-04 卡顿修复）：首次见「未加载」提示，
                #    点「刷新」才真正拉 /api/tags
                note0 = page.locator("#builtin-note").inner_text()
                assert "尚未加载" in note0, f"首次打开应显示未加载提示（零探测）: {note0}"
                page.click("#btn-refresh-builtin")
                page.wait_for_timeout(2500)   # 真 Ollama 探测，本机通常亚秒完成
                m = _models_ui(page)
                assert m["builtinRows"] > 0, f"点刷新后内置 Ollama 应列出模型: {m}"
                assert m["builtinActive"], f"应标出当前在用的内置模型: {m}"
                assert "使用中" in m["builtinTags"], f"在用项要有「使用中」标记: {m}"

                # 3. 新建：先测一个必然连不上的地址 —— 错误要指路
                page.click("#btn-new-model")
                page.wait_for_timeout(300)
                f = _form_state(page)
                assert f["formOpen"], "点「新建」应展开表单"
                assert "新建" in f["title"], f
                assert f["keyType"] == "password", f"Key 输入框应是 password: {f}"

                # 表单比抽屉高：展开后必须把「保存」带进视野，否则用户点了新建
                # 却像没反应（实测过：保存按钮压在底部固定操作栏下面看不见）
                vis = page.evaluate(
                    """() => {
                      const save = document.getElementById('btn-mf-save');
                      const act = document.querySelector('.drawer-actions');
                      const sr = save.getBoundingClientRect();
                      const ar = act.getBoundingClientRect();
                      return {visible: sr.top >= 0 && sr.bottom <= window.innerHeight,
                              covered: sr.bottom > ar.top && sr.top < ar.bottom,
                              top: Math.round(sr.top)};
                    }""")
                assert vis["visible"], f"展开表单后保存按钮应在视口内: {vis}"
                assert not vis["covered"], f"保存按钮不能被底部操作栏盖住: {vis}"

                page.fill("#mf-name", "中转站测试")
                page.select_option("#mf-provider", "openai")
                page.fill("#mf-host", "https://127.0.0.1:9/v1")
                page.fill("#mf-key", "sk-ui-test-1234567890")
                page.click("#btn-mf-probe")
                page.wait_for_timeout(4000)
                pr = page.evaluate(
                    "() => ({txt: document.getElementById('mf-probe-result').textContent.trim(),"
                    " cls: document.getElementById('mf-probe-result').className})")
                assert "bad" in pr["cls"], f"坏地址应标成失败样式: {pr}"
                assert "连不上" in pr["txt"], f"要说清是连不上: {pr}"
                assert "本机" in pr["txt"], f"要指出是本机地址: {pr}"
                assert len(pr["txt"]) > 20, f"错误要带下一步动作: {pr}"
                print(f"  ok  测连通失败文案 = {pr['txt'][:70]}")

                # 4. 换成可用的（用本地 Ollama 的 OpenAI 兼容端点不存在，所以这里
                #    直接保存 —— 保存不该被探测结果拦住，允许「先存着回头再修」）
                page.fill("#mf-host", "https://api.example-vendor.test/v1")
                page.fill("#mf-model", "vendor-model-x")
                # num_ctx 在 OpenAI 兼容协议下是禁用的（属于 Ollama 原生参数，
                # 发过去也会被丢弃）—— 这里顺带断言这个「不适用」是真的禁掉并给了理由
                ctx_state = page.evaluate(
                    "() => { const e = document.getElementById('mf-ctx');"
                    " return {disabled: e.disabled, title: e.title}; }")
                assert ctx_state["disabled"], \
                    f"OpenAI 协议下 num_ctx 应禁用: {ctx_state}"
                assert "Ollama" in ctx_state["title"], \
                    f"禁用要说明为什么: {ctx_state}"
                page.click("#btn-mf-save")
                page.wait_for_timeout(1200)

                m = _models_ui(page)
                assert m["customRows"] == 1, f"保存后自定义列表应有 1 条: {m}"
                assert any("中转站测试" in n for n in m["customNames"]), m
                assert not m["formOpen"], "保存后表单应收起"

                # 5. 落盘核对：真写进配置文件，且 Key 是完整的
                disk = json.loads(cfg_path.read_text(encoding="utf-8"))
                row = (disk.get("profiles") or [])[0]
                assert row["host"] == "https://api.example-vendor.test/v1", row
                assert row["api_key"] == "sk-ui-test-1234567890", "Key 应以明文落盘"
                assert row["model"] == "vendor-model-x", row
                assert disk.get("active") == row["id"], "新建后应自动切到它"
                print(f"  ok  落盘 ok={row['name']} model={row['model']}")

                # 5.5 选中互斥 + 切换反馈。曾出过的事故：内置区和自定义区
                # 同时亮「使用中」（两区判据各看各的字段，/api/config 换协议
                # 从不更新 active）——这里把「全面板至多一处高亮」钉死。
                m = _models_ui(page)
                assert len(m["customActive"]) == 1, \
                    f"自定义生效时应有且仅有一处高亮: {m}"
                assert m["builtinActive"] == [], \
                    f"自定义生效时内置区不允许有高亮: {m}"
                idle_tags = [t for t, n in zip(m["builtinTags"], m["builtinNames"])
                             if n not in m["builtinActive"]]
                assert idle_tags and all("切换" in t for t in idle_tags), \
                    f"非选中内置行应有「切换」行动提示: {m}"

                first_builtin = page.locator("#builtin-models .model-row").first
                first_builtin.click()
                page.wait_for_timeout(250)
                sw = page.evaluate(
                    """() => {
                      const r = document.querySelector(
                        '#builtin-models .model-row.switching .m-tag');
                      return r ? r.textContent.trim() : '';
                    }""")
                assert "切换中" in sw, f"点击后应出现「切换中…」过渡态: {sw!r}"
                # 切换 = 重建后端 + 本地模型加载，可能要几秒，轮询等高亮迁移
                deadline = time.time() + 60
                while time.time() < deadline:
                    m = _models_ui(page)
                    if m["builtinActive"] and not m["customActive"]:
                        break
                    page.wait_for_timeout(500)
                else:
                    raise AssertionError(f"切换后高亮应迁回内置且仅一处: {m}")
                print(f"  ok  互斥 + 切换反馈（高亮迁到内置 {m['builtinActive'][0]}）")

                # 6. 界面上不能出现完整 Key
                blob = page.inner_text("#settings")
                assert "sk-ui-test-1234567890" not in blob, "界面上不该出现完整 Key"
                assert "…" in blob or "****" in blob, "应收显打码后的 Key"

                # 7. 编辑：点这一条的编辑按钮，表单要回填，Key 框留空 + 提示
                page.click("#custom-models .m-edit")
                page.wait_for_timeout(500)
                f = _form_state(page)
                assert f["formOpen"], "点编辑应展开表单"
                assert "编辑" in f["title"], f
                assert f["name"] == "中转站测试", f
                assert f["host"] == "https://api.example-vendor.test/v1", f
                assert f["model"] == "vendor-model-x", f
                assert f["keyVal"] == "", f"编辑时 Key 框应留空: {f}"
                assert f["delVisible"], "编辑已有条目时应显示删除按钮"

                page.fill("#mf-name", "改名后的中转站")
                page.click("#btn-mf-save")
                page.wait_for_timeout(1000)
                disk = json.loads(cfg_path.read_text(encoding="utf-8"))
                assert disk["profiles"][0]["name"] == "改名后的中转站", disk["profiles"][0]
                assert disk["profiles"][0]["api_key"] == "sk-ui-test-1234567890", \
                    "留空 Key 保存后原 Key 必须保留"
                m = _models_ui(page)
                assert any("改名后的中转站" in n for n in m["customNames"]), m
                print("  ok  编辑：改名生效 + 空 Key 保留原值")

                # 8. 偏好页：切过去、改步数、保存，模型配置不能被清掉
                page.click('.set-tab[data-tab="prefs"]')
                page.wait_for_timeout(300)
                assert _tabs(page)["active"] == "prefs", "应切到偏好页"
                page.fill("#set-steps", "9")
                page.click("#btn-apply")
                page.wait_for_timeout(900)
                disk = json.loads(cfg_path.read_text(encoding="utf-8"))
                assert disk["ui"]["max_steps"] == 9, disk["ui"]
                assert len(disk.get("profiles") or []) == 1, "改偏好不该删掉模型配置"
                print("  ok  偏好页只改偏好，模型配置仍在")

                # 9. 切回模型页，删除
                page.click('.set-tab[data-tab="models"]')
                page.wait_for_timeout(400)
                page.click("#custom-models .m-edit")
                page.wait_for_timeout(400)
                page.click("#btn-mf-delete")
                page.wait_for_timeout(500)
                # 确认弹窗是独立的 #yesno-modal，别点到工具确认的「允许」上
                assert page.evaluate(
                    "() => !document.getElementById('yesno-modal')"
                    ".classList.contains('hidden')"), "删除前应有确认弹窗"
                page.click("#yn-ok")
                page.wait_for_timeout(1400)
                disk = json.loads(cfg_path.read_text(encoding="utf-8"))
                assert disk["profiles"] == [], f"删除后配置文件里不该还有: {disk}"
                assert disk["active"] == "ollama-local", "删生效项应回落内置"
                m = _models_ui(page)
                assert m["customRows"] == 0, f"列表里应已消失: {m}"
                print("  ok  删除：配置清空 + active 回落内置")

                assert not errors, f"不应有 JS 报错: {errors}"
                print("PASS: 标签页 + 内置列表 + 新建 + 测连通 + 编辑 + 偏好隔离 + 删除")
                page.close()
            finally:
                browser.close()
    finally:
        httpd.shutdown()
    print("PASS: 模型管理界面 全过")


if __name__ == "__main__":
    main()
