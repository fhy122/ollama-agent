"""第 3 档端到端验证：模型管理 HTTP 全流程。

用临时配置目录 + 临时端口，绝不碰用户真实的 ~/.ollama-agent/config.json。
运行：python tests/_e2e_models.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PORT = int(os.environ.get("E2E_PORT", "8849"))
BASE = f"http://127.0.0.1:{PORT}"


def call(path, data=None):
    t0 = time.time()
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(
        BASE + path, data=body,
        headers={"Content-Type": "application/json"},
        method="POST" if body else "GET")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode() or "{}"), time.time() - t0
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode() or "{}"), time.time() - t0


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="oa-e2e-"))
    cfg_path = tmp / "config.json"
    env = dict(os.environ, OLLAMA_AGENT_CONFIG=str(cfg_path), PYTHONUNBUFFERED="1")

    proc = subprocess.Popen(
        [sys.executable, "-m", "webui", "--port", str(PORT),
         "--no-browser", "--no-warm", "--no-hotkey"],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

    def disk():
        try:
            return json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    try:
        ready = False
        for _ in range(80):
            if proc.poll() is not None:
                print("服务提前退出，输出：")
                print((proc.stdout.read() or "")[:4000])
                return 1
            try:
                urllib.request.urlopen(BASE + "/api/config", timeout=2)
                ready = True
                break
            except Exception:
                time.sleep(0.25)
        if not ready:
            print("服务 20s 内没起来")
            return 1

        out = []

        st, cfg, dt = call("/api/config")
        out.append(f"[1 初始 config] {st} {dt:.2f}s provider={cfg.get('provider')} "
                   f"model={cfg.get('model')} models列表={len(cfg.get('models') or [])}项")

        st, r, dt = call("/api/models")
        bi = r.get("builtin") or {}
        out.append(f"[2 GET models] {st} {dt:.2f}s active={r.get('active')} "
                   f"内置激活={bi.get('active')} 内置模型数={len(bi.get('models') or [])} "
                   f"自定义={len(r.get('profiles') or [])} 配置文件={r.get('config_path')}")

        st, r, dt = call("/api/models/probe",
                         {"provider": "openai", "host": "https://127.0.0.1:9/v1",
                          "api_key": "sk-x"})
        out.append(f"[3 探测坏地址] {st} {dt:.2f}s ok={r.get('ok')} "
                   f"stage={r.get('stage')} hint={str(r.get('hint'))[:34]}")

        st, r, dt = call("/api/models/probe",
                         {"provider": "openai", "host": "https://x/v1", "api_key": ""})
        out.append(f"[4 探测缺Key] {st} {dt:.2f}s ok={r.get('ok')} stage={r.get('stage')}")

        key = "sk-live-abcdefghijklmnop1234"
        st, r, dt = call("/api/models/save", {
            "name": "中转站 DeepSeek", "provider": "openai",
            "host": "https://www.aigatehub.xyz/v1", "api_key": key,
            "model": "deepseek-v4.1-flash", "think": "off",
            "num_ctx": 32768, "temperature": 0.3})
        pid = r.get("id")
        out.append(f"[5 保存自定义] {st} {dt:.2f}s ok={r.get('ok')} "
                   f"created={r.get('created')} id={pid} err={r.get('error')}")
        if not pid:
            out.append("！！保存接口没回 id，前端新建后无法再编辑/删除")
            print("\n".join(out))
            return 2

        d = disk()
        row = (d.get("profiles") or [{}])[0]
        out.append(f"[6 落盘核对] profiles={len(d.get('profiles') or [])} "
                   f"active={d.get('active')} Key真写盘={row.get('api_key') == key}")

        st, cfg, dt = call("/api/config")
        blob = json.dumps(cfg, ensure_ascii=False)
        out.append(f"[7 Key不泄漏] {st} {dt:.2f}s 含完整Key={key in blob} "
                   f"含打码={'…' in blob}")

        st, r2, dt = call("/api/models/save", {
            "name": "第二个", "provider": "openai",
            "host": "https://api.deepseek.com/v1",
            "api_key": "sk-second-999999999", "model": "deepseek-chat"})
        out.append(f"[8 保存第二条] {st} {dt:.2f}s ok={r2.get('ok')} id={r2.get('id')}")

        st, r3, dt = call("/api/models/save", {
            "id": pid, "name": "改名后", "provider": "openai",
            "host": "https://www.aigatehub.xyz/v1", "api_key": "",
            "model": "deepseek-v4.1-flash", "num_ctx": 65536})
        kept_rows = [p for p in (disk().get("profiles") or []) if p.get("id") == pid]
        if not kept_rows:
            out.append(f"[9 编辑留空Key] 找不到 id={pid}；盘上 id 有 "
                       f"{[p.get('id') for p in (disk().get('profiles') or [])]}")
            print("\n".join(out))
            return 2
        kept = kept_rows[0]
        out.append(f"[9 编辑留空Key] {st} {dt:.2f}s ok={r3.get('ok')} "
                   f"name={kept.get('name')} Key保留={kept.get('api_key') == key} "
                   f"ctx={kept.get('num_ctx')}")

        st, r4, dt = call("/api/models/activate", {"id": pid})
        out.append(f"[10 激活自定义] {st} {dt:.2f}s ok={r4.get('ok')} "
                   f"active={disk().get('active')}")

        st, r5, dt = call("/api/models/activate", {"id": "ollama-local"})
        st2, cfg2, _ = call("/api/config")
        out.append(f"[11 切回内置] {st} {dt:.2f}s active={disk().get('active')} "
                   f"provider={cfg2.get('provider')} "
                   f"backends_ok={'ollama' in str(cfg2.get('backends') or 'ollama')}")

        st, r6, dt = call("/api/models/delete", {"id": "ollama-local"})
        out.append(f"[12 删内置被拒] {st} {dt:.2f}s ok={r6.get('ok')} err={r6.get('error')}")

        # 关键路径：先切回这条自定义（让它成为生效项），再删它 ——
        # 删生效项必须让后端跟着回落到 Ollama，否则界面显示本地而请求还打中转站
        call("/api/models/activate", {"id": pid})
        st, r7, dt = call("/api/models/delete", {"id": pid})
        st7, cfg7, _ = call("/api/config")
        out.append(f"[13 删生效项] {st} {dt:.2f}s ok={r7.get('ok')} "
                   f"剩={len(disk().get('profiles') or [])} "
                   f"active={disk().get('active')} provider={cfg7.get('provider')}")

        st, r8, dt = call("/api/config", {"confirm": False, "max_steps": 7,
                                          "workspace": ""})
        d = disk()
        out.append(f"[14 改偏好] {st} {dt:.2f}s ui={d.get('ui')} "
                   f"自定义未被误删={len(d.get('profiles') or [])}")

        # 15 重启后能不能恢复 —— 持久化的唯一真判据。
        # 存一条新的自定义并激活，然后杀掉进程重起，看 cfg 是不是自动切到它。
        st, r9, _ = call("/api/models/save", {
            "name": "重启验证", "provider": "openai",
            "host": "https://api.restart-test.com/v1",
            "api_key": "sk-restart-abcdefgh", "model": "restart-model",
            "num_ctx": 16384, "temperature": 0.15})
        out.append(f"[15 存重启用配置] ok={r9.get('ok')} id={r9.get('id')}")

        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()
        proc = subprocess.Popen(
            [sys.executable, "-m", "webui", "--port", str(PORT),
             "--no-browser", "--no-warm", "--no-hotkey"],
            cwd=str(ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for _ in range(80):
            try:
                urllib.request.urlopen(BASE + "/api/config", timeout=2)
                break
            except Exception:
                time.sleep(0.25)
        st, cfg3, dt = call("/api/config")
        st, mv, _ = call("/api/models")
        out.append(f"[16 重启后恢复] {st} {dt:.2f}s provider={cfg3.get('provider')} "
                   f"model={cfg3.get('model')} host={cfg3.get('host')} "
                   f"active={mv.get('active')} "
                   f"Key在={any(p.get('has_key') for p in (mv.get('profiles') or []))}")

        print("\n".join(out))
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
