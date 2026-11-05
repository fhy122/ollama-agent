"""验证测试不污染真实配置。

回归测试：曾因缺少隔离，测试里的占位数据写进了真实 ~/.ollama-agent/config.json，
污染用户正在用的配置。conftest 把默认路径重定向到临时目录后，这里断言：
默认 ConfigStore / SessionStore 指向临时目录，而非真实 home 下的隐藏目录。
"""

import os

from agent.userconfig import ConfigStore
from webui.store import SessionStore


def test_config_store_default_points_to_tmp():
    # conftest 已把环境变量指到临时目录；这里只验证「默认路径确实被隔离」。
    store = ConfigStore()
    assert "ollama-agent-test" in str(store.path), store.path
    assert os.path.expanduser("~/.ollama-agent") not in str(store.path)


def test_session_store_default_points_to_tmp():
    store = SessionStore()
    assert "ollama-agent-test" in str(store.root), store.root
    assert os.path.expanduser("~/.ollama-agent") not in str(store.root)


def test_real_config_untouched_after_session_default(tmp_path):
    """用隔离后的默认 store 落一次盘，真实配置目录不应多出任何文件。"""
    real_root = os.path.expanduser("~/.ollama-agent")
    before = set(os.listdir(real_root)) if os.path.isdir(real_root) else set()

    store = ConfigStore()
    store.save({"version": 1, "active": "ollama-local", "profiles": []})

    after = set(os.listdir(real_root)) if os.path.isdir(real_root) else set()
    assert after == before, f"真实配置目录被测试改动：{after - before}"
