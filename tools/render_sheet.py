"""render_sheet — 把表格渲染成图片，给视觉模型看样式（"截个屏看看做得对不对"）。

xlsx/xlsm/csv/tsv → HTML 表格 → Playwright 截图 → PNG → 附进工具结果。
不依赖 Office/Pillow；只依赖已安装的 playwright + chromium。
"""
from __future__ import annotations

import base64
import csv
import html
import tempfile
import time
from pathlib import Path

from agent.registry import ToolContext, ToolResult, tool

SHEET_SUFFIXES = {".xlsx", ".xlsm"}
TEXT_SUFFIXES = {".csv", ".tsv"}
MAX_ROWS = 200

_CSS = """
*{box-sizing:border-box}
body{margin:0;background:#fff;font-family:"Microsoft YaHei","Segoe UI",sans-serif;
     font-size:14px;color:#1b1f24}
#sheet{padding:22px 24px 16px;display:inline-block}
h1{font-size:17px;margin:0 0 14px;font-weight:600}
h1 span{color:#8a8f98;font-weight:400;font-size:13px;margin-left:10px}
table{border-collapse:collapse;table-layout:fixed}
th,td{border:1px solid #d0d7de;padding:8px 10px;vertical-align:top;line-height:1.55;
      word-break:break-word;overflow-wrap:anywhere}
th{background:#1f4e79;color:#fff;font-weight:600;text-align:center;white-space:nowrap}
td.num{text-align:center;color:#6b7280}
td.mono{font-family:Consolas,"Courier New",monospace;font-size:13px;white-space:nowrap}
tr:nth-child(even) td{background:#f7f9fc}
.note{color:#9aa0a6;font-size:12px;margin-top:10px;font-family:Consolas,monospace}
"""


def _load_rows(p: Path, sheet: str, max_rows: int) -> tuple[list[list[str]], str, bool]:
    """返回 (行, 表名, 是否截断)。"""
    suf = p.suffix.lower()
    if suf in SHEET_SUFFIXES:
        try:
            from openpyxl import load_workbook
        except ImportError as e:
            raise RuntimeError("需要 openpyxl：pip install openpyxl") from e
        wb = load_workbook(p, data_only=True, read_only=True)
        try:
            if sheet:
                if sheet not in wb.sheetnames:
                    raise ValueError(
                        f"工作表 {sheet!r} 不存在，可用: {', '.join(wb.sheetnames)}")
                ws = wb[sheet]
            else:
                ws = wb.worksheets[0]
            rows = [["" if v is None else str(v) for v in r]
                    for r in ws.iter_rows(values_only=True)]
            name = ws.title
            names = wb.sheetnames
        finally:
            wb.close()   # 必须关：read_only 模式会一直占着文件句柄（Windows 会锁文件）
        if not rows:
            raise ValueError("工作表是空的")
        cut = len(rows) > max_rows + 1
        if cut:
            rows = rows[:max_rows + 1]
        return rows, f"{name}（共 {len(names)} 个工作表: {', '.join(names)}）", cut

    if suf in TEXT_SUFFIXES:
        delim = "\t" if suf == ".tsv" else ","
        with p.open(encoding="utf-8-sig", newline="") as f:
            rows = [list(r) for r in csv.reader(f, delimiter=delim)]
        if not rows:
            raise ValueError("文件是空的")
        cut = len(rows) > max_rows + 1
        if cut:
            rows = rows[:max_rows + 1]
        return rows, p.name, cut

    raise ValueError(f"render_sheet 只支持 xlsx/xlsm/csv/tsv，不支持 {suf or '无扩展名'}")


def _col_widths(rows: list[list[str]], ncol: int) -> list[int]:
    """按各列最长内容估算像素宽，clamp 到 [64, 340]。"""
    widths = []
    for j in range(ncol):
        longest = 0
        for r in rows[:60]:
            cell = r[j] if j < len(r) else ""
            # 中文按 2 个单位、其余按 1 个估算
            est = sum(2 if ord(c) > 127 else 1 for c in cell.split("\n")[0])
            longest = max(longest, min(est, 60))
        widths.append(max(64, min(340, 26 + longest * 7)))
    return widths


def _build_html(rows: list[list[str]], title: str, cut: bool) -> str:
    ncol = max(len(r) for r in rows)
    widths = _col_widths(rows, ncol)
    head = rows[0] + [""] * (ncol - len(rows[0]))
    body = rows[1:]

    cols = "".join(f'<col style="width:{w}px">' for w in widths)
    th = "".join(f"<th>{html.escape(h)}</th>" for h in head)
    trs = []
    for r in body:
        cells = r + [""] * (ncol - len(r))
        tds = []
        for j, v in enumerate(cells):
            cls = "num" if (j == 0 and v.strip().isdigit()) else ""
            if any(c in v for c in "@") or v.replace("-", "").isdigit() and len(v) >= 8:
                cls = (cls + " mono").strip()
            tds.append(f'<td class="{cls}">{html.escape(v)}</td>')
        trs.append(f"<tr>{''.join(tds)}</tr>")

    more = '<div class="note">…还有更多行未渲染（已按 max_rows 截断）</div>' if cut else ""
    return f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>{_CSS}</style>
</head><body><div id="sheet">
<h1>{html.escape(title)}<span>{len(body)} 行 · {ncol} 列</span></h1>
<table><colgroup>{cols}</colgroup>
<thead><tr>{th}</tr></thead><tbody>{''.join(trs)}</tbody></table>
{more}</div></body></html>"""


@tool(
    name="render_sheet",
    description=(
        "把表格文件渲染成图片（截图预览），返回后视觉模型能直接看到表格样式。"
        "用于回答「表格做得对不对 / 长什么样 / 截个屏看看 / 排版有没有错位」。"
        "支持 xlsx/xlsm/csv/tsv；只是想把内容读出来用 read_file 就够，不必渲染。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "表格文件路径（相对路径基于工作空间）"},
            "sheet": {"type": "string",
                      "description": "工作表名，默认第一个；多表时可指定"},
            "max_rows": {"type": "integer",
                         "description": f"最多渲染多少数据行，默认 40，上限 {MAX_ROWS}"},
        },
        "required": ["path"],
    },
    notes=("用户要「看表格长什么样/截个屏/检查排版对不对」时用本工具，不要自己写 python 画图；"
           "只是想把内容读出来用 read_file。"),
)
def render_sheet(ctx: ToolContext, path: str, sheet: str = "",
                 max_rows: int = 40) -> ToolResult:
    p = ctx.resolve(path)
    if not p.exists():
        return ToolResult(f"[错误] 文件不存在: {p}（相对路径基于工作空间 {ctx.workspace}）")
    if p.suffix.lower() not in (SHEET_SUFFIXES | TEXT_SUFFIXES):
        return ToolResult(
            f"[错误] {p.name} 不是表格文件（render_sheet 支持 xlsx/xlsm/csv/tsv）。"
            f"图片请用 load_image；docx/pptx 请用 read_file 读内容。")

    try:
        max_rows = max(1, min(int(max_rows or 40), MAX_ROWS))
    except (TypeError, ValueError):
        max_rows = 40

    try:
        rows, title, cut = _load_rows(p, sheet.strip(), max_rows)
    except (ValueError, RuntimeError, OSError) as e:
        return ToolResult(f"[错误] {e}")

    doc = _build_html(rows, p.stem, cut)

    out_dir = Path(tempfile.gettempdir()) / "ollama_agent_render"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{p.stem}_{int(time.time() * 1000) % 10 ** 8}.png"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return ToolResult(
            "[错误] 渲染需要 playwright：pip install playwright && playwright install chromium")

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            try:
                page = browser.new_page(viewport={"width": 1500, "height": 900},
                                        device_scale_factor=2)
                page.set_content(doc, wait_until="load")
                page.query_selector("#sheet").screenshot(path=str(out))
            finally:
                browser.close()
    except Exception as e:
        return ToolResult(
            f"[错误] 渲染失败：{type(e).__name__}: {e}\n"
            f"（常见原因：chromium 未安装，跑 playwright install chromium）")

    raw = out.read_bytes()
    b64 = base64.b64encode(raw).decode("ascii")
    more = "（行数过多已截断）" if cut else ""
    return ToolResult(
        f"[表格已渲染] {p.name} → {title}{more}，图片已附在本条消息中。"
        f"看样式、对齐、换行是否正常；需要精细内容仍以 read_file 为准。",
        images=[b64],
    )
