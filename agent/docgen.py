"""文档生成：把结构化文本写成真正的 .xlsx / .docx。

用 openpyxl（Excel）与 python-docx（Word）生成标准 Office 文件，Office/WPS 直接可开。
内容契约（write_file 按扩展名路由到这里）：
  .xlsx/.xlsm ← JSON（二维数组 / 对象数组 / {表名: 行}）—— 推荐，列边界明确、天然抗歧义
                CSV / TSV / Markdown 表格 —— 兼容，但**行列数必须与表头一致**，
                参差行（如分组续行漏写首列）会报错回传，绝不静默左对齐
                多工作表：用 '=== 表名 ===' 分节（兼容 read_file 输出的分节格式）
  .docx       ← Markdown 子集（# 标题、- / 1. 列表、| 表格 |、**加粗**、`代码`）
其余格式由 write_file 自己处理。
"""
from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from pathlib import Path

from docx import Document
from docx.shared import Cm, Inches
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

# ---------------- 公共 ----------------

_SHEET_BAD_CHARS = set('[]:*?/\\')


def _display_width(s: str) -> int:
    """估算显示宽度：CJK 全角算 2。"""
    return sum(2 if ord(ch) > 0x2E80 else 1 for ch in s)


def _sanitize_sheet_name(name: str, used: dict[str, int]) -> str:
    """Excel 表名规则：≤31 字符、禁 []:*?/\\、去重。"""
    name = (name or "").strip().strip("'").strip()
    name = "".join(ch for ch in name if ch not in _SHEET_BAD_CHARS)
    name = name[:31] or "Sheet"
    key = name.lower()
    if key in used:
        used[key] += 1
        name = f"{name[:28]}_{used[key]}"
    else:
        used[key] = 1
    return name


_NUM_RE = re.compile(r"^[+-]?\d+(?:\.\d+)?$")


def _is_number(s: str) -> bool:
    """判断是否应写成数值单元格。

    规则：纯数字字面量，但——
    - 整数带前导零（"007"、区号）保持文本
    - 位数 > 15（超出 Excel 双精度精确范围）保持文本
    """
    if not _NUM_RE.match(s):
        return False
    digits = s.lstrip("+-")
    if "." not in digits:
        if len(digits) > 15:
            return False
        if len(digits) > 1 and digits[0] == "0":
            return False
    return True


# ---------------- 表格内容解析 → {表名: 行} ----------------

_SECTION_RE = re.compile(
    r"^={3,}\s*(?:工作表[:：]\s*|sheet[:：]\s*)?(.+?)\s*={3,}\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_MD_SEP_RE = re.compile(r"^\s*\|?[\s:|-]*-[-\s:|]*\|?\s*$")


def parse_table_content(content: str) -> list[tuple[str, list[list[str]]]]:
    """解析表格类 content。返回 [(表名, 行列表)]，行列表元素均为字符串。

    支持：JSON（数组/对象/多表）、'=== 表名 ===' 分节、Markdown 表格、CSV/TSV。
    解析失败抛 ValueError（回传给模型让它改写）。
    """
    text = (content or "").lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")

    # ---- JSON 优先 ----
    stripped = text.lstrip()
    if stripped[:1] in ("[", "{"):
        return _parse_json_sheets(stripped)

    # ---- '=== 表名 ===' 分节（兼容 read_file 输出风格）----
    parts = _SECTION_RE.split(text)
    if len(parts) >= 3:
        out: list[tuple[str, list[list[str]]]] = []
        used: dict[str, int] = {}
        head = parts[0].strip()
        if head:
            rows = _parse_table_block(head)
            if rows:
                out.append((("Sheet1"), rows))
        for i in range(1, len(parts) - 1, 2):
            name = _sanitize_sheet_name(parts[i], used)
            rows = _parse_table_block(parts[i + 1])
            out.append((name, rows))
        return out or [("Sheet1", [[""]])]

    rows = _parse_table_block(text)
    return [("Sheet1", rows)]


def _parse_table_block(block: str) -> list[list[str]]:
    lines = [ln for ln in block.split("\n")]
    # Markdown 表格：任一行以 | 开头
    if any(ln.lstrip().startswith("|") for ln in lines if ln.strip()):
        return _parse_md_table(lines)
    return _parse_delimited(lines)


def _parse_md_table(lines: list[str]) -> list[list[str]]:
    rows: list[list[str]] = []
    for ln in lines:
        s = ln.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if _MD_SEP_RE.match(s):          # |---|---:| 分隔行
            continue
        rows.append(cells)
    return rows


def _parse_delimited(lines: list[str]) -> list[list[str]]:
    text = "\n".join(ln for ln in lines if ln.strip() != "")
    if not text.strip():
        return []
    first = text.split("\n", 1)[0]
    if "\t" in first:
        delim = "\t"
    elif first.count(";") > first.count(","):
        delim = ";"
    else:
        delim = ","
    rows = [list(r) for r in csv.reader(io.StringIO(text), delimiter=delim)]
    return [[c.strip() for c in r] for r in rows]


def _parse_json_sheets(stripped: str) -> list[tuple[str, list[list[str]]]]:
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as e:
        raise ValueError(f"content 以 [ 或 {{ 开头但不是合法 JSON: {e}") from e

    used: dict[str, int] = {}

    def rows_from(value, default_name: str) -> tuple[str, list[list[str]]]:
        if not isinstance(value, list):
            raise ValueError(f"JSON 表 {default_name} 的值应是行数组")
        rows: list[list[str]] = []
        if value and isinstance(value[0], dict):          # 对象数组 → 表头取键并集
            headers: list[str] = []
            for item in value:
                for k in item:
                    if k not in headers:
                        headers.append(k)
            rows.append(headers)
            for item in value:
                rows.append(["" if item.get(h) is None else str(item.get(h))
                             for h in headers])
        else:                                              # 二维数组
            for r in value:
                if isinstance(r, list):
                    rows.append(["" if c is None else str(c) for c in r])
                else:
                    rows.append(["" if r is None else str(r)])
        return _sanitize_sheet_name(default_name, used), rows

    if isinstance(data, dict):
        sheets = data.get("sheets") if isinstance(data.get("sheets"), dict) else data
        out = []
        for i, (name, value) in enumerate(sheets.items(), 1):
            out.append(rows_from(value, str(name) or f"Sheet{i}"))
        return out
    if isinstance(data, list):
        return [rows_from(data, "Sheet1")]
    raise ValueError("JSON 应是二维数组、对象数组或 {表名: 行数组}")


# ---------------- XLSX 生成（openpyxl） ----------------

def _validate_rectangular(sheets: list[tuple[str, list[list[str]]]]) -> None:
    """校验每个表的每行列数都与首行（表头）一致。

    参差行几乎都是上游漏列（如 Excel 里同一项目的续行没写首列项目名），
    左对齐写入会把整行数据整体挪到错列、且不报错——这类静默错位比直接失败
    更难发现，所以这里宁可抛错回传给模型重写。
    """
    for name, rows in sheets:
        if not rows:
            continue
        width = len(rows[0])
        bad = [(i, len(r)) for i, r in enumerate(rows, 1) if len(r) != width]
        if not bad:
            continue
        detail = "、".join(f"第{i}行 {w} 列" for i, w in bad[:3])
        more = f" 等共 {len(bad)} 行" if len(bad) > 3 else ""
        raise ValueError(
            f"工作表「{name}」行列数不一致：首行 {width} 列，{detail}{more}。"
            f"每行列数必须与首行相同，缺的列用空字符串占位（不要省掉整列，否则数据会错位）。"
            f"推荐 content 直接传 JSON 二维数组："
            f'[["列1","列2","列3"],["值1","","值3"]]'
        )


def write_xlsx(path: Path, content: str) -> str:
    """把表格 content 写成真 .xlsx。返回摘要。"""
    sheets = parse_table_content(content) or [("Sheet1", [[""]])]
    _validate_rectangular(sheets)
    wb = Workbook()
    wb.remove(wb.active)                     # 去掉默认空表，按解析结果建表
    for name, rows in sheets:
        _fill_sheet(wb.create_sheet(title=name), rows)
    wb.save(path)
    if path.suffix.lower() == ".xlsm":
        _as_macro_enabled(path)
    total_rows = sum(len(r) for _, r in sheets)
    width = max((max((len(row) for row in rows), default=0) for _, rows in sheets),
                default=0)
    names = "、".join(n for n, _ in sheets)
    return (f"Excel 表格：{len(sheets)} 个工作表（{names}），"
            f"共 {total_rows} 行，最宽 {width} 列")


def _fill_sheet(ws, rows: list[list[str]]) -> None:
    bold = Font(bold=True)
    for ri, row in enumerate(rows, 1):
        for ci, val in enumerate(row, 1):
            if val == "":
                continue
            cell = ws.cell(row=ri, column=ci)
            if _is_number(val):
                cell.value = int(val) if "." not in val else float(val)
            else:
                cell.value = val
            if ri == 1:
                cell.font = bold                 # 首行加粗表头
    # 列宽：按内容估算（全角算 2），钳制 [6, 48]，留 2 字符余量
    ncol = max((len(r) for r in rows), default=0)
    for c in range(1, ncol + 1):
        w = max((_display_width(r[c - 1]) for r in rows if c <= len(r)), default=6)
        ws.column_dimensions[get_column_letter(c)].width = min(max(w + 2, 6), 48)


def _as_macro_enabled(path: Path) -> None:
    """openpyxl 只产 xlsx 包；.xlsm 把 workbook 主内容类型改成 macroEnabled 以匹配扩展名。"""
    old = b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
    new = b"application/vnd.ms-excel.sheet.macroEnabled.main+xml"
    with zipfile.ZipFile(path) as z:
        items = [(info, z.read(info.filename)) for info in z.infolist()]
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for info, data in items:
            if info.filename == "[Content_Types].xml":
                data = data.replace(old, new)
            z.writestr(info, data)


# ---------------- DOCX 生成（python-docx） ----------------

_TOKEN_RE = re.compile(r"(\*\*.+?\*\*|`[^`]+`)")
_LIST_NUM_RE = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")
_LIST_BUL_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_HR_RE = re.compile(r"^\s*(-{3,}|\*{3,}|_{3,})\s*$")


def _inline_runs(text: str) -> list[tuple[str, bool, bool]]:
    """拆行内格式 → [(文本, 加粗, 等宽)]。"""
    runs: list[tuple[str, bool, bool]] = []
    for part in _TOKEN_RE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            runs.append((part[2:-2], True, False))
        elif part.startswith("`") and part.endswith("`") and len(part) > 1:
            runs.append((part[1:-1], False, True))
        else:
            runs.append((part, False, False))
    return runs


def _add_runs(paragraph, text: str) -> None:
    for t, bold, code in _inline_runs(text):
        run = paragraph.add_run(t)
        if bold:
            run.bold = True
        if code:
            run.font.name = "Consolas"


def _add_table(doc: Document, rows: list[list[str]]) -> None:
    ncol = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=ncol)
    try:
        table.style = "Table Grid"
    except KeyError:
        pass
    table.autofit = False
    # 列宽按内容权重分配（A4 正文宽 ≈ 15.9cm）
    weights = []
    for c in range(ncol):
        w = max((_display_width(r[c]) for r in rows if c < len(r)), default=4)
        weights.append(min(max(w, 4), 40))
    s = sum(weights) or 1
    for ci, wt in enumerate(weights):
        width = Cm(max(15.9 * wt / s, 1.2))
        for cell in table.columns[ci].cells:
            cell.width = width
    for ri, row in enumerate(rows):
        for ci in range(ncol):
            val = row[ci] if ci < len(row) else ""
            p = table.cell(ri, ci).paragraphs[0]
            _add_runs(p, val)
            if ri == 0:                          # 表头加粗
                for r in p.runs:
                    r.bold = True


def _list_para(doc: Document, ordered: bool, level: int, text: str) -> None:
    base = "List Number" if ordered else "List Bullet"
    style = base if level == 0 else f"{base} {level + 1}"
    try:
        p = doc.add_paragraph(style=style)
    except KeyError:                             # 模板没有多级列表样式 → 缩进兜底
        p = doc.add_paragraph(style=base)
        if level:
            p.paragraph_format.left_indent = Inches(0.25 * (level + 1))
    _add_runs(p, text)


def write_docx(path: Path, content: str) -> str:
    """把 Markdown 子集写成真 .docx。返回摘要。"""
    text = (content or "").replace("\r\n", "\n").replace("\r", "\n")
    doc = Document()
    stats = {"段": 0, "标题": 0, "列表项": 0, "表格": 0}
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i]
        s = ln.strip()
        if not s or _HR_RE.match(ln):
            i += 1
            continue

        # Markdown 表格：连续 | 开头的行
        if s.startswith("|"):
            block = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i])
                i += 1
            rows = _parse_md_table(block)
            if rows:
                _add_table(doc, rows)
                stats["表格"] += 1
            continue

        # 标题
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            level = min(len(m.group(1)), 3)
            p = doc.add_paragraph(style=f"Heading {level}")
            _add_runs(p, m.group(2))
            stats["标题"] += 1
            i += 1
            continue

        # 列表
        m = _LIST_NUM_RE.match(ln)
        if m:
            _list_para(doc, True, min(len(m.group(1)) // 2, 4), m.group(2))
            stats["列表项"] += 1
            i += 1
            continue
        m = _LIST_BUL_RE.match(ln)
        if m:
            _list_para(doc, False, min(len(m.group(1)) // 2, 4), m.group(2))
            stats["列表项"] += 1
            i += 1
            continue

        # 普通段落（一行一段）
        p = doc.add_paragraph()
        _add_runs(p, ln)
        stats["段"] += 1
        i += 1

    doc.save(path)
    return (f"Word 文档：{stats['标题']} 个标题、{stats['段']} 个段落、"
            f"{stats['列表项']} 个列表项、{stats['表格']} 个表格")
