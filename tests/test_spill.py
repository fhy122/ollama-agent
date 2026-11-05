"""spill 工具输出落盘测试。

契约（借鉴 DSH dsh-spill-policy）：
- 超预算文本：全文落盘 + 头尾预览 + 精确省略字节数 + 取回指引
- 预算内文本 / 豁免工具 / 已是 spill 预览：原样返回
- 落盘失败：保原文（尽力而为，不因 spill 丢结果）
- dispatch 集成：按 ctx.cfg.spill_max_chars 生效，0 = 关闭
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from agent import spill
from agent.config import AgentConfig
from agent.registry import ToolContext, ToolResult, tool, dispatch


MAXC = 200


def test_small_text_unchanged(tmp_path):
    """预算内文本原样返回，不落盘。"""
    out = spill.spill_if_oversize("browse_url", "短文本", tmp_path, MAXC)
    assert out == "短文本"
    assert not (tmp_path / "spills").exists() or not any((tmp_path / "spills").iterdir())


def test_oversize_spills_with_recoverable_full_text(tmp_path):
    """超预算：全文落盘，预览含头、GAP、尾、精确省略字节数与路径。"""
    text = "甲" * 150 + "\n" + "乙" * 150          # 301 字符 > 200
    out = spill.spill_if_oversize("browse_url", text, tmp_path, MAXC)

    # 预览结构：头 + GAP + 尾 + 空行 + 通知
    assert spill.GAP in out
    assert out.startswith("甲" * 50), "头应占预算一半"
    assert out.rstrip().endswith(")"), "应以取回指引结尾"
    assert "省略" in out and "read_file" in out and "grep_file" in out

    # 全文落盘且省略字节数精确：301 字符 = 150甲 + \n + 150乙，
    # UTF-8 共 901 字节；头尾各 100 字符（600 字节）→ 省略 301 字节
    import re
    m = re.search(r"省略 (\d+) 字节", out)
    assert m and int(m.group(1)) == 301, (m and m.group(1), out[-200:])
    files = list(tmp_path.glob("browse_url-*.txt"))
    assert len(files) == 1, files
    assert files[0].read_text(encoding="utf-8") == text, "落盘必须是完整原文"


def test_excluded_tool_untouched(tmp_path):
    """read_file 等豁免工具不 spill（防递归：读 spill 文件本身不能被再 spill）。"""
    text = "x" * 500
    for name in ("read_file", "read_long_doc"):
        assert spill.spill_if_oversize(name, text, tmp_path, MAXC) == text
    assert not list(tmp_path.glob("*.txt"))


def test_skill_result_spills_when_oversize(tmp_path):
    """技能正文不再豁免：超预算落盘，预览仍带技能名与取回指引。

    错误路径要能指路：模型看完预览得知道「这是哪个技能」以及
    「怎么拿回全文」，否则折叠/落盘就变成静默丢信息。
    """
    text = "[技能: big]\n" + "正文" * 300
    out = spill.spill_if_oversize("skill", text, tmp_path, MAXC)

    assert out.startswith("[技能: big]"), "预览头部要保留技能标识"
    assert "完整结果已存至" in out and "read_file" in out and "grep_file" in out
    files = list(tmp_path.glob("skill-*.txt"))
    assert len(files) == 1 and files[0].read_text(encoding="utf-8") == text


def test_idempotent_on_existing_notice(tmp_path):
    """已是 spill 预览的文本不再二次处理。"""
    text = "y" * 500
    once = spill.spill_if_oversize("browse_url", text, tmp_path, MAXC)
    twice = spill.spill_if_oversize("browse_url", once, tmp_path, MAXC)
    assert once == twice
    assert len(list(tmp_path.glob("*.txt"))) == 1


def test_save_failure_keeps_original(tmp_path, monkeypatch):
    """落盘失败（磁盘满/权限）：保原文，不抛异常。"""
    def boom(self, data, encoding=None):
        raise OSError("disk full")
    monkeypatch.setattr(Path, "write_text", boom)
    text = "z" * 500
    out = spill.spill_if_oversize("browse_url", text, tmp_path, MAXC)
    assert out == text


def test_disabled_when_max_chars_zero(tmp_path):
    """max_chars=0 完全关闭。"""
    assert spill.spill_if_oversize("browse_url", "a" * 999, tmp_path, 0) == "a" * 999


# ---------- dispatch 集成 ----------

@tool(name="_spill_big", description="测试用大输出工具", parameters={"type": "object", "properties": {}})
def _spill_big(ctx, size: int = 1000):
    return ToolResult("q" * size)


def test_dispatch_applies_spill(tmp_path):
    """dispatch 漏斗按 ctx.cfg 生效：默认超限落盘，0 关闭。"""
    ctx = ToolContext(workspace=tmp_path, cfg=AgentConfig(spill_max_chars=200))

    r = dispatch("_spill_big", {}, ctx)
    assert "完整结果已存至" in r.text
    spills = list((tmp_path / ".ollama-agent" / "spills").glob("_spill_big-*.txt"))
    assert len(spills) == 1 and spills[0].read_text(encoding="utf-8") == "q" * 1000

    # 关闭开关：原样返回
    ctx2 = ToolContext(workspace=tmp_path / "b", cfg=AgentConfig(spill_max_chars=0))
    r2 = dispatch("_spill_big", {}, ctx2)
    assert r2.text == "q" * 1000


def test_dispatch_no_cfg_uses_default(tmp_path):
    """ctx.cfg 为 None（部分调用方不注入配置）时用默认预算 12000。"""
    ctx = ToolContext(workspace=tmp_path, cfg=None)
    r = dispatch("_spill_big", {"size": 20000}, ctx)
    assert "完整结果已存至" in r.text
