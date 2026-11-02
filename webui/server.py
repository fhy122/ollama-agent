"""Web UI HTTP 服务：静态页 + JSON API + NDJSON 流式对话。

只绑 127.0.0.1（本地工具，不对外网开洞）。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from agent.config import THINK_VALUES, AgentConfig
from webui.session import AgentSession
from webui.stream import StreamEmitter

import tools as tools_pkg

STATIC_DIR = Path(__file__).parent / "static"
MAX_UPLOAD = 20 * 1024 * 1024  # 20MB
_SKIP_DIRS = {".uploads", ".clip_cache", ".workbuddy", "__pycache__", ".git", "node_modules"}


def _search_files(ws: Path, q: str, limit: int = 20) -> list[dict]:
    """工作空间文件模糊搜索（@ 上下文用）。q 为空列根目录文件。"""
    q = (q or "").strip().lower()
    out: list[dict] = []
    for p in sorted(ws.rglob("*")):
        if len(out) >= limit:
            break
        try:
            rel = p.relative_to(ws)
        except ValueError:
            continue
        if not p.is_file() or any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if not q or q in str(rel).lower():
            out.append({"name": p.name, "path": str(rel)})
    return out


def _json_body(handler: BaseHTTPRequestHandler) -> dict:
    n = int(handler.headers.get("Content-Length") or 0)
    if n <= 0:
        return {}
    raw = handler.rfile.read(n)
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".ico", ".svg"}
TABLE_SUFFIXES = {".xlsx", ".xlsm", ".csv", ".tsv"}
MAX_TEXT_PREVIEW = 400 * 1024      # 400KB 文本预览上限
MAX_IMAGE_PREVIEW = 12 * 1024 * 1024
MAX_TABLE_ROWS = 200
MAX_TABLE_COLS = 30

_LANG_BY_SUFFIX = {
    ".py": "python", ".js": "javascript", ".ts": "typescript", ".tsx": "tsx",
    ".jsx": "jsx", ".html": "html", ".htm": "html", ".css": "css", ".json": "json",
    ".yml": "yaml", ".yaml": "yaml", ".toml": "toml", ".sh": "shell", ".bat": "bat",
    ".ps1": "powershell", ".sql": "sql", ".go": "go", ".rs": "rust", ".java": "java",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".md": "markdown", ".txt": "text",
    ".csv": "csv", ".log": "log", ".xml": "xml", ".ini": "ini",
}


def _read_table(p: Path) -> dict:
    """表格预览：读前 N 行前 N 列（xlsx/csv）。"""
    suf = p.suffix.lower()
    rows: list[list[str]] = []
    truncated = False
    if suf in {".csv", ".tsv"}:
        import csv
        delim = "\t" if suf == ".tsv" else ","
        with p.open("r", encoding="utf-8-sig", errors="replace", newline="") as f:
            for i, row in enumerate(csv.reader(f, delimiter=delim)):
                if i >= MAX_TABLE_ROWS:
                    truncated = True
                    break
                rows.append([str(c) for c in row[:MAX_TABLE_COLS]])
    else:
        from openpyxl import load_workbook
        wb = load_workbook(p, data_only=True, read_only=True)
        try:
            ws = wb.worksheets[0]
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i >= MAX_TABLE_ROWS:
                    truncated = True
                    break
                rows.append(["" if v is None else str(v) for v in row[:MAX_TABLE_COLS]])
        finally:
            wb.close()
    return {"rows": rows, "truncated": truncated}


def _read_preview(p: Path) -> dict:
    """按类型返回预览数据：image / table / text / binary。"""
    size = p.stat().st_size
    suf = p.suffix.lower()
    base = {"name": p.name, "path": str(p), "size": size,
            "mime": mimetypes.guess_type(str(p))[0] or "application/octet-stream"}

    if suf in IMAGE_SUFFIXES and size <= MAX_IMAGE_PREVIEW:
        base.update(kind="image",
                    b64=base64.b64encode(p.read_bytes()).decode("ascii"))
        return base

    if suf in TABLE_SUFFIXES:
        try:
            base.update(kind="table", **_read_table(p))
            return base
        except Exception as e:      # openpyxl 缺失 / 文件损坏 → 退回文本
            base["table_error"] = f"{type(e).__name__}: {e}"

    raw = p.read_bytes()[:MAX_TEXT_PREVIEW + 1]
    try:
        text = raw.decode("utf-8")
        is_text = "\x00" not in text
    except UnicodeDecodeError:
        is_text = False
        text = ""
    if is_text:
        base.update(kind="text", text=text[:MAX_TEXT_PREVIEW],
                    truncated=len(raw) > MAX_TEXT_PREVIEW,
                    lang=_LANG_BY_SUFFIX.get(suf, "text"),
                    markdown=suf in {".md", ".markdown"})
        return base

    base.update(kind="binary")
    return base



def make_handler(app: "WebUIApp"):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"   # 响应写完即关，天然支持流式
        server_version = "OllamaAgentUI/1.0"

        def log_message(self, fmt, *args):  # 静音访问日志
            pass

        # ---------- 基础应答 ----------

        def send_json(self, obj, code=200):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def send_static(self, rel: str):
            path = (STATIC_DIR / rel).resolve()
            if not str(path).startswith(str(STATIC_DIR.resolve())) or not path.is_file():
                self.send_json({"error": "not found"}, 404)
                return
            ctype = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)

        # ---------- 路由 ----------

        def do_GET(self):
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self.send_static("index.html")
            elif path.startswith("/static/"):
                self.send_static(path[len("/static/"):])
            elif path == "/api/health":
                self.send_json({"ok": True, "model": app.session.cfg.model})
            elif path == "/api/config":
                snap = app.session.snapshot()
                try:
                    snap["models"] = app.session.client.list_models()
                except Exception:
                    snap["models"] = []
                snap["tools"] = [
                    {"name": t.name, "description": t.description}
                    for t in tools_pkg.all_tools()
                ]
                snap["think_values"] = list(THINK_VALUES)
                self.send_json(snap)
            elif path == "/api/pending":
                self.send_json({"items": app.session.drain_clip()})
            elif path == "/api/skills":
                from tools.skill import list_skills
                self.send_json({"skills": list_skills(str(app.session.workspace))})
            elif path == "/api/sessions":
                self.send_json({"sessions": app.session.list_sessions(),
                                "current": app.session.current()})
            elif path == "/api/sessions/search":
                q = (urlparse(self.path).query or "")
                m = re.search(r"(?:^|&)q=([^&]*)", q)
                from urllib.parse import unquote
                kw = unquote(m.group(1)) if m else ""
                self.send_json({"q": kw,
                                "results": app.session.search_sessions(kw)})
            elif path == "/api/files":
                q = (urlparse(self.path).query or "")
                m = re.search(r"(?:^|&)q=([^&]*)", q)
                from urllib.parse import unquote
                self.send_json({"files": _search_files(
                    app.session.workspace, unquote(m.group(1)) if m else "")})
            elif path == "/api/file":
                self.handle_file()
            else:
                self.send_json({"error": "not found"}, 404)

        def do_POST(self):
            path = urlparse(self.path).path
            if path == "/api/chat":
                self.handle_chat()
            elif path == "/api/config":
                body = _json_body(self)
                snap = app.session.update_config(body)
                self.send_json(snap)
            elif path == "/api/confirm":
                body = _json_body(self)
                ok = app.session.answer_confirm(
                    str(body.get("id") or ""),
                    bool(body.get("approved")),
                    bool(body.get("allow_rest")),
                )
                self.send_json({"ok": ok})
            elif path == "/api/reset":
                self.send_session_result(app.session.reset())
            elif path == "/api/sessions/new":
                self.send_session_result(app.session.new_session())
            elif path == "/api/sessions/open":
                body = _json_body(self)
                self.send_session_result(
                    app.session.open_session(str(body.get("id") or "")))
            elif path == "/api/sessions/delete":
                body = _json_body(self)
                self.send_session_result(
                    app.session.delete_session(str(body.get("id") or "")))
            elif path == "/api/stop":
                app.session.request_stop()
                self.send_json({"ok": True})
            elif path == "/api/paste":
                self.handle_paste()
            elif path == "/api/upload":
                self.handle_upload()
            else:
                self.send_json({"error": "not found"}, 404)

        # ---------- 业务 ----------

        def send_session_result(self, r: dict):
            """会话操作的结果：有 error 时按 code（默认 409 忙碌 / 404 不存在）回状态码。"""
            if r.get("error"):
                self.send_json(r, int(r.get("code") or 409))
            else:
                self.send_json(r)

        def handle_chat(self):
            body = _json_body(self)
            message = str(body.get("message") or "").strip()
            images = [i for i in (body.get("images") or []) if i]
            files = [str(f) for f in (body.get("files") or []) if f]

            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()

            emitter = StreamEmitter(self.wfile,
                                    on_disconnect=app.session.request_stop)
            if not message and not images and not files:
                emitter.notice("空消息")
                emitter._send({"type": "done", "text": ""})
                return

            # ---- 斜杠命令（与 CLI 对齐）----
            # /skill <名> [问题]：先注入技能；带问题则继续走模型（可带附件）
            if message.startswith("/"):
                cmd, _, arg = message.partition(" ")
                cmd, arg = cmd.lower(), arg.strip()
                if cmd == "/skill":
                    name, _, question = arg.partition(" ")
                    r = (app.session.load_skill(name) if name
                         else {"ok": False, "error": "用法: /skill <技能名> [问题]"})
                    if not r.get("ok"):
                        emitter.notice(str(r.get("error") or "加载失败"))
                        emitter._send({"type": "done", "text": ""})
                        return
                    emitter.notice(f"技能 {r['name']} 已注入对话（{r['chars']} 字符）")
                    if not question.strip():
                        if not images and not files:
                            emitter._send({"type": "done", "text": ""})
                            return
                        message = ""          # 挂了附件 → 继续走模型
                    else:
                        message = question.strip()   # 带问题 → 继续走模型
                elif cmd == "/reset":
                    app.session.reset()
                    emitter.notice("本条对话已清空（其他对话不受影响）")
                    emitter._send({"type": "done", "text": ""})
                    return

            send_text = message or "看一下我附带的内容"
            if files:
                listing = "\n".join(f"- {p}" for p in files)
                # 别叫它「剪贴板文件」：模型会顺着这个词去调 clipboard_image，
                # 结果把一张截图当成 PDF 读。明确说是已上传到工作空间的文件。
                send_text += (f"\n\n[我附带了这些文件（已上传到工作空间，不是剪贴板截图），"
                              f"直接用 read_file 读；PDF 抽不出文字再用 load_image 按页渲染]\n"
                              f"{listing}")
            try:
                final = app.session.run(send_text, images or None, emitter,
                                        files=files, display=message)
                emitter._send({"type": "done", "text": final,
                               "session": {"id": app.session.sid,
                                           "title": app.session.title}})
            except Exception as e:
                # 先往消息流里落一条持久可见的提示，再发 error（前端只把它弹成 toast，
                # 一闪就没，用户会以为"说完就断了"而看不到任何原因）
                emitter.notice(f"[⚠ 本轮没能开始：{type(e).__name__}: {e}]")
                emitter._send({"type": "error",
                               "text": f"{type(e).__name__}: {e}"})
                emitter._send({"type": "done", "text": ""})

        def handle_file(self):
            """文件预览 / 下载。只允许工作空间内的路径（本地工具不做网盘）。"""
            from urllib.parse import parse_qs, quote

            qs = parse_qs(urlparse(self.path).query)
            raw = (qs.get("path") or [""])[0]
            if not raw:
                self.send_json({"error": "缺少 path 参数"}, 400)
                return

            ws = app.session.workspace.resolve()
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = ws / p
            try:
                p = p.resolve()
            except OSError:
                self.send_json({"error": "路径无法解析"}, 400)
                return
            if not p.is_relative_to(ws):
                self.send_json({"error": "只能预览工作空间内的文件"}, 403)
                return
            if not p.is_file():
                self.send_json({"error": "文件不存在"}, 404)
                return

            if qs.get("raw"):
                try:
                    data = p.read_bytes()
                except OSError as e:
                    self.send_json({"error": f"读取失败：{e}"}, 500)
                    return
                ctype = mimetypes.guess_type(str(p))[0] or "application/octet-stream"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Disposition",
                                 f"attachment; filename*=UTF-8''{quote(p.name)}")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(data)
                return

            try:
                self.send_json(_read_preview(p))
            except OSError as e:
                self.send_json({"error": f"读取失败：{e}"}, 500)

        def handle_paste(self):
            try:
                from agent.clipboard import grab_any
                files, img_path = grab_any(app.session.workspace)
            except Exception as e:
                self.send_json({"error": str(e), "files": [], "image": None}, 200)
                return
            image_b64 = None
            cache = None
            if img_path:
                image_b64 = base64.b64encode(img_path.read_bytes()).decode("ascii")
                cache = str(img_path)
            self.send_json({
                "files": [str(f) for f in files],
                "image": image_b64,
                "cache": cache,
            })

        def handle_upload(self):
            body = _json_body(self)
            name = Path(str(body.get("name") or "upload.bin")).name
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name) or "upload.bin"
            data_b64 = body.get("data") or ""
            try:
                data = base64.b64decode(data_b64)
            except Exception:
                self.send_json({"error": "base64 解码失败"}, 400)
                return
            if len(data) > MAX_UPLOAD:
                self.send_json({"error": "文件超过 20MB"}, 400)
                return
            up_dir = app.session.workspace / ".uploads"
            up_dir.mkdir(parents=True, exist_ok=True)
            dest = up_dir / name
            dest.write_bytes(data)
            self.send_json({"path": str(dest)})

    return Handler


class WebUIApp:
    """UI 应用：持有会话 + HTTP 服务 + 可选全局热键。"""

    def __init__(self, cfg: AgentConfig, host: str = "127.0.0.1", port: int = 8888):
        self.session = AgentSession(cfg)
        self.host = host
        self.port = port
        self.httpd: ThreadingHTTPServer | None = None
        self.hotkey = None

    # ---------- 热键（Alt+V → 剪贴板进暂存区，前端轮询取走） ----------

    def start_hotkey(self) -> bool:
        try:
            from agent import hotkeys as hk
            if not hk.is_supported():
                return False

            def on_hotkey():
                try:
                    from agent.clipboard import grab_any
                    files, img_path = grab_any(self.session.workspace)
                except Exception as e:
                    self.session.push_clip({"error": str(e)})
                    return
                self.session.push_clip({
                    "files": [str(f) for f in files],
                    "image": (base64.b64encode(img_path.read_bytes()).decode("ascii")
                              if img_path else None),
                    "cache": str(img_path) if img_path else None,
                })

            self.hotkey = hk.HotkeyListener(on_hotkey)
            return self.hotkey.start()
        except Exception:
            return False

    def stop_hotkey(self):
        if self.hotkey:
            try:
                self.hotkey.stop()
            except Exception:
                pass
            self.hotkey = None

    # ---------- 启动 ----------

    def warmup(self) -> None:
        """把模型提前载入显存/内存，别让用户第一句话干等加载（cfg.warm 控制）。"""
        cfg = self.session.cfg
        print(f"预热模型 {cfg.model} | think={cfg.think} "
              f"| ctx={cfg.num_ctx or '服务端默认'} …", flush=True)
        print("  正在加载权重，首次可能要 1-2 分钟（Ctrl+C 可跳过，跳过不影响使用）",
              flush=True)
        t0 = time.time()
        try:
            self.session.client.warmup()
        except KeyboardInterrupt:
            print("已跳过预热（第一次提问时仍会自动加载）", flush=True)
            return
        except Exception as e:
            print(f"[警告] 预热失败: {type(e).__name__}: {e}", flush=True)
            print(f"  模型拉取了吗？试试  ollama pull {cfg.model}", flush=True)
            return
        print(f"模型就绪（{time.time() - t0:.1f}s）", flush=True)

    def serve(self, open_browser: bool = True) -> None:
        # 预热放在建 HTTP 服务之前：浏览器打开时模型已在显存里，
        # 而不是等服务起来了再让第一句话去触发加载
        if self.session.cfg.warm:
            self.warmup()
        handler = make_handler(self)
        self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        url = f"http://{self.host}:{self.port}/"
        print(f"Ollama Agent UI 已启动: {url}", flush=True)
        print(f"模型 {self.session.cfg.model} | think={self.session.cfg.think} "
              f"| ctx={self.session.cfg.num_ctx or '服务端默认'}", flush=True)
        print(f"工作空间: {self.session.workspace}", flush=True)
        if self.start_hotkey():
            print("全局热键 Alt+V: 剪贴板图片/文件直达对话", flush=True)
        if open_browser:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        try:
            self.httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n正在关闭…", flush=True)
        finally:
            self.stop_hotkey()
            self.httpd.server_close()
