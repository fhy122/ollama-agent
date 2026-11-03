"""会话持久化：一条对话一个 JSON 文件，默认放 ~/.ollama-agent/sessions/。

为什么在用户目录而不是工作空间：工作空间是「工具读写文件的根」，会随设置切换；
对话历史属于这个应用本身，跟着人走，不该被换目录清空。

文件结构：
{
  "id": "20261101-211500-a1b2",
  "title": "侧栏收起后无法展开",
  "created": 1762000000.0,
  "updated": 1762000100.0,
  "workspace": "D:/projectspace/ollama-agent",
  "model": "qwen3.5:9b",
  "history": [ {role, content, ...} ],       # 给模型看的完整上下文
  "turns":   [ {role:"user", ...}, {role:"assistant", events:[...]} ]   # 给界面回放
}

turns 里存的是流式事件（text/think/tool_call/tool_result/stats/notice），
前端按原样重放即可还原气泡、代码块、工具卡片，不用在服务端拼 HTML。
"""
from __future__ import annotations

import json
import os
import random
import re
import string
import threading
import time
from pathlib import Path

from agent import HIDDEN_DIRNAME

DEFAULT_ROOT = Path.home() / HIDDEN_DIRNAME / "sessions"
ENV_ROOT = "OLLAMA_AGENT_SESSIONS"

MAX_TURNS = 200          # 单个会话保留的最近轮次（防止文件无限长）
UNTITLED = "新对话"
_SAFE_ID = re.compile(r"[^0-9A-Za-z_-]")


def new_id() -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    tail = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{stamp}-{tail}"


def make_title(text: str, limit: int = 24) -> str:
    """用第一句提问当标题：去掉 markdown 噪音，压成一行。"""
    s = re.sub(r"```[\s\S]*?```", " ", str(text or ""))
    s = re.sub(r"[`*_#>\[\]|]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    if not s:
        return UNTITLED
    return s if len(s) <= limit else s[:limit].rstrip() + "…"


def _snippet(text: str, needle: str, width: int = 92) -> str:
    """从长文本里截一段命中处前后的内容，压成一行（给侧栏结果展示用）。"""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(s) <= width:
        return s
    i = s.lower().find(needle.lower())
    if i < 0:
        return s[:width].rstrip() + "…"
    half = max(18, (width - len(needle)) // 2)
    start = max(0, i - half)
    end = min(len(s), i + len(needle) + half)
    out = s[start:end].strip()
    return ("…" if start > 0 else "") + out + ("…" if end < len(s) else "")


def _same_line(snippet: str, title: str) -> bool:
    """片段是不是和标题几乎是同一句话（那就不必再显示一遍）。"""
    x = re.sub(r"[…\s]", "", str(snippet or ""))
    y = re.sub(r"[…\s]", "", str(title or ""))
    if not x or not y:
        return False
    if x == y:
        return True
    short, long_ = (x, y) if len(x) <= len(y) else (y, x)
    return short in long_ and len(short) >= len(long_) * 0.7


def _body_blocks(data: dict):
    """按优先级吐出会话正文里可搜的文本块：(字段标签, 文本)。

    不含标题——标题在结果行里已经单独展示并高亮，再当片段就是重复。
    """
    for t in data.get("turns") or []:
        if not isinstance(t, dict):
            continue
        if t.get("role") == "user":
            yield "提问", str(t.get("text") or "")
            for p in t.get("files") or []:
                yield "附件", str(p)
            continue
        for ev in t.get("events") or []:
            if not isinstance(ev, dict):
                continue
            kind = ev.get("type")
            if kind == "text":
                yield "回答", str(ev.get("text") or "")
            elif kind == "think":
                yield "思考", str(ev.get("text") or "")
            elif kind == "notice":
                yield "提示", str(ev.get("text") or "")
            elif kind in ("tool_call", "tool_result"):
                yield "工具", f"{ev.get('name') or ''} {ev.get('text') or ''} " \
                              f"{json.dumps(ev.get('args') or {}, ensure_ascii=False)}"


class SessionStore:
    """会话目录的读写。所有写操作走临时文件 + 替换，避免写一半留下坏 JSON。"""

    def __init__(self, root: str | Path | None = None):
        base = root or os.environ.get(ENV_ROOT) or DEFAULT_ROOT
        self.root = Path(base).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # ---------- 路径 ----------

    def path(self, sid: str) -> Path:
        safe = _SAFE_ID.sub("", str(sid or ""))
        if not safe:
            raise ValueError("非法会话 id")
        return self.root / f"{safe}.json"

    # ---------- 读 ----------

    def list(self) -> list[dict]:
        """按更新时间倒序返回元信息（不含 history/turns，列表用不到）。

        没有任何提问的会话不列出来：点了「新对话」却一个字没发就关掉，
        不该在历史里留一条空记录。
        """
        out: list[dict] = []
        for p in self.root.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue
            turns = data.get("turns") or []
            n = sum(1 for t in turns if t.get("role") == "user")
            if n <= 0:
                continue
            out.append({
                "id": data.get("id") or p.stem,
                "title": data.get("title") or UNTITLED,
                "updated": float(data.get("updated") or 0),
                "turns": n,
            })
        out.sort(key=lambda x: x["updated"], reverse=True)
        return out

    # ---------- 搜索 ----------

    def search(self, query: str, limit: int = 60) -> list[dict]:
        """全文搜索历史对话，返回带命中片段的元信息。

        规则：空格分隔的多个词要**全部**出现（大小写不敏感）；一个会话只出
        一条结果，片段取优先级最高的**正文**命中（提问 > 回答 > 思考 > 工具）。
        标题命中的会话**排在前面**（搜「你好」时，标题就叫「你好」的那条该是
        第一条，而不是正文里偶然提到过「你好」的别的对话），同组内按更新时间倒序。
        片段和标题几乎是同一句话时留空——标题那行已经高亮了，不必重复。
        没有提问的空会话本来就不在 list() 里，天然不参与搜索。
        """
        terms = [t for t in re.split(r"\s+", str(query or "").strip()) if t]
        if not terms:
            return []
        lows = [t.lower() for t in terms]
        needle = lows[0]
        out: list[dict] = []
        for meta in self.list():
            data = self.load(meta["id"])
            if not data:
                continue
            title = str(data.get("title") or "")
            title_hit = all(t in title.lower() for t in lows)
            best: tuple[str, str] | None = None
            count = 1 if title_hit else 0
            for field, text in _body_blocks(data):
                low = str(text).lower()
                if all(t in low for t in lows):
                    count += 1
                    if best is None:
                        best = (field, str(text))
            if best is None and not title_hit:
                continue
            field, text = best if best else ("标题", "")
            snippet = _snippet(text, needle) if text else ""
            if snippet and _same_line(snippet, title):
                snippet, field = "", "标题"
            out.append({
                **meta,
                "_title_hit": title_hit,   # 只为排序，返回前剔掉
                "field": field,
                "snippet": snippet,
                "hits": count,
            })
        out.sort(key=lambda x: (0 if x["_title_hit"] else 1, -x["updated"]))
        for x in out:
            x.pop("_title_hit", None)
        return out[:limit]

    def purge_empty(self) -> int:
        """删掉没有提问的空会话文件（启动时清一次，防止残留）。"""
        n = 0
        for p in self.root.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                turns = data.get("turns") or []
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(data, dict) and not any(t.get("role") == "user" for t in turns):
                with self._lock:
                    try:
                        p.unlink()
                        n += 1
                    except OSError:
                        pass
        return n

    def load(self, sid: str) -> dict | None:
        p = self.path(sid)
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        data.setdefault("id", p.stem)
        data.setdefault("title", UNTITLED)
        data.setdefault("history", [])
        data.setdefault("turns", [])
        return data

    # ---------- 写 ----------

    def save(self, data: dict) -> Path:
        sid = str(data.get("id") or new_id())
        data = dict(data, id=sid)
        turns = data.get("turns") or []
        data["turns"] = turns[-MAX_TURNS:]
        if not data.get("updated"):
            data["updated"] = time.time()
        p = self.path(sid)
        tmp = p.with_suffix(".json.tmp")
        payload = json.dumps(data, ensure_ascii=False, default=str)
        with self._lock:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, p)
        return p

    def create(self, *, workspace: str = "", model: str = "") -> dict:
        now = time.time()
        data = {
            "id": new_id(),
            "title": UNTITLED,
            "created": now,
            "updated": now,
            "workspace": workspace,
            "model": model,
            "history": [],
            "turns": [],
        }
        self.save(data)
        return data

    def delete(self, sid: str) -> bool:
        p = self.path(sid)
        with self._lock:
            try:
                p.unlink()
                return True
            except FileNotFoundError:
                return False
            except OSError:
                return False
