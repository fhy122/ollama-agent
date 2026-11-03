"""coding-standards 技能 + verify 工具的离线自测。运行: python tests/test_coding_standards.py

覆盖三类场景（规范第三节要求）：
  正常路径 —— 内置技能可发现、verify 各档在干净样本上通过
  错误路径 —— 每个失败必须指出「哪个文件第几行」，不能只说"失败"
  边界     —— 空目录、无测试脚本、未知检查项、no-compile 豁免、代码块里的假链接
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools  # noqa: E402  触发全部工具注册
from agent.registry import ToolContext, dispatch  # noqa: E402

REPO = Path(__file__).resolve().parent.parent


def _w(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_builtin_skill() -> None:
    """内置技能随仓库发布：任何工作空间都能发现，且用户目录可覆盖。"""
    from tools.skill import list_skills, skill_index

    md = REPO / "skills" / "coding-standards" / "SKILL.md"
    assert md.is_file(), f"内置技能文件缺失: {md}"

    # 传一个与仓库无关的工作空间，仍应发现内置技能
    with tempfile.TemporaryDirectory() as td:
        names = {s["name"] for s in list_skills(td)}
        assert "coding-standards" in names, f"内置技能未进清单: {sorted(names)}"
        idx = skill_index(td)
        assert "coding-standards" in idx and "可用技能" in idx, idx

        # frontmatter 归一：display_name 走了中文名，description_zh 缺省时回落到 description
        item = next(s for s in list_skills(td) if s["name"] == "coding-standards")
        assert item["display"] == "编码与验证规范", item
        assert "铁律" in item["desc"], item["desc"]

        # 全文可加载，且 frontmatter 被剥离、附环境工具映射
        ctx = ToolContext(workspace=Path(td))
        r = dispatch("skill", {"name": "coding-standards"}, ctx)
        assert "四条铁律" in r.text and "[环境提示]" in r.text, r.text[:400]
        assert "agent_created:" not in r.text, "frontmatter 应被剥离"
        assert "verify" in r.text, "正文应指引使用 verify 工具"

        # 与 docs/ 人类版同源：关键条款两边都在（防单边漂移）
        human = (REPO / "docs" / "CODING_STANDARDS.md").read_text(encoding="utf-8")
        for clause in ("没有验证输出的代码，不算写完", "测试不碰用户数据目录",
                       "不为通过测试改断言"):
            assert clause in human, f"docs 版缺条款: {clause}"
            assert clause in r.text, f"技能版缺条款: {clause}"

    print("PASS: coding-standards 内置技能（跨工作空间可见 + 全文加载 + 与 docs 同源）")


def test_verify_syntax() -> None:
    """第 0 档：能编译就过；不能编译必须给「文件:行号」。"""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        _w(ws / "ok.py", "def f():\n    return 1\n")
        ctx = ToolContext(workspace=ws)

        r = dispatch("verify", {"checks": ["syntax"]}, ctx)
        assert "1/1 通过" in r.text and "全部通过" in r.text, r.text

        # 错误路径：语法错误要指到行，且有下一步怎么办
        _w(ws / "bad.py", "def f(:\n    return 1\n")
        r = dispatch("verify", {"checks": ["syntax"]}, ctx)
        assert "1/2 通过" in r.text and "1 项未通过" in r.text, r.text
        assert "bad.py" in r.text and "第 1 行" in r.text, r.text
        assert "一次只改一处" in r.text, "失败结论应给出可执行下一步"

        # 边界：非 UTF-8（GBK）文件不应崩，也不应误报语法错
        (ws / "gbk.py").write_bytes("# 中文注释\ndef g():\n    return 2\n".encode("gbk"))
        r = dispatch("verify", {"checks": ["syntax"]}, ctx)
        assert "gbk.py" not in r.text.split("[结论]")[0] or "✗ gbk.py" not in r.text, r.text

        # 边界：空文件合法（compile 空源码是通过的）
        _w(ws / "empty.py", "")
        r = dispatch("verify", {"checks": ["syntax"]}, ctx)
        assert "empty.py" not in r.text.replace("✗ bad.py", ""), r.text

    print("PASS: verify syntax（正常/语法错定位到行/GBK 不误报/空文件）")


def test_verify_links() -> None:
    """第六节：相对链接必须指向真实文件；代码块里的假链接不算。"""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        _w(ws / "target.md", "# 目标\n")
        ctx = ToolContext(workspace=ws)

        _w(ws / "good.md", "[目标](target.md) [锚点](#x) [外链](https://example.com)\n")
        r = dispatch("verify", {"checks": ["links"]}, ctx)
        assert "全部通过" in r.text, r.text

        # 错误路径：断链要点名是哪个链接
        _w(ws / "broken.md", "[在](missing.md)\n")
        r = dispatch("verify", {"checks": ["links"]}, ctx)
        assert "1 项未通过" in r.text and "broken.md" in r.text, r.text
        assert "missing.md" in r.text, "断链必须点名目标路径"

        # 边界：围栏代码块里的 ](x) 不是真链接，不能误报
        _w(ws / "broken.md", "```markdown\n[示例](不存在.md)\n```\n")
        r = dispatch("verify", {"checks": ["links"]}, ctx)
        assert "全部通过" in r.text, f"代码块内链接被误判成断链: {r.text}"

    print("PASS: verify links（正常/断链点名/代码块内假链接不误报）")


def test_verify_blocks() -> None:
    """第六节：文档代码块要 compile；故意不完整的块用 no-compile 显式豁免。"""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        ctx = ToolContext(workspace=ws)

        _w(ws / "a.md", "```python\nx = 1\nprint(x)\n```\n")
        r = dispatch("verify", {"checks": ["blocks"]}, ctx)
        assert "1 个 Python 块通过" in r.text and "全部通过" in r.text, r.text

        # 错误路径：坏块要指出文档行号
        _w(ws / "b.md", "# 标题\n\n```python\ndef f(:\n    pass\n```\n")
        r = dispatch("verify", {"checks": ["blocks"]}, ctx)
        assert "1 项未通过" in r.text and "b.md" in r.text, r.text
        assert "第 3 行起" in r.text, f"应报代码块起始行: {r.text}"
        (ws / "b.md").unlink()          # 清掉坏样本，后续断言只针对当前用例

        # 边界：no-compile 豁免生效，且豁免数量要说出来（不静默）
        _w(ws / "c.md", "```python no-compile\ndef f(:\n```\n")
        r = dispatch("verify", {"checks": ["blocks"]}, ctx)
        assert "全部通过" in r.text, r.text
        assert "no-compile 豁免" in r.text, f"豁免应显式报出: {r.text}"

        # 边界：非 Python 围栏不参与编译（bash 里的 python 语法无关）
        _w(ws / "d.md", "```bash\nif [ -f x ]; then echo hi; fi\n```\n")
        r = dispatch("verify", {"checks": ["blocks"]}, ctx)
        assert "全部通过" in r.text, r.text

    print("PASS: verify blocks（正常/坏块定位/no-compile 豁免/非 Python 围栏跳过）")


def test_verify_tests() -> None:
    """第 2/3 档：真起进程跑测试，退出码说了算。"""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "tests").mkdir()
        ctx = ToolContext(workspace=ws)

        # 边界：没有测试脚本 → 不能"静默通过"，要指出 pattern 和怎么补
        r = dispatch("verify", {"checks": ["tests"]}, ctx)
        assert "没有匹配到测试脚本" in r.text, r.text
        assert "tests/test_*.py" in r.text, "错误文案应给出期望路径"

        # 正常路径
        _w(ws / "tests" / "test_ok.py", "print('PASS: 假用例')\n")
        r = dispatch("verify", {"checks": ["tests"]}, ctx)
        assert "exit 0" in r.text and "全部通过" in r.text, r.text
        assert "PASS: 假用例" in r.text, "末行应是脚本真实输出的最后一行"

        # 错误路径：非零退出要报退出码 + stderr
        _w(ws / "tests" / "test_fail.py", "import sys\nsys.stderr.write('断言炸了')\nsys.exit(9)\n")
        r = dispatch("verify", {"checks": ["tests"]}, ctx)
        assert "exit 9" in r.text and "断言炸了" in r.text, r.text
        assert "1 项未通过" in r.text, r.text

    print("PASS: verify tests（正常 exit 0 / 失败 exit≠0 + stderr / 无脚本给指路）")


def test_verify_params() -> None:
    """参数边界：未知检查项、空 checks、不存在的路径都要给可懂的正解。"""
    with tempfile.TemporaryDirectory() as td:
        ctx = ToolContext(workspace=Path(td))

        r = dispatch("verify", {"checks": ["nope"]}, ctx)
        assert "未知检查项" in r.text and "syntax" in r.text, r.text

        r = dispatch("verify", {"checks": []}, ctx)
        assert "不能为空" in r.text and "syntax" in r.text, r.text

        r = dispatch("verify", {"path": "不存在.md"}, ctx)
        assert "路径不存在" in r.text and "工作空间" in r.text, r.text

        # 只查单个文件时不越界扫全库
        _w(Path(td) / "one.md", "```python\nx=1\n```\n")
        r = dispatch("verify", {"checks": ["blocks"], "path": "one.md"}, ctx)
        assert "1 个 Python 块通过" in r.text, r.text

    print("PASS: verify 参数边界（未知项/空列表/路径不存在/单文件限定）")


def test_repo_self_check() -> None:
    """仓库自身跑一遍：内置技能文档 + 规范文档的代码块与链接必须干净。"""
    ctx = ToolContext(workspace=REPO)
    r = dispatch("verify", {"checks": ["syntax", "links", "blocks"],
                            "path": "skills/coding-standards"}, ctx)
    assert "全部通过" in r.text, r.text

    r = dispatch("verify", {"checks": ["links", "blocks"], "path": "docs/CODING_STANDARDS.md"}, ctx)
    assert "全部通过" in r.text, r.text
    print("PASS: 仓库自检（技能文档 + 规范文档 链接/代码块干净）")


def main() -> None:
    test_builtin_skill()
    test_verify_syntax()
    test_verify_links()
    test_verify_blocks()
    test_verify_tests()
    test_verify_params()
    test_repo_self_check()
    print("\nALL PASS: coding-standards 技能 + verify 工具")


if __name__ == "__main__":
    main()
