"""能力声明 + 配置不被越权覆盖的回归测试。

背景：UI 在 OpenAI 协议下把 num_ctx / think 置灰，但输入框里仍有残值
（ctx 显示 0）。如果这个 0 被提交上去，后端 `n if n > 0 else None` 会把
用户给 Ollama 存的 65536 清成 None —— 切一次中转站再切回来，设置就没了。
这里锁住「不支持的协议不许改这两个字段」。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.backends import OllamaClient, OpenAICompatClient, create_client  # noqa: E402
from agent.config import AgentConfig  # noqa: E402

OPENAI_HOST = "https://www.aigatehub.xyz/v1"


def _cfg(**kw) -> AgentConfig:
    base = dict(host="http://localhost:11434", model="m", api_key="")
    base.update(kw)
    return AgentConfig(**base)


def test_ollama_declares_all_capabilities():
    cli = OllamaClient(_cfg(provider="ollama"))
    assert cli.supports_num_ctx is True
    assert cli.supports_think is True
    assert cli.supports_keep_alive is True
    assert cli.supports_unload is True


def test_openai_declares_none():
    cli = OpenAICompatClient(_cfg(provider="openai", host=OPENAI_HOST))
    assert cli.supports_num_ctx is False
    assert cli.supports_think is False
    assert cli.supports_keep_alive is False
    assert cli.supports_unload is False


def test_capability_flags_are_class_level():
    """必须是类属性：session 里会用 getattr 读，实例没初始化也不能炸。"""
    assert OllamaClient.supports_num_ctx is True
    assert OpenAICompatClient.supports_num_ctx is False


def test_factory_returns_flagged_client_without_network():
    cli = create_client(_cfg(provider="openai", host=OPENAI_HOST, model="x",
                             api_key="k"))
    assert isinstance(cli, OpenAICompatClient)
    assert cli.supports_num_ctx is False


def _apply(session, patch):
    """直接驱动 AgentSession.update_config 的配置段（不建 socket）。"""
    return session.update_config(patch)


def _make_session():
    """构造一个只用于改配置的 session：绕开真实连接。"""
    from webui.session import AgentSession
    s = AgentSession.__new__(AgentSession)
    import threading
    from agent.config import AgentConfig as C
    s.lock = threading.RLock()
    s.cfg = C(host="http://localhost:11434", model="qwen3.5:9b",
              provider="ollama")
    s.cfg.num_ctx = 65536
    s.cfg.think = "off"
    s.client = OllamaClient(s.cfg)
    s.workspace = Path(".").resolve()
    s.confirm = True
    s.ctx = type("Ctx", (), {"confirm": True, "workspace": s.workspace})()
    s.turns = []
    s.history = []
    s.sid = ""
    s.title = ""
    import time as _t
    s.created = _t.time()
    return s


def test_switch_to_openai_keeps_saved_num_ctx():
    """切到 OpenAI 时前端发 num_ctx=0，不能把 65536 清掉。"""
    s = _make_session()
    _apply(s, {"provider": "openai", "host": OPENAI_HOST, "model": "x",
               "num_ctx": 0, "think": "off"})
    assert s.cfg.num_ctx == 65536, "协议不支持时不许覆盖已存的 num_ctx"
    assert s.cfg.think == "off"


def test_switch_back_restores_real_values():
    s = _make_session()
    _apply(s, {"provider": "openai", "host": OPENAI_HOST, "model": "x",
               "num_ctx": 0})
    snap_openai = s.snapshot()
    assert snap_openai["num_ctx"] is None
    assert snap_openai["caps"]["num_ctx"] is False

    _apply(s, {"provider": "ollama", "host": "http://localhost:11434",
               "model": "qwen3.5:9b"})
    snap_ollama = s.snapshot()
    assert snap_ollama["caps"]["num_ctx"] is True
    assert snap_ollama["num_ctx"] == 65536, "切回 Ollama 后原值必须还在"


def test_ollama_can_still_change_num_ctx():
    """支持的时候必须真的能改，别为了防覆盖把功能锁死。"""
    s = _make_session()
    _apply(s, {"num_ctx": 131072})
    assert s.cfg.num_ctx == 131072
    assert s.snapshot()["num_ctx"] == 131072


# ---------- 边界：num_ctx 的非法/极端输入 ----------
# 依据规范第三节「边界」：空 / 超大 / 参差 / 编码 / 超时都要想过。
# 这几个值原先会静默写进配置（10**9 原样存下、1.5 截成 1、True 当成 1），
# 再下发给 ollama 就是一个难归因的 OOM 或一个用户没设过的值。

import pytest  # noqa: E402

from agent.config import MAX_CTX  # noqa: E402


@pytest.mark.parametrize("raw", [
    -1, "abc", None, "", 1.5, True, False, "  ", [], {},
])
def test_num_ctx_rejects_invalid_values(raw):
    """非法输入一律落到 None（= 用服务端默认），不能变成某个奇怪的数。"""
    s = _make_session()
    _apply(s, {"num_ctx": raw})
    assert s.cfg.num_ctx is None, f"{raw!r} 被接受成了 {s.cfg.num_ctx!r}"


def test_num_ctx_clamps_oversized():
    """超大值要截到 MAX_CTX，而不是原样下发触发 OOM。"""
    s = _make_session()
    _apply(s, {"num_ctx": 10 ** 9})
    assert s.cfg.num_ctx == MAX_CTX


@pytest.mark.parametrize("raw,expect", [
    ("131072", 131072),      # 前端 input 提交的是字符串
    (131072.0, 131072),      # 整数值的浮点应被接受
    (4096, 4096),
])
def test_num_ctx_accepts_valid_values(raw, expect):
    s = _make_session()
    _apply(s, {"num_ctx": raw})
    assert s.cfg.num_ctx == expect


# ---------- 错误路径：文案里要有指路 ----------
# 规范第三节：只断「抛了 / 没抛」是废测试，要断「失败时还在教用户怎么改」。

def test_capability_hint_tells_why_and_how():
    """禁用提示必须同时回答「为什么」和「那怎么办」。"""
    import re
    js = (Path(__file__).resolve().parents[1]
          / "webui" / "static" / "app.js").read_text(encoding="utf-8")
    m = re.search(r"const CTX_OFF_HINT =([\s\S]*?);\n", js)
    assert m, "找不到 CTX_OFF_HINT 常量"
    hint = m.group(1)
    assert "OpenAI" in hint or "兼容协议" in hint, "没说清为什么不适用"
    assert "切回 Ollama" in hint, f"没告诉用户怎么才能调：{hint!r}"
    assert "不会下发" in hint, "没说明参数实际会不会生效"


def test_server_rejects_without_corrupting_config():
    """拒绝非法值时不能顺手把原值改掉。"""
    s = _make_session()
    _apply(s, {"num_ctx": 32768})
    assert s.cfg.num_ctx == 32768
    _apply(s, {"num_ctx": "不是数字"})
    assert s.cfg.num_ctx is None, "非法值应回落默认，而不是保留旧值假装成功"
