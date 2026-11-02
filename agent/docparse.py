"""文档解析：把 Word / Excel / PowerPoint 抽成纯文本。

docx / xlsx 用 python-docx / openpyxl 解析；
pptx 不引入 python-pptx，保留 zip+xml 抽每页 a:t 文本（只读、够用）。
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
LEGACY_SUFFIXES = {".doc", ".xls", ".ppt", ".wps"}

MAX_CELLS_ROWS = 500          # 表格最多抽这么多行
MAX_SLIDES = 50


def is_text_file(path: Path) -> bool:
    if path.suffix.lower() in TEXT_SUFFIXES:
        return True
    # 无扩展名的常见文本（Makefile/Dockerfile 等）或未知扩展名：试探是否 UTF-8 文本
    if path.suffix.lower() in DOC_SUFFIXES | SHEET_SUFFIXES | SLIDE_SUFFIXES | LEGACY_SUFFIXES:
        return False
    if path.suffix == "":
        return True
    return False


def extract_text(path: Path) -> tuple[str, str]:
    """按格式抽取文本。返回 (格式标签, 文本)。失败抛 ValueError。"""
    suf = path.suffix.lower()
    if suf in DOC_SUFFIXES:
        return "Word 文档", _docx_text(path)
    if suf in SHEET_SUFFIXES:
        return "Excel 表格", _xlsx_text(path)
    if suf in SLIDE_SUFFIXES:
        return "PPT 幻灯片", _pptx_text(path)
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
