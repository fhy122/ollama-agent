"""write_file — 写入本地文件（文本 / Word / Excel，按扩展名路由）。"""
from __future__ import annotations

from pathlib import Path

from agent import docgen, docparse
from agent.registry import ToolContext, ToolResult, tool


@tool(
    name="write_file",
    description=(
        "把内容写入本地文件（覆盖写，自动创建父目录）。相对路径基于工作空间。"
        "按扩展名自动选择格式："
        ".xlsx/.xlsm = 生成真正的 Excel 表格（content 推荐 JSON 二维数组："
        '`[["列1","列2"],["值1","值2"]]`，首行是表头，每行列数必须与表头一致、'
        "缺值用空字符串占位；绝不为省事少写整列（否则整行错位）；同一分组下有多条记录时"
        "每条都写全各列（重复分组值或留空串），不要合并成一行；字段按语义归列"
        "（邮箱进邮箱列、电话进联系人列），不要照原文出现顺序硬搬；"
        "也支持 CSV / TSV / Markdown 表格，行列数不一致会报错；"
        "多工作表用 '=== 表名 ===' 分节）；"
        ".docx = 生成真正的 Word 文档（content 传 Markdown 子集：# 标题、- 或 1. 列表、| 表格 |、**加粗**、`代码`）；"
        "其他扩展名 = 纯文本原样写入。"
        "【重要】.svg/.html/.css/.js/.json/.py/.md 这类代码文件也是纯文本，一律直接写："
        "用户说「生成/做一个 xxx.svg」「输出个 html 页面」时，就把源码交给本工具落盘，"
        "不要只回复代码让用户手动保存，也不要说自己无法生成文件。"
        "不要把 CSV/Markdown 存成 .txt 冒充 Excel/Word，扩展名决定了真正的文件格式。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "目标文件路径。相对路径基于工作空间；扩展名决定输出格式（.xlsx/.docx 为 Office 文件）",
            },
            "content": {
                "type": "string",
                "description": (
                    "完整内容。.xlsx 推荐 JSON 二维数组，首行表头、每行列数一致、"
                    "缺值用空字符串占位（也接受 CSV/TSV/Markdown 表格）；"
                    ".docx 传 Markdown 文本；其他传纯文本"
                ),
            },
        },
        "required": ["path", "content"],
    },
    confirm=True,  # 写盘属于改动操作，配合 CLI --confirm 可人工把关
)
def write_file(ctx: ToolContext, path: str, content: str) -> ToolResult:
    p = ctx.resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    content = content or ""
    suf = p.suffix.lower()

    # ---- 旧版二进制格式：不支持生成 ----
    if suf in docparse.LEGACY_SUFFIXES:
        return ToolResult(
            f"[错误] 旧版二进制格式 {suf} 不支持写入，会是伪文件。"
            f"请改用 {'xlsx' if suf == '.xls' else 'docx' if suf == '.doc' else 'pptx'} 扩展名"
        )

    # ---- PDF：不支持直接生成 ----
    if suf in docparse.PDF_SUFFIXES:
        return ToolResult(
            "[错误] 不支持直接生成 .pdf（会产出打不开的伪文件）。"
            "内容先存 .md / .docx，需要 PDF 时用 Word/WPS 或浏览器「打印为 PDF」导出"
        )

    # ---- Excel ----
    if suf in docparse.SHEET_SUFFIXES:
        try:
            summary = docgen.write_xlsx(p, content)
        except ValueError as e:
            return ToolResult(f"[错误] 表格内容解析失败: {e}")
        where = "工作空间" if _in_workspace(p, ctx.workspace) else "外部路径"
        return ToolResult(f"[已写入·{where}] {p}（{summary}，真 xlsx 可直接用 Excel 打开）")

    # ---- Word ----
    if suf in docparse.DOC_SUFFIXES:
        try:
            summary = docgen.write_docx(p, content)
        except ValueError as e:
            return ToolResult(f"[错误] 文档内容解析失败: {e}")
        where = "工作空间" if _in_workspace(p, ctx.workspace) else "外部路径"
        return ToolResult(f"[已写入·{where}] {p}（{summary}，真 docx 可直接用 Word 打开）")

    # ---- PPT：暂不支持 ----
    if suf in docparse.SLIDE_SUFFIXES:
        return ToolResult("[错误] 暂不支持生成 .pptx。可先写 .docx（Markdown 子集），或用文本格式保存内容")

    # ---- 纯文本 ----
    p.write_text(content, encoding="utf-8")
    where = "工作空间" if _in_workspace(p, ctx.workspace) else "外部路径"
    return ToolResult(f"[已写入·{where}] {p}（{len(content)} 字符，{content.count(chr(10)) + 1} 行）")


def _in_workspace(p: Path, ws: Path) -> bool:
    try:
        p.resolve().relative_to(ws.resolve())
        return True
    except ValueError:
        return False
