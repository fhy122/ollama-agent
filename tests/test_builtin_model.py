"""内置 Ollama「上次选的模型」持久化回归（离线，不连 Ollama）。

背景：内置条目由 /api/tags 实时提供，不落盘成 profile；以前连「选了哪个模型」
也一起不记，导致每次进 Web UI 都显示启动参数 / DEFAULT_MODEL（qwen3.5:9b），
用户刚在模型列表里挑的模型一刷新就没了。

判据：
1. 全新配置没有 builtin_model → 空串，沿用默认模型；
2. 切换后 _sync_user_cfg 把名字落盘，重启后 _restore_profile 原样恢复；
3. 显式 --model 优先级高于持久化的选择；
4. 有自定义 profile 生效时，profile.model 才是权威，不被 builtin_model 顶掉；
5. 旧配置文件没有该字段时补空串，不凭空造模型名。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import userconfig as uc  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from webui.session import AgentSession  # noqa: E402


def _session(tmp_path, model="qwen3.5:9b", explicit=False):
    """只驱动配置读写逻辑的 session，绕开真实连接与 __init__。"""
    s = AgentSession.__new__(AgentSession)
    s.config_store = uc.ConfigStore(tmp_path / "config.json")
    s.user_cfg = s.config_store.load()
    s.cfg = AgentConfig(model=model)
    s.cfg._model_explicit = explicit
    s.workspace = tmp_path
    s.confirm = True
    return s


def test_fresh_config_keeps_boot_model(tmp_path):
    s = _session(tmp_path)
    assert s.user_cfg["builtin_model"] == "", s.user_cfg
    s._restore_profile()
    assert s.cfg.model == "qwen3.5:9b"


def test_switch_persists_and_restores(tmp_path):
    # 模拟前端点内置模型后 update_config → switch_model 改了内存态
    s = _session(tmp_path)
    s.cfg.model = "llama3.2:3b"
    s._sync_user_cfg()

    raw = uc.ConfigStore(s.config_store.path).load()
    assert raw["builtin_model"] == "llama3.2:3b", raw

    # 重启：新 session 仍从默认值起步，靠 _restore_profile 拉回上次选择
    s2 = _session(tmp_path)
    assert s2.cfg.model == "qwen3.5:9b", "构造时应是启动默认值"
    s2._restore_profile()
    assert s2.cfg.model == "llama3.2:3b"


def test_explicit_model_flag_wins(tmp_path):
    s = _session(tmp_path)
    s.cfg.model = "llama3.2:3b"
    s._sync_user_cfg()

    s2 = _session(tmp_path, model="my-model:latest", explicit=True)
    s2._restore_profile()
    assert s2.cfg.model == "my-model:latest"


def test_custom_profile_is_authoritative(tmp_path):
    st = uc.ConfigStore(tmp_path / "config.json")
    data = st.load()
    row = st.upsert_profile(data, {
        "name": "中转站", "provider": "openai", "host": "https://x/v1",
        "model": "deepseek-v4", "api_key": "k"})
    data["active"] = row["id"]
    data["builtin_model"] = "llama3.2:3b"     # 曾经选过的本地模型
    st.save(data)

    s = _session(tmp_path)
    s._restore_profile()
    assert s.cfg.model == "deepseek-v4", "生效的是 profile，别拿本地模型名盖掉"


def test_old_config_gets_empty_string(tmp_path):
    st = uc.ConfigStore(tmp_path / "old.json")
    st.save({"version": 1, "active": uc.BUILTIN_ID, "profiles": [], "ui": {}})
    assert st.load()["builtin_model"] == ""


def test_normalize_strips_builtin_model(tmp_path):
    st = uc.ConfigStore(tmp_path / "c.json")
    st.save({"version": 1, "active": uc.BUILTIN_ID, "profiles": [],
             "builtin_model": "  qwen2.5:7b  ", "ui": {}})
    assert st.load()["builtin_model"] == "qwen2.5:7b"
