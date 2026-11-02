"""文档解析：把 Word / Excel / PowerPoint / PDF 抽成纯文本。

docx / xlsx 用 python-docx / openpyxl 解析；
pptx 不引入 python-pptx，保留 zip+xml 抽每页 a:t 文本（只读、够用）。
pdf 用 PyMuPDF（惰性导入）抽文字层，另提供"渲染某页为 PNG"给视觉模型看扫描件。
旧版二进制 .doc/.xls 不支持，返回提示让用户另存为新格式。
"""
from __future__ import annotations

import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from docx import Document as DocxDocument
from docx.oxml.ns import qn as docx_qn
from docx.table import Table as DocxTable
from docx.text.paragraph import Paragraph as DocxParagraph
from openpyxl import load_workbook

# 文本类扩展名（read_file 直接当文本读）
TEXT_SUFFIXES = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".log",
    ".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala",
    ".sh", ".bat", ".ps1", ".cmd", ".zsh", ".fish",
    ".json", ".jsonl", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".xml", ".html", ".htm", ".css", ".scss", ".less", ".sql", ".vue",
    ".env", ".gitignore", ".dockerfile", ".makefile", ".cmake", ".gradle",
}
DOC_SUFFIXES = {".docx"}
SHEET_SUFFIXES = {".xlsx", ".xlsm"}
SLIDE_SUFFIXES = {".pptx"}
PDF_SUFFIXES = {".pdf"}
LEGACY_SUFFIXES = {".doc", ".xls", ".ppt", ".wps"}

MAX_CELLS_ROWS = 500          # 表格最多抽这么多行
MAX_SLIDES = 50
MAX_PDF_PAGES = 300           # 单次最多抽这么多页（防止超大 PDF 卡住）
MAX_PDF_CHARS = 40000         # PDF 单次输出字符上限，超出提示用 pages 分段读
PDF_RENDER_DPI = 120          # 渲染给视觉模型的默认分辨率


def is_text_file(path: Path) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    # 无扩展名的常见文本（Makefile/Dockerfile 等）或未知扩展名：试探是否 UTF-8 文本
    if path.suffix.lower() in (DOC_SUFFIXES | SHEET_SUFFIXES | SLIDE_SUFFIXES
                               | LEGACY_SUFFIXES | PDF_SUFFIXES):
        return False
    if path.suffix == "":
        return True
    return False


def extract_text(path: Path, pages: str | None = None) -> tuple[str, str]:
    """按格式抽取文本。返回 (格式标签, 文本)。失败抛 ValueError。

    pages 只对 PDF 生效（"1" / "1-5" / "2,4,7"），其余格式忽略。
    """
    suf = path.suffix.lower()
    if suf in DOC_SUFFIXES:
        return "Word 文档", _docx_text(path)
    if suf in SHEET_SUFFIXES:
        return "Excel 表格", _xlsx_text(path)
    if suf in SLIDE_SUFFIXES:
        return "PPT 幻灯片", _pptx_text(path)
    if suf in PDF_SUFFIXES:
        return "PDF 文档", _pdf_text(path, pages)
    if suf in LEGACY_SUFFIXES:
        raise ValueError(
            f"旧版二进制格式 {suf} 无法解析，请用 Office/WPS 另存为 "
            f"{'docx' if suf == '.doc' else 'xlsx' if suf == '.xls' else 'pptx'} 后再读"
        )
    raise ValueError(f"不支持的文档格式: {suf or '(无扩展名)'}")


# ---------------- Word (.docx) ----------------

def _docx_text(path: Path) -> str:
    doc = DocxDocument(str(path))
    lines: list[str] = []
    for block in _iter_block_items(doc):
        if isinstance(block, DocxParagraph):
            line = block.text.strip()
            if line:
                lines.append(line)
        elif isinstance(block, DocxTable):
            for row in block.rows:
                cells = [c.text.strip().replace("\n", " ") for c in row.cells]
                if any(cells):
                    lines.append(" | ".join(cells))
    return "\n".join(lines)


def _iter_block_items(doc):
    """按文档顺序产出段落与表格（python-docx 的 paragraphs 不含表格）。"""
    for child in doc.element.body.iterchildren():
        if child.tag == docx_qn("w:p"):
            yield DocxParagraph(child, doc)
        elif child.tag == docx_qn("w:tbl"):
            yield DocxTable(child, doc)


# ---------------- Excel (.xlsx) ----------------

def _xlsx_text(path: Path) -> str:
    wb = load_workbook(path, read_only=True, data_only=False)
    try:
        names = wb.sheetnames
        out: list[str] = []
        for name in names[:20]:
            out.append(f"=== 工作表: {name} ===")
            out.append(_sheet_text(wb[name]))
        if len(names) > 20:
            out.append(f"… 共 {len(names)} 个工作表，仅显示前 20")
        return "\n".join(out).strip()
    finally:
        wb.close()


def _sheet_text(ws) -> str:
    rows_out: list[str] = []
    for row_count, row in enumerate(ws.iter_rows(values_only=True), 1):
        if row_count > MAX_CELLS_ROWS:
            rows_out.append(f"…[表格超过 {MAX_CELLS_ROWS} 行，已截断]")
            break
        cells: dict[int, str] = {}
        for ci, val in enumerate(row):
            if val is None:
                continue
            s = str(val).replace("\n", " ").replace("\t", " ").strip()
            if s:
                cells[ci] = s
        if cells:
            width = max(cells) + 1
            rows_out.append(f"行{row_count}: " + " | ".join(cells.get(i, "") for i in range(width)))
    return "\n".join(rows_out) if rows_out else "(空工作表)"


# ---------------- PowerPoint (.pptx) ----------------
# 不引入 python-pptx：pptx 是 zip，按页抽 a:t 文本，只读够用。

_A_NS = "{http://schemas.openxmlformats.org/drawingml/2006/main}"


def _pptx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as z:
        slides = sorted(
            (n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
            key=lambda n: int(re.search(r"\d+", Path(n).stem).group()),
        )[:MAX_SLIDES]
        out: list[str] = []
        for i, sn in enumerate(slides, 1):
            root = ET.fromstring(z.read(sn))
            texts = [(t.text or "").strip() for t in root.iter(f"{_A_NS}t")]
            texts = [t for t in texts if t]
            out.append(f"=== 第 {i} 页 ===\n" + ("\n".join(texts) if texts else "(无文字)"))
        if not out:
            return "(无幻灯片)"
        return "\n".join(out)


# ---------------- PDF (.pdf) ----------------
# PyMuPDF 惰性导入：没装时只影响 PDF，其它格式照常；报错文案直接给可执行的安装命令。
# 两条路：有文字层→抽文本（省 token）；扫描件/图片页→渲染成 PNG 交给视觉模型。

def _open_pdf(path: Path, what: str = "读取 PDF"):
    try:
        import pymupdf  # 惰性导入，避免没装就整个 docparse 挂掉
    except ImportError as e:
        raise ValueError(f"{what}需要 PyMuPDF，请先安装：pip install pymupdf") from e
    try:
        doc = pymupdf.open(str(path))
    except Exception as e:
        raise ValueError(f"打不开 PDF（文件损坏或被占用）: {e}") from e
    if doc.needs_pass:
        doc.close()
        raise ValueError("PDF 已加密，需要密码才能打开（暂不支持输入密码）")
    return pymupdf, doc


def parse_pages(spec: str | int | None, total: int) -> list[int]:
    """把 "1" / "1-5" / "2,4,7" / "3-" 解析成 1-based 页码列表。

    total 用于夹紧越界页码；spec 为空时取前 MAX_PDF_PAGES 页。
    """
    if spec is None or str(spec).strip() == "":
        return list(range(1, min(total, MAX_PDF_PAGES) + 1))
    raw = str(spec).strip().replace("，", ",").replace("－", "-").replace("—", "-")
    picked: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            a = a.strip() or "1"
            b = b.strip() or str(total)
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"页码格式看不懂: {spec}（支持 1 / 1-5 / 2,4,7 这几种写法）")
            lo, hi = int(a), int(b)
            if lo > hi:
                lo, hi = hi, lo
            picked.extend(range(lo, hi + 1))
        elif part.isdigit():
            picked.append(int(part))
        else:
            raise ValueError(f"页码格式看不懂: {spec}（支持 1 / 1-5 / 2,4,7 这几种写法）")
    picked = sorted({n for n in picked if 1 <= n <= total})
    if not picked:
        raise ValueError(f"页码 {spec} 超出范围（该 PDF 共 {total} 页）")
    if len(picked) > MAX_PDF_PAGES:
        picked = picked[:MAX_PDF_PAGES]
    return picked


def _tidy_pdf_text(text: str) -> str:
    lines = [ln.rstrip() for ln in text.splitlines()]
    out: list[str] = []
    for ln in lines:
        if not ln.strip() and (not out or not out[-1].strip()):
            continue          # 连续空行压成一行
        out.append(ln)
    return "\n".join(out).strip()


def _pdf_text(path: Path, pages: str | None = None) -> str:
    pymupdf, doc = _open_pdf(path)
    try:
        total = doc.page_count
        if total == 0:
            return "(PDF 无页面)"
        want = parse_pages(pages, total)
        blocks: list[str] = []
        empty_pages: list[int] = []
        used = 0
        truncated = False
        for no in want:
            txt = _tidy_pdf_text(doc[no - 1].get_text("text"))
            if not txt:
                empty_pages.append(no)
                blocks.append(f"=== 第 {no} 页 ===\n(本页抽不到文字，可能是扫描件/纯图页)")
                continue
            if used + len(txt) > MAX_PDF_CHARS:
                room = MAX_PDF_CHARS - used
                if room > 200:
                    blocks.append(f"=== 第 {no} 页 ===\n{txt[:room]}\n…[本页超出字数上限，已截断]")
                truncated = True
                break
            used += len(txt)
            blocks.append(f"=== 第 {no} 页 ===\n{txt}")
        head = f"[PDF 文档] {path}  共 {total} 页，本次显示第 {want[0]}-{want[-1]} 页"
        if total > MAX_PDF_PAGES and pages is None:
            head += f"（超过 {MAX_PDF_PAGES} 页，请用 pages 分段读）"
        filled = len(want) - len(empty_pages)
        if filled == 0:
            # 只看"本次显示的页是否全空"——按总字数判会误伤短文本 PDF（一页就一两行字）
            head += ("\n⚠️ 本次显示的页全部抽不到文字，基本可断定是扫描件/纯图 PDF —— "
                     "用 load_image(path=…, page=N) 按页渲染成图给视觉模型看")
        elif empty_pages:
            head += (f"\n⚠️ 第 {'、'.join(str(n) for n in empty_pages[:8])}"
                     f"{'…' if len(empty_pages) > 8 else ''} 页抽不到文字："
                     f"可能是扫描页/图片页，用 load_image(path=…, page=N) 渲染成图看")
        if truncated:
            head += f"\n⚠️ 输出已达 {MAX_PDF_CHARS} 字上限，改用 pages=\"页码\" 分段读剩余页"
        return head + "\n" + "\n".join(blocks)
    finally:
        doc.close()


def pdf_render_page(path: Path, page: int = 1, dpi: int = PDF_RENDER_DPI) -> tuple[bytes, int, int]:
    """把 PDF 指定页渲染成 PNG。返回 (png 字节, 实际页码, 总页数)。

    扫描件、图表页、盖章文件走这条路：模型抽不到文字时直接"看"图片。
    """
    pymupdf, doc = _open_pdf(path, "渲染 PDF")
    try:
        total = doc.page_count
        if total == 0:
            raise ValueError("PDF 无页面")
        no = max(1, min(int(page or 1), total))
        pg = doc[no - 1]
        zoom = max(0.1, dpi / 72.0)
        longest = max(pg.rect.width, pg.rect.height) * zoom
        if longest > 2400:                    # 海报/长图收一收，别生成几十 MB 的 PNG
            zoom *= 2400 / longest
        pix = pg.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return pix.tobytes("png"), no, total
    finally:
        doc.close()
