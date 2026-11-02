"""端到端验证：Ollama 说一半就断，WebUI 全链路是否保住内容并给出提示。

流程：假 Ollama（只发两行 text 就关连接，不发 done 帧）→ 起真实 WebUI →
POST /api/chat → 检查 NDJSON 事件与会话存档。

期望：
  - 收到 text 事件（已生成的字不丢）
  - 收到 notice，内容含「回答中断」
  - 会话存档里最后一条 assistant 带着那两行字 + 中断说明（能接着说「继续」）
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

ROOT = pathlib.Path(__file__).resolve().parents[1]
PORT_OLLAMA = 11599
PORT_UI = 8899

CHUNKS = ["## 自动化测试策略\n\n基于现有项目，", "我建议先补三类用例："]


class FakeOllama(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"      # 靠关连接结束响应，正好模拟「没有 done 帧」

    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.path != "/api/chat":
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        for t in CHUNKS:
            self.wfile.write((json.dumps({"message": {"content": t}, "done": False},
                                         ensure_ascii=False) + "\n").encode("utf-8"))
            self.wfile.flush()
            time.sleep(0.2)
        # 关键：不发 {"done": true}，直接结束 handler → 连接关闭


def _wait_ready(url: str, timeout: float = 20) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.3)
    return False


def main() -> None:
    # 隔离：会话存到临时目录，工作空间也指到临时目录。
    # 早期版本直接用 ~/.ollama-agent/sessions，而 WebUI 启动会「接上最近一次对话」，
    # 结果测试实例把真实会话当测试数据写了一遍又删掉 —— 这条路径不能再碰用户目录。
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="ollama-agent-e2e-"))
    sess_dir = tmp / "sessions"
    workspace = tmp / "ws"
    workspace.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, OLLAMA_AGENT_SESSIONS=str(sess_dir))

    fake = ThreadingHTTPServer(("127.0.0.1", PORT_OLLAMA), FakeOllama)
    Thread(target=fake.serve_forever, daemon=True).start()
    print(f"假 Ollama 已起 :{PORT_OLLAMA}（只发 {len(CHUNKS)} 段文本，不发 done 帧）")
    print(f"隔离目录：{tmp}")

    proc = subprocess.Popen(
        [sys.executable, "-m", "webui", "--port", str(PORT_UI),
         "--ollama", f"http://127.0.0.1:{PORT_OLLAMA}",
         "--model", "stub", "--no-browser", "--no-hotkey",
         "--workspace", str(workspace)],
        cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
    )
    try:
        if not _wait_ready(f"http://127.0.0.1:{PORT_UI}/api/health"):
            out = proc.stdout.read().decode("utf-8", "replace")[:800] if proc.stdout else ""
            raise RuntimeError(f"WebUI 没起来：{out}")

        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT_UI}/api/chat",
            data=json.dumps({"message": "怎么做自动化测试"}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST")
        raw = urllib.request.urlopen(req, timeout=60).read().decode("utf-8", "replace")

        events = []
        for ln in raw.splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                events.append(json.loads(ln))
            except json.JSONDecodeError:
                pass

        kinds = [e.get("type") for e in events]
        text = "".join(e.get("text", "") for e in events if e.get("type") == "text")
        notices = [e.get("text", "") for e in events if e.get("type") == "notice"]

        print(f"\n收到 {len(events)} 个事件：{kinds}")
        assert text, "前端应收到已生成的文字"
        assert "自动化测试策略" in text, text
        assert any("回答中断" in n for n in notices), f"应有一条持久提示，实际 {notices}"
        assert "done" in kinds, "流必须以 done 收尾，前端才会解除忙碌态"
        print(f"PASS: 事件流保住了 {len(text)} 字，并提示：{notices[-1][:60]}")

        saved = list(sess_dir.glob("*.json"))
        assert len(saved) == 1, f"隔离目录里应只有本次会话，实际 {[p.name for p in saved]}"
        data = json.loads(saved[0].read_text(encoding="utf-8"))
        assert data["workspace"].replace("\\", "/").endswith("ws"), data["workspace"]
        last = data["history"][-1]
        assert last["role"] == "assistant", last
        assert "自动化测试策略" in last["content"], "存档里必须保留半截回答"
        assert "回答中断" in last["content"], last["content"]
        print(f"PASS: 会话存档末条 = assistant，含 {len(last['content'])} 字（可接着说「继续」）")
        print(f"      存档文件：{saved[0].name}（隔离目录内）")
        print("PASS: 端到端断流验证全过")
    finally:
        proc.kill()
        fake.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)   # 只删本次临时目录


if __name__ == "__main__":
    main()
