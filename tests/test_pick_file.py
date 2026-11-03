"""自测：本机文件选择框接口（不弹真窗口）。

背景：用户痛点是「记不住路径、也不想复制粘贴」——手动输入行只配当
兜底，主通道是后端 comdlg32 弹系统原生选择框。这里 mock 掉
webui.server._native_pick（真弹窗没法自动化），锁住 handler 的四条契约：
正常返回、空选（取消）、并发 409、弹窗异常 500。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("OLLAMA_AGENT_SESSIONS", tempfile.mkdtemp(prefix="oa-pick-"))

from agent.config import AgentConfig  # noqa: E402
import webui.server as srv  # noqa: E402


def _post(url, obj, timeout=10):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def main():
    import http.server

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()

    app = srv.WebUIApp(AgentConfig(use_color=False, warm=False), port=port)
    app.start_hotkey = lambda: False
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                            srv.make_handler(app))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    fake_paths = []
    calls = []

    def fake_pick(initialdir):
        calls.append(initialdir)
        if getattr(fake_pick, "raise_error", False):
            raise RuntimeError("no dialog")
        time.sleep(getattr(fake_pick, "slow", 0))
        return list(fake_paths)

    srv._native_pick = fake_pick
    srv._pick_last_dir["dir"] = ""

    # 1) 正常选中 → 原样返回完整路径；下次对话框开在上次选的目录
    fake_paths = [r"D:\docs\报告.pptx", r"D:\docs\数据.xlsx"]
    st, body = _post(base + "/api/pick_file", {})
    assert st == 200 and body["paths"] == fake_paths, (st, body)
    assert calls[-1] == "", "首次无历史目录"
    st, body = _post(base + "/api/pick_file", {})
    assert calls[-1] == r"D:\docs", f"下次应开在上次的目录: {calls[-1]}"
    print("PASS: 正常多选返回路径 + 记住上次目录")

    # 2) 用户取消（空列表）→ 200 且不动历史目录
    fake_paths = []
    st, body = _post(base + "/api/pick_file", {})
    assert st == 200 and body["paths"] == [], (st, body)
    assert calls[-1] == r"D:\docs", "取消不该改上次目录"
    print("PASS: 取消选择 → 200 空列表，目录历史不动")

    # 3) 并发：第一个窗口还没人理，第二个请求 409
    fake_pick.slow = 1.5
    fake_paths = [r"D:\x\a.txt"]
    t = threading.Thread(target=lambda: _post(base + "/api/pick_file", {}))
    t.start()
    time.sleep(0.4)   # 保证第一个已拿到锁
    st, body = _post(base + "/api/pick_file", {})
    t.join()
    assert st == 409 and "待处理" in body["error"], (st, body)
    fake_pick.slow = 0
    print("PASS: 并发弹窗 → 第二个请求 409 提示待处理")

    # 4) tkinter 挂了 → 500 + 指路手动输入
    fake_pick.raise_error = True
    st, body = _post(base + "/api/pick_file", {})
    assert st == 500 and "手动粘贴" in body["error"], (st, body)
    print("PASS: 选择器异常 → 500 并指向手动输入兜底")

    print("ALL PASS")


if __name__ == "__main__":
    main()
