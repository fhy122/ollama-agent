"""离线自测：不依赖 Ollama / 网络。运行: python tests/test_tools.py"""
from __future__ import annotations

import importlib.util
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools  # noqa: E402  触发全部工具注册
from agent.registry import ToolContext, all_specs, dispatch  # noqa: E402


def _make_docx(path: Path) -> None:
    """用 python-docx 造两段测试文档。"""
    from docx import Document
    doc = Document()
    doc.add_paragraph("第一段你好")
    doc.add_paragraph("第二段世界")
    doc.save(path)


def _make_xlsx(path: Path) -> None:
    """用 openpyxl 造 2 行 2 列测试表。"""
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "数据表"
    ws.append(["名称", "数值"])
    ws.append(["苹果", 42])
    wb.save(path)


def _pymupdf():
    """PDF 用例需要 pymupdf；没装就跳过（不影响其它格式回归）。"""
    try:
        import pymupdf
        return pymupdf
    except ImportError:
        return None


def _make_pdf(path: Path, pages: list[str]) -> None:
    """造带文字层的 PDF（每页一行中文）。"""
    pymupdf = _pymupdf()
    doc = pymupdf.open()
    for txt in pages:
        pg = doc.new_page()
        pg.insert_text((72, 100), txt, fontname="china-s", fontsize=16)
    doc.save(path)
    doc.close()


def _make_scanned_pdf(path: Path, src: Path) -> None:
    """把 src 首页渲染成图片再贴进新 PDF —— 只有图、没有文字层，模拟扫描件。"""
    pymupdf = _pymupdf()
    pix = pymupdf.open(str(src))[0].get_pixmap(matrix=pymupdf.Matrix(1.5, 1.5))
    doc = pymupdf.open()
    doc.new_page().insert_image(pymupdf.Rect(50, 50, 545, 792), pixmap=pix)
    doc.save(path)
    doc.close()


def main() -> None:
    specs = all_specs()
    names = {s["function"]["name"] for s in specs}
    expected = {"read_file", "write_file", "edit_file", "list_dir", "load_image",
                "browse_url", "web_search", "clipboard_image", "skill",
                "delete_file", "run_command", "grep_file", "glob_file", "render_sheet",
                "web_screenshot"}
    # web_chat 是可选私有模块（公开仓库不含），在就要求注册
    if importlib.util.find_spec("tools.web_chat") is not None:
        expected.add("web_chat")
    missing = expected - names
    assert not missing, f"缺少工具: {missing}"
    assert len(tools.LOADED_MODULES) == len(specs), "工具模块数与注册数不一致"

    with tempfile.TemporaryDirectory() as td:
        ws = Path(td) / "ws"
        ws.mkdir()
        outside = Path(td) / "outside"
        outside.mkdir()
        ctx = ToolContext(workspace=ws)

        # ---- 工作空间：相对路径锚定 ----
        r = dispatch("write_file", {"path": "notes/hello.txt", "content": "你好\n世界"}, ctx)
        assert "已写入" in r.text and "工作空间" in r.text, r.text
        assert (ws / "notes" / "hello.txt").exists(), "相对路径应写进工作空间"

        r = dispatch("read_file", {"path": "notes/hello.txt"}, ctx)
        assert "你好" in r.text and "世界" in r.text, r.text

        r = dispatch("read_file", {"path": "notes/hello.txt", "offset": 1, "limit": 1}, ctx)
        assert "世界" in r.text and "你好" not in r.text, r.text

        # ---- 绝对路径：按原样（可出工作空间）----
        ext = outside / "ext.txt"
        r = dispatch("write_file", {"path": str(ext), "content": "外部"}, ctx)
        assert "外部路径" in r.text and ext.exists(), r.text
        r = dispatch("read_file", {"path": str(ext)}, ctx)
        assert "外部" in r.text, r.text

        # ---- list_dir 默认工作空间 ----
        r = dispatch("list_dir", {}, ctx)
        assert "notes" in r.text, r.text

        # ---- Word 文档 ----
        docx = ws / "报告.docx"
        _make_docx(docx)
        r = dispatch("read_file", {"path": "报告.docx"}, ctx)
        assert "Word" in r.text and "第一段你好" in r.text and "第二段世界" in r.text, r.text

        # ---- Excel 表格 ----
        xlsx = ws / "数据.xlsx"
        _make_xlsx(xlsx)
        r = dispatch("read_file", {"path": "数据.xlsx"}, ctx)
        assert "Excel" in r.text and "数据表" in r.text, r.text
        assert "名称" in r.text and "苹果" in r.text and "42" in r.text, r.text

        # ---- 旧版格式提示 ----
        old = ws / "old.xls"
        old.write_bytes(b"\xd0\xcf\x11\xe0 fake ole")
        r = dispatch("read_file", {"path": "old.xls"}, ctx)
        assert "另存为" in r.text or "错误" in r.text, r.text

        # ---- PDF：文字层抽取 / 页码选择 / 扫描件引导 / 渲染成图 ----
        if _pymupdf() is None:
            print("跳过 PDF 用例：未安装 pymupdf")
        else:
            _make_pdf(ws / "合同.pdf", ["第一页 合同金额", "第二页 付款方式", "第三页 违约责任"])
            r = dispatch("read_file", {"path": "合同.pdf"}, ctx)
            assert "PDF" in r.text and "共 3 页" in r.text, r.text
            assert "第一页" in r.text and "第三页" in r.text, r.text
            # 短文本 PDF 不能被误判成扫描件（按总字数判会误伤，必须是"本次显示页全空"才报）
            assert "扫描件" not in r.text, f"短文本 PDF 被误判为扫描件: {r.text}"

            # 页码选择：只要第 2 页
            r = dispatch("read_file", {"path": "合同.pdf", "pages": "2"}, ctx)
            assert "第二页" in r.text and "第一页" not in r.text, r.text
            r = dispatch("read_file", {"path": "合同.pdf", "pages": "1-2,3"}, ctx)
            assert "第一页" in r.text and "第三页" in r.text, r.text

            # 越界页码给出可懂的错，而不是空结果
            r = dispatch("read_file", {"path": "合同.pdf", "pages": "9"}, ctx)
            assert "超出范围" in r.text, r.text

            # 扫描件：抽不到字 → 明确引导去 load_image 渲染
            _make_scanned_pdf(ws / "扫描件.pdf", ws / "合同.pdf")
            r = dispatch("read_file", {"path": "扫描件.pdf"}, ctx)
            assert "抽不到文字" in r.text and "load_image" in r.text, r.text

            # load_image 能把 PDF 页渲染成 PNG 交给视觉模型
            r = dispatch("load_image", {"path": "扫描件.pdf", "page": 1}, ctx)
            assert "已渲染成图片" in r.text and "第 1/1 页" in r.text, r.text
            assert r.images and len(r.images) == 1, "PDF 渲染结果必须带图"
            import base64
            assert base64.b64decode(r.images[0]).startswith(b"\x89PNG\r\n\x1a\n"), "渲染结果应是 PNG"

            # 不许生成伪 PDF
            r = dispatch("write_file", {"path": "假.pdf", "content": "hi"}, ctx)
            assert "不支持直接生成" in r.text and not (ws / "假.pdf").exists(), r.text

        # ---- 写 xlsx：CSV → 真 Excel（zip 容器 + 回读还原）----
        csv_content = (
            "项目名称,项目编号,联系人,电话\n"
            "清运雪项目,3251,张三,15065701922\n"
            "防护检测,3256,李四,01012345678\n"
        )
        r = dispatch("write_file", {"path": "10.25-26.xlsx", "content": csv_content}, ctx)
        assert "已写入" in r.text and "Excel" in r.text, r.text
        xf = ws / "10.25-26.xlsx"
        assert xf.read_bytes()[:2] == b"PK", "xlsx 应是 zip 容器，不是纯文本"
        r = dispatch("read_file", {"path": "10.25-26.xlsx"}, ctx)
        assert "项目名称" in r.text and "3251" in r.text and "15065701922" in r.text, r.text
        # 数值/文本类型：42 类数字是数值单元格；前导零电话保持文本
        r = dispatch("write_file", {"path": "types.xlsx", "content": "qty,phone\n42,0101234"}, ctx)
        assert "已写入" in r.text, r.text
        from openpyxl import load_workbook
        wb = load_workbook(ws / "types.xlsx")
        sheet = wb.active
        assert sheet["A2"].value == 42 and isinstance(sheet["A2"].value, (int, float)), sheet["A2"].value
        assert sheet["B2"].value == "0101234" and isinstance(sheet["B2"].value, str), sheet["B2"].value

        # ---- 写 xlsx：Markdown 表格 + '=== 表名 ===' 多工作表 ----
        md_sheets = (
            "=== 汇总 ===\n"
            "| 名称 | 数值 |\n|---|---|\n| 苹果 | 42 |\n"
            "=== 明细 ===\n"
            "| a | b |\n|---|---|\n| 1 | 2 |\n"
        )
        r = dispatch("write_file", {"path": "multi.xlsx", "content": md_sheets}, ctx)
        assert "2 个工作表" in r.text, r.text
        r = dispatch("read_file", {"path": "multi.xlsx"}, ctx)
        assert "汇总" in r.text and "明细" in r.text and "苹果" in r.text, r.text

        # ---- 写 xlsx：JSON 多表 ----
        js = '{"汇总": [["名称","数值"],["苹果",42]], "明细": [["a","b"],["x","y"]]}'
        r = dispatch("write_file", {"path": "json.xlsx", "content": js}, ctx)
        assert "2 个工作表" in r.text, r.text

        # ---- 写 xlsx：非法 JSON 报错回传 ----
        r = dispatch("write_file", {"path": "bad.xlsx", "content": "{not json"}, ctx)
        assert "错误" in r.text and "JSON" in r.text, r.text

        # ---- 写 xlsx：JSON 二维数组（分组续行用空字符串占位，不错位）----
        js2d = ('[["项目","公司","联系人","邮箱"],'
                '["3251","嘉乔","张三","a@b.c"],'
                '["","盛璟","李四","d@e.f"]]')
        r = dispatch("write_file", {"path": "js2d.xlsx", "content": js2d}, ctx)
        assert "已写入" in r.text, r.text
        wb = load_workbook(ws / "js2d.xlsx")
        sheet = wb.active
        assert sheet["B3"].value == "盛璟" and sheet["D3"].value == "d@e.f", (
            sheet["A3"].value, sheet["B3"].value, sheet["C3"].value, sheet["D3"].value)
        assert sheet["A3"].value is None, sheet["A3"].value   # 空串占位 → 真空单元格

        # ---- 写 xlsx：参差行必须报错（防静默错位）----
        ragged = ("项目\t公司\t联系人\t邮箱\n"
                  "3251\t嘉乔\t张三\ta@b.c\n"
                  "国检\t谢丽杰\tc@d.e")            # 续行漏了首列 → 3 列
        r = dispatch("write_file", {"path": "ragged.xlsx", "content": ragged}, ctx)
        assert "错误" in r.text and "列数不一致" in r.text, r.text
        assert not (ws / "ragged.xlsx").exists(), "参差行不应产出文件"

        # ---- 写 xlsx：Markdown 表格参差行同样报错 ----
        md_ragged = "| a | b | c |\n|---|---|---|\n| 1 | 2 |\n"
        r = dispatch("write_file", {"path": "md_ragged.xlsx", "content": md_ragged}, ctx)
        assert "错误" in r.text and "列数不一致" in r.text, r.text

        # ---- 写 docx：Markdown 子集 → 真 Word（zip 容器 + 回读还原）----
        doc_md = (
            "# 项目报告\n\n## 背景\n\n正文段落，**重点**文字。\n\n"
            "- 无序一\n- 无序二\n\n1. 有序一\n2. 有序二\n\n"
            "| 列A | 列B |\n|---|---|\n| 1 | 2 |\n"
        )
        r = dispatch("write_file", {"path": "报告2.docx", "content": doc_md}, ctx)
        assert "已写入" in r.text and "Word" in r.text, r.text
        df = ws / "报告2.docx"
        assert df.read_bytes()[:2] == b"PK", "docx 应是 zip 容器，不是纯文本"
        r = dispatch("read_file", {"path": "报告2.docx"}, ctx)
        assert ("项目报告" in r.text and "背景" in r.text and "正文段落" in r.text
                and "无序一" in r.text and "有序一" in r.text and "列A" in r.text), r.text

        # ---- 写旧版二进制格式：拒绝 ----
        r = dispatch("write_file", {"path": "old2.xls", "content": "a,b\n1,2"}, ctx)
        assert "错误" in r.text and "xlsx" in r.text, r.text
        r = dispatch("write_file", {"path": "old2.doc", "content": "正文"}, ctx)
        assert "错误" in r.text and "docx" in r.text, r.text

        # ---- 写 pptx：暂不支持 ----
        r = dispatch("write_file", {"path": "deck.pptx", "content": "第一页"}, ctx)
        assert "错误" in r.text and "pptx" in r.text, r.text

        # ---- 二进制兜底 ----
        binf = ws / "blob.bin"
        binf.write_bytes(bytes(range(256)))
        r = dispatch("read_file", {"path": "blob.bin"}, ctx)
        assert "二进制" in r.text or "错误" in r.text, r.text

        # ---- 错误路径 ----
        r = dispatch("load_image", {"path": "nope.png"}, ctx)
        assert "不存在" in r.text and "工作空间" in r.text, r.text
        r = dispatch("read_file", {"path": "missing.txt"}, ctx)
        assert "不存在" in r.text, r.text
        r = dispatch("no_such_tool", {}, ctx)
        assert "未知工具" in r.text, r.text

        # ---- load_image 类型校验（别把文档当图片，会让模型 400）----
        # ① xlsx/csv 等文档 → 报错并指名 read_file，绝不返回 images
        r = dispatch("load_image", {"path": "数据.xlsx"}, ctx)
        assert "错误" in r.text and "read_file" in r.text and not r.images, r.text
        csvf = ws / "img_test.csv"
        csvf.write_text("a,b\n1,2\n", encoding="utf-8")
        r = dispatch("load_image", {"path": "img_test.csv"}, ctx)
        assert "错误" in r.text and "read_file" in r.text and not r.images, r.text
        # ② 扩展名是 .png 但内容是别的文件（改名/损坏）→ 文件头不匹配，拒绝
        fake_png = ws / "假的.png"
        fake_png.write_bytes((ws / "数据.xlsx").read_bytes())
        r = dispatch("load_image", {"path": "假的.png"}, ctx)
        assert "错误" in r.text and "文件头" in r.text and not r.images, r.text
        # ③ 空文件
        (ws / "空.png").write_bytes(b"")
        r = dispatch("load_image", {"path": "空.png"}, ctx)
        assert "错误" in r.text and "空文件" in r.text, r.text
        # ④ 真 PNG → 正常附图
        from tools.load_image import sniff_image
        assert sniff_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32) == "png"
        assert sniff_image(b"\xff\xd8\xff\xe0" + b"\x00" * 32) == "jpeg"
        assert sniff_image(b"GIF89a" + b"\x00" * 32) == "gif"
        assert sniff_image(b"BM" + b"\x00" * 32) == "bmp"
        assert sniff_image(b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8) == "webp"
        assert sniff_image(b"PK\x03\x04" + b"\x00" * 32) is None   # zip/xlsx
        real_png = ws / "真图.png"
        real_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        r = dispatch("load_image", {"path": "真图.png"}, ctx)
        assert r.images and len(r.images) == 1 and "png" in r.text, r.text
        print("PASS: load_image 类型校验（文档拒绝+引导 read_file / 假图头 / 空文件 / 魔数识别）")

        # ---- render_sheet：表格渲染成图片（「截个屏看看做得对不对」）----
        r = dispatch("render_sheet", {"path": "数据.xlsx"}, ctx)
        assert r.images and len(r.images) == 1, r.text
        assert "表格已渲染" in r.text, r.text
        assert r.images[0].startswith("iVBORw0K"), "渲染结果应是 PNG"
        r = dispatch("render_sheet", {"path": "数据.xlsx", "sheet": "不存在的表"}, ctx)
        assert "错误" in r.text and "不存在" in r.text and not r.images, r.text
        r = dispatch("render_sheet", {"path": "blob.bin"}, ctx)
        assert "错误" in r.text and "xlsx" in r.text, r.text
        r = dispatch("render_sheet", {"path": "missing.xlsx"}, ctx)
        assert "不存在" in r.text, r.text
        print("PASS: render_sheet（xlsx→PNG、错表名/非表格/文件不存在）")

        # ---- web_screenshot：网页/HTML 截成图片（「截个图看看长什么样」）----
        # 用 html 参数走本地渲染，不联网，保证离线自测
        r = dispatch("web_screenshot", {
            "html": "<h1 style='color:#1f4e79'>截图测试</h1><p>版式检查</p>",
            "width": 800, "height": 600}, ctx)
        assert r.images and len(r.images) == 1, r.text
        assert r.images[0].startswith("iVBORw0K"), "截图结果应是 PNG"
        assert "已截图" in r.text and "800x600" in r.text, r.text
        # 全页长图分支也要走通
        r = dispatch("web_screenshot", {
            "html": "<body style='margin:0'><div style='height:1600px'>长页</div></body>",
            "full_page": True}, ctx)
        assert r.images and len(r.images) == 1, r.text
        # 参数校验：都给 / 都不给 / 非 http 协议
        r = dispatch("web_screenshot", {"url": "https://example.com", "html": "<p>x</p>"}, ctx)
        assert "错误" in r.text and "二选一" in r.text, r.text
        r = dispatch("web_screenshot", {}, ctx)
        assert "错误" in r.text and "至少" in r.text, r.text
        r = dispatch("web_screenshot", {"url": "ftp://example.com"}, ctx)
        assert "错误" in r.text and "http" in r.text, r.text
        print("PASS: web_screenshot（HTML→PNG、全页长图、二选一/空参/协议校验）")

        # ---- delete_file：删文件 / 空目录 / 非空目录保护 / recursive ----
        del_f = ws / "todel.txt"
        del_f.write_text("垃圾", encoding="utf-8")
        r = dispatch("delete_file", {"path": "todel.txt"}, ctx)
        assert "已删除" in r.text and not del_f.exists(), r.text

        r = dispatch("delete_file", {"path": "ghost.txt"}, ctx)
        assert "不存在" in r.text, r.text

        empty_d = ws / "empty_dir"
        empty_d.mkdir()
        r = dispatch("delete_file", {"path": "empty_dir"}, ctx)
        assert "已删除" in r.text and not empty_d.exists(), r.text

        full_d = ws / "full_dir"
        (full_d / "sub").mkdir(parents=True)
        (full_d / "sub" / "a.txt").write_text("a", encoding="utf-8")
        r = dispatch("delete_file", {"path": "full_dir"}, ctx)
        assert "错误" in r.text and "recursive" in r.text and full_d.exists(), r.text
        r = dispatch("delete_file", {"path": "full_dir", "recursive": True}, ctx)
        assert "已删除" in r.text and not full_d.exists(), r.text

        # 安全底线：工作空间根不给删
        r = dispatch("delete_file", {"path": "."}, ctx)
        assert "错误" in r.text and "工作空间根" in r.text and ws.exists(), r.text

        # 安全底线：工作空间外不给删（与 read/write 不同，删除是破坏性操作）
        out_f = outside / "ext.txt"
        r = dispatch("delete_file", {"path": str(out_f)}, ctx)
        assert "错误" in r.text and "工作空间" in r.text and out_f.exists(), r.text

        # --confirm 拒绝路径：用户拒绝时文件保留
        keep = ws / "keep.txt"
        keep.write_text("留", encoding="utf-8")
        ctx_deny = ToolContext(workspace=ws, confirm=True, ask=lambda what: False)
        r = dispatch("delete_file", {"path": "keep.txt"}, ctx_deny)
        assert "拒绝" in r.text and keep.exists(), r.text
        ctx_ok = ToolContext(workspace=ws, confirm=True, ask=lambda what: True)
        r = dispatch("delete_file", {"path": "keep.txt"}, ctx_ok)
        assert "已删除" in r.text and not keep.exists(), r.text

        # ---- run_command：执行/退出码/stderr/工作目录/超时/空命令/确认闸 ----
        r = dispatch("run_command", {"command": "echo hello-run"}, ctx)
        assert "hello-run" in r.text and "exit 0" in r.text, r.text

        r = dispatch("run_command", {"command": f'"{sys.executable}" -c "import sys; sys.exit(3)"'}, ctx)
        assert "exit 3" in r.text, r.text

        r = dispatch("run_command", {"command": f'"{sys.executable}" -c "import sys; sys.stderr.write(\'boom\'); sys.exit(1)"'}, ctx)
        assert "exit 1" in r.text and "boom" in r.text, r.text

        sub = ws / "sub"
        sub.mkdir(exist_ok=True)
        r = dispatch("run_command", {"command": "echo cwd-test > out.txt", "cwd": "sub"}, ctx)
        assert "exit 0" in r.text and (sub / "out.txt").exists(), r.text
        assert "cwd-test" in (sub / "out.txt").read_text(encoding="utf-8", errors="replace"), r.text

        r = dispatch("run_command", {"command": f'"{sys.executable}" -c "import time; time.sleep(5)"', "timeout": 1}, ctx)
        assert "超时" in r.text, r.text

        r = dispatch("run_command", {"command": "  "}, ctx)
        assert "不能为空" in r.text, r.text

        # 确认闸：拒绝时不执行，放行才执行
        ctx_deny2 = ToolContext(workspace=ws, confirm=True, ask=lambda what: False)
        r = dispatch("run_command", {"command": "echo no > gated.txt"}, ctx_deny2)
        assert "拒绝" in r.text and not (ws / "gated.txt").exists(), r.text
        r = dispatch("run_command", {"command": "echo yes > gated.txt"}, ctx_ok)
        assert "exit 0" in r.text and (ws / "gated.txt").exists(), r.text

        # ---- grep_file：定位/过滤/忽略大小写/无命中/坏正则/二进制跳过 ----
        pkg = ws / "pkg"
        pkg.mkdir(exist_ok=True)
        (pkg / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
        (pkg / "b.py").write_text("def beta():\n    from pkg.a import alpha\n", encoding="utf-8")
        (pkg / "notes.txt").write_text("alpha beta\n", encoding="utf-8")
        (pkg / "blob.bin").write_bytes(b"alpha\x00\x01\x02")

        r = dispatch("grep_file", {"pattern": "def alpha"}, ctx)
        assert "a.py:1" in r.text and "def alpha" in r.text, r.text

        r = dispatch("grep_file", {"pattern": "alpha", "glob": "*.py"}, ctx)
        assert "pkg/a.py" in r.text and "pkg/b.py" in r.text and "notes.txt" not in r.text, r.text

        r = dispatch("grep_file", {"pattern": "ALPHA", "ignore_case": True, "glob": "*.txt"}, ctx)
        assert "notes.txt" in r.text, r.text

        r = dispatch("grep_file", {"pattern": "def alpha", "path": "pkg/a.py"}, ctx)
        assert "a.py:1" in r.text, r.text

        r = dispatch("grep_file", {"pattern": "zzz_nomatch_xxx"}, ctx)
        assert "无命中" in r.text, r.text

        r = dispatch("grep_file", {"pattern": "def ["}, ctx)
        assert "错误" in r.text and "正则" in r.text, r.text

        # ---- glob_file：递归/带路径/无匹配/目录要求 ----
        r = dispatch("glob_file", {"pattern": "*.py"}, ctx)
        assert "pkg/a.py" in r.text and "pkg/b.py" in r.text, r.text

        r = dispatch("glob_file", {"pattern": "pkg/*.py"}, ctx)
        assert "pkg/a.py" in r.text and "pkg/b.py" in r.text, r.text

        r = dispatch("glob_file", {"pattern": "notes.txt"}, ctx)
        assert "notes.txt" in r.text, r.text

        r = dispatch("glob_file", {"pattern": "*.nomatch"}, ctx)
        assert "无匹配" in r.text, r.text

        r = dispatch("glob_file", {"pattern": "*.py", "path": "pkg/a.py"}, ctx)
        assert "错误" in r.text and "目录" in r.text, r.text

        # ---- edit_file：不唯一拒绝/精确替换/replace_all/不在文中/不存在/确认闸 ----
        ef = ws / "mod.py"
        ef.write_text("def f():\n    return 1\n\ndef g():\n    return 1\n", encoding="utf-8")

        r = dispatch("edit_file", {"path": "mod.py", "old_string": "return 1", "new_string": "return 42"}, ctx)
        assert "错误" in r.text and "2" in r.text and "replace_all" in r.text, r.text

        r = dispatch("edit_file", {"path": "mod.py", "old_string": "def f():\n    return 1",
                                   "new_string": "def f():\n    return 42"}, ctx)
        assert "已修改" in r.text and "替换 1 处" in r.text, r.text
        txt = ef.read_text(encoding="utf-8")
        assert "return 42" in txt and txt.count("return 1") == 1, txt

        r = dispatch("edit_file", {"path": "mod.py", "old_string": "return 1",
                                   "new_string": "return 42", "replace_all": True}, ctx)
        assert "已修改" in r.text and "替换 1 处" in r.text, r.text
        assert ef.read_text(encoding="utf-8").count("return 42") == 2, "replace_all 应替换剩下那处"

        r = dispatch("edit_file", {"path": "mod.py", "old_string": "no_such_text", "new_string": "x"}, ctx)
        assert "错误" in r.text and "不在" in r.text, r.text

        r = dispatch("edit_file", {"path": "ghost.py", "old_string": "a", "new_string": "b"}, ctx)
        assert "不存在" in r.text and "write_file" in r.text, r.text

        r = dispatch("edit_file", {"path": "mod.py", "old_string": "", "new_string": "x"}, ctx)
        assert "错误" in r.text and "write_file" in r.text, r.text

        r = dispatch("edit_file", {"path": "pkg/blob.bin", "old_string": "a", "new_string": "b"}, ctx)
        assert "错误" in r.text and "二进制" in r.text, r.text

        ctx_deny3 = ToolContext(workspace=ws, confirm=True, ask=lambda what: False)
        r = dispatch("edit_file", {"path": "mod.py", "old_string": "return 42",
                                   "new_string": "return 7"}, ctx_deny3)
        assert "拒绝" in r.text and "return 42" in ef.read_text(encoding="utf-8"), r.text

    # ---- 剪贴板：写图→抓图→PNG 像素往返 ----
    try:
        from agent.clipboard import (grab_and_cache, grab_image_png,
                                     png_encode, dib_to_rgba, _set_dib_for_test)
        w, h = 3, 2
        rgba = bytes([
            255, 0, 0, 255,   0, 255, 0, 255,   0, 0, 255, 255,
            10, 20, 30, 255,  40, 50, 60, 255,  70, 80, 90, 255,
        ])
        png = png_encode(w, h, rgba)
        assert png[:8] == b"\x89PNG\r\n\x1a\n", "PNG 魔数不对"

        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            ctx = ToolContext(workspace=ws)
            _set_dib_for_test(w, h, rgba)
            got = grab_image_png()
            gw, gh = struct.unpack(">II", got[16:24])
            assert (gw, gh) == (w, h), f"尺寸不对: {gw}x{gh}"

            r = dispatch("clipboard_image", {"save_as": "shots/clip.png"}, ctx)
            assert "剪贴板图片" in r.text and r.images, r.text
            assert (ws / ".clip_cache").exists(), "缓存目录应存在"
            assert (ws / "shots" / "clip.png").exists(), "save_as 未生效"

            # save_as 只许图片后缀：曾让模型把 PNG 字节写进 .pdf，
            # 之后 read_file 按 PDF 解析失败、load_image 读出一张「纯红色空白页」
            r = dispatch("clipboard_image", {"save_as": "假文档.pdf"}, ctx)
            assert "错误" in r.text and ".pdf" in r.text, r.text
            assert not (ws / "假文档.pdf").exists(), "非图片后缀不应落盘"

            # 已有同名文件时改名，绝不覆盖（曾把用户 PDF 覆盖成 70 字节截图）
            keep = ws / "shots" / "keep.png"
            keep.write_bytes(b"ORIGINAL-DATA")
            r = dispatch("clipboard_image", {"save_as": "shots/keep.png"}, ctx)
            assert keep.read_bytes() == b"ORIGINAL-DATA", "原文件被覆盖了！"
            assert (ws / "shots" / "keep_1.png").exists(), "应改存为 keep_1.png"

        print("PASS: 剪贴板 DIB→PNG 往返 + clipboard_image 缓存/save_as")

        # ---- 剪贴板文件（Ctrl+C / CF_HDROP）----
        with tempfile.TemporaryDirectory() as td2:
            ws2 = Path(td2)
            f1 = ws2 / "文档.txt"
            f1.write_text("hello", encoding="utf-8")
            f2 = ws2 / "数据.bin"
            f2.write_bytes(b"\x00\x01")
            from agent.clipboard import grab_files, grab_any, _set_files_for_test
            _set_files_for_test([str(f1), str(f2)])
            got = grab_files()
            assert {p.name for p in got} == {"文档.txt", "数据.bin"}, got
            files, img = grab_any(ws2)
            assert {p.name for p in files} == {"文档.txt", "数据.bin"} and img is None, (files, img)

            # 图片优先级：无文件时才取图
            _set_dib_for_test(1, 1, b"\xff\x00\x00\xff")
            files, img = grab_any(ws2)
            assert files == [] and img is not None and img.suffix == ".png", (files, img)
        print("PASS: 剪贴板 CF_HDROP 文件往返 + grab_any 文件优先/图兜底")

    except ImportError:
        print("SKIP: 剪贴板测试（模块导入失败）")
    except AssertionError:
        raise  # 断言失败必须暴露
    except Exception as e:
        # 无桌面环境/剪贴板被占等场景不算失败
        print(f"SKIP: 剪贴板测试（{type(e).__name__}: {e}）")

    # ---- 热键模块冒烟（不真注册，避免抢用户 Alt+V）----
    try:
        from agent.hotkeys import HotkeyListener, hotkey_label, is_supported
        assert hotkey_label() == "Alt+V", hotkey_label()
        assert is_supported() == __import__("sys").platform.startswith("win")
        lis = HotkeyListener(lambda: None)
        assert lis.hotkey_id and callable(lis.callback)
        print("PASS: hotkeys 模块冒烟（label/接口）")
    except AssertionError:
        raise
    except Exception as e:
        print(f"SKIP: 热键测试（{type(e).__name__}: {e}）")

    # ---- skill 工具：索引注入 + 清单 + 加载 + 截断 + 找不到 ----
    try:
        from tools.skill import skill_index
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td) / "ws"
            sk = ws / ".workbuddy" / "skills" / "demo-skill"
            sk.mkdir(parents=True)
            (sk / "SKILL.md").write_text(
                "---\n"
                "name: demo-skill\n"
                "display_name: 演示技能\n"
                "description_zh: 演示用技能：当用户要求演示技能加载时触发\n"
                "---\n"
                "\n# 演示技能\n\n第一步：回复「技能加载成功」。\n"
                + "\n" + "填充段落，用于测试超长正文截断。" * 50 + "\n",
                encoding="utf-8")
            ctx = ToolContext(workspace=ws)

            idx = skill_index(str(ws))
            assert "demo-skill" in idx and "可用技能" in idx, idx

            r = dispatch("skill", {}, ctx)
            assert "demo-skill" in r.text and "个技能" in r.text, r.text
            assert "- demo-skill（演示技能）" in r.text, r.text

            r = dispatch("skill", {"name": "demo-skill"}, ctx)
            assert "技能加载成功" in r.text and "[环境提示]" in r.text, r.text
            assert "display_name: 演示技能" not in r.text, "frontmatter 应被剥离"

            r = dispatch("skill", {"name": "demo-skill", "max_chars": 10}, ctx)
            assert "[已截断]" in r.text and "read_file" in r.text, r.text

            r = dispatch("skill", {"name": "nope"}, ctx)
            assert "未找到技能" in r.text, r.text

        print("PASS: skill 索引注入 + 清单/加载/截断/未命中")
    except AssertionError:
        raise
    except Exception as e:
        print(f"SKIP: skill 测试（{type(e).__name__}: {e}）")

    # ---- CLI @ 上下文引用解析（agent/cli.py）----
    try:
        from agent.cli import resolve_at_refs, find_context_files, _match_skills
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            (ws / "docs").mkdir()
            (ws / "docs" / "report.md").write_text("报告内容", encoding="utf-8")
            (ws / "docs" / "report2.md").write_text("另一份", encoding="utf-8")
            (ws / "readme.md").write_text("读我", encoding="utf-8")
            (ws / "报告.md").write_text("年度报告", encoding="utf-8")
            (ws / "截图.png").write_bytes(b"\x89PNG fake")
            (ws / ".git").mkdir()
            (ws / ".git" / "hidden.md").write_text("x", encoding="utf-8")

            # 唯一命中：替换为相对路径 + 附文件
            t, imgs, files = resolve_at_refs("总结 @readme 的要点", ws)
            assert t == "总结 readme.md 的要点" and files == ["readme.md"], (t, files)

            # 中文紧邻 + 尾部中文裁剪：@报告的要点 → 报告.md，尾巴保留在正文
            t, imgs, files = resolve_at_refs("总结@报告的要点", ws)
            assert t == "总结报告.md的要点" and files == ["报告.md"], (t, files)

            # 多命中：picker 选中 / 无 picker 跳过原文保留
            t, imgs, files = resolve_at_refs("@report 看一下", ws, picker=lambda c: c[0])
            assert files == ["docs/report.md"], (t, files)
            t, imgs, files = resolve_at_refs("@report 看一下", ws)
            assert not files and "@report" in t, (t, files)

            # 纯 @引用 → 文本置空（调用方补默认提示）
            t, imgs, files = resolve_at_refs("@readme", ws)
            assert t == "" and files == ["readme.md"], (t, files)

            # 未命中 → 原文保留
            t, imgs, files = resolve_at_refs("看 @不存在的文件 一下", ws)
            assert "@不存在的文件" in t and not files and not imgs

            # 图片 → 视觉（base64），文本文件 → 附路径
            t, imgs, files = resolve_at_refs("看 @截图.png 啊", ws)
            assert len(imgs) == 1 and not files and t == "看 截图.png 啊", (t, len(imgs), files)

            # 邮箱不误伤
            t, imgs, files = resolve_at_refs("联系 foo@bar.com 即可", ws)
            assert t == "联系 foo@bar.com 即可" and not files and not imgs, (t, files)

            # 重复引用去重
            t, imgs, files = resolve_at_refs("@readme 和 @readme 都看", ws)
            assert files == ["readme.md"] and t == "readme.md 和 readme.md 都看", (t, files)

            # 隐藏目录跳过 / 精确文件名优先
            assert find_context_files(ws, "hidden") == []
            t, imgs, files = resolve_at_refs("@report.md 怎么写", ws)
            assert files == ["docs/report.md"], (t, files)

        # 技能名称/显示名匹配（真实技能目录存在才断言）
        sk = _match_skills(Path(__file__).resolve().parent.parent, "ui-design-system")
        assert isinstance(sk, list)
        if sk:
            assert any(s["name"] == "ui-design-system" for s in sk), sk
        print("PASS: CLI @ 上下文引用（唯一/多命中/裁剪/图片/邮箱/去重）+ 技能匹配")
    except AssertionError:
        raise
    except Exception as e:
        print(f"SKIP: CLI @ 引用测试（{type(e).__name__}: {e}）")

    # ---- 行内选择面板（agent/palette.py）：纯逻辑 + 按键驱动 ----
    try:
        import io as _io
        from agent.palette import PaletteState, _read_line_raw

        _files = ["README.md", "docs/report.md", "docs/report2.md",
                  "report_summary.txt", "报告.md", "截图.png"]
        _skills = [("ui-design-system", "UI设计系统", "专业UI生成与现有界面优化技能"),
                   ("dev-expert", "开发专家", "编程专家：P8 级编程助手")]

        def _st(buf: str = ""):
            s = PaletteState(files=_files, skills=_skills)
            s.buf = buf
            s._refresh()
            return s

        # @ 前缀匹配（文件名前缀优先于路径前缀）
        s = _st("@rep")
        assert s.menu_active, "输入 @rep 应弹面板"
        assert [i.label for i in s.items] == ["docs/report.md", "docs/report2.md",
                                             "report_summary.txt"], [i.label for i in s.items]
        # ↓ + Enter 选中第 2 项 → 摘掉 @token、记录附件
        s.apply("down")
        assert s.sel == 1 and s.apply("enter") == "continue"
        assert s.picked == ["docs/report2.md"] and s.buf == "", (s.picked, s.buf)

        # 光 @ 回车 → 选中第一个文件（能选上，不是"只输入一个 @"）；光 / 回车 → 列指令清单
        s = _st("@")
        assert s.apply("enter") == "continue" and s.picked == ["README.md"], s.picked
        assert _st("/").apply("enter") == "send"

        # / 前缀 → 命中指令排第一，Enter 直接回填 "/reset "
        s = _st("/re")
        assert s.items[0].label == "/reset"
        assert s.apply("enter") == "continue" and s.buf == "/reset ", repr(s.buf)

        # 全名 + Enter → 直接发送（不追加空格）
        s = _st("/reset")
        assert s.apply("enter") == "send" and s.buf == "/reset"

        # 技能：名称/中文显示名都能前缀匹配，选中回填 /skill <原始名>
        assert any(i.payload == "ui-design-system" for i in _st("/ui").items)
        assert any(i.payload == "dev-expert" for i in _st("/开发").items)
        s = _st("/ui")
        s.apply("enter")
        assert s.buf == "/skill ui-design-system ", repr(s.buf)
        # 技能组不被过滤淘汰：不命中时仍全量列出（归入「其它技能」组）；命中的排前面；hint 用描述
        s = _st("/zzz")
        sks = [i for i in s.items if i.kind != "cmd"]
        assert len(sks) == 2 and all(i.kind == "skill_more" for i in sks), \
            [(i.label, i.kind) for i in sks]                    # 全保留
        s = _st("/dev")                                         # dev-expert 命中排最前
        sks = [i for i in s.items if i.kind != "cmd"]
        assert sks[0].payload == "dev-expert" and sks[0].kind == "skill" and len(sks) == 2
        assert "编程专家" in sks[0].hint                        # hint 显示描述而非显示名
        # /re 选中第一条仍是命中指令（未命中技能在后面的组里，不抢占）
        s = _st("/re")
        assert s.items[0].label == "/reset" and s.items[0].kind == "cmd"

        # Esc 关闭 → 回车原样发送；回删恢复面板；控制字符忽略；邮箱不触发
        s = _st("@rep")
        s.apply("esc")
        assert not s.menu_active and s.apply("enter") == "send" and s.buf == "@rep"
        s.apply("backspace")
        assert _st("foo@bar").menu_active is False
        s2 = _st("ab")
        s2.apply(("char", "\x16"))
        assert s2.buf == "ab"

        # 无匹配 → 回车原样发送；超宽截断
        s = _st("@zzz")
        assert s.menu_active and s.items == [] and s.apply("enter") == "send"
        s = _st("@")
        s.files = ["很长的文件名" * 40 + ".md"]
        s._refresh()
        assert "…" in s.render_lines(False)[0]

        # /skill 打完就有候选（含二级补全：/skill 空格后继续补技能名）
        s = _st("/skill")
        assert any(i.kind.startswith("skill") for i in s.items), [i.label for i in s.items]
        s = _st("/skill dev")
        assert s.items and s.items[0].payload == "dev-expert", [i.label for i in s.items]
        s.apply("enter")
        assert s.buf == "/skill dev-expert ", repr(s.buf)
        s = _st("/skill dev-expert ")                 # 已写完技能名 → 面板收起，可写问题
        assert not s.menu_active

        # 已附加反馈：选中后常驻「✓ 已附加」行（菜单收起也显示），再 @ 回车可取消
        s = _st("@rep")
        s.apply("enter")
        assert s.picked == ["docs/report.md"], s.picked
        assert any("已附加" in l for l in s.render_lines(False)), s.render_lines(False)
        s = PaletteState(files=_files, skills=_skills, picked=["docs/report.md"])
        s.buf, s.sel = "@", 0
        s._refresh()
        att = [i for i in s.items if i.payload == "docs/report.md"]
        assert att and att[0].label.startswith("✓ "), [i.label for i in s.items]
        s.sel = s.items.index(att[0])
        s.apply("enter")                                     # 再选一次 = 取消
        assert s.picked == [], s.picked
        # 空行退格 = 逐个删最后一个附件（无需再 @ 找文件）；编号显示
        s = PaletteState(files=_files, skills=_skills, picked=["docs/report.md", "报告.md"])
        lines = s.render_lines(False)
        assert any("已附加 2 个" in l and "[1]" in l and "[2]" in l for l in lines), lines
        assert s.apply("backspace") == "continue" and s.picked == ["docs/report.md"]
        assert any("已附加 1 个" in l for l in s.render_lines(False))
        s.apply("backspace")
        assert s.picked == [] and s.render_lines(False) == [], s.picked
        # 输入行有内容时退格仍删字符，不碰附件
        s = PaletteState(files=_files, skills=_skills, picked=["报告.md"])
        s.buf = "@re"; s._refresh()
        s.apply("backspace")
        assert s.buf == "@r" and s.picked == ["报告.md"]

        # read_line 传入 attached → 保留到返回值（面板里也能看到）
        _it = iter([("char", "@"), "enter", "enter"])
        res = _read_line_raw("\n你> ", _files, _skills, False,
                             lambda: next(_it), _io.StringIO(), attached=["截图.png"])
        assert "截图.png" in res.picked and "README.md" in res.picked, res.picked

        # 按键驱动端到端：@rep → ↓ → Enter 选中 → Enter 发送
        keys = [("char", "@"), ("char", "r"), ("char", "e"), ("char", "p"),
                "down", "enter", "enter"]
        it = iter(keys)
        res = _read_line_raw("\n你> ", _files, _skills, False, lambda: next(it), _io.StringIO())
        assert res.picked == ["docs/report2.md"] and res.text == "", (res.text, res.picked)

        # 按键驱动端到端：正文中 @ 选中后继续输入
        keys = [("char", "总"), ("char", "结"), ("char", " "), ("char", "@"),
                ("char", "报"), "enter", ("char", "要"), ("char", "点"), "enter"]
        it = iter(keys)
        res = _read_line_raw("\n你> ", _files, _skills, False, lambda: next(it), _io.StringIO())
        assert res.picked == ["报告.md"] and res.text == "总结 要点", (res.text, res.picked)
        print("PASS: 行内面板（@ 前缀匹配/↓选中// 指令与技能回填/Esc/端到端按键）")
    except AssertionError:
        raise
    except Exception as e:
        print(f"SKIP: 行内面板测试（{type(e).__name__}: {e}）")

    # ---- 附件路径指路：模型猜错目录时，错误信息要直接给正确路径 ----
    # 实战：WebUI 附件在 .uploads/，模型猜成 .clip_cache/xxx.pdf，
    # 秒失败后开始乱试 clipboard_image / load_image，把截图当 PDF 读
    with tempfile.TemporaryDirectory() as td3:
        ws3 = Path(td3) / "ws"
        ws3.mkdir()
        ctx3 = ToolContext(workspace=ws3)
        up = ws3 / ".uploads"
        up.mkdir()
        real = up / "研习室.pdf"
        real.write_text("x", encoding="utf-8")

        r = dispatch("read_file", {"path": ".clip_cache/研习室.pdf"}, ctx3)
        assert "文件不存在" in r.text and "同名文件其实在" in r.text, r.text
        assert str(real) in r.text, "应给出附件的真实绝对路径"

        r = dispatch("read_file", {"path": "压根没有这个文件.pdf"}, ctx3)
        assert "附带的文件在" in r.text and ".uploads" in r.text, r.text
        assert "研习室.pdf" in r.text, "应列出 .uploads 里的真实附件"

        print("PASS: read_file 附件路径指路（同名直给真实路径 / 无同名列最近附件）")

    print(f"PASS: 注册 {len(specs)} 个工具 {sorted(names)}")
    print("PASS: 工作空间相对/绝对路径、list_dir 默认、docx/xlsx 解析、旧格式/二进制提示")
    print("PASS: write_file / read_file / load_image / delete_file / 错误路径均正常")
    print("PASS: run_command 执行/退出码/超时/确认闸，grep_file/glob_file 搜索，edit_file 精确替换")


if __name__ == "__main__":
    main()
