"""工作空间文件树（侧栏按目录展示）的回归测试。

判据：
  1. 根目录只列一层，目录在前、文件在后，排序大小写无关；
  2. 隐藏/忽略目录（.git / __pycache__ / .workbuddy / node_modules …）不出现；
  3. 子目录同样只列一层（不做递归），子目录以 dir=True 返回；
  4. 越界路径（`../`、跳出工作空间的软链接）返回空列表 + 说明，绝不列出外部文件；
  5. 传文件路径或不存在路径 → 不是目录，空列表；
  6. 条目超上限时给 truncated，且不超上限长度；
  7. HTTP 层 `/api/files/tree?path=` 可用（含 URL 编码的中文/空格路径）；
  8. 旧接口 `/api/files?q=` 行为不变（递归、只要文件、带 name/path）—— 输入框 @ 取上下文靠它。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

_TMP = Path(tempfile.mkdtemp(prefix="oa-filetree-"))
# 别让测试碰到真实 ~/.ollama-agent（会话存档 / 用户配置）
os.environ.setdefault("OLLAMA_AGENT_SESSIONS", str(_TMP / "sessions"))
os.environ.setdefault("OLLAMA_AGENT_CONFIG", str(_TMP / "config.json"))

from agent.config import AgentConfig  # noqa: E402
from webui.server import WebUIApp, _list_dir, make_handler  # noqa: E402


def _make_ws(tmp_path: Path) -> Path:
    """一个典型工作空间：多层目录 + 隐藏目录 + 根文件。"""
    ws = tmp_path / "ws"
    (ws / "src" / "inner").mkdir(parents=True)
    (ws / "docs").mkdir()
    (ws / "src" / "main.py").write_text("print(1)", encoding="utf-8")
    (ws / "src" / "inner" / "deep.py").write_text("x", encoding="utf-8")
    (ws / "docs" / "readme.md").write_text("doc", encoding="utf-8")
    (ws / "z_root.txt").write_text("z", encoding="utf-8")
    (ws / "Apple.txt").write_text("a", encoding="utf-8")
    for ignored in (".git", "__pycache__", ".workbuddy", "node_modules"):
        (ws / ignored).mkdir()
        (ws / ignored / "secret.py").write_text("nope", encoding="utf-8")
    return ws


def test_root_lists_one_level_dirs_first(tmp_path):
    ws = _make_ws(tmp_path)
    data = _list_dir(ws, "")
    names = [e["name"] for e in data["entries"]]
    # 目录在前、文件在后；两组内部都按小写字母序（Apple 在 z_root 前）
    assert names == ["docs", "src", "Apple.txt", "z_root.txt"], names
    assert [e["dir"] for e in data["entries"]] == [True, True, False, False]
    # 只列一层：孙文件不该出现
    assert "main.py" not in names and "readme.md" not in names
    assert data["path"] == "" and data["truncated"] is False


def test_ignored_dirs_never_listed(tmp_path):
    ws = _make_ws(tmp_path)
    names = {e["name"] for e in _list_dir(ws, "")["entries"]}
    for ignored in (".git", "__pycache__", ".workbuddy", "node_modules"):
        assert ignored not in names


def test_subdir_lists_children_with_relative_paths(tmp_path):
    ws = _make_ws(tmp_path)
    data = _list_dir(ws, "src")
    entries = {e["name"]: e for e in data["entries"]}
    assert set(entries) == {"inner", "main.py"}, entries
    assert entries["inner"]["dir"] is True
    assert entries["main.py"]["dir"] is False
    # 路径是相对工作空间的完整路径，前端据此做 data-path / 预览
    assert entries["inner"]["path"] == "src/inner"
    assert entries["main.py"]["path"] == "src/main.py"
    # 不递归：inner 里的 deep.py 不在这一层
    assert "deep.py" not in entries

    deep = _list_dir(ws, "src/inner")
    assert [e["path"] for e in deep["entries"]] == ["src/inner/deep.py"]


def test_traversal_is_rejected(tmp_path):
    ws = _make_ws(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.txt").write_text("secret", encoding="utf-8")

    data = _list_dir(ws, "../outside")
    assert data["entries"] == [], data
    assert data["error"], "越界必须给出说明，而不是静默当空目录"
    assert "leak.txt" not in json.dumps(data, ensure_ascii=False)


def test_symlink_escape_is_rejected(tmp_path):
    """软链接指向工作空间外：resolve 后不在 base 之下，必须拒绝。"""
    ws = _make_ws(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "leak.txt").write_text("secret", encoding="utf-8")
    link = ws / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("本机不允许创建软链接（Windows 需要额外权限）")

    data = _list_dir(ws, "escape")
    assert data["entries"] == [], data
    assert data["error"]


def test_file_and_missing_path_are_not_dirs(tmp_path):
    ws = _make_ws(tmp_path)
    for rel in ("src/main.py", "no_such_dir"):
        data = _list_dir(ws, rel)
        assert data["entries"] == [], rel
        assert data["error"] == "不是目录", data


def test_limit_and_truncated_flag(tmp_path):
    ws = _make_ws(tmp_path)
    data = _list_dir(ws, "", limit=2)
    assert len(data["entries"]) == 2
    assert data["truncated"] is True
    # 目录仍排前面：截断不会把目录挤掉
    assert all(e["dir"] for e in data["entries"])


def test_backslash_and_trailing_slash_tolerated(tmp_path):
    """Windows 风格分隔符与多余斜杠不该让整层变空。"""
    ws = _make_ws(tmp_path)
    for rel in ("src/", "/src", "src\\inner"):
        data = _list_dir(ws, rel)
        assert data["entries"], (rel, data)
        assert not data.get("error"), (rel, data)


# ---------------- HTTP 层 ----------------


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
    return httpd, port, app


def _get(port: int, path: str) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=30) as r:
        return json.loads(r.read().decode())


def test_http_tree_endpoint(tmp_path):
    ws = _make_ws(tmp_path)
    httpd, port, _app = _serve(ws)
    try:
        root = _get(port, "/api/files/tree?path=")
        assert [e["name"] for e in root["entries"]][:2] == ["docs", "src"]

        sub = _get(port, "/api/files/tree?path=src")
        assert {e["name"] for e in sub["entries"]} == {"inner", "main.py"}

        bad = _get(port, "/api/files/tree?path=" + urllib.parse.quote("../outside"))
        assert bad["entries"] == [] and bad["error"]
    finally:
        httpd.shutdown()


def test_http_tree_handles_encoded_names(tmp_path):
    """目录名带空格/中文：URL 编码要能正确还原，否则点开就是空目录。"""
    ws = tmp_path / "ws"
    (ws / "我的 文档").mkdir(parents=True)
    (ws / "我的 文档" / "笔记.md").write_text("x", encoding="utf-8")
    httpd, port, _app = _serve(ws)
    try:
        root = _get(port, "/api/files/tree?path=")
        assert [e["name"] for e in root["entries"]] == ["我的 文档"]
        sub = _get(port, "/api/files/tree?path=" + urllib.parse.quote("我的 文档"))
        assert [e["name"] for e in sub["entries"]] == ["笔记.md"], sub
    finally:
        httpd.shutdown()


def test_http_legacy_files_search_unchanged(tmp_path):
    """@ 取上下文仍走旧接口：递归、只要文件、name+path。"""
    ws = _make_ws(tmp_path)
    httpd, port, _app = _serve(ws)
    try:
        r = _get(port, "/api/files?q=")
        names = {f["name"] for f in r["files"]}
        assert "deep.py" in names, "旧接口应递归包含子目录文件"
        assert all("path" in f and "name" in f for f in r["files"])
        assert "secret.py" not in names, "忽略目录里的文件不该出现"

        hit = _get(port, "/api/files?q=" + urllib.parse.quote("deep"))
        # 旧接口用 str(relative_to()) —— Windows 上是反斜杠；树接口统一给 "/"。
        # 两种都能被 Path / 预览接口吃下，这里按「指向同一个文件」比较，不锁分隔符。
        assert [Path(f["path"]).as_posix() for f in hit["files"]] == ["src/inner/deep.py"]

        # 树给的 "/" 路径必须能直接拿去预览（否则点一下就是 404，树白做）
        tree = _get(port, "/api/files/tree?path=src")
        p = next(e["path"] for e in tree["entries"] if e["name"] == "main.py")
        assert p == "src/main.py"
        preview = _get(port, "/api/file?path=" + urllib.parse.quote(p))
        assert "print(1)" in json.dumps(preview, ensure_ascii=False), preview
    finally:
        httpd.shutdown()
