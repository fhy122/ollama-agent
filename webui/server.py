"""Web UI HTTP 服务：静态页 + JSON API + NDJSON 流式对话。

只绑 127.0.0.1（本地工具，不对外网开洞）。
"""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import shutil
import subprocess
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from agent import HIDDEN_DIRNAME
from agent.config import THINK_VALUES, AgentConfig
from webui.session import SessionHub
from webui.stream import StreamEmitter

import tools as tools_pkg

STATIC_DIR = Path(__file__).parent / "static"
# Windows 的 mimetypes 表里没有 .woff2（guess_type 返回 None → octet-stream），
# 而本地 KaTeX 的数学字体靠这个后缀加载，显式注册别让浏览器当二进制丢掉。
mimetypes.add_type("font/woff2", ".woff2")
MAX_UPLOAD = 20 * 1024 * 1024  # 20MB
# 应用数据统一在 {workspace}/.ollama-agent/ 下（HIDDEN_DIRNAME）。
# 旧的 .uploads/.clip_cache 留在集合里：迁移前的残留也不该出现在搜索结果里。
_SKIP_DIRS = {HIDDEN_DIRNAME, ".uploads", ".clip_cache", ".workbuddy",
              "__pycache__", ".git", "node_modules"}

# 旧版散落在工作区根下的隐藏目录 → 新家（.ollama-agent/ 下的子目录名）。
_LEGACY_DIRS = {".uploads": "uploads", ".clip_cache": "clip"}

# ---------- /api/pick_file（本机文件选择框） ----------
# 每请求一线程（ThreadingHTTPServer），同时只许一个选择窗口：
# 并发弹两个窗口行为未定义，锁还顺带挡住前端重复点击。
_pick_lock = threading.Lock()
# 记住上次选择的目录，下次对话框开在老地方（模块级：服务生命周期内有效）
_pick_last_dir = {"dir": ""}

_PICK_TIMEOUT_SEC = 360   # 对话框前人工挑选的兜底上限；前端超时 300s，比它长


def _native_pick(initialdir: str) -> list[str]:
    """弹系统原生「打开文件」对话框，返回选中的完整路径（可多选）。

    为什么走 PowerShell 子进程（WinForms）而不是进程内 ctypes：
      - comdlg32 GetOpenFileNameW 在 Python 进程里怎么调都
        CDERR_STRUCTSIZE(0x1)（lStructSize 128/120 都拒、加 owner/换缓冲
        无效），IFileOpenDialog 更是 Show 阶段直接访问违例崩进程——
        而同进程 MessageBoxW 正常、PowerShell 弹同一对话框正常；
      - 本机 Python 没带 tkinter（ModuleNotFoundError）；
      - PowerShell 子进程是唯一在本机实证可弹的通道（tests/exp_dialog.ps1），
        代价 ~1s 启动开销，可接受（点按钮本来就要等人挑文件）。

    输出用 '|' 连接——Windows 文件名禁止该字符，不会撞车。
    取消/没选返回 []。
    """
    esc = (initialdir or "").replace("'", "''")
    ps = (
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8;"
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null;"
        "$d = New-Object System.Windows.Forms.OpenFileDialog;"
        "$d.Title = '选择要附加的文件（可多选，原路径直读不复制）';"
        "$d.Multiselect = $true;"
        "$d.Filter = '所有文件 (*.*)|*.*';"
        + (f"$d.InitialDirectory = '{esc}';" if esc else "")
        + "if ($d.ShowDialog() -eq 'OK') { $d.FileNames -join '|' }"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True, text=True, encoding="utf-8",
            timeout=_PICK_TIMEOUT_SEC,
            creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        raise RuntimeError("选择窗口挂了太久，已放弃——请重试")
    out = (r.stdout or "").strip()
    return [p for p in out.split("|") if p]


def migrate_legacy_dirs(ws: Path) -> None:
    """把旧版散落的 .uploads/.clip_cache 收进 .ollama-agent/（启动时一次性）。

    只 move 不 copy，工作区根下不再留旧目录。旧会话里存的附件绝对路径
    会因此失效，但 read_file 的 _missing_hint 会按文件名在新目录里找到
    同名文件并给出正确路径，断链自动纠正。迁移失败不挡启动（旧目录留原位）。
    """
    base = ws / HIDDEN_DIRNAME
    for old, new in _LEGACY_DIRS.items():
        src = ws / old
        if not src.is_dir():
            continue
        dst = base / new
        try:
            dst.mkdir(parents=True, exist_ok=True)
            for item in src.iterdir():
                target = dst / item.name
                if not target.exists():   # 新家已有同名就不动，宁缺勿覆盖
                    shutil.move(str(item), str(target))
            if not any(src.iterdir()):    # 全搬走才删壳；有残留（同名跳过）留原地
                src.rmdir()
        except OSError:
            pass


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


def build_files_hint(files: list[str], workspace: Path) -> str:
    """附件指引，按「副本 / 原件」分开说话。

    两条来源的文件长得一样（都是路径字符串），但模型该做的事不同：
      - 拖拽/选文件上传的副本落在工作区 .ollama-agent/uploads/ 里，模型得知道去那儿找；
      - 「从路径添加」的是本机原件（绝对路径），没有副本，直读即可 ——
        别让模型再 list_dir 去摸一遍（实测它真的会去摸）。
    相对路径一律视为工作区内（read_file 的 resolve 规则也是这么锚定的）。
    """
    inner, outer = [], []
    for f in files:
        p = Path(f)
        if not p.is_absolute():
            inner.append(f)
            continue
        try:
            inside = p.resolve().is_relative_to(workspace.resolve())
        except OSError:
            inside = False
        (inner if inside else outer).append(f)

    parts = []
    if inner:
        # 别叫它「剪贴板文件」：模型会顺着这个词去调 clipboard_image，
        # 结果把一张截图当成 PDF 读。
        parts.append("[我附带了这些文件（已上传到工作空间，不是剪贴板截图），"
                     "短的直接用 read_file 读；很长（几十万字/几百页）的必须用 "
                     "read_long_doc 分段精读；PDF 抽不出文字再用 load_image 按页渲染]\n"
                     + "\n".join(f"- {p}" for p in inner))
    if outer:
        parts.append("[这些是我本机的文件，给的是绝对路径原件（没有上传副本），"
                     "用 read_file 直接按这个路径读]\n"
                     + "\n".join(f"- {p}" for p in outer))
    return "\n\n".join(parts)


def _full_config(app) -> dict:
    """GET / POST 同一份配置快照：带上当前后端能列出的模型和工具清单。

    切后端（本地 Ollama <-> 中转站）时模型集合完全不同，两边返回结构必须一致，
    否则前端得靠再请求一次来猜状态。
    """
    snap = app.session.snapshot()
    snap["models"] = _safe_list_models(app.session.client)
    snap["tools"] = [
        {"name": t.name, "description": t.description}
        for t in tools_pkg.all_tools()
    ]
    snap["think_values"] = list(THINK_VALUES)
    return snap


# 列模型的总预算（秒）。内置 urllib 的 timeout 只管 socket 读，
# 实测遇到「域名解析慢 / 对端不响应 TLS 握手」时能挂到十几秒以上，
# 而这个接口是 GET /api/config 同步路径 —— 前端每次开设置、每次保存完
# 都要调它，一挂就把整个设置面板卡成转圈。
LIST_MODELS_BUDGET = 6.0

# TTL 缓存（60s）：打开设置同样不该反复等这 6 秒预算。单槽位（当前生效的
# client 才有意义，换配置即失效）；空结果不缓存——瞬态失败不该粘住一分钟。
_LIST_MODELS_TTL = 60.0
_list_models_cache: dict = {"client": None, "at": 0.0, "models": []}


def _safe_list_models(client) -> list[str]:
    """带超时预算地列模型；超时/失败一律返回空列表，绝不阻塞设置面板。

    为什么要线程兜底：urllib 的 timeout 覆盖不到所有阻塞阶段，
    所以这里把调用丢进守护线程，超预算就直接放弃它（返回空）。
    泄漏一个后台线程可以接受（它最终会自己超时结束），
    但让人对着设置面板干等 15 秒不可接受。
    """
    now = time.time()
    hit = _list_models_cache
    if (hit["client"] is client and now - hit["at"] < _LIST_MODELS_TTL):
        return hit["models"]
    box: dict = {}

    def work():
        try:
            box["models"] = client.list_models()
        except Exception:
            box["models"] = []

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(LIST_MODELS_BUDGET)
    models = box.get("models") or []
    if models:
        hit["client"] = client
        hit["at"] = now
        hit["models"] = models
    return models


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
            elif path == "/favicon.ico":
                self.send_static("favicon.ico")
            elif path == "/api/health":
                self.send_json({"ok": True, "model": app.session.cfg.model})
            elif path == "/api/config":
                self.send_json(_full_config(app))
            elif path == "/api/pending":
                self.send_json({"items": app.session.drain_clip()})
            elif path == "/api/live":
                # 刷新/重连后接管进行中的回合（前端轮询增量渲染）
                self.send_json(app.session.live())
            elif path == "/api/statuses":
                # 会话列表运行态（busy=生成中 / attention=后台完成待查看）
                self.send_json({"statuses": app.session.statuses()})
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
            elif path == "/api/models":
                # 默认走缓存秒回（打开设置面板不能卡）；?refresh=1 才真正探测
                # 本地 Ollama（前端「刷新」/「刷新全部」按钮专用）
                qs = parse_qs(urlparse(self.path).query)
                self.send_json(app.session.profiles_view(
                    refresh_builtin=bool(qs.get("refresh"))))
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
                app.session.update_config(body)
                # 和 GET 返回同构：换了后端模型列表完全不同，
                # 只回一个不带动 models 的快照，前端得再猜一次
                self.send_json(_full_config(app))
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
            elif path == "/api/steer":
                # 输入队列「立即发送」：空闲时回 409 idle，前端回退为普通发送
                body = _json_body(self)
                r = app.session.steer(str(body.get("text") or ""))
                self.send_json(r, code=int(r.get("code", 200)))
            elif path == "/api/models/probe":
                self.handle_probe()
            elif path == "/api/models/save":
                body = _json_body(self)
                r = app.session.save_profile(body)
                self.send_json(r)
            elif path == "/api/models/activate":
                body = _json_body(self)
                self.send_json(app.session.activate_profile(
                    str(body.get("id") or "")))
            elif path == "/api/models/delete":
                body = _json_body(self)
                self.send_json(app.session.delete_profile(
                    str(body.get("id") or "")))
            elif path == "/api/paste":
                self.handle_paste()
            elif path == "/api/upload":
                self.handle_upload()
            elif path == "/api/attach_path":
                self.handle_attach_path()
            elif path == "/api/pick_file":
                self.pick_files()
            else:
                self.send_json({"error": "not found"}, 404)

        # ---------- 业务 ----------

        def send_session_result(self, r: dict):
            """会话操作的结果：有 error 时按 code（默认 409 忙碌 / 404 不存在）回状态码。"""
            if r.get("error"):
                self.send_json(r, int(r.get("code") or 409))
            else:
                self.send_json(r)

        def handle_probe(self):
            """测一个「还没保存」的后端配置能不能连通。

            收的是表单里的原值（可能是正在编辑、尚未落盘的内容），
            所以不能只按当前生效配置去测 —— 那样用户改完地址永远测不出效果。
            """
            body = _json_body(self)
            key = str(body.get("api_key") or "").strip()
            if not key:
                # 前端只有打码值，编辑已有配置时 Key 框是空的。
                # 用已保存的那把，否则「改了地址点测试」永远报「缺 Key」。
                key = app.session.key_for(str(body.get("id") or ""))
            from agent.probe import probe
            r = probe(
                str(body.get("provider") or "ollama"),
                str(body.get("host") or ""),
                key,
                str(body.get("model") or ""),
            )
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

            # 断连（刷新/关页）不再打断回合：回合线程照跑、事件照记，
            # 前端回来走 /api/live 接管。打断只走显式的 /api/stop。
            emitter = StreamEmitter(self.wfile)
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
                send_text += "\n\n" + build_files_hint(files, app.session.workspace)
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
            up_dir = app.session.workspace / HIDDEN_DIRNAME / "uploads"
            up_dir.mkdir(parents=True, exist_ok=True)
            dest = up_dir / name
            dest.write_bytes(data)
            self.send_json({"path": str(dest)})

        def handle_attach_path(self):
            """按本机原路径挂附件（不复制）。

            服务和浏览器在同一台机器上（只绑 127.0.0.1），「选文件上传」
            实际是同机拷贝。这里让用户直接给路径，验证存在后原样返回，
            模型用 read_file 按绝对路径直读，省掉一份拷贝和附件副本堆积。
            """
            body = _json_body(self)
            # 容忍从资源管理器「复制文件地址」带出来的引号
            raw = str(body.get("path") or "").strip().strip('"').strip("'")
            p = Path(raw).expanduser()
            if not raw:
                self.send_json({"error": "路径为空"}, 400)
                return
            if not p.is_absolute():
                self.send_json({"error": f"要绝对路径（如 D:\\docs\\xx.pptx），收到: {raw}"}, 400)
                return
            if not p.is_file():
                self.send_json({"error": f"文件不存在: {p}"}, 400)
                return
            self.send_json({"path": str(p), "name": p.name,
                            "size": p.stat().st_size})

        def pick_files(self):
            """弹本机文件选择框，选中的按原路径挂附件（不复制）。

            痛点是「记不住路径、也不想复制粘贴」——手动输入行只当兜底。
            服务跑在用户本机（只绑 127.0.0.1），comdlg32 弹的就是
            Windows 系统原生对话框。拿不到锁说明已有一个窗口没人理，
            回 409 让前端提示，而不是再叠一个。
            """
            if not _pick_lock.acquire(timeout=0.5):
                self.send_json({"error": "已有一个文件选择窗口待处理"}, 409)
                return
            try:
                try:
                    paths = _native_pick(_pick_last_dir["dir"])
                except Exception as e:
                    self.send_json({"error": f"打不开文件选择器（{e}），"
                                             "可展开路径输入行手动粘贴"}, 500)
                    return
                if paths:
                    # 下次开在上次选的目录——顺着用户正在整理的文件夹走
                    _pick_last_dir["dir"] = str(Path(paths[0]).parent)
                self.send_json({"paths": paths})
            finally:
                _pick_lock.release()

    return Handler


class WebUIApp:
    """UI 应用：持有会话 + HTTP 服务 + 可选全局热键。"""

    def __init__(self, cfg: AgentConfig, host: str = "127.0.0.1", port: int = 8888):
        self.session = SessionHub(cfg)   # 多会话并行：每条对话独立实例各自跑
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
        # 旧版散落的 .uploads/.clip_cache 收进 .ollama-agent/（幂等，没有就跳过）
        migrate_legacy_dirs(self.session.workspace)
        # 预热放在建 HTTP 服务之前：浏览器打开时模型已在显存里，
        # 而不是等服务起来了再让第一句话去触发加载
        if self.session.cfg.warm:
            self.warmup()
        handler = make_handler(self)
        self.httpd = ThreadingHTTPServer((self.host, self.port), handler)
        url = f"http://{self.host}:{self.port}/"
        print(f"Agent UI 已启动: {url}", flush=True)
        cli = self.session.client
        extra = (f" | think={self.session.cfg.think}"
                 f" | ctx={self.session.cfg.num_ctx or '服务端默认'}")
        print(f"后端 {cli.name} ({self.session.cfg.host})"
              f" | 模型 {self.session.cfg.model or '未选'}{extra}", flush=True)
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
