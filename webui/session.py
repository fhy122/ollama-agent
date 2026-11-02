"""Web UI 会话状态：配置 + 客户端 + 历史 + 剪贴板暂存，线程安全。"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from agent.client import OllamaClient
from agent.config import THINK_VALUES, AgentConfig
from agent.loop import new_history, run_turn
from agent.registry import ToolContext
from webui.store import UNTITLED, SessionStore, make_title, new_id

# 后端能力清单，随 /api/config 下发。前端据此判断「页面是新版、后端还是旧进程」：
# 少一项就提示重启，而不是把「接口 404」当成「没搜到」。
FEATURES = (
    "sessions",          # 多会话留存与切换
    "sessions.search",   # 对话全文搜索
    "sessions.reset",    # 清空当前会话（连存档一起删）
    "files.search",      # 工作空间文件搜索
    "file.preview",      # 文件预览 / 下载
)


def _same_ws(a, b) -> bool:
    """两个工作空间是不是同一个目录（容忍分隔符、末尾斜杠、相对路径写法差异）。"""
    try:
        return Path(str(a)).resolve() == Path(str(b)).resolve()
    except OSError:
        return str(a).replace("\\", "/").rstrip("/") == str(b).replace("\\", "/").rstrip("/")


class RecordingEmitter:
    """把回合事件转发给真实 emitter，同时记进列表（给会话落盘用）。

    text / think 是逐 token 推的，连续同类事件合并成一条，否则一次回答能塞几千个事件。
    confirm 不记录：确认请求是瞬时的，回放出来只会变成一串永远没人应答的弹窗。
    """

    def __init__(self, inner, events: list[dict]):
        self._inner = inner
        self._events = events

    def _push(self, obj: dict) -> None:
        ev = self._events
        if obj["type"] in ("text", "think") and ev and ev[-1].get("type") == obj["type"]:
            ev[-1]["text"] = ev[-1].get("text", "") + obj.get("text", "")
        else:
            ev.append(obj)

    def step(self, n: int) -> None:
        self._push({"type": "step", "n": n})
        self._inner.step(n)

    def text(self, t: str) -> None:
        self._push({"type": "text", "text": t})
        self._inner.text(t)

    def think(self, t: str) -> None:
        self._push({"type": "think", "text": t})
        self._inner.think(t)

    def newline(self) -> None:
        self._inner.newline()

    def tool_call(self, name: str, args: dict) -> None:
        self._push({"type": "tool_call", "name": name, "args": args})
        self._inner.tool_call(name, args)

    def tool_result(self, name: str, text: str, dt: float,
                    images: list[str] | None = None) -> None:
        self._push({"type": "tool_result", "name": name, "text": text,
                    "dt": round(dt, 2), "images": images or []})
        self._inner.tool_result(name, text, dt, images)

    def confirm(self, cid: str, question: str) -> None:
        self._inner.confirm(cid, question)

    def stats(self, st: dict) -> None:
        if st:
            self._push({"type": "stats",
                        "prompt_tokens": st.get("prompt_eval_count"),
                        "gen_tokens": st.get("eval_count"),
                        "duration": round((st.get("total_duration") or 0) / 1e9, 2)})
        self._inner.stats(st)

    def notice(self, text: str) -> None:
        self._push({"type": "notice", "text": text})
        self._inner.notice(text)



class ConfirmGate:
    """工具执行前的人工确认闸（网页弹窗）。

    回合线程调 ask() 发出问题后阻塞；浏览器点按钮 → HTTP 线程调 answer() 放行。
    answer 不取会话大锁，避免与持锁等确认的回合线程互锁。
    超时未确认 = 自动拒绝（fail-safe，宁可不执行）。
    """

    timeout: float = 300.0  # 秒；超时自动拒绝

    def __init__(self):
        self._lock = threading.Lock()
        self._pending: dict[str, dict] = {}  # id -> {"ok": bool, "ev": Event}
        self._seq = 0
        self.allow_rest = False  # 「本回合全部允许」快捷开关，每回合开头重置
        self.last_timeout = False

    def reset(self) -> None:
        with self._lock:
            self.allow_rest = False

    def ask(self, what: str, notify) -> bool:
        """notify(cid, question) 把确认请求推给前端；返回是否放行。"""
        self.last_timeout = False
        with self._lock:
            if self.allow_rest:
                return True
            self._seq += 1
            cid = f"c{self._seq}"
            entry = {"ok": False, "ev": threading.Event()}
            self._pending[cid] = entry
        try:
            notify(cid, what)
        except Exception:
            with self._lock:
                self._pending.pop(cid, None)
            return False
        finished = entry["ev"].wait(self.timeout)
        with self._lock:
            self._pending.pop(cid, None)
            if not finished:
                self.last_timeout = True
                return False
            return entry["ok"]

    def answer(self, cid: str, approved: bool, allow_rest: bool = False) -> bool:
        """应答一次确认。返回 False = id 不存在（已超时/已应答/过期）。"""
        with self._lock:
            entry = self._pending.get(cid)
            if entry is None:
                return False
            entry["ok"] = bool(approved)
            if approved and allow_rest:
                self.allow_rest = True
            entry["ev"].set()
            return True

    def reject_all(self) -> None:
        """打断回合时唤醒所有等待中的确认（一律按拒绝处理）。"""
        with self._lock:
            entries = list(self._pending.values())
            self._pending.clear()
            self.allow_rest = False
        for e in entries:
            e["ok"] = False
            e["ev"].set()


class AgentSession:
    """一个对话会话。HTTP 线程通过 lock 串行跑回合；同一时刻只打开一条对话。"""

    def __init__(self, cfg: AgentConfig, store: SessionStore | None = None):
        self.cfg = cfg
        self.client = OllamaClient(cfg)
        self.workspace = Path(cfg.workspace) if cfg.workspace else Path.cwd()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.confirm = True  # 安全口默认开：写/删/联网工具执行前人工确认（设置面板可关）
        self.gate = ConfirmGate()
        self.ctx = ToolContext(
            confirm=self.confirm,
            use_color=False,
            ask=None,  # run() 时注入带流式通知的 ask
            workspace=self.workspace,
        )
        self.history = new_history(str(self.workspace))
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()   # 打断开关：置位即停当前回合
        self.pending_clip: "queue.Queue[dict]" = queue.Queue()  # 热键剪贴板暂存

        # ---- 会话持久化 ----
        self.store = store or SessionStore()
        self.sid = ""
        self.title = UNTITLED
        self.created = time.time()
        self.turns: list[dict] = []
        self._resume_or_create()

    # ---------- 会话管理 ----------

    def _resume_or_create(self) -> None:
        """启动时接上最近一次对话（工作空间变了就不接，避免历史里的路径对不上）。"""
        try:
            self.store.purge_empty()   # 清掉「点了新对话没说话」的空壳
        except Exception:
            pass
        for meta in self.store.list():
            data = self.store.load(meta["id"])
            if data and _same_ws(data.get("workspace") or "", self.workspace) \
                    and isinstance(data.get("history"), list) and data["history"]:
                self._apply(data)
                return
        self.new_session(save=False)

    def _apply(self, data: dict) -> None:
        self.sid = str(data["id"])
        self.title = str(data.get("title") or UNTITLED)
        self.created = float(data.get("created") or time.time())
        self.turns = list(data.get("turns") or [])
        hist = data.get("history")
        self.history = hist if isinstance(hist, list) and hist else new_history(str(self.workspace))

    def _save(self) -> None:
        if not self.sid:
            return
        try:
            self.store.save({
                "id": self.sid,
                "title": self.title,
                "created": self.created,
                "updated": time.time(),
                "workspace": str(self.workspace),
                "model": self.cfg.model,
                "history": self.history,
                "turns": self.turns,
            })
        except Exception:
            pass      # 落盘失败不能把回合结果吐掉

    def _is_empty(self) -> bool:
        return not self.turns and len(self.history) <= 1

    def list_sessions(self) -> list[dict]:
        return self.store.list()

    def search_sessions(self, query: str) -> list[dict]:
        """搜历史对话正文（问过什么、答过什么、调过什么工具都能搜到）。"""
        return self.store.search(query)

    def current(self) -> dict:
        """当前会话（含回放用的 turns）。"""
        return {"id": self.sid, "title": self.title, "workspace": str(self.workspace),
                "model": self.cfg.model, "turns": self.turns}

    def _busy(self) -> bool:
        return self.lock.locked()

    def open_session(self, sid: str) -> dict:
        """切换到另一条对话。正在生成时拒绝（回合线程持锁）。"""
        if self._busy():
            return {"error": "正在生成，先停止本轮回答再切换对话", "code": 409}
        with self.lock:
            data = self.store.load(sid)
            if not data:
                return {"error": "对话不存在或已被删除", "code": 404}
            self.cancel_event.clear()
            self._apply(data)
            if data.get("workspace") and Path(str(data["workspace"])).is_dir() \
                    and not _same_ws(data["workspace"], self.workspace):
                # 换回旧对话时跟着它的工作空间走，历史里的相对路径才对得上
                self.workspace = Path(str(data["workspace"]))
                self.ctx.workspace = self.workspace
                self.cfg.workspace = str(self.workspace)
            return self.current()

    def new_session(self, save: bool = True) -> dict:
        """开一条新对话。当前对话还是空的就直接复用，免得列表里堆一串空「新对话」。"""
        if self._busy():
            return {"error": "正在生成，先停止本轮回答再新建对话", "code": 409}
        with self.lock:
            if save and self._is_empty():
                self.history = new_history(str(self.workspace))
                self.turns = []
                self.title = UNTITLED
                if not self.sid:
                    self.sid = new_id()
                self._save()
                return self.current()
            self.sid = new_id() if save else ""
            self.title = UNTITLED
            self.created = time.time()
            self.turns = []
            self.history = new_history(str(self.workspace))
            self.cancel_event.clear()
            if save:
                self._save()
            return self.current()

    def delete_session(self, sid: str) -> dict:
        if self._busy():
            return {"error": "正在生成，先停止本轮回答再删除对话", "code": 409}
        ok = self.store.delete(sid)
        if sid == self.sid:
            with self.lock:
                self.sid = ""
                self.turns = []
                self.title = UNTITLED
                self.created = time.time()
                self.history = new_history(str(self.workspace))
        return {"ok": ok, "sessions": self.list_sessions(), "current": self.current()}

    # ---------- 对话 ----------

    def run(self, text: str, images: list[str] | None, emitter,
            files: list[str] | None = None, display: str | None = None) -> str:
        with self.lock:
            if not self.sid:
                self.sid = new_id()      # 空壳会话在第一次真正提问时才落盘
                self.created = time.time()
            self.gate.reset()
            self.cancel_event.clear()
            self.ctx.confirm = self.confirm
            self.ctx.ask = lambda what: self._ask(what, emitter)
            user_turn = {
                "role": "user",
                "text": display if display is not None else text,
                "files": list(files or []),
                "imgs": len(images or []),
            }
            events: list[dict] = []
            self.turns.extend([user_turn, {"role": "assistant", "events": events}])
            rec = RecordingEmitter(emitter, events)
            try:
                return run_turn(self.client, self.cfg, self.ctx, self.history,
                                text, images, emitter=rec,
                                cancel=self.cancel_event)
            finally:
                self.ctx.ask = None
                if self.title in ("", UNTITLED):
                    self.title = make_title(user_turn["text"])
                self._save()


    def request_stop(self) -> None:
        """打断当前回合（HTTP 线程调用，不取会话锁，可与持锁的回合线程并行）。

        四件事：停流式生成、**掐断 HTTP 连接**、唤醒并拒绝所有待确认工具、
        跳过后续工具步骤。

        掐连接这一步不能省：长上下文 prefill 期间 Ollama 不吐任何数据，
        读循环阻塞在 `for raw in resp` 上，cancel_event 传到不进去。
        只置 event 的话，用户点「停止」后要干等 prefill 跑完才生效，
        而会话锁一直没释放，界面上新建/切换对话都会被 409 挡回。
        """
        self.cancel_event.set()
        try:
            self.client.abort()          # 立刻断开 → 阻塞的读循环抛异常返回
        except Exception:
            pass
        self.gate.reject_all()

    def _ask(self, what: str, emitter) -> bool:
        ok = self.gate.ask(what, lambda cid, q: emitter.confirm(cid, q))
        if not ok and self.gate.last_timeout:
            emitter.notice("[确认超时] 未在限定时间内确认，已自动拒绝执行")
        return ok

    def answer_confirm(self, cid: str, approved: bool, allow_rest: bool = False) -> bool:
        """应答确认请求（HTTP 线程调用，不取会话锁）。"""
        return self.gate.answer(cid, approved, allow_rest)

    def load_skill(self, name: str) -> dict:
        """加载技能并注入历史（等价 CLI /skill）。返回 {ok, chars|error}。"""
        from agent.registry import dispatch
        with self.lock:
            res = dispatch("skill", {"name": name}, self.ctx)
            text = res.text
            if text.startswith("[错误]") or text.startswith("[提示]"):
                return {"ok": False, "error": text}
            self.history.append({
                "role": "user",
                "content": f"[技能指令已加载，后续回答请严格遵循此技能]\n{text}",
            })
            return {"ok": True, "name": name, "chars": len(text)}

    def reset(self) -> dict:
        """清空当前这条对话（内容 + 存档一起删掉），其他对话不受影响。

        这里以前只清内容、把空壳留在磁盘：空会话不进列表，于是界面上那条「消失」了，
        磁盘上却留着一份永远不会被打开的文件；再从右上角点一下，看着就像「清空全部」。
        现在直接删掉本条并回到一条空白新对话，列表里少了哪一条一目了然。
        """
        if self._busy():
            return {"error": "正在生成，先停止本轮回答再清空对话", "code": 409}
        with self.lock:
            old = self.sid
            self.sid = ""
            self.history = new_history(str(self.workspace))
            self.turns = []
            self.title = UNTITLED
            self.created = time.time()
            self.cancel_event.clear()
            if old:
                self.store.delete(old)
        return {"ok": True, "current": self.current(),
                "sessions": self.list_sessions()}

    # ---------- 配置热更新 ----------

    def update_config(self, patch: dict) -> dict:
        """应用前端设置补丁，返回生效后的配置。工作空间变更会重置历史。"""
        with self.lock:
            if "model" in patch and patch["model"]:
                # switch_model 会先把旧模型从显存卸掉（4GB 卡上两个模型并集会 OOM）
                self.client.switch_model(str(patch["model"]))
            if "think" in patch and patch["think"] in THINK_VALUES:
                self.cfg.think = patch["think"]
                self.client.think = THINK_VALUES[self.cfg.think]
            if "num_ctx" in patch:
                try:
                    n = int(patch["num_ctx"])
                except (TypeError, ValueError):
                    n = 0
                self.cfg.num_ctx = n if n > 0 else None
                self.client.num_ctx = self.cfg.num_ctx
            if "temperature" in patch:
                try:
                    self.cfg.temperature = float(patch["temperature"])
                    self.client.temperature = self.cfg.temperature
                except (TypeError, ValueError):
                    pass
            if "max_steps" in patch:
                try:
                    self.cfg.max_steps = max(1, int(patch["max_steps"]))
                except (TypeError, ValueError):
                    pass
            if "confirm" in patch:
                self.confirm = bool(patch["confirm"])
                self.ctx.confirm = self.confirm
            if "workspace" in patch and str(patch["workspace"]).strip():
                p = Path(str(patch["workspace"])).expanduser().resolve()
                if p.is_dir() and str(p) != str(self.workspace):
                    # 换工作空间 = 换上下文：开新对话，旧对话留在历史列表里
                    self.workspace = p
                    self.ctx.workspace = p
                    self.cfg.workspace = str(p)
                    self.sid = ""
                    self.title = UNTITLED
                    self.created = time.time()
                    self.turns = []
                    self.history = new_history(str(p))
        return self.snapshot()

    def snapshot(self) -> dict:
        """当前配置快照（给前端设置面板）。"""
        return {
            "model": self.cfg.model,
            "think": self.cfg.think,
            "num_ctx": self.cfg.num_ctx,
            "temperature": self.cfg.temperature,
            "max_steps": self.cfg.max_steps,
            "confirm": self.confirm,
            "workspace": str(self.workspace),
            "host": self.cfg.host,
            "features": list(FEATURES),
        }

    # ---------- 剪贴板暂存（全局热键 Alt+V） ----------

    def push_clip(self, item: dict) -> None:
        self.pending_clip.put(item)

    def drain_clip(self) -> list[dict]:
        out = []
        while True:
            try:
                out.append(self.pending_clip.get_nowait())
            except queue.Empty:
                return out
