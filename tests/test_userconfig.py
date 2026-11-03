"""模型配置文件与连通性探测的单元测试。

覆盖三类场景（规范第三节：只写正常路径是半成品）：
  正常  —— 存得下、读得回、原子替换后内容不丢
  错误  —— 文件损坏 / 格式不对 / 删最后一个 / 删不存在的 id，都要能指路
  边界  —— 空文件、脏字段、重复 id、超长 Key、非 ASCII 名称

运行：python tests/test_userconfig.py     期望最后一行 PASS 且退出码 0
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import userconfig as uc
from agent import probe as pb


def _fresh() -> tuple[uc.ConfigStore, Path]:
    d = Path(tempfile.mkdtemp(prefix="oa-cfg-"))
    return uc.ConfigStore(d / "config.json"), d


def test_defaults():
    """文件不存在 → 给一份可用的默认配置，active 指向内置条目。"""
    st, _ = _fresh()
    cfg = st.load()
    assert cfg["version"] == uc.VERSION
    assert cfg["active"] == uc.BUILTIN_ID, cfg
    # 内置 Ollama 是虚拟条目：不落盘进 profiles（列表来自 /api/tags）
    assert cfg["profiles"] == [], cfg
    assert "error" not in cfg


def test_roundtrip():
    """存下来再读，字段一个不少（这是持久化的核心判据）。"""
    st, d = _fresh()
    cfg = st.load()
    st.upsert_profile(cfg, {
        "id": "", "name": "中转站 deepseek",
        "provider": "openai", "host": "https://www.aigatehub.xyz/v1",
        "api_key": "sk-test-abcdefgh", "model": "deepseek-v4.1-flash",
        "think": "off", "num_ctx": 32768, "temperature": 0.3,
    })
    st.save(cfg)

    again = uc.ConfigStore(d / "config.json").load()
    row = [p for p in again["profiles"] if p["provider"] == "openai"][0]
    assert row["host"] == "https://www.aigatehub.xyz/v1", row
    assert row["api_key"] == "sk-test-abcdefgh", row
    assert row["model"] == "deepseek-v4.1-flash", row
    assert row["num_ctx"] == 32768 and abs(row["temperature"] - 0.3) < 1e-9, row
    assert row["name"] == "中转站 deepseek", row


def test_atomic_no_leftover():
    """保存后不应留下 .tmp 残留（原子替换的判据）。"""
    st, d = _fresh()
    cfg = st.load()
    st.save(cfg)
    left = list(d.glob("*.tmp"))
    assert not left, f"残留临时文件: {left}"
    assert (d / "config.json").is_file()


def test_corrupt_file_recovers_with_reason():
    """损坏的 JSON → 退回默认，但必须说明「这次是重来的」。"""
    st, d = _fresh()
    (d / "config.json").write_text("{ 这不是 json", encoding="utf-8")
    cfg = st.load()
    assert cfg["active"] == uc.BUILTIN_ID
    assert "损坏" in cfg.get("_recovered", ""), cfg


def test_non_dict_file_recovers():
    """文件是合法 JSON 但结构不对（数组/字符串）→ 也要能起。"""
    st, d = _fresh()
    (d / "config.json").write_text("[1,2,3]", encoding="utf-8")
    cfg = st.load()
    assert "格式不对" in cfg.get("_recovered", ""), cfg


def test_partial_fields_filled():
    """手改配置少写字段 → 补默认，不让整个配置作废。"""
    st, d = _fresh()
    (d / "config.json").write_text(json.dumps({
        "profiles": [{"id": "x", "name": "手写的", "host": "http://a/v1",
                      "provider": "openai"}]
    }), encoding="utf-8")
    cfg = st.load()
    row = cfg["profiles"][0]
    assert row["id"] == "x" and row["name"] == "手写的", row
    assert row["model"] == "", row                        # 补成空串，不是崩掉
    assert row["temperature"] == 0.7, row                 # 补默认
    assert cfg["active"] == "x", cfg                      # active 缺失时指向唯一那条


def test_duplicate_ids_dropped():
    """重复 id 只保留第一个（否则删一个会删出两条的歧义）。"""
    st, d = _fresh()
    (d / "config.json").write_text(json.dumps({
        "profiles": [{"id": "dup", "model": "a"}, {"id": "dup", "model": "b"}]
    }), encoding="utf-8")
    cfg = st.load()
    assert len(cfg["profiles"]) == 1, cfg
    assert cfg["profiles"][0]["model"] == "a", cfg


def test_bad_provider_falls_back():
    """provider 写错（比如 'open-ai'）→ 退回 ollama，而不是带着错值去建客户端。"""
    st, d = _fresh()
    (d / "config.json").write_text(json.dumps({
        "profiles": [{"id": "q", "provider": "open-ai", "model": "m"}]
    }), encoding="utf-8")
    cfg = st.load()
    assert cfg["profiles"][0]["provider"] == "ollama", cfg


def test_upsert_updates_in_place():
    """按 id 更新：不新增条目、位置不变。"""
    st, _ = _fresh()
    cfg = st.load()
    first = st.upsert_profile(cfg, {"name": "甲", "provider": "openai",
                                    "host": "https://a/v1", "model": "m1"})
    pid = first["id"]
    before = len(cfg["profiles"])
    st.upsert_profile(cfg, {"id": pid, "name": "改过名", "model": "x"})
    assert len(cfg["profiles"]) == before, "同 id 更新不该新增条目"
    assert cfg["profiles"][0]["name"] == "改过名"


def test_builtin_not_deletable():
    """内置 Ollama 是虚拟条目 → 删它必须被拒绝，并给出替代做法。"""
    st, _ = _fresh()
    cfg = st.load()
    r = st.delete_profile(cfg, uc.BUILTIN_ID)
    assert not r["ok"], r
    assert "内置" in r["error"], r["error"]
    assert "切换" in r["error"], f"错误文案要指路: {r['error']}"


def test_builtin_id_not_writable():
    """伪造 id=ollama-local 想覆盖内置条目 → 只能新增一条自定义，不能顶掉内置。"""
    st, _ = _fresh()
    cfg = st.load()
    row = st.upsert_profile(cfg, {"id": uc.BUILTIN_ID, "name": "假冒",
                                  "provider": "openai", "model": "m"})
    assert row["id"] != uc.BUILTIN_ID, row
    assert all(p["id"] != uc.BUILTIN_ID for p in cfg["profiles"]), cfg
    assert len(cfg["profiles"]) == 1


def test_delete_all_allowed_builtin_remains():
    """自定义条目可以删到一条不剩 —— 因为内置 Ollama 永远在，不会「没得用」。"""
    st, _ = _fresh()
    cfg = st.load()
    ids = [st.upsert_profile(cfg, {"name": f"第{i}", "provider": "openai",
                                   "host": f"https://{i}/v1", "model": "m"})["id"]
           for i in range(3)]
    for pid in ids:
        assert st.delete_profile(cfg, pid)["ok"]
    assert cfg["profiles"] == [], cfg
    assert cfg["active"] == uc.BUILTIN_ID, cfg


def test_delete_missing_id_hint():
    """删不存在的 id → 说清可能原因（多半是页面过期）。"""
    st, _ = _fresh()
    cfg = st.load()
    r = st.delete_profile(cfg, "not-exist")
    assert not r["ok"]
    assert "刷新页面" in r["error"], f"错误文案要指路: {r['error']}"


def test_delete_active_falls_back_to_builtin():
    """删掉当前生效的那条 → active 回内置 Ollama，不留悬空引用。"""
    st, _ = _fresh()
    cfg = st.load()
    a = st.upsert_profile(cfg, {"name": "唯一的自定义", "provider": "openai",
                                "host": "https://x/v1", "api_key": "k",
                                "model": "m"})["id"]
    cfg["active"] = a
    r = st.delete_profile(cfg, a)
    assert r["ok"], r
    assert cfg["active"] == uc.BUILTIN_ID, cfg     # 回内置，不挑别人的站
    assert cfg["profiles"] == [], cfg


def test_state_survives_reload_after_delete():
    """删完再存再读：磁盘上不能又冒出被删的那条（持久化的真判据）。"""
    st, d = _fresh()
    cfg = st.load()
    a = st.upsert_profile(cfg, {"name": "要删的", "provider": "openai",
                                "host": "https://x/v1", "api_key": "k",
                                "model": "m"})["id"]
    st.save(cfg)
    again = uc.ConfigStore(d / "config.json").load()
    assert [p["id"] for p in again["profiles"]] == [a], again
    st.delete_profile(again, a)
    st.save(again)
    final = uc.ConfigStore(d / "config.json").load()
    assert final["profiles"] == [], final
    assert final["active"] == uc.BUILTIN_ID, final
    assert uc.BUILTIN_ID not in [p["id"] for p in final["profiles"]], "内置条目不该落盘"


def test_mask_key_never_leaks():
    """打码后不能包含完整 Key。"""
    key = "sk-1234567890abcdefXYZ"
    m = uc.mask_key(key)
    assert key not in m and m != key, m
    assert m.startswith("sk-1") and m.endswith("XYZ"), m
    assert "…" in m
    assert uc.mask_key("short") == "*****"
    assert uc.mask_key("") == ""


def test_unicode_name():
    """中文/emoji 名称能存能读（编码边界）。"""
    st, d = _fresh()
    cfg = st.load()
    st.upsert_profile(cfg, {"name": "本地 Ollama 🛠️", "model": "qwen3.5:9b"})
    st.save(cfg)
    again = uc.ConfigStore(d / "config.json").load()
    assert any("🛠️" in p["name"] for p in again["profiles"]), again


def test_env_override_isolation():
    """ENV_CONFIG 能改路径 —— 测试靠它隔离，绝不能碰用户真实配置。"""
    d = Path(tempfile.mkdtemp(prefix="oa-cfg-env-"))
    old = os.environ.get(uc.ENV_CONFIG)
    os.environ[uc.ENV_CONFIG] = str(d / "c.json")
    try:
        st = uc.ConfigStore()
        assert st.path == d / "c.json", st.path
    finally:
        if old is None:
            os.environ.pop(uc.ENV_CONFIG, None)
        else:
            os.environ[uc.ENV_CONFIG] = old


def test_to_agent_patch_shape():
    """profile → AgentSession.update_config 的补丁字段齐全（契约层）。"""
    p = dict(uc.DEFAULT_PROFILE, id="z", name="n",
             provider="openai", host="https://h/v1", api_key="k", model="m")
    patch = uc.to_agent_patch(p, {"confirm": False, "max_steps": 5,
                                  "workspace": "/tmp"})
    for k in ("provider", "host", "api_key", "model", "think", "num_ctx",
              "temperature", "confirm", "max_steps", "workspace"):
        assert k in patch, f"补丁缺字段 {k}"
    assert patch["confirm"] is False and patch["max_steps"] == 5


# ---------------- probe：不发网络请求的用例 ----------------

def test_probe_empty_host():
    r = pb.probe("openai", "")
    assert not r["ok"] and r["stage"] == "url", r
    assert "https://" in r["hint"], f"提示要给出可复制的例子: {r['hint']}"


def test_probe_missing_scheme():
    r = pb.probe("ollama", "localhost:11434")
    assert not r["ok"] and r["stage"] == "url", r
    assert "http://" in r["error"], r


def test_probe_missing_key_for_openai():
    r = pb.probe("openai", "https://x/v1", api_key="")
    assert not r["ok"] and r["stage"] == "auth", r
    assert "API Key" in r["error"], r


def test_probe_unreachable_host():
    """打一个必然连不上的端口：必须报「连不上」并给代理提示，且不抛异常。"""
    r = pb.probe("ollama", "http://127.0.0.1:9")
    assert not r["ok"], r
    assert r["stage"] in ("connect", "http"), r
    assert r["hint"], "错误必须带下一步提示"


def test_probe_loopback_diagnosis():
    """本机地址连不上 → 必须指向「服务没起 / 代理拦了本机」，
    不能让人以为是对面站点坏了（这是实测踩过的坑：代理返回 502，
    原文案劝人「访问境外站点需要代理」，方向完全错）。"""
    r = pb.probe("openai", "https://127.0.0.1:9/v1", api_key="sk-x")
    assert not r["ok"], r
    assert r["stage"] == "connect", r
    assert "本机" in r["error"], f"应点明是连本机: {r['error']}"
    assert "代理" in r["hint"], f"要提示代理可能拦本机: {r['hint']}"
    assert "ollama serve" in r["hint"], f"要给可执行动作: {r['hint']}"


def test_is_loopback():
    """本机判定：localhost/127.x/::1 算，真实域名不算。"""
    assert pb._is_loopback("http://127.0.0.1:11434")
    assert pb._is_loopback("http://localhost:9/v1")
    assert pb._is_loopback("http://[::1]:1")
    assert not pb._is_loopback("https://api.deepseek.com/v1")
    assert not pb._is_loopback("")


def test_probe_parse_models():
    """两种协议各自的模型列表解析（纯函数，不联网）。"""
    ollama = {"models": [{"name": "qwen3.5:9b"}, {"model": "llama3"}]}
    assert pb._parse_models("ollama", ollama) == ["qwen3.5:9b", "llama3"]
    oai = {"data": [{"id": "gpt-4o"}, {"id": "deepseek-chat"}]}
    assert pb._parse_models("openai", oai) == ["gpt-4o", "deepseek-chat"]
    assert pb._parse_models("openai", {"nope": 1}) == []
    assert pb._parse_models("ollama", "不是 dict") == []


def main():
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for fn in fns:
        fn()
        print(f"  ok  {fn.__name__}")
    print(f"PASS: {len(fns)} 个用例全过")


if __name__ == "__main__":
    main()
