"""桌面通知的正文来源：/api/statuses 必须带 rev / title / preview。

前端拿到通知后要显示「谁、哪条对话、说了什么」，而 statuses 原本只有
busy/attention 两个布尔值——布尔值撑不起一条通知。这里钉住三件事：
  1. 摘要必须剥掉 markdown（通知里显示不了 ** 和 ```）；
  2. preview 只在 attention=True 时算（2.5s 轮询一次，别为没人在看的
     会话每轮拼一遍文本）；
  3. rev 是「第几轮」，前端靠它做去重签名——同一句话连说两轮也得弹两次
     两个不同的通知，用摘要当键会漏。
"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.config import AgentConfig
from agent.userconfig import ConfigStore
from webui.session import SessionHub, _plain_preview
from webui.store import SessionStore


class _Client:
    """立即回一段文本；gate 用来把回合卡在生成中。"""

    def __init__(self, reply: str):
        self.reply = reply
        self.gate = threading.Event()
        self.gate.set()

    def chat(self, messages, tools=None, on_text=None, on_think=None,
             cancel=None, on_wait=None, think_hidden=True):
        on_text(self.reply)
        self.gate.wait(timeout=10)
        return {"content": self.reply, "tool_calls": [], "stats": {},
                "cancelled": False, "ttft": 0.01}

    def abort(self):
        self.gate.set()

    @property
    def name(self):
        return "stub"

    def switch_model(self, m):
        pass


class _Null:
    think_hidden = False

    def __getattr__(self, name):
        return lambda *a, **k: None


def _hub(tmp_path):
    cfg = AgentConfig(use_color=False, warm=False, workspace=str(tmp_path))
    return SessionHub(cfg, store=SessionStore(tmp_path / "sessions"),
                      config_store=ConfigStore(tmp_path / "userconfig.json"))


def test_plain_preview_strips_markdown():
    src = ("## 结论\n**可以**，先 `pip install x`。\n\n---\n\n"
           "- 第一步：读配置\n- 第二步：见[文档](https://example.com)\n\n"
           "```python\nprint(1)\n```\n")
    out = _plain_preview(src)
    assert out == "结论 可以，先 pip install x。 第一步：读配置 第二步：见文档", out
    assert "```" not in out and "**" not in out and "---" not in out
    assert _plain_preview("") == ""
    assert _plain_preview("   \n\n ") == ""


def test_plain_preview_truncates_with_ellipsis():
    out = _plain_preview("字" * 300, limit=30)
    assert len(out) == 30 and out.endswith("…"), out


def test_statuses_expose_preview_for_background_done(tmp_path):
    """后台跑完：statuses 带标题、轮次和剥好的摘要（前端照着拼通知）。"""
    h = _hub(tmp_path)
    a = h._cur
    a.client = _Client("**搞定了**，见 `a.py`\n\n---\n\n## 下一步\n继续")
    a.client.gate.clear()     # 卡在生成中：必须等切走之后才放行，否则回合
                              # 在 new_session 之前就结束，attention 不会置位
    t = threading.Thread(target=lambda: h.run("干点活", None, _Null()), daemon=True)
    t.start()
    time.sleep(0.15)          # 让它进入生成
    h.new_session()           # 切走 → 该会话转后台
    a.client.gate.set()
    t.join(10)
    time.sleep(0.1)           # 等 finally 里的 on_turn_end 跑完

    st = h.statuses()[a.sid]
    assert st["attention"] is True, st
    assert st["busy"] is False, st
    assert st["rev"] == 1, st
    assert st["title"], st
    assert st["preview"] == "搞定了，见 a.py 下一步 继续", st["preview"]


def test_preview_empty_when_session_is_current(tmp_path):
    """当前会话（attention=False）不算摘要：省掉每轮无谓的文本拼接。"""
    h = _hub(tmp_path)
    a = h._cur
    a.client = _Client("回复内容")
    h.run("问题", None, _Null())
    st = h.statuses()[a.sid]
    assert st["attention"] is False
    assert st["preview"] == ""
    assert st["rev"] == 1


def test_preview_fallback_without_text(tmp_path):
    """只调工具、没文字：不能返回空，否则通知退化成一句「有新消息」。"""
    a = _hub(tmp_path)._cur
    a.turns = [{"role": "assistant",
                "events": [{"type": "tool_call", "name": "read_file", "args": {}}]}]
    assert a.last_reply_preview() == "（本轮只执行了工具，没有文字回复）"
    a.turns = [{"role": "assistant", "events": []}]
    assert a.last_reply_preview() == ""


def test_turn_rev_counts_user_turns(tmp_path):
    a = _hub(tmp_path)._cur
    a.turns = [{"role": "user", "text": "q1"}, {"role": "assistant", "events": []},
               {"role": "user", "text": "q2"}, {"role": "assistant", "events": []}]
    assert a.turn_rev() == 2
