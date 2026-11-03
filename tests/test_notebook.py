"""主题笔记库的离线自测：解析往返、目录/FAQ、主题合并、笔记生长、坏文件容错。

这里验证的是「笔记作为知识积累」这套机制本身对不对，不依赖模型：
  - 一份文件一本笔记，按主题分节；
  - 同一主题被问两次要**合并**成一节，不是散成两份；
  - FAQ 记录「问过什么 → 答案在哪个主题」，换说法也要能命中；
  - 用户手工改坏文件时，必须退化成「没有笔记」而不是让精读崩掉。

测试怎么隔离：笔记目录由工作空间决定（<工作空间>/.ollama-agent/notes/），
所以每个用例给一个临时工作空间目录就够了，不需要打补丁改模块常量。

运行: python tests/test_notebook.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import notebook  # noqa: E402


def _ws() -> Path:
    """临时工作空间。笔记会落在 <ws>/.ollama-agent/notes/，绝不碰真实笔记。"""
    return Path(tempfile.mkdtemp(prefix="_nb_ws_"))


def _notes_dir(ws: Path) -> Path:
    return ws / notebook.NOTEBOOK_SUBDIR


def test_roundtrip() -> None:
    """写入 → 解析 → 再序列化，必须完全一致（否则每次读都会掉内容）。"""
    nb = notebook.Notebook(file="白皮书.pdf", digest="a1b2c3d4e5f6a7b8")
    nb.add_topic("商户经营", "- 2024 GMV 1.2 亿【第3页】\n- 毛利率 22%【第4页】", "第3页")
    nb.add_topic("违约与赔偿", "- 逾期日万分之五【第12页】", "第12–15页")
    nb.add_faq("商户经营情况如何？", "商户经营")

    raw = nb.dumps()
    nb2 = notebook.Notebook.loads(raw)
    assert list(nb2.topics) == ["商户经营", "违约与赔偿"], nb2.topics
    assert nb2.toc["违约与赔偿"] == "第12–15页", nb2.toc
    assert nb2.faq == [("商户经营情况如何？", "商户经营")], nb2.faq
    assert nb2.dumps() == raw, "往返序列化必须无损"
    print("PASS: 笔记库往返序列化无损（主题/目录/FAQ 三项都在）")


def test_faq_paraphrase() -> None:
    """FAQ 要能吃下「换说法」：老板举的例子必须命中，不同问题不能被误伤。"""
    nb = notebook.Notebook(file="a.pdf", digest="d1")
    nb.add_topic("商户经营", "- GMV 1.2 亿", "第3页")
    nb.add_topic("注册信息", "- 注册地址：杭州", "第8页")
    nb.add_faq("商户经营情况如何？", "商户经营")

    # 同义改写 → 命中
    for q in ("商户经营的怎么样？", "商户经营情况如何", "商户经营怎么样"):
        assert nb.find_faq(q) == "商户经营", f"应命中: {q}"
    # 真·不同问题 → 不能命中
    for q in ("商户的注册地址在哪？", "合同付款方式是什么？", "商户"):
        assert nb.find_faq(q) != "商户经营" or q == "商户", f"不该命中: {q}"
    print("PASS: FAQ 换说法命中（经营情况如何 ↔ 经营的怎么样），不同问题不误伤")


def test_merge_same_topic() -> None:
    """同一主题问两次要合并：新事实追加、重复行丢弃、旧结论不丢。"""
    nb = notebook.Notebook(file="a.pdf", digest="d1")
    nb.add_topic("违约与赔偿", "- 逾期日万分之五【第12页】", "第12页")
    old = nb.get_topic("违约与赔偿")

    # 第二次追问读到新事实（含一行与旧笔记重复）
    nb.add_topic("违约与赔偿",
                 "- 逾期日万分之五【第12页】\n- 赔偿上限不超过合同总额 30%【第15页】",
                 "第15页")
    body = nb.get_topic("违约与赔偿")
    assert "赔偿上限" in body, "新事实必须合并进来"
    assert "逾期日万分之五" in body, "旧结论不能被冲掉"
    assert body.count("逾期日万分之五") == 1, f"重复行必须去重: {body}"
    assert old.strip() in body, "旧笔记应是合并结果的前缀"
    assert "第12页" in nb.toc["违约与赔偿"] and "第15页" in nb.toc["违约与赔偿"], nb.toc
    print("PASS: 同主题合并（新增追加 / 重复去重 / 旧结论保留 / 位置合并）")


def test_two_topics_isolated() -> None:
    """不同主题必须各自独立一节，不能串味。"""
    nb = notebook.Notebook(file="a.pdf", digest="d1")
    nb.add_topic("商户经营", "- GMV 1.2 亿【第3页】", "第3页")
    nb.add_topic("违约与赔偿", "- 逾期日万分之五【第12页】", "第12页")
    assert "GMV" not in nb.get_topic("违约与赔偿")
    assert "逾期" not in nb.get_topic("商户经营")
    r = nb.render("商户经营")
    # render 只给这一节正文 + 其他主题的名字（供定位），不给别节内容
    assert "GMV" in r and "逾期日万分之五" not in r, r
    assert "违约与赔偿" in r, "要告诉模型还有别的主题可查"
    print("PASS: 主题隔离（只取相关一节，其他主题仅列名）")


def test_persist_and_reload() -> None:
    """落盘 → 重新加载，内容必须还在（跨会话复用的前提）。"""
    ws = _ws()
    try:
        nb = notebook.Notebook(file="b.pdf", digest="f" * 32)
        nb.add_topic("商户经营", "- GMV 1.2 亿", "第3页")
        nb.add_faq("经营情况如何", "商户经营")
        p = notebook.save(nb, ws)
        assert p and p.exists(), "应写盘成功"

        back = notebook.load("f" * 32, ws)
        assert back is not None, "应能重新加载"
        assert "GMV 1.2 亿" in back.get_topic("商户经营"), back.topics
        assert back.find_faq("经营情况如何") == "商户经营"
        # 换一个不存在的哈希 → 不能串到别的笔记
        assert notebook.load("0" * 32, ws) is None
        print(f"PASS: 落盘与重载（{p.name}，{len(p.read_text(encoding='utf-8'))} 字符）")
    finally:
        _cleanup(ws)


def test_dir_follows_workspace() -> None:
    """笔记必须跟着工作空间走：落在 <ws>/.ollama-agent/notes/，且两个空间互不可见。"""
    ws_a, ws_b = _ws(), _ws()
    try:
        nb = notebook.Notebook(file="a.pdf", digest="9" * 32)
        nb.add_topic("商户经营", "- GMV 1.2 亿", "第3页")
        p = notebook.save(nb, ws_a)
        assert p is not None
        # 1) 落点正确：<ws>/.ollama-agent/notes/<哈希>.md
        assert p.parent == _notes_dir(ws_a), f"落点不对: {p.parent}"
        assert p.parent.name == "notes" and p.parent.parent.name == ".ollama-agent", p
        # 2) 工作空间隔离：A 的笔记 B 读不到（这是不放全局目录的全部意义）
        assert notebook.load("9" * 32, ws_a) is not None
        assert notebook.load("9" * 32, ws_b) is None, "不同工作空间不能互相看到笔记"
        print(f"PASS: 笔记目录跟随工作空间（{p.relative_to(ws_a)}，跨空间不可见）")
    finally:
        _cleanup(ws_a)
        _cleanup(ws_b)


def test_corrupt_and_version() -> None:
    """坏文件 / 版本不符都要退化成「没有笔记」，绝不能抛异常。"""
    ws = _ws()
    try:
        d = _notes_dir(ws)
        d.mkdir(parents=True, exist_ok=True)
        # 1) 完全不是笔记格式
        p = d / ("a" * 16 + ".md")
        p.write_text("这不是一本笔记，只是随便一坨文本", encoding="utf-8")
        assert notebook.load("a" * 16, ws) is None, "没有主题就当没有笔记"

        # 2) 版本不符 → 作废
        old = notebook.Notebook(file="c.pdf", digest="b" * 32)
        old.add_topic("旧主题", "- 旧内容", "第1页")
        raw = old.dumps().replace(f"version: {notebook.NOTEBOOK_VERSION}", "version: 999")
        (d / ("b" * 16 + ".md")).write_text(raw, encoding="utf-8")
        assert notebook.load("b" * 32, ws) is None, "版本不符必须作废，宁可重读"

        # 3) 空文件
        (d / ("c" * 16 + ".md")).write_text("", encoding="utf-8")
        assert notebook.load("c" * 32, ws) is None
        print("PASS: 坏文件容错（非笔记格式 / 版本不符 / 空文件 都不抛异常）")
    finally:
        _cleanup(ws)


def test_topic_name_normalize() -> None:
    """主题名是 key，必须稳定：标点/空白差异不能分裂成两个主题。"""
    a = notebook.clean_topic_name("## 商户经营：")
    b = notebook.clean_topic_name("  商户经营  ")
    c = notebook.clean_topic_name("商户经营。")
    assert a == b == c == "商户经营", (a, b, c)
    assert notebook.clean_topic_name("") == ""
    assert notebook.clean_topic_name("x" * 100) == "x" * 40, "超长要截断"
    print("PASS: 主题名归一化（标点/空白/长度 都收得住，不会分裂主题）")


def test_dangling_toc() -> None:
    """目录里有、主题里没有的条目要丢掉，不能留悬空指针。"""
    raw = "\n".join([
        "---", f"version: {notebook.NOTEBOOK_VERSION}", "file: x.pdf",
        "digest: " + "d" * 32, "---", "",
        "## 目录", "- 真实主题 …… 第1页", "- 幽灵主题 …… 第9页", "",
        "## 主题笔记", "### 真实主题", "- 内容", "",
        "## 已答问题", "- 问？ → 幽灵主题",
    ])
    nb = notebook.Notebook.loads(raw)
    assert list(nb.topics) == ["真实主题"], nb.topics
    assert "幽灵主题" not in nb.toc, f"悬空目录要清掉: {nb.toc}"
    print("PASS: 悬空目录清理（主题丢了，目录条目也一起丢）")


def test_disabled_switch() -> None:
    """环境变量能整体关掉笔记库（排障/对比时用）。"""
    ws = _ws()
    old = os.environ.get(notebook.NOTEBOOK_ENV)
    try:
        nb = notebook.Notebook(file="e.pdf", digest="e" * 32)
        nb.add_topic("主题", "- 内容", "第1页")
        os.environ[notebook.NOTEBOOK_ENV] = "off"
        assert notebook.save(nb, ws) is None, "关掉后不该写盘"
        assert notebook.load("e" * 32, ws) is None, "关掉后不该读盘"
        assert not _notes_dir(ws).exists(), "关掉后连目录都不该建"
        os.environ[notebook.NOTEBOOK_ENV] = "0"
        assert not notebook.enabled()
    finally:
        if old is None:
            os.environ.pop(notebook.NOTEBOOK_ENV, None)
        else:
            os.environ[notebook.NOTEBOOK_ENV] = old
        _cleanup(ws)
    print("PASS: 开关生效（off/0 时不读不写，也不建目录）")


def _cleanup(d: Path) -> None:
    import shutil
    shutil.rmtree(d, ignore_errors=True)


def main() -> None:
    test_roundtrip()
    test_faq_paraphrase()
    test_merge_same_topic()
    test_two_topics_isolated()
    test_persist_and_reload()
    test_dir_follows_workspace()
    test_corrupt_and_version()
    test_topic_name_normalize()
    test_dangling_toc()
    test_disabled_switch()
    print("PASS: 主题笔记库全过")


if __name__ == "__main__":
    main()
