"""多会话并行（SessionHub）：每条对话独立实例各自跑回合，切换不 409。

设计参考 DeepSeek Harness：会话=独立 agent 实例；agent/status 迁移可查；
turn/end 在非当前会话上完成 → 打 attention 标记（列表通知徽标）。
"""
import threading
import time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import AgentConfig
from webui.session import SessionHub
from webui.store import SessionStore


class _GateClient:
    """每个会话一个实例；chat 卡在自己的 gate 上，测试分别放行。"""

    def __init__(self, cfg, tag=""):
        self.gate = threading.Event()
        self.calls = 0
        self.tag = tag

    def chat(self, messages, tools=None, on_text=None, on_think=None,
             cancel=None, on_wait=None, think_hidden=True):
        self.calls += 1
        on_text("答案-" + self.tag + str(self.calls))
        self.gate.wait(timeout=10)
        return {"content": "答案-" + self.tag + str(self.calls), "tool_calls": [],
                "stats": {}, "cancelled": False, "ttft": 0.01}

    def abort(self):
        self.gate.set()          # request_stop 调它打断阻塞

    @property
    def name(self):
        return "stub"

    def switch_model(self, m):
        pass


def _hub(tmp_path):
    from agent.userconfig import ConfigStore
    cfg = AgentConfig(use_color=False, warm=False, workspace=str(tmp_path))
    # 配置存到 tmp：不读本机真实配置（confirm 等偏好），也不写脏它
    cs = ConfigStore(tmp_path / "userconfig.json")
    h = SessionHub(cfg, store=SessionStore(tmp_path / "sessions"),
                   config_store=cs)
    return h


class _Null:
    think_hidden = False

    def __getattr__(self, name):
        return lambda *a, **k: None


def _run(h, text, client=None):
    return threading.Thread(
        target=lambda: h.run(text, None, _Null()), daemon=True)


def test_two_sessions_run_in_parallel(tmp_path):
    """两条对话同时生成：互不持锁、互不阻塞。"""
    h = _hub(tmp_path)
    a = h._cur             # 当前（主实例），client 是 _GateClient
    a.client = _GateClient(a.cfg, tag="A")

    t1 = _run(h, "问题一")
    t1.start()
    time.sleep(0.2)        # A 进入生成（持 A 的锁）

    # 并行核心：A 在跑时也能开新对话并立即发消息
    r = h.new_session()
    assert not r.get("error"), r
    b = h._cur
    assert b is not a, "新对话应是独立实例"
    b.client = _GateClient(b.cfg, tag="B")
    t2 = _run(h, "问题二")
    t2.start()
    time.sleep(0.2)

    assert a._busy() and b._busy(), "两条应同时在生成"
    st = h.statuses()
    assert st[a.sid]["busy"] and st[b.sid]["busy"], st

    # 放行两条，各自跑完互不影响
    a.client.gate.set()
    b.client.gate.set()
    t1.join(10); t2.join(10)
    assert not t1.is_alive() and not t2.is_alive()
    assert "答案-A1" in str(a.history[-1]["content"])
    assert "答案-B1" in str(b.history[-1]["content"])


def test_attention_marked_for_background_completion(tmp_path):
    """后台完成的会话打 attention；用户切过去看一眼就清掉。"""
    h = _hub(tmp_path)
    a = h._cur
    a.client = _GateClient(a.cfg, tag="A")
    t1 = _run(h, "慢活")
    t1.start()
    time.sleep(0.2)

    h.new_session()        # 切走，A 转后台
    assert h._cur is not a
    a.client.gate.set()
    t1.join(10)
    time.sleep(0.1)        # 等 finally 里的回调跑完
    st = h.statuses()
    assert st[a.sid]["attention"] is True, st
    assert st[a.sid]["busy"] is False, st

    # 打开它 → 通知清掉
    r = h.open_session(a.sid)
    assert r.get("error") is None
    assert h.statuses()[a.sid]["attention"] is False


def test_switch_to_running_session_reports_busy(tmp_path):
    """切到生成中的会话：放行且返回 busy=True（前端据此走 /api/live 接管）。"""
    h = _hub(tmp_path)
    a = h._cur
    a.client = _GateClient(a.cfg, tag="A")
    t1 = _run(h, "跑着")
    t1.start()
    time.sleep(0.2)

    h.new_session()                       # 切到新对话
    r = h.open_session(a.sid)             # 再切回 A：不 409
    assert r.get("error") is None, r
    assert r["busy"] is True, r
    assert h._cur is a
    a.client.gate.set()
    t1.join(10)


def test_delete_busy_background_session_rejected(tmp_path):
    """删后台正在生成的会话 → 409；跑完后可删。"""
    h = _hub(tmp_path)
    a = h._cur
    a.client = _GateClient(a.cfg, tag="A")
    t1 = _run(h, "慢活")
    t1.start()
    time.sleep(0.2)
    h.new_session()

    r = h.delete_session(a.sid)
    assert r.get("error") and r.get("code") == 409, r

    a.client.gate.set()
    t1.join(10)
    time.sleep(0.1)
    r = h.delete_session(a.sid)
    assert r.get("ok") is True, r


def test_config_shared_across_instances(tmp_path):
    """共享 user_cfg：主实例改偏好，并行实例可见（同一 dict）。"""
    h = _hub(tmp_path)
    a = h._cur
    h.update_config({"confirm": False})   # 走当前实例改（同步共享 user_cfg）
    assert a.confirm is False

    b = h._spawn("")                      # 新并行实例：读到关闭后的偏好
    assert b.confirm is False
    assert b.user_cfg is a.user_cfg, "共享同一 user_cfg dict"
