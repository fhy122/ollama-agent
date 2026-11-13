"""工具 / 技能启用策略（只对话模式、自定义白名单、按技能开关）的回归测试。

背景：所有工具的 schema 每次请求全量下发（约 8.7k 字符）。纯聊天时这些
schema 白吃上下文，模型还会时不时想出个工具来调。这里锁住三件事：

1. 默认行为不变 —— 不配置时模型看到的 tools 数组与加功能之前完全一致；
2. 「只对话」只留下读文件 / 读图 / 联网四件，其余连 schema 都不下发；
3. 关掉的工具即使模型硬调也不执行（只靠不下发挡不住小模型照旧对话硬调）。

判据：
- specs_for: all=全部 / chat=4 件 / none=None / custom=白名单（空→None）
- dispatch: 不在白名单里的工具返回「已禁用」，且不产生副作用
- new_history: skill 工具关掉 → 技能索引不注入；skills_disabled → 该技能不进索引
- skill 工具：显式点名被关掉的技能 → 明说「已禁用」而不是「找不到」
- 持久化：ui 段往返；旧配置没有这些字段 → 默认全部启用
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

import tools  # noqa: E402,F401  触发工具注册
from agent import toolpolicy  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.loop import new_history  # noqa: E402
from agent.registry import ToolContext, all_specs, all_tools, dispatch  # noqa: E402
from agent.userconfig import ConfigStore  # noqa: E402
from tools.skill import list_skills, skill_index  # noqa: E402


def _names(specs):
    return {s["function"]["name"] for s in specs}


# ---------------- 模式解析 ----------------

def test_default_is_all_tools():
    cfg = AgentConfig()
    assert toolpolicy.mode_of(cfg) == "all"
    assert toolpolicy.allowed_names(cfg) is None
    assert _names(toolpolicy.specs_for(cfg)) == {t.name for t in all_tools()}


def test_chat_mode_keeps_only_read_and_web():
    cfg = AgentConfig(tool_mode="chat")
    got = _names(toolpolicy.specs_for(cfg))
    assert got == set(toolpolicy.CHAT_TOOLS), got
    # 这四个必须真存在（名字写错会让「只对话」静默少一个能力）
    assert got <= {t.name for t in all_tools()}
    for name in ("write_file", "edit_file", "delete_file", "run_command", "skill"):
        assert name not in got


def test_chat_mode_resolves_to_four():
    assert len(toolpolicy.CHAT_TOOLS) == 4


def test_none_mode_sends_no_tools():
    for cfg in (AgentConfig(tool_mode="none"), AgentConfig(no_tools=True)):
        assert toolpolicy.specs_for(cfg) is None


def test_custom_whitelist():
    cfg = AgentConfig(tool_mode="custom", tools_enabled=["read_file", "web_search"])
    assert _names(toolpolicy.specs_for(cfg)) == {"read_file", "web_search"}


def test_custom_empty_gives_no_tools():
    cfg = AgentConfig(tool_mode="custom", tools_enabled=[])
    assert toolpolicy.specs_for(cfg) is None


def test_no_tools_flag_beats_mode():
    # 老开关 no_tools 与 tool_mode 同时存在时，no_tools 说话（等价 none）
    cfg = AgentConfig(no_tools=True, tool_mode="all")
    assert toolpolicy.mode_of(cfg) == "none"
    assert toolpolicy.specs_for(cfg) is None


def test_unknown_mode_falls_back_to_all():
    assert toolpolicy.normalize_mode("garbage") == "all"
    assert toolpolicy.normalize_mode("") == "all"
    assert toolpolicy.normalize_mode("CHAT") == "chat"


def test_parse_spec():
    assert toolpolicy.parse_spec(None) == ("all", [])
    assert toolpolicy.parse_spec("chat") == ("chat", [])
    assert toolpolicy.parse_spec(" NONE ") == ("none", [])
    assert toolpolicy.parse_spec("read_file,web_search") == (
        "custom", ["read_file", "web_search"])
    # 中文逗号、空格、重复项都要能吃掉
    assert toolpolicy.parse_spec("read_file，web_search read_file") == (
        "custom", ["read_file", "web_search"])


def test_split_names_ignores_empty():
    assert toolpolicy.split_names("") == []
    assert toolpolicy.split_names(None) == []
    assert toolpolicy.split_names(["a", " a ", ""]) == ["a"]


def test_enabled_count():
    assert toolpolicy.enabled_count(AgentConfig()) == len(all_tools())
    assert toolpolicy.enabled_count(AgentConfig(tool_mode="chat")) == 4
    assert toolpolicy.enabled_count(AgentConfig(tool_mode="none")) == 0


def test_catalog_and_groups_cover_every_tool():
    """界面清单必须把每个工具都画出来 —— 漏一个等于「那个工具没开关」。"""
    cfg = AgentConfig()
    assert {r["name"] for r in toolpolicy.catalog(cfg)} == {t.name for t in all_tools()}
    gs = toolpolicy.groups(cfg)
    flat = [t["name"] for g in gs for t in g["tools"]]
    assert sorted(flat) == sorted(t.name for t in all_tools())
    assert all(g["label"] for g in gs), gs


def test_groups_marks_enabled_state():
    gs = toolpolicy.groups(AgentConfig(tool_mode="chat"))
    rows = {t["name"]: t["enabled"] for g in gs for t in g["tools"]}
    assert rows["read_file"] is True
    assert rows["write_file"] is False


# ---------------- dispatch 拦截 ----------------

def test_dispatch_blocks_disabled_tool(tmp_path):
    cfg = AgentConfig(tool_mode="chat")
    ctx = ToolContext(workspace=tmp_path, cfg=cfg,
                      tools_allowed=toolpolicy.allowed_names(cfg))
    r = dispatch("write_file", {"path": "x.txt", "content": "hi"}, ctx)
    assert "已禁用" in r.text
    assert not (tmp_path / "x.txt").exists(), "被拒的工具绝不能留下副作用"


def test_dispatch_allows_enabled_tool(tmp_path):
    (tmp_path / "a.txt").write_text("hello", encoding="utf-8")
    cfg = AgentConfig(tool_mode="chat")
    ctx = ToolContext(workspace=tmp_path, cfg=cfg,
                      tools_allowed=toolpolicy.allowed_names(cfg))
    r = dispatch("read_file", {"path": "a.txt"}, ctx)
    assert "hello" in r.text


def test_dispatch_unrestricted_when_no_policy(tmp_path):
    """tools_allowed=None 是「不限制」，别把 None 当成空集合。"""
    (tmp_path / "b.txt").write_text("ok", encoding="utf-8")
    ctx = ToolContext(workspace=tmp_path, cfg=AgentConfig())
    r = dispatch("write_file", {"path": "c.txt", "content": "x"}, ctx)
    assert "已禁用" not in r.text
    assert (tmp_path / "c.txt").exists()


# ---------------- 技能 ----------------

@pytest.fixture()
def ws_with_skill(tmp_path):
    d = tmp_path / ".workbuddy" / "skills" / "demo"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: demo\ndescription: 一个测试技能\n---\n正文内容\n",
        encoding="utf-8")
    return tmp_path


def test_skill_index_present_by_default(ws_with_skill):
    hist = new_history(str(ws_with_skill), AgentConfig())
    assert "demo" in hist[0]["content"]


def test_skill_index_dropped_in_chat_mode(ws_with_skill):
    """chat 模式没有 skill 工具 → 索引也不该占 token（模型没法加载正文）。"""
    hist = new_history(str(ws_with_skill), AgentConfig(tool_mode="chat"))
    assert "可用技能" not in hist[0]["content"]


def test_skill_index_dropped_when_skill_tool_off(ws_with_skill):
    cfg = AgentConfig(tool_mode="custom", tools_enabled=["read_file"])
    assert "可用技能" not in new_history(str(ws_with_skill), cfg)[0]["content"]


def test_skill_disabled_filters_index(ws_with_skill):
    cfg = AgentConfig(skills_disabled=["demo"])
    # 注意：不断言「整段索引消失」——本机用户目录里可能装着别的真实技能，
    # 索引会照常出现，只是不含被关掉的那一个。
    assert "demo" not in new_history(str(ws_with_skill), cfg)[0]["content"]
    assert "demo" not in skill_index(str(ws_with_skill),
                                     toolpolicy.disabled_skills(cfg))
    # 但清单默认仍包含它（设置面板要能重新勾上）
    assert "demo" in [s["name"] for s in list_skills(str(ws_with_skill))]


def test_skill_tool_refuses_disabled_skill(ws_with_skill):
    cfg = AgentConfig(skills_disabled=["demo"])
    ctx = ToolContext(workspace=ws_with_skill, cfg=cfg)
    r = dispatch("skill", {"name": "demo"}, ctx)
    assert "已禁用" in r.text, r.text


def test_skill_tool_lists_only_enabled(ws_with_skill):
    cfg = AgentConfig(skills_disabled=["demo"])
    ctx = ToolContext(workspace=ws_with_skill, cfg=cfg)
    r = dispatch("skill", {}, ctx)
    assert "demo" not in r.text
    assert "被关闭" in r.text


def test_new_history_without_cfg_keeps_old_behaviour(ws_with_skill):
    """cfg=None 是「不限制」，老调用方（测试/教学代码）行为不变。"""
    assert "demo" in new_history(str(ws_with_skill))[0]["content"]


# ---------------- 系统提示词的「工具口径」 ----------------

def test_prompt_names_the_enabled_tools(ws_with_skill):
    """工具被裁掉后，提示词不能再要求「必须调用工具」而不说有哪些。"""
    p = new_history(str(ws_with_skill), AgentConfig(tool_mode="chat"))[0]["content"]
    assert "只启用了这些工具" in p
    assert "read_file" in p and "browse_url" in p
    assert "write_file" not in p, "说没有的工具比不说更糟：模型会去调它"


def test_prompt_says_no_tools_when_none(ws_with_skill):
    p = new_history(str(ws_with_skill), AgentConfig(tool_mode="none"))[0]["content"]
    assert "没有启用任何工具" in p
    assert "不要编造" in p


def test_prompt_unchanged_when_all_tools(ws_with_skill):
    """全部启用时不加任何限制语，升级零行为变化。"""
    p = new_history(str(ws_with_skill), AgentConfig())[0]["content"]
    assert "只启用了这些工具" not in p
    assert "没有启用任何工具" not in p


# ---------------- 持久化 ----------------

def test_config_roundtrip(tmp_path):
    st = ConfigStore(tmp_path / "c.json")
    data = st.load()
    data["ui"]["tool_mode"] = "chat"
    data["ui"]["tools_enabled"] = ["read_file"]
    data["ui"]["skills_disabled"] = ["demo"]
    st.save(data)

    raw = ConfigStore(tmp_path / "c.json").load()
    assert raw["ui"]["tool_mode"] == "chat"
    assert raw["ui"]["tools_enabled"] == ["read_file"]
    assert raw["ui"]["skills_disabled"] == ["demo"]


def test_old_config_defaults_to_all(tmp_path):
    st = ConfigStore(tmp_path / "old.json")
    st.save({"version": 1, "active": "ollama-local", "profiles": [], "ui": {}})
    ui = st.load()["ui"]
    assert ui["tool_mode"] == "all"
    assert ui["tools_enabled"] == []
    assert ui["skills_disabled"] == []


def test_bad_mode_in_file_is_normalized(tmp_path):
    st = ConfigStore(tmp_path / "bad.json")
    st.save({"version": 1, "profiles": [], "ui": {"tool_mode": "whatever"}})
    assert st.load()["ui"]["tool_mode"] == "all"


# ---------------- WebUI 配置热更新 ----------------

class _FakeClient:
    """snapshot() 只读这些能力标记，不需要真的连后端。"""
    name = "ollama"
    model = "m"
    supports_num_ctx = True
    supports_think = True
    supports_repeat_penalty = True
    supports_keep_alive = True
    supports_unload = True


def _bare_session(tmp_path, cfg=None):
    """只驱动配置逻辑的 session：不连后端、不碰真实 IO。"""
    import threading

    from agent.userconfig import ConfigStore
    from webui.session import AgentSession

    s = AgentSession.__new__(AgentSession)
    s.config_store = ConfigStore(tmp_path / "config.json")
    s.user_cfg = s.config_store.load()
    s.cfg = cfg or AgentConfig(workspace=str(tmp_path))
    s.client = _FakeClient()
    s.workspace = tmp_path
    s.confirm = True
    s.ctx = ToolContext(workspace=tmp_path, cfg=s.cfg)
    s.history = new_history(str(tmp_path), s.cfg)
    s.lock = threading.Lock()
    return s


def test_update_config_applies_and_persists(tmp_path):
    s = _bare_session(tmp_path)
    s.update_config({"tool_mode": "chat"})
    assert s.cfg.tool_mode == "chat"
    assert s.config_store.load()["ui"]["tool_mode"] == "chat"


def test_update_config_clears_no_tools_flag(tmp_path):
    """命令行 --no-tools 启动后，界面上切「只对话」要真的生效。"""
    s = _bare_session(tmp_path, AgentConfig(no_tools=True, workspace=str(tmp_path)))
    s.update_config({"tool_mode": "chat"})
    assert s.cfg.no_tools is False
    assert toolpolicy.specs_for(s.cfg) is not None


def test_update_config_rewrites_system_prompt(tmp_path, ws_with_skill):
    s = _bare_session(ws_with_skill)
    s.workspace = ws_with_skill
    s.history = new_history(str(ws_with_skill), s.cfg)
    assert "可用技能" in s.history[0]["content"]

    s.update_config({"tool_mode": "chat"})
    assert "可用技能" not in s.history[0]["content"], "关掉技能后索引要立刻消失"
    assert s.history[0]["role"] == "system"


def test_update_config_ignores_unknown_mode(tmp_path):
    s = _bare_session(tmp_path)
    s.update_config({"tool_mode": "nonsense"})
    assert s.cfg.tool_mode == "all", "认不出的模式不能把配置改坏"


# ---------------- CLI /tools 的基线语义 ----------------

def test_cli_toggle_off_keeps_other_tools(tmp_path, capsys):
    """「全部」模式下 off 一个 = 19-1，不是「一个都不剩」。

    白名单本来就空（全开时不写白名单），直接拿它当基线会把其余工具一起关掉。
    """
    from agent.cli import tools_command

    cfg = AgentConfig()
    ctx = ToolContext(workspace=tmp_path, cfg=cfg,
                      tools_allowed=toolpolicy.allowed_names(cfg))
    hist = new_history(str(tmp_path), cfg)
    tools_command("off write_file", cfg, hist, tmp_path, False, ctx)

    assert cfg.tool_mode == "custom"
    assert "write_file" not in cfg.tools_enabled
    assert toolpolicy.enabled_count(cfg) == len(all_tools()) - 1
    # 上下文里的允许集合也要同步，否则 dispatch 还放行
    assert ctx.tools_allowed is not None and "write_file" not in ctx.tools_allowed


def test_cli_toggle_off_in_chat_mode(tmp_path, capsys):
    """「只对话」模式下 off 一个 = 四件减一，不是从空开始。"""
    from agent.cli import tools_command

    cfg = AgentConfig(tool_mode="chat")
    ctx = ToolContext(workspace=tmp_path, cfg=cfg,
                      tools_allowed=toolpolicy.allowed_names(cfg))
    hist = new_history(str(tmp_path), cfg)
    tools_command("off browse_url", cfg, hist, tmp_path, False, ctx)

    assert set(cfg.tools_enabled) == set(toolpolicy.CHAT_TOOLS) - {"browse_url"}


def test_cli_unknown_tool_name_is_rejected(tmp_path, capsys):
    from agent.cli import tools_command

    cfg = AgentConfig()
    hist = new_history(str(tmp_path), cfg)
    tools_command("off no_such_tool", cfg, hist, tmp_path, False, None)
    out = capsys.readouterr().out
    assert "没有这个工具" in out
    assert cfg.tool_mode == "all", "打错名字不该顺手把模式改掉"
