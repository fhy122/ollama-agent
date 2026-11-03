"""build_files_hint 自测：附件指引必须区分「工作区内副本 / 本机原件」。

背景：老板指出同机场景下「上传=纯拷贝」，新增「从路径添加」后，
给模型的指引若还笼统说「已上传到工作空间」，模型会去 .uploads 里
list_dir 瞎摸原件（实测行为）。锁住三类分组行为。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from webui.server import build_files_hint  # noqa: E402


def test_inner_only():
    """上传副本（工作区内绝对路径 + 相对路径）→ 只说工作空间那份。"""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        up = ws / ".uploads" / "a.pptx"
        up.parent.mkdir(parents=True)
        up.write_bytes(b"x")
        hint = build_files_hint([str(up), "doc/说明.md"], ws)
        assert "已上传到工作空间" in hint
        assert "原件" not in hint
        assert str(up) in hint and "doc/说明.md" in hint
    print("PASS: 工作区内附件 → 上传指引")


def test_outer_only():
    """本机原件（工作区外绝对路径）→ 明说没有副本、直读。"""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        outer = Path(td).parent / "elsewhere" / "x.pptx"
        hint = build_files_hint([str(outer)], ws)
        assert "没有上传副本" in hint
        assert "按这个路径读" in hint
        assert "已上传到工作空间" not in hint
        assert str(outer) in hint
    print("PASS: 工作区外原件 → 直读指引")


def test_mixed():
    """混挂：两段指引都在，各自文件归各自段落。"""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        up = ws / ".uploads" / "b.pdf"
        up.parent.mkdir(parents=True)
        up.write_bytes(b"x")
        outer = ws.parent / "outside.docx"
        hint = build_files_hint([str(up), str(outer)], ws)
        inner_line = [ln for ln in hint.splitlines() if str(up) in ln][0]
        outer_line = [ln for ln in hint.splitlines() if str(outer) in ln][0]
        assert "已上传到工作空间" in hint and "没有上传副本" in hint
        assert "已上传" not in outer_line and "没有上传副本" not in inner_line
    print("PASS: 混合附件 → 两段指引互不串")


def test_relative_is_inner():
    """相对路径按 read_file 的 resolve 规则视为工作区内。"""
    with tempfile.TemporaryDirectory() as td:
        hint = build_files_hint(["a/b.md"], Path(td))
        assert "已上传到工作空间" in hint and "原件" not in hint
    print("PASS: 相对路径 → 工作区内")


if __name__ == "__main__":
    test_inner_only()
    test_outer_only()
    test_mixed()
    test_relative_is_inner()
