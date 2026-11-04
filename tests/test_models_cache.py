"""设置面板模型列表缓存测试（离线，不依赖 Ollama）。

背景（老板 2026-11-04）：打开设置面板每次都卡一下——profiles_view 每次同步探测
本地 Ollama /api/tags（Ollama 忙着生成时排队数秒）。改为缓存制：打开设置零探测，
只在点「刷新」/「刷新全部」或切换内置模型校验时真正访问。

判据：
  1. profiles_view() 默认零探测；从没拉过时 stale=True（前端显示「点刷新获取」）；
  2. profiles_view(refresh_builtin=True) 才探测，成功喂缓存；此后默认视图不再探测；
  3. 探测失败不清空已有缓存（网络抖动不该把能用的列表变空）；
  4. _local_model_names 的校验读取成功也喂缓存（切过内置模型就顺手有了列表）；
  5. server._safe_list_models 的 60s TTL 缓存：同一 client 不重复探测，空结果不缓存。

运行：python tests/test_models_cache.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
TMP = tempfile.mkdtemp(prefix="oa-models-cache-")
os.environ["OLLAMA_AGENT_SESSIONS"] = str(Path(TMP) / "sessions")
os.environ["OLLAMA_AGENT_CONFIG"] = str(Path(TMP) / "config.json")

import pytest  # noqa: E402

from agent.config import AgentConfig  # noqa: E402
import webui.server as SRV  # noqa: E402
import webui.session as WS  # noqa: E402
from webui.store import SessionStore  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_cache():
    """模块级缓存是跨测试的全局状态：进出一律复位，避免用例间互相污染。"""
    for cache in (WS._OLLAMA_MODELS_CACHE,):
        cache["models"] = []
        cache["fetched"] = False
    SRV._list_models_cache.update(client=None, at=0.0, models=[])
    yield
    WS._OLLAMA_MODELS_CACHE["models"] = []
    WS._OLLAMA_MODELS_CACHE["fetched"] = False
    SRV._list_models_cache.update(client=None, at=0.0, models=[])


def _session(root: Path) -> WS.AgentSession:
    return WS.AgentSession(AgentConfig(use_color=False, warm=False),
                           store=SessionStore(root / "sessions"))


# ---------------- 1. 打开设置零探测 ----------------

def test_open_settings_does_not_probe(tmp_path):
    calls = []
    orig = WS._list_models_with_budget

    def spy(client, budget):
        calls.append(1)
        return []

    WS._list_models_with_budget = spy
    try:
        s = _session(tmp_path)
        v = s.profiles_view()
    finally:
        WS._list_models_with_budget = orig

    assert calls == [], "打开设置（默认视图）不得探测本地 Ollama"
    b = v["builtin"]
    assert b["models"] == [] and b["stale"] is True, \
        "从没拉过列表应标记 stale，让前端提示「点刷新获取」"
    print("PASS: 打开设置零探测（stale=True）")


# ---------------- 2. 显式刷新才探测，且喂缓存 ----------------

def test_refresh_probes_and_caches(tmp_path):
    calls = []
    orig = WS._list_models_with_budget

    def fake(client, budget):
        calls.append(1)
        return ["m1", "m2"]

    WS._list_models_with_budget = fake
    try:
        s = _session(tmp_path)
        v1 = s.profiles_view(refresh_builtin=True)
        assert len(calls) == 1, "点「刷新」应恰好探测一次"
        assert v1["builtin"]["models"] == ["m1", "m2"]
        assert v1["builtin"]["stale"] is False

        n = len(calls)
        v2 = s.profiles_view()          # 再开一次设置：吃缓存
        assert len(calls) == n, "缓存生效后默认视图不应再探测"
        assert v2["builtin"]["models"] == ["m1", "m2"]
        assert v2["builtin"]["stale"] is False
    finally:
        WS._list_models_with_budget = orig
    print("PASS: 刷新探测一次 + 缓存生效")


# ---------------- 3. 探测失败不清空已有缓存 ----------------

def test_probe_failure_keeps_cache(tmp_path):
    WS._OLLAMA_MODELS_CACHE["models"] = ["old-model"]
    WS._OLLAMA_MODELS_CACHE["fetched"] = True
    orig = WS._list_models_with_budget

    def boom(client, budget):
        raise RuntimeError("连不上 Ollama")

    WS._list_models_with_budget = boom
    try:
        s = _session(tmp_path)
        v = s.profiles_view(refresh_builtin=True)
    finally:
        WS._list_models_with_budget = orig

    assert v["builtin"]["models"] == ["old-model"], "网络抖动不该把能用的列表清空"
    assert v["builtin"]["error"] == "", "有旧列表兜底时不应渲染成错误"
    print("PASS: 探测失败保留旧缓存")


# ---------------- 4. 切内置模型的校验读取顺手喂缓存 ----------------

def test_local_model_names_feeds_cache(tmp_path):
    orig = WS._list_models_with_budget
    WS._list_models_with_budget = lambda client, budget: ["b-model", "a-model"]
    try:
        s = _session(tmp_path)
        names = s._local_model_names("http://localhost:11434")
    finally:
        WS._list_models_with_budget = orig

    assert names == {"b-model", "a-model"}
    assert WS._OLLAMA_MODELS_CACHE["fetched"] is True
    assert WS._OLLAMA_MODELS_CACHE["models"] == ["a-model", "b-model"], "应去重排序后进缓存"
    print("PASS: 切内置模型校验读取喂缓存")


# ---------------- 5. server._safe_list_models TTL 缓存 ----------------

class _FakeClient:
    def __init__(self):
        self.calls = 0

    def list_models(self):
        self.calls += 1
        return ["x"]


def test_safe_list_models_ttl():
    c = _FakeClient()
    m1 = SRV._safe_list_models(c)
    m2 = SRV._safe_list_models(c)
    assert m1 == m2 == ["x"], m1
    assert c.calls == 1, f"TTL 内同一 client 不应重复探测，实际 {c.calls} 次"
    c2 = _FakeClient()
    SRV._safe_list_models(c2)
    assert c2.calls == 1, "换 client（换了后端配置）必须重新探测"
    print("PASS: /api/config 列模型走 TTL 缓存")


def test_safe_list_models_empty_not_cached():
    class Empty:
        def __init__(self):
            self.calls = 0

        def list_models(self):
            self.calls += 1
            return []

    e = Empty()
    SRV._safe_list_models(e)
    SRV._safe_list_models(e)
    assert e.calls == 2, "空结果不该缓存（瞬态失败/没装模型不该粘住 60 秒）"
    print("PASS: 空结果不进 TTL 缓存")


if __name__ == "__main__":
    import tempfile as _tf
    root = Path(_tf.mkdtemp(prefix="oa-mc-main-"))
    test_open_settings_does_not_probe(root)
    test_refresh_probes_and_caches(root)
    test_probe_failure_keeps_cache(root)
    test_local_model_names_feeds_cache(root)
    test_safe_list_models_ttl()
    test_safe_list_models_empty_not_cached()
    print("ALL PASS")
