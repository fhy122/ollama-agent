"""migrate_legacy_dirs 自测：旧散落目录收进 .ollama-agent/ 的三条规矩。

背景：老板要求工作区里别再散落 .uploads/.clip_cache，统一收进
.{APP_NAME}/。迁移必须 move（不留旧壳）、同名冲突时宁缺勿覆盖、
无旧目录时幂等安静。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import HIDDEN_DIRNAME  # noqa: E402
from webui.server import migrate_legacy_dirs  # noqa: E402


def test_migrates_both():
    """旧 .uploads/.clip_cache 有文件 → 全搬进新家，旧壳删除。"""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / ".uploads").mkdir()
        (ws / ".uploads" / "a.pptx").write_bytes(b"x")
        (ws / ".clip_cache").mkdir()
        (ws / ".clip_cache" / "clip_1.png").write_bytes(b"y")

        migrate_legacy_dirs(ws)

        assert (ws / HIDDEN_DIRNAME / "uploads" / "a.pptx").read_bytes() == b"x"
        assert (ws / HIDDEN_DIRNAME / "clip" / "clip_1.png").read_bytes() == b"y"
        assert not (ws / ".uploads").exists(), "旧目录壳必须删掉"
        assert not (ws / ".clip_cache").exists()
    print("PASS: 旧目录整体迁入 .ollama-agent/，旧壳删除")


def test_no_overwrite_existing():
    """新家已有同名文件 → 源文件留在原地，旧目录不删（宁缺勿覆盖）。"""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / ".uploads").mkdir()
        (ws / ".uploads" / "a.pptx").write_bytes(b"old")
        new_home = ws / HIDDEN_DIRNAME / "uploads"
        new_home.mkdir(parents=True)
        (new_home / "a.pptx").write_bytes(b"new")

        migrate_legacy_dirs(ws)

        assert (new_home / "a.pptx").read_bytes() == b"new", "不许覆盖新文件"
        assert (ws / ".uploads" / "a.pptx").read_bytes() == b"old", \
            "被跳过的源文件必须留在原地"
        assert (ws / ".uploads").exists(), "有残留时旧目录壳不许删"
    print("PASS: 同名冲突 → 新家不覆盖、源文件留原地")


def test_idempotent_without_legacy():
    """没有旧目录 → 什么都不做也不报错（幂等）。"""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "doc").mkdir()
        migrate_legacy_dirs(ws)
        assert not (ws / HIDDEN_DIRNAME).exists(), "不该凭空建数据目录"
    print("PASS: 无旧目录 → 幂等安静")


if __name__ == "__main__":
    test_migrates_both()
    test_no_overwrite_existing()
    test_idempotent_without_legacy()
