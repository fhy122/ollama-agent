"""长文分段精读的离线自测：切段、预算、编排、笔记压缩、取消、工具层。

不依赖 Ollama / 网络：用假客户端顶替模型，只验证「代码调度」这部分是否正确——
小模型读得好不好没法离线测，但「切得对不对、读了几段、笔记有没有传下去、
打断后是不是如实说没读完」这些必须能验证，否则线上就是一片静默失败。

运行: python tests/test_longdoc.py
"""
from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools  # noqa: E402  触发全部工具注册
from agent import longdoc, notebook  # noqa: E402
from agent.registry import ToolContext, dispatch  # noqa: E402

# 笔记目录跟着工作空间走（<ws>/.ollama-agent/notes/），所以每个用例把自己的
# 临时目录当工作空间传进去就天然隔离了。注意必须传：不传会退回当前目录，
# 也就是往仓库里写笔记。


class FakeClient:
    """假模型：记录每次请求，按 prompt 类型返回不同内容。

    on_chunk 可以注入异常/打断，用来测「某段失败要不要跳过」这条路。
    """

    def __init__(self, on_chunk=None, cancel_after=0):
        self.prompts: list[str] = []
        self.on_chunk = on_chunk
        self.cancel_after = cancel_after
        self.seg_calls = 0

    def chat(self, messages, tools=None, on_text=None, on_think=None, cancel=None):
        prompt = messages[-1]["content"]
        self.prompts.append(prompt)
        if "关注点" in prompt and "3-6 行" in prompt:
            return {"content": "- 各型号功率与价格\n- 交付周期",
                    "tool_calls": [], "stats": {}, "cancelled": False, "error": None}
        if "压缩到" in prompt:
            return {"content": "- 压缩后：功率 3.5kW，价格 1200 元，交期 30 天【行1–99】",
                    "tool_calls": [], "stats": {}, "cancelled": False, "error": None}
        # 分段读笔记
        self.seg_calls += 1
        if self.cancel_after and self.seg_calls > self.cancel_after:
            return {"content": "", "tool_calls": [], "stats": {},
                    "cancelled": True, "error": None}
        if self.on_chunk:
            self.on_chunk(self.seg_calls)
        return {"content": f"- 第{self.seg_calls}段：功率 3.5kW，价格 1200 元【出处】",
                "tool_calls": [], "stats": {}, "cancelled": False, "error": None}


def _big_text(n_sent: int = 400, salt: int = 0) -> str:
    """造 n_sent 句中文，每句约 30 字 → 约 1.2 万字。

    salt 用来让不同用例的内容不同：笔记库按**文件内容哈希**索引，
    两个用例内容一样就会命中同一份笔记，让「读了几段」的断言失真。
    """
    return "".join(f"第{salt}批第{i}句：这台设备的额定功率是 3.5 千瓦，"
                   f"售价 1200 元，交付周期 30 天。" for i in range(n_sent))


def test_budget() -> None:
    """单段预算 + 笔记上限要随上下文缩放，且永不把整段挤成 0。"""
    b8 = longdoc.chunk_budget(8192)
    b32 = longdoc.chunk_budget(32768)
    b64 = longdoc.chunk_budget(65536)
    assert b8 < b32 < b64, (b8, b32, b64)
    assert b8 >= longdoc.MIN_CHUNK, f"小上下文也不能低于下限: {b8}"
    # 预算 + 笔记上限 + 保留量 必须装得进上下文，否则每段都会溢出
    for ctx in (8192, 32768, 65536, 131072):
        need = longdoc.chunk_budget(ctx) + longdoc.notes_budget(ctx) + longdoc.RESERVE_TOKENS
        assert need <= ctx, f"ctx={ctx} 预算超了: {need}"
    assert longdoc.notes_budget(8192) < longdoc.notes_budget(65536)
    print(f"PASS: 预算（8K→{b8}字/32K→{b32}字/64K→{b64}字，笔记上限随 ctx 缩放且不超预算）")


def test_split() -> None:
    """切段：不丢字、不重复、顺序不变，行号能对回原文。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "手册.txt"
        raw = _big_text(400)
        p.write_text(raw, encoding="utf-8")

        chunks, total, label = longdoc.build_chunks(p, 2000)
        assert total == len(raw), (total, len(raw))
        assert len(chunks) > 3, f"1.2 万字按 2000 字切应该有多段，实际 {len(chunks)}"
        assert label == "文本"

        # 拼回去必须完全等于原文：一个字的丢失都是静默的数据损坏
        assert "".join(c.text for c in chunks) == raw, "拼回原文不一致"
        for c in chunks:
            assert len(c.text) <= 2000, f"段 {c.index} 超预算: {len(c.text)}"
        assert [c.index for c in chunks] == list(range(1, len(chunks) + 1))

        # 行号标注要在范围内，且能 index 回原文对应位置
        for c in chunks:
            assert c.label.startswith("行"), c.label
        print(f"PASS: 切段（{total} 字 → {len(chunks)} 段，拼接无损、行号标注 {chunks[0].label}）")

        # 单句超预算：必须硬切而不是整段塞进去
        p2 = Path(td) / "无标点.txt"
        p2.write_text("啊" * 5000, encoding="utf-8")
        c2, _t, _l = longdoc.build_chunks(p2, 1000)
        assert all(len(c.text) <= 1000 for c in c2), [len(c.text) for c in c2]
        assert "".join(c.text for c in c2) == "啊" * 5000
        print(f"PASS: 无标点长文本硬切（{len(c2)} 段，均不超预算，拼接无损）")


def test_run_basic() -> None:
    """主流程：需求分析 → 逐段笔记（含前面笔记）→ 最后汇总。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "报告.txt"
        p.write_text(_big_text(400, salt=1), encoding="utf-8")
        notes: list[str] = []
        progress: list[tuple] = []

        fake = FakeClient()
        res = longdoc.run_long_doc(
            fake, p, "这台设备多少钱", num_ctx=8192,
            workspace=p.parent,
            notice=notes.append, _progress=lambda i, n, w: progress.append((i, n, w)))

        assert res.done == res.segments, (res.done, res.segments)
        assert res.done > 1, f"8192 上下文读 1.2 万字应该切多段，实际 {res.done}"
        # 第一次请求是关注点分析，之后每段一次
        assert "3-6 行" in fake.prompts[0], "第一次请求应该是需求分析"
        assert len(fake.prompts) >= res.done + 1
        # 每段的提示词里必须带上关注点，否则「按需求做笔记」无从谈起
        assert any("各型号功率与价格" in s for s in fake.prompts[1:]), "关注点没传进分段请求"
        # 第二段起必须看到前面的笔记（这是「累积」的关键）
        later = fake.prompts[2]
        assert "第1段" in later, "前一段的笔记没传给下一段"
        # 进度回调覆盖所有段
        assert [x[0] for x in progress] == list(range(1, res.done + 1)), progress
        # 最终文本要包含笔记、关注点、以及「只依据笔记」的约束
        assert "【分段笔记】" in res.text and "【读前确定的关注点】" in res.text
        assert "文档中没有提到" in res.text
        assert any("逐段记笔记" in n for n in notes), notes[:2]
        print(f"PASS: 主流程（{res.segments} 段全部读完，关注点与累计笔记逐段传入，进度 {len(progress)} 条）")


def test_skip_and_compress() -> None:
    """单段失败要跳过并如实报告；笔记超限要压缩，且压缩后继续读。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "长文.txt"
        p.write_text(_big_text(400, salt=2), encoding="utf-8")

        def boom(n: int) -> None:
            if n == 2:
                raise RuntimeError("模拟断流")

        fake = FakeClient(on_chunk=boom)
        res = longdoc.run_long_doc(fake, p, "总结", num_ctx=8192,
                                   workspace=p.parent)
        assert res.done == res.segments - 1, (res.done, res.segments)
        assert res.failed and "模拟断流" in res.failed[0], res.failed
        assert "没读到" in res.text and "模拟断流" in res.text, res.text[-400:]
        print(f"PASS: 单段失败跳过（{res.done}/{res.segments} 段成功，失败原因如实回传）")

        # 笔记压缩：每段笔记都写满，总长必然超上限
        fake2 = FakeClient()
        orig = fake2.chat

        def fat_chat(messages, **kw):
            r = orig(messages, **kw)
            if "3-6 行" not in messages[-1]["content"] and "压缩到" not in messages[-1]["content"]:
                r["content"] = "- " + "很长的笔记内容" * 200
            return r

        fake2.chat = fat_chat
        res2 = longdoc.run_long_doc(fake2, p, "总结", num_ctx=8192,
                                    workspace=p.parent)
        compress_calls = [s for s in fake2.prompts if "压缩到" in s]
        assert compress_calls, "笔记超上限却没触发压缩"
        assert "压缩后：功率" in res2.text, "压缩结果没进最终笔记"
        assert res2.done == res2.segments
        print(f"PASS: 笔记超限自动压缩（触发 {len(compress_calls)} 次，压缩后仍读完全部 "
              f"{res2.segments} 段）")


def test_cancel() -> None:
    """打断后必须停下，并如实说明「只读到第几段」，不能假装读完了。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "长文.txt"
        p.write_text(_big_text(400, salt=3), encoding="utf-8")
        fake = FakeClient(cancel_after=2)
        res = longdoc.run_long_doc(fake, p, "总结", num_ctx=8192,
                                   workspace=p.parent)
        assert res.cancelled is True
        assert res.done == 2, res.done
        assert "已取消" in res.text and "只读到第 2 段" in res.text, res.text[:300]
        assert fake.seg_calls <= 3, f"打断后还在继续读: {fake.seg_calls}"

        # Event 版取消（WebUI 的停止按钮走这条）
        ev = threading.Event()
        ev.set()
        fake2 = FakeClient()
        res2 = longdoc.run_long_doc(fake2, p, "总结", num_ctx=8192, cancel=ev,
                                    workspace=p.parent)
        assert res2.cancelled and res2.done == 0, (res2.cancelled, res2.done)
        assert fake2.seg_calls == 0, "取消事件已置位却还是发了分段请求"
        assert "还没有任何笔记" in res2.text or "没读到" in res2.text
        print("PASS: 打断即停（取消返回 cancelled，如实报已读段数，不再发请求）")


def test_single_chunk() -> None:
    """整篇装得下就直接给原文，不把 3 万字压成 1200 字笔记。"""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "短文.txt"
        raw = _big_text(20)
        p.write_text(raw, encoding="utf-8")
        fake = FakeClient()
        res = longdoc.run_long_doc(fake, p, "总结", num_ctx=65536,
                                   workspace=p.parent)
        assert res.segments == 1 and res.done == 1
        assert fake.prompts == [], "装得下时不该再调模型记笔记"
        assert raw in res.text, "原文应完整返回"
        print("PASS: 单段装得下 → 直接返回全文（0 次子请求）")


def test_errors() -> None:
    """不支持的格式、空文件、扫描件要给能照着做的提示。"""
    with tempfile.TemporaryDirectory() as td:
        d = Path(td)
        (d / "空.txt").write_text("   \n\n", encoding="utf-8")
        try:
            longdoc.build_chunks(d / "空.txt", 2000)
            raise AssertionError("空文件应当报错")
        except ValueError as e:
            assert "空" in str(e), e

        (d / "老.doc").write_text("x", encoding="utf-8")
        try:
            longdoc.build_chunks(d / "老.doc", 2000)
            raise AssertionError("旧格式应当报错")
        except ValueError as e:
            assert "另存为" in str(e), e

        (d / "表.xlsx").write_text("x", encoding="utf-8")
        try:
            longdoc.build_chunks(d / "表.xlsx", 2000)
            raise AssertionError("xlsx 应当拒绝分段精读")
        except ValueError as e:
            assert "工作表" in str(e), e
        print("PASS: 错误提示（空文件 / 旧格式另存为 / xlsx 走工作表读）")


def test_tool_layer() -> None:
    """工具层：拿不到 client 要明说，而不是静默返回空。"""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "a.txt").write_text(_big_text(400, salt=4), encoding="utf-8")

        ctx = ToolContext(workspace=ws)          # 没有 client（离线/纯工具场景）
        r = dispatch("read_long_doc", {"path": "a.txt", "question": "多少钱"}, ctx)
        assert "没有可用的聊天客户端" in r.text, r.text[:200]

        ctx2 = ToolContext(workspace=ws, client=FakeClient())
        r2 = dispatch("read_long_doc", {"path": "不存在.txt", "question": "x"}, ctx2)
        assert "文件不存在" in r2.text

        # 正常路径：走 AgentConfig 的 num_ctx（小上下文才会真的分段）
        from agent.config import AgentConfig
        ctx3 = ToolContext(workspace=ws, client=FakeClient(),
                           cfg=AgentConfig(num_ctx=8192))
        r3 = dispatch("read_long_doc", {"path": "a.txt", "question": "多少钱"}, ctx3)
        assert "[长文精读完成]" in r3.text, r3.text[:300]

        # 同一份文件换个大上下文再问：会命中刚写下的笔记（不重读）。
        # 这是笔记库的预期行为——文件没改、问题一样，没有理由再读一遍。
        ctx4 = ToolContext(workspace=ws, client=FakeClient(), cfg=AgentConfig())
        r4 = dispatch("read_long_doc", {"path": "a.txt", "question": "多少钱"}, ctx4)
        assert "【主题】" in r4.text and "【本笔记已有的其他主题】" in r4.text, r4.text[:200]

        # 加 refresh=true 才是「大 ctx 直读全文」那条路：整篇装得下就给原文
        r5 = dispatch("read_long_doc",
                      {"path": "a.txt", "question": "多少钱", "refresh": True}, ctx4)
        assert "[全文已读完]" in r5.text and "第0句" in r5.text, r5.text[:200]
        print("PASS: 工具层（无 client 明确报错 / 文件不存在 / 分段读 / 命中笔记 / refresh 强制直读全文）")


def test_read_file_hint() -> None:
    """read_file 遇到超长文档要主动劝退到 read_long_doc。"""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        (ws / "长.txt").write_text(_big_text(400, salt=5), encoding="utf-8")
        (ws / "短.txt").write_text("第一行\n第二行\n", encoding="utf-8")

        from agent.config import AgentConfig
        ctx = ToolContext(workspace=ws, cfg=AgentConfig())   # ctx 65536 → 每段约 5.2 万字
        r = dispatch("read_file", {"path": "长.txt"}, ctx)
        # 1.2 万字在 64K 上下文里其实装得下，不该乱劝
        assert "read_long_doc" not in r.text, "装得下却劝退到长文精读，属于误伤"

        ctx_small = ToolContext(workspace=ws, cfg=AgentConfig(num_ctx=8192))
        r2 = dispatch("read_file", {"path": "长.txt"}, ctx_small)
        assert "read_long_doc" in r2.text, r2.text[:300]
        assert "不要用 read_file 反复翻页" in r2.text
        print("PASS: read_file 长文引导（64K 不误伤，8K 明确指向 read_long_doc）")


def test_attachment_hint() -> None:
    """用户拖入大文档时，消息里要提前点名用 read_long_doc。"""
    with tempfile.TemporaryDirectory() as td:
        ws = Path(td)
        big = ws / "手册.txt"
        big.write_text("x" * 200_000, encoding="utf-8")
        small = ws / "便签.txt"
        small.write_text("嗯", encoding="utf-8")

        msg = ("请总结\n\n[我附带的文件：短的用 read_file 读；很长（几十万字/几百页）的"
               "必须用 read_long_doc 分段精读]\n- 手册.txt\n- 便签.txt")
        hint = longdoc.attachment_hint(msg, ws, 32768)
        assert "read_long_doc" in hint and "手册.txt" in hint, hint
        assert "便签.txt" not in hint, "小文件不该被点名"
        assert "不要用 read_file 反复翻页" in hint

        no_attach = longdoc.attachment_hint("请总结这份文档", ws, 32768)
        assert no_attach == "", no_attach
        print("PASS: 附件提示（只点名超长文件，未带附件时不打扰）")


def test_note_growth() -> None:
    """核心场景：同一份文档，问题一个个追问，笔记要长成一本、而不是散成多份。

    第1问 → 新主题 A；第2问（同义改写）→ 命中 A，不重读；
    第3问（新主题）→ 精读后并入同一本笔记，A 还在。
    """
    with tempfile.TemporaryDirectory() as td:
        from agent.config import AgentConfig
        ws = Path(td)
        (ws / "doc.txt").write_text(_big_text(400, salt=9), encoding="utf-8")

        class TopicClient(FakeClient):
            """按问题造主题名，方便断言「归到哪个主题」。

            注意判据要看**用户问题那一行**，不能在整个 prompt 里搜关键词——
            提示词里本来就带着已有主题名，搜关键词会永远命中。
            """

            @staticmethod
            def _q(prompt: str) -> str:
                for line in prompt.splitlines():
                    if line.startswith(("现在用户问：", "这段笔记是围绕这个问题记的：")):
                        return line.split("：", 1)[-1]
                return ""

            def chat(self, messages, tools=None, on_text=None, on_think=None, cancel=None):
                prompt = messages[-1]["content"]
                self.prompts.append(prompt)
                q = self._q(prompt)
                if "3-6 行" in prompt:               # 关注点
                    return {"content": "- 功率\n- 价格", "tool_calls": [], "stats": {},
                            "cancelled": False, "error": None}
                if "是否已经包含在上面某个主题里" in prompt:   # 主题命中判断
                    if "功率" in q or "价格" in q:
                        return {"content": "有 功率与价格", "tool_calls": [], "stats": {},
                                "cancelled": False, "error": None}
                    return {"content": "无", "tool_calls": [], "stats": {},
                            "cancelled": False, "error": None}
                if "归到一个主题下" in prompt:         # 归主题
                    name = "功率与价格" if ("功率" in q or "价格" in q) else "交付与售后"
                    return {"content": f"主题：{name}\n位置：第3页", "tool_calls": [],
                            "stats": {}, "cancelled": False, "error": None}
                self.seg_calls += 1
                return {"content": f"- 功率 3.5kW，价格 1200 元【第3页】",
                        "tool_calls": [], "stats": {}, "cancelled": False, "error": None}

        cfg = AgentConfig(num_ctx=8192)
        c = TopicClient()

        # 第1问：全新文档 → 精读并建主题
        ctx = ToolContext(workspace=ws, client=c, cfg=cfg)
        r1 = dispatch("read_long_doc", {"path": "doc.txt", "question": "功率和价格是多少"}, ctx)
        assert "[长文精读完成]" in r1.text, r1.text[:200]

        nb = notebook.load(notebook.file_digest(ws / "doc.txt"), ws)
        assert nb and "功率与价格" in nb.topics, f"应建出主题: {nb and nb.topics}"

        # 第2问：同义改写 → 必须命中，一次都不该再读原文
        before = c.seg_calls
        ctx2 = ToolContext(workspace=ws, client=c, cfg=cfg)
        r2 = dispatch("read_long_doc",
                      {"path": "doc.txt", "question": "功率和价格怎么样"}, ctx2)
        assert "【主题】" in r2.text, r2.text[:200]
        assert c.seg_calls == before, f"命中笔记后不该再读原文，却读了 {c.seg_calls - before} 段"

        # 第3问：新主题 → 精读后并入**同一本**笔记，旧主题不能丢
        ctx3 = ToolContext(workspace=ws, client=c, cfg=cfg)
        dispatch("read_long_doc", {"path": "doc.txt", "question": "交付周期多长"}, ctx3)
        nb2 = notebook.load(notebook.file_digest(ws / "doc.txt"), ws)
        assert nb2 and "功率与价格" in nb2.topics, f"旧主题不能丢: {nb2 and nb2.topics}"
        assert "交付与售后" in nb2.topics, f"新主题应并入同一本: {nb2.topics}"
        assert len(nb2.topics) == 2, nb2.topics
        # FAQ 要记住两次问题，换说法也得能查回来
        assert nb2.find_faq("功率和价格是多少") == "功率与价格", nb2.faq
        assert nb2.find_faq("功率和价格怎么样") == "功率与价格", nb2.faq
        # 同一份文档只该有一本笔记（按内容哈希命名，追问不会另开新文件）
        one = notebook.notebook_path(notebook.file_digest(ws / "doc.txt"), ws)
        assert one.exists(), "这本笔记应落盘"
        # 落点必须在工作空间里，而不是某个全局目录
        assert one.parent == ws / notebook.NOTEBOOK_SUBDIR, f"笔记落错了地方: {one}"
        same_digest = [f for f in one.parent.glob(one.stem[:16] + "*")]
        assert len(same_digest) == 1, f"同一份文档不能有多本: {[f.name for f in same_digest]}"
        print(f"PASS: 笔记生长（1 份文档 = 1 本笔记，主题 {list(nb2.topics)}，"
              f"同义问题命中不再重读，落在 {one.parent.relative_to(ws)}）")


def main() -> None:
    test_budget()
    test_split()
    test_run_basic()
    test_skip_and_compress()
    test_cancel()
    test_single_chunk()
    test_errors()
    test_tool_layer()
    test_read_file_hint()
    test_attachment_hint()
    test_note_growth()
    print("PASS: 长文分段精读全过")


if __name__ == "__main__":
    main()
