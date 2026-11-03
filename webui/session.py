"""Web UI 会话状态：配置 + 客户端 + 历史 + 剪贴板暂存，线程安全。"""
from __future__ import annotations

import queue
import threading
import time
from pathlib import Path

from agent.backends import create_client
from agent.config import DEFAULT_HOST, MAX_CTX, THINK_VALUES, AgentConfig
from agent.loop import new_history, run_turn
from agent.registry import ToolContext
from agent.userconfig import BUILTIN_ID, ConfigStore, mask_key, to_agent_patch
from webui.store import UNTITLED, SessionStore, make_title, new_id

# 后端能力清单，随 /api/config 下发。前端据此判断「页面是新版、后端还是旧进程」：
# 少一项就提示重启，而不是把「接口 404」当成「没搜到」。
FEATURES = (
    "sessions",          # 多会话留存与切换
    "sessions.search",   # 对话全文搜索
    "sessions.reset",    # 清空当前会话（连存档一起删）
    "files.search",      # 工作空间文件搜索
    "file.preview",      # 文件预览 / 下载
    "provider.switch",   # 设置面板里切换 Ollama / OpenAI 兼容后端
    "models.profiles",   # 模型配置管理：多配置增删改 + 持久化
)


def _list_models_with_budget(client, budget: float) -> list[str]:
    """带超时预算地列模型；超时或失败返回空列表。

    urllib 的 timeout 覆盖不到所有阻塞阶段（DNS、TLS 握手），实测能挂到十几秒。
    这个函数被设置面板的同步路径调用，绝不能让人对着转圈 —— 超预算就放弃。
    """
    box: dict = {}

    def work():
        try:
            box["models"] = client.list_models()
        except Exception:
            box["models"] = []

    th = threading.Thread(target=work, daemon=True)
    th.start()
    th.join(budget)
    return box.get("models") or []


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
    thinking 不记录：那是每秒一次的心跳，一次长工具调用就能灌进几百条，
    回放对话时整屏刷「已等 N 秒」，只会把有用内容冲掉。
    """

    def __init__(self, inner, events: list[dict]):
        self._inner = inner
        self._events = events

    @property
    def think_hidden(self) -> bool:
        return bool(getattr(self._inner, "think_hidden", False))

    def _push(self, obj: dict) -> None:
        ev = self._events
        if obj["type"] in ("text", "think") and ev and ev[-1].get("type") == obj["type"]:
            ev[-1]["text"] = ev[-1].get("text", "") + obj.get("text", "")
        else:
            ev.append(obj)

    def step(self, n: int) -> None:
        self._push({"type": "step", "n": n})
        self._inner.step(n)

    def step_time(self, step: int, t_start: float, t_end: float,
                  ttft: float | None = None) -> None:
        obj: dict = {"type": "step_time", "step": step,
                     "t_start": int(t_start * 1000), "t_end": int(t_end * 1000),
                     "duration": round(t_end - t_start, 2)}
        if ttft is not None:
            obj["ttft"] = round(ttft, 2)
        self._push(obj)
        inner = getattr(self._inner, "step_time", None)
        if inner:
            inner(step, t_start, t_end, ttft=ttft)

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

    def user_message(self, text: str) -> None:
        self._push({"type": "user", "text": text})
        self._inner.user_message(text)

    def tool_result(self, name: str, text: str, dt: float,
                    images: list[str] | None = None) -> None:
        self._push({"type": "tool_result", "name": name, "text": text,
                    "dt": round(dt, 2), "images": images or []})
        self._inner.tool_result(name, text, dt, images)

    def confirm(self, cid: str, question: str) -> None:
        self._inner.confirm(cid, question)

    def stats(self, st: dict, step: int = 0) -> None:
        if st:
            self._push({"type": "stats", "step": step,
                        "prompt_tokens": st.get("prompt_eval_count"),
                        "gen_tokens": st.get("eval_count"),
                        "duration": round((st.get("total_duration") or 0) / 1e9, 2)})
        self._inner.stats(st, step)

    def thinking(self, elapsed: float, chars: int = 0) -> None:
        """生成参数期间的心跳：**只转发、不记录**。

        这个回调是每秒一次的，录进 _events 的话，一次 200 秒的工具调用会往历史里
        灌 200 条无用事件，回放对话时整屏都在刷「已等 N 秒」。回放只需要结果，
        不需要当时的等待过程。
        """
        inner = getattr(self._inner, "thinking", None)
        if inner:
            inner(elapsed, chars)

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

    def __init__(self, cfg: AgentConfig, store: SessionStore | None = None,
                 config_store: ConfigStore | None = None):
        # ---- 用户配置（模型配置持久化）----
        # 必须在造 client 之前读：要拿上次最后用的那个 profile 覆盖 cfg，
        # 否则重启后永远回到默认的 qwen3.5:9b，用户每次都要重填中转站地址和 Key。
        self.config_store = config_store or ConfigStore()
        self.user_cfg = self.config_store.load()
        self.cfg = cfg
        self._restore_profile()

        self.client = create_client(cfg)
        self.workspace = Path(cfg.workspace) if cfg.workspace else Path.cwd()
        self.workspace.mkdir(parents=True, exist_ok=True)
        # 安全口默认开：写/删/联网工具执行前人工确认（设置面板可关）。
        # 持久化在配置文件的 ui 段，不随模型配置切换而重置。
        self.confirm = bool(self.user_cfg.get("ui", {}).get("confirm", True))
        self.gate = ConfirmGate()
        self.ctx = ToolContext(
            confirm=self.confirm,
            use_color=False,
            ask=None,  # run() 时注入带流式通知的 ask
            workspace=self.workspace,
            cfg=self.cfg,
            client=self.client,   # 长文精读要自己发子请求；换后端时同步更新
        )
        self.history = new_history(str(self.workspace))
        self.lock = threading.Lock()
        self.cancel_event = threading.Event()   # 打断开关：置位即停当前回合
        self.pending_clip: "queue.Queue[dict]" = queue.Queue()  # 热键剪贴板暂存
        self._steer_q: "queue.Queue[str]" = queue.Queue()  # 输入队列「立即发送」的引导消息

        # ---- 会话持久化 ----
        self.store = store or SessionStore()
        self.sid = ""
        self.title = UNTITLED
        self.created = time.time()
        self.turns: list[dict] = []
        self._resume_or_create()

    # ---------- 模型配置（profile）管理 ----------

    def _restore_profile(self) -> None:
        """启动时把上次用的 profile 灌回 cfg。

        只覆盖「连哪个模型」相关的字段；workspace / confirm / max_steps 属于
        ui 段，已经在上面单独取过了，这里不重复搬运。

        profile 里 model 为空时不覆盖 cfg.model（保留构造时传进来的值），
        避免「配置文件里只有一条空壳记录」把 CLI 的 --model 参数吃掉。
        """
        pid = self.user_cfg.get("active") or ""
        if pid == BUILTIN_ID:
            # 内置 Ollama 不落盘成 profile：协议固定 ollama，
            # 地址沿用 cfg 默认（用户可以用「偏好」里的工作空间以外的东西改不到它，
            # 想连别人的 Ollama 就新建一条自定义配置）。
            self.cfg.provider = "ollama"
            return
        prof = self.config_store.get_profile(self.user_cfg, pid)
        if not prof:
            return
        self.cfg.provider = prof.get("provider") or self.cfg.provider
        if prof.get("host"):
            self.cfg.host = prof["host"]
        # 空 Key 也照写：用户可能故意把这个配置的 Key 清掉
        self.cfg.api_key = prof.get("api_key") or ""
        if prof.get("model"):
            self.cfg.model = prof["model"]
        think = prof.get("think")
        if think in THINK_VALUES:
            self.cfg.think = think
        ctx = prof.get("num_ctx")
        if isinstance(ctx, int) and not isinstance(ctx, bool) and ctx > 0:
            self.cfg.num_ctx = min(ctx, MAX_CTX)
        elif ctx in (None, 0):
            self.cfg.num_ctx = None
        try:
            self.cfg.temperature = float(prof.get("temperature", self.cfg.temperature))
        except (TypeError, ValueError):
            pass

    def profiles_view(self) -> dict:
        """给前端的模型配置清单：Key 一律打码，内置条目带实时模型列表。"""
        store = self.config_store
        cfg = self.user_cfg
        builtin_models: list[str] = []
        builtin_error = ""
        try:
            # 内置 Ollama 条目：模型列表实时从 /api/tags 读，不写进配置文件
            # （写进去就会过期：装一个新模型，列表还是旧的）。
            # 加超时预算：本地 Ollama 没启动时，DNS/连接阶段可能挂很久，
            # 而这个接口是打开设置面板必调的，不能让它拖住整个抽屉。
            from agent.backends.ollama import OllamaClient
            tmp = AgentConfig()
            tmp.provider = "ollama"
            builtin_models = _list_models_with_budget(OllamaClient(tmp), 4.0)
        except Exception as e:
            builtin_error = f"读不到本地 Ollama 模型列表：{e}"

        rows = []
        for p in cfg.get("profiles") or []:
            row = {k: p.get(k) for k in
                   ("id", "name", "provider", "host", "model",
                    "think", "num_ctx", "temperature")}
            key = p.get("api_key") or ""
            row["has_key"] = bool(key)
            row["api_key_masked"] = mask_key(key)   # 完整 Key 永不出本进程
            row["deletable"] = True                 # 内置条目不在这个列表里
            rows.append(row)
        return {
            "active": cfg.get("active") or "",
            "profiles": rows,
            "builtin": {
                "id": BUILTIN_ID,
                "name": "本地 Ollama",
                "active": cfg.get("active") == BUILTIN_ID,
                "model": self.cfg.model if self.cfg.resolved_provider == "ollama" else "",
                "host": self.cfg.host,
                "models": builtin_models,
                "error": builtin_error,
            },
            "config_path": str(store.path),
            "recovered": cfg.get("_recovered") or "",
        }

    def key_for(self, pid: str) -> str:
        """取某个 profile 的完整 Key（只给服务端内部用，绝不下发前端）。

        前端只拿得到打码值，所以「编辑已有配置时点测试」必然带着空 Key；
        这里按 id 回查已保存的那把。id 指不到时退回当前生效配置的 Key。
        """
        prof = self.config_store.get_profile(self.user_cfg, pid) if pid else None
        if not prof or not prof.get("api_key"):
            cur = self.config_store.get_profile(
                self.user_cfg, self.user_cfg.get("active") or "")
            prof = prof or cur
        return str((prof or {}).get("api_key") or "")

    def save_profile(self, patch: dict) -> dict:
        """新增或修改一个模型配置，并立刻生效。

        不测连通性：探测是独立接口（/api/models/probe），
        让用户在保存前先测一次，也允许「先存着回头再修」。

        锁的用法：这一段**只在改配置文件时持锁**，应用配置放到锁外。
        update_config 自己要拿同一把锁（threading.Lock 不可重入），
        持锁再调它就是自己锁死自己 —— 实测表现为请求永久挂住。
        """
        with self.lock:
            pid = str(patch.get("id") or "").strip()
            old = self.config_store.get_profile(self.user_cfg, pid) if pid else None
            row = dict(patch)
            if old:
                # Key 留空 = 不修改（前端只拿得到打码值，不可能回传完整 Key）
                if not str(patch.get("api_key") or "").strip():
                    row["api_key"] = old.get("api_key") or ""
            row = self.config_store.upsert_profile(self.user_cfg, row)
            if patch.get("activate", True):
                self.user_cfg["active"] = row["id"]
            self._persist()
            should_apply = self.user_cfg.get("active") == row["id"]
            saved_id = row["id"]
            created = old is None
        if should_apply:
            self._apply_profile(row)
        view = self.profiles_view()
        # 把刚落盘的 id 一并回给前端：不返回的话前端只能靠「名字/模型名猜」，
        # 新建后立刻再编辑或删除就会定位不到那一条（或误伤同名条目）。
        view["ok"] = True
        view["id"] = saved_id
        view["created"] = created
        return view

    def activate_profile(self, pid: str) -> dict:
        """切换到某个模型配置（地址/Key/模型名/推理参数一起换）。

        内置 Ollama 也走这条路：它不在 profiles 列表里，但要能切回去，
        否则用户一旦切到中转站就再也回不到本地模型（只能手改配置文件）。
        """
        with self.lock:
            if pid == BUILTIN_ID:
                self.user_cfg["active"] = BUILTIN_ID
                self._persist()
                prof = None
            else:
                prof = self.config_store.get_profile(self.user_cfg, pid)
                if not prof:
                    return {"error": "这个模型配置不存在，可能已被删除（刷新页面看看）"}
                self.user_cfg["active"] = pid
                self._persist()
        if prof:
            self._apply_profile(prof)      # 锁外应用，理由同上
        else:
            self._apply_builtin()          # 切回内置：协议换回 ollama
        return self.snapshot()

    def _apply_builtin(self) -> None:
        """切回内置 Ollama：只把协议改回 ollama，地址/模型沿用当前值。

        不重置 model 是有意的 —— 用户可能刚在本地模型列表里点了某个模型名，
        切回去就该还是那个，而不是被塞回写死的默认值。
        """
        patch = {
            "provider": "ollama",
            "host": self.cfg.host if self.cfg.resolved_provider == "ollama"
                    else DEFAULT_HOST,
            "api_key": "",
            "model": self.cfg.model,
            "workspace": "",
            "confirm": self.confirm,
        }
        self.update_config(patch)

    def delete_profile(self, pid: str) -> dict:
        with self.lock:
            r = self.config_store.delete_profile(self.user_cfg, pid)
            if not r.get("ok"):
                return {"error": r.get("error") or "删除失败"}
            self._persist()
            prof = self.config_store.get_profile(self.user_cfg, self.user_cfg["active"])
        if prof:
            self._apply_profile(prof)  # 锁外应用
        else:
            # 删掉的正好是生效项 → active 已回落内置，后端也得跟着换回 ollama，
            # 否则界面显示「本地 Ollama」而请求还发往中转站。
            self._apply_builtin()
        return self.profiles_view()

    def _apply_profile(self, prof: dict) -> None:
        """把一条 profile 应用到运行中的 cfg 并重建后端。

        走 update_config 而不是自己逐字段赋值：换后端要重建 client 实例
        （协议不同，类也不同），这套判断只该有一处。
        **调用方不能持有 self.lock** —— update_config 内部会拿它。
        """
        patch = to_agent_patch(prof, self.user_cfg.get("ui") or {})
        patch["workspace"] = ""        # workspace 由 ui 段管，别在这里二次触发
        patch["confirm"] = self.confirm  # 同上，保持当前值
        self.update_config(patch)

    def _persist(self) -> None:
        """写盘。失败时不能静默 —— 用户以为存了，重启却没了。"""
        try:
            self.config_store.save(self.user_cfg)
        except OSError as e:
            # 记进 last_error，由 snapshot 下发给前端弹提示
            self._persist_error = f"配置保存失败（{self.config_store.path}）：{e}"

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
            self._drain_steer()          # 换对话了，旧队列里排着的引导不再适用
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
        self._drain_steer()              # 删的是当前对话时队列同步作废
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
                                cancel=self.cancel_event, steer=self._drain_steer)
            finally:
                self.ctx.ask = None
                if self.title in ("", UNTITLED):
                    self.title = make_title(user_turn["text"])
                self._save()


    def _drain_steer(self) -> list[str]:
        """回合线程在步边界调用：取走全部待注入的引导消息。"""
        out: list[str] = []
        while True:
            try:
                out.append(self._steer_q.get_nowait())
            except queue.Empty:
                return out

    def steer(self, text: str) -> dict:
        """输入队列「立即发送」：消息注入运行中的回合（步边界生效），不打断模型。

        HTTP 线程调用，不取会话锁（回合线程正持锁）。空闲时返回 idle，
        前端回退为普通发送。
        """
        text = (text or "").strip()
        if not text:
            return {"error": "空消息", "code": 400}
        if not self._busy():
            return {"error": "idle", "code": 409}
        self._steer_q.put(text)
        return {"ok": True}

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
        self._drain_steer()              # 滞留的引导消息随回合一起作废
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
            # 换后端（本地 Ollama <-> 中转站）：地址/协议/Key 一起换。
            # 协议变了就必须重建 client —— 它现在是 ChatClient 的具体实现类，
            # 不是「一个带 host 属性的对象」了。只改 host 会让 OllamaClient
            # 继续按 /api/chat 打一个 OpenAI 兼容站，必挂。
            if "provider" in patch or "host" in patch or "api_key" in patch:
                old_provider = self.client.name
                if patch.get("provider") in ("auto", "ollama", "openai"):
                    self.cfg.provider = patch["provider"]
                if patch.get("host"):
                    self.cfg.host = str(patch["host"]).strip()
                if "api_key" in patch:
                    # 空串 = 用户没填（保持原样）；要清空得显式传 __CLEAR__
                    k = str(patch.get("api_key") or "").strip()
                    if k == "__CLEAR__":
                        self.cfg.api_key = ""
                    elif k and k != "__KEEP__":
                        self.cfg.api_key = k
                self._rebuild_client()
                if self.client.name != old_provider:
                    # 两端模型名不是一个集合，换完留旧名字只会让人点一下报错
                    self.cfg.model = ""
                    self.client.model = ""
            if "model" in patch and patch["model"]:
                # switch_model 会先把旧模型从显存卸掉（4GB 卡上两个模型并集会 OOM）
                self.client.switch_model(str(patch["model"]))
            if "think" in patch and patch["think"] in THINK_VALUES:
                # 只有当前协议真的下发这个字段时才写配置。OpenAI 兼容后端
                # 会把 think 静默丢弃，此时前端那个默认 "off" 不该覆盖
                # 用户给 Ollama 设过的值 —— 否则切一次中转站再切回来，
                # 用户的 think 设置就没了。
                if getattr(self.client, "supports_think", False):
                    self.cfg.think = patch["think"]
                    if hasattr(self.client, "think"):
                        self.client.think = THINK_VALUES[self.cfg.think]
            if "num_ctx" in patch:
                # 同上：不支持的协议不能把 num_ctx 抹成 None。
                # 前端在 OpenAI 模式下会发 0（输入框被禁用但仍有值），
                # 直接写就会把保存的 65536 清掉。
                if getattr(self.client, "supports_num_ctx", False):
                    # bool 是 int 的子类，True 会被当成 1 —— 这不是用户想设的值。
                    # 浮点静默截断（1.5→1）同样是有害的：设了却不知道设成了什么。
                    # 这两种直接当非法处理，落到「用服务端默认」。
                    raw = patch["num_ctx"]
                    n = 0
                    if not isinstance(raw, bool):
                        try:
                            n = int(raw) if float(raw) == int(float(raw)) else 0
                        except (TypeError, ValueError):
                            n = 0
                    if n > MAX_CTX:
                        n = MAX_CTX
                    self.cfg.num_ctx = n if n > 0 else None
                    if hasattr(self.client, "num_ctx"):
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
            self._sync_user_cfg()
        return self.snapshot()

    def _sync_user_cfg(self) -> None:
        """把当前生效的配置回写进 user_cfg 并落盘。

        没有这一步，设置面板上改的参数据只在内存里活着 —— 重启回到默认值，
        用户会以为「设置根本没保存」。

        没有 user_cfg 时直接返回：单元测试会用 __new__ 造一个「只驱动配置逻辑」
        的 session（不碰磁盘、不建连接），那种实例没有配置文件可写。
        """
        cfg = getattr(self, "user_cfg", None)
        if not isinstance(cfg, dict):
            return
        pid = cfg.get("active") or ""
        if pid != BUILTIN_ID:
            # active 是内置 Ollama 时不回写任何 profile：
            # 内置条目由 /api/tags 实时提供，写进去只会造出一条会过期的假记录
            for p in cfg.get("profiles") or []:
                if p.get("id") != pid:
                    continue
                p["provider"] = self.cfg.resolved_provider
                p["host"] = self.cfg.host
                p["api_key"] = self.cfg.api_key
                p["model"] = self.cfg.model
                p["think"] = self.cfg.think
                p["num_ctx"] = self.cfg.num_ctx
                p["temperature"] = self.cfg.temperature
                break
        ui = cfg.setdefault("ui", {})
        ui["confirm"] = self.confirm
        ui["max_steps"] = self.cfg.max_steps
        ui["workspace"] = str(self.workspace)
        self._persist()

    def _rebuild_client(self) -> None:
        """按当前配置重建后端实例（协议可能变了，实例类也得跟着变）。

        ctx.client 必须同步换：read_long_doc 用它发逐段子请求，
        留着旧实例就会拿着上一个后端的地址/Key 去请求。
        """
        self.client = create_client(self.cfg)
        self.ctx.client = self.client

    def snapshot(self) -> dict:
        """当前配置快照（给前端设置面板）。

        num_ctx / think 只对 Ollama 协议有意义。OpenAI 兼容后端下发这两个字段会 400，
        所以这里一律回 None —— 让界面显式显示「不适用」，而不是继续挂着一个
        65536 让用户以为调过了。
        """
        cli = self.client
        ctx_ok = getattr(cli, "supports_num_ctx", False)
        think_ok = getattr(cli, "supports_think", False)
        return {
            "model": self.cfg.model,
            "think": self.cfg.think if think_ok else None,
            "num_ctx": self.cfg.num_ctx if ctx_ok else None,
            "caps": {
                "num_ctx": ctx_ok,
                "think": think_ok,
                "keep_alive": getattr(cli, "supports_keep_alive", False),
                "unload": getattr(cli, "supports_unload", False),
            },
            "temperature": self.cfg.temperature,
            "max_steps": self.cfg.max_steps,
            "confirm": self.confirm,
            "workspace": str(self.workspace),
            "host": self.cfg.host,
            "provider": self.cfg.resolved_provider,
            "provider_raw": self.cfg.provider,
            "has_key": bool(self.cfg.api_key),
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
