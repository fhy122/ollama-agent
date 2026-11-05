"""测试隔离：把所有落到磁盘的默认路径重定向到临时目录。

教训来源：某次没有隔离时，`AgentSession()` / `ConfigStore()` / `SessionStore()`
走默认路径，把测试里的占位数据（如 `relay.example/v1`）写进了真实的
`~/.ollama-agent/config.json`，污染了用户正在用的配置，进而导致重启后
后端连不上（DNS 解析失败）。

隔离靠两个环境变量（见 `agent/userconfig.py:ENV_CONFIG` 与
`webui/store.py:ENV_ROOT`），它们在 `ConfigStore` / `SessionStore` 的
`__init__` 里读取，所以每个测试开始前置好即可，无需改业务代码。
"""

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolate_user_dirs(tmp_path_factory):
    """把配置与会话的默认根目录指到临时目录，测试绝不碰真实 ~/.ollama-agent。"""
    base = tmp_path_factory.mktemp("ollama-agent-test")
    cfg = base / "config.json"
    sessions = base / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    old = {
        "OLLAMA_AGENT_CONFIG": os.environ.get("OLLAMA_AGENT_CONFIG"),
        "OLLAMA_AGENT_SESSIONS": os.environ.get("OLLAMA_AGENT_SESSIONS"),
    }
    os.environ["OLLAMA_AGENT_CONFIG"] = str(cfg)
    os.environ["OLLAMA_AGENT_SESSIONS"] = str(sessions)
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
