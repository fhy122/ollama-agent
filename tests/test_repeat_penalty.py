"""repeat_penalty / repeat_last_n 全链路回归（离线，不连 Ollama）。

判据（写功能的同时写「怎么才算对」）：
1. 默认 None = 完全不下发 —— 升级不改变任何既有输出行为；
2. 设了值就精确进 /api/chat 的 options；
3. OpenAI 兼容后端一个字段都不下发，切协议也不抹掉已存值；
4. 非法输入回落 None，不静默截断、不写 0 进去；
5. 0 / -1 是官方合法值；
6. profile 往返持久化后仍能原样恢复，旧配置文件不含新字段时补 None。

官方文档：
  https://docs.ollama.com/modelfile#valid-parameters-and-values
  https://github.com/ollama/ollama/blob/main/docs/api.md （POST /api/chat → options）
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import userconfig as uc  # noqa: E402
from agent.backends import OllamaClient, OpenAICompatClient  # noqa: E402
from agent.config import (AgentConfig, parse_repeat_last_n,  # noqa: E402
                          parse_repeat_penalty)
from webui.session import AgentSession  # noqa: E402

HIST = [{"role": "user", "content": "hi"}]
OPENAI_HOST = "https://relay.example/v1"


def _cfg(**kw) -> AgentConfig:
    base = dict(host="http://localhost:11434", model="m", api_key="")
    base.update(kw)
    return AgentConfig(**base)


def _options(**kw) -> dict:
    return OllamaClient(_cfg(**kw)).build_payload(HIST)["options"]


# ---------- 1 / 2 / 5：下发契约 ----------

def test_default_does_not_send_anything():
    """没设过就一个字段都不写 —— 零行为变化是这次改动的前提。"""
    opts = _options()
    assert "repeat_penalty" not in opts and "repeat_last_n" not in opts, opts
    assert opts == {"temperature": 0.7, "num_ctx": 65536}, opts


def test_explicit_values_land_in_options():
    opts = _options(repeat_penalty=1.1, repeat_last_n=256)
    assert opts["repeat_penalty"] == 1.1, opts
    assert opts["repeat_last_n"] == 256, opts


def test_zero_and_minus_one_are_valid():
    """官方文档：0 = 禁用、-1 = num_ctx，都不能被当成「没设」。"""
    for n in (0, -1):
        opts = _options(repeat_penalty=1.1, repeat_last_n=n)
        assert opts["repeat_last_n"] == n, (n, opts)


def test_openai_backend_never_sends_ollama_options():
    cli = OpenAICompatClient(_cfg(provider="openai", host=OPENAI_HOST,
                                  api_key="sk-x", repeat_penalty=1.1,
                                  repeat_last_n=256))
    payload = cli.build_payload(HIST)
    assert "options" not in payload, payload
    assert cli.supports_repeat_penalty is False


def test_capability_flags():
    assert OllamaClient(_cfg()).supports_repeat_penalty is True
    assert OpenAICompatClient(_cfg(provider="openai")).supports_repeat_penalty is False


# ---------- 4：输入校验 ----------

def test_parse_repeat_penalty():
    assert parse_repeat_penalty("1.1") == 1.1
    assert parse_repeat_penalty(None) is None
    for bad in ("", "  ", "abc", 0, -1, True, float("nan"), float("inf"), "1.5x"):
        assert parse_repeat_penalty(bad) is None, bad


def test_parse_repeat_last_n():
    assert parse_repeat_last_n("256") == 256
    assert parse_repeat_last_n(0) == 0
    assert parse_repeat_last_n(-1) == -1
    for bad in ("", "abc", 1.5, -2, True, float("nan"), None):
        assert parse_repeat_last_n(bad) is None, bad


def _make_session():
    """只驱动配置逻辑的 session，绕开真实连接（同 test_capabilities.py）。"""
    s = AgentSession.__new__(AgentSession)
    s.lock = threading.RLock()
    s.cfg = _cfg(provider="ollama")
    s.client = OllamaClient(s.cfg)
    s.workspace = Path(".").resolve()
    s.confirm = True
    s.ctx = type("Ctx", (), {"confirm": True, "workspace": s.workspace})()
    s.turns, s.history, s.sid, s.title = [], [], "", ""
    s.created = time.time()
    return s


def test_update_config_applies_to_both_cfg_and_client():
    s = _make_session()
    s.update_config({"repeat_penalty": 1.1, "repeat_last_n": 256})
    assert s.cfg.repeat_penalty == 1.1 and s.client.repeat_penalty == 1.1
    assert s.cfg.repeat_last_n == 256 and s.client.repeat_last_n == 256
    assert s.snapshot()["repeat_penalty"] == 1.1
    assert s.snapshot()["caps"]["repeat_penalty"] is True


def test_invalid_patch_falls_back_to_none():
    s = _make_session()
    s.update_config({"repeat_penalty": 1.1, "repeat_last_n": 256})
    s.update_config({"repeat_penalty": "oops", "repeat_last_n": 1.5})
    assert s.cfg.repeat_penalty is None and s.cfg.repeat_last_n is None
    assert "repeat_penalty" not in s.client.build_payload(HIST)["options"]


# ---------- 3：协议切换的既有坑 ----------

def test_switch_to_openai_keeps_saved_values():
    """切到 OpenAI 兼容协议时不许把已存的 1.1 抹掉（同 num_ctx 的老坑）。"""
    s = _make_session()
    s.update_config({"repeat_penalty": 1.1, "repeat_last_n": 256})
    s.update_config({"provider": "openai", "host": OPENAI_HOST, "model": "x",
                     "repeat_penalty": None, "repeat_last_n": None})
    assert s.cfg.repeat_penalty == 1.1, s.cfg
    assert s.cfg.repeat_last_n == 256
    snap = s.snapshot()
    assert snap["repeat_penalty"] is None and snap["caps"]["repeat_penalty"] is False

    s.update_config({"provider": "ollama", "host": "http://localhost:11434",
                     "model": "m"})
    assert s.snapshot()["repeat_penalty"] == 1.1, "切回 Ollama 后原值必须还在"


# ---------- 6：持久化 ----------

def test_profile_roundtrip(tmp_path):
    st = uc.ConfigStore(tmp_path / "config.json")
    data = st.load()
    row = st.upsert_profile(data, {
        "name": "本地", "provider": "ollama", "model": "m",
        "repeat_penalty": 1.1, "repeat_last_n": 256,
    })
    st.save(data)
    again = uc.ConfigStore(st.path).load()
    got = uc.ConfigStore(st.path).get_profile(again, row["id"])
    assert got["repeat_penalty"] == 1.1 and got["repeat_last_n"] == 256, got
    patch = uc.to_agent_patch(got, again.get("ui") or {})
    assert patch["repeat_penalty"] == 1.1 and patch["repeat_last_n"] == 256


def test_old_profile_without_new_fields_gets_none(tmp_path):
    """旧配置文件没有这两个字段 → 补 None，不能凭空造一个惩罚值出来。"""
    st = uc.ConfigStore(tmp_path / "old.json")
    st.save({"version": 1, "active": "p-old", "profiles": [
        {"id": "p-old", "name": "旧", "provider": "ollama", "model": "m"}]})
    got = uc.ConfigStore(st.path).get_profile(uc.ConfigStore(st.path).load(), "p-old")
    assert got["repeat_penalty"] is None and got["repeat_last_n"] is None, got
