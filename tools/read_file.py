"""read_file — 读取本地文件（文本 / Word / Excel / PPT / PDF）。"""
from __future__ import annotations

from pathlib import Path

from agent import HIDDEN_DIRNAME, docparse, longdoc
from agent.registry import ToolContext, ToolResult, tool


def _long_hint(ctx: ToolContext, chars: int) -> str:
    """超出单次可读量时，附一段「改用 read_long_doc」的硬指令。

    为什么要在 read_file 里主动劝退：文档一超长，模型的标准应对就是
    read_file(offset=200) → (offset=400) → …，十几步之后步数耗尽，
    而它手里只有开头几段，却会当成全文答。把出口直接写在它第一次读的地方最有效。
    """
    n = ctx.num_ctx()
    if not longdoc.too_long(chars, n):
        return ""
    return longdoc.long_read_hint(chars, n)


# WebUI 拖入/上传的附件目录（webui/server.py handle_upload 写这里）。
# 放应用数据目录下，不再是工作区根下散落的 .uploads/
UPLOAD_DIRNAME = Path(HIDDEN_DIRNAME) / "uploads"


def _missing_hint(p: Path, ws: Path) -> str:
    """文件不存在时给「下一步该干嘛」，而不是只报一个路径。

    实战教训：WebUI 拖入的附件落在 {workspace}/.ollama-agent/uploads/，
    模型拿相对路径去猜，猜成截图缓存目录里的 pdf 之后 read_file 秒失败，
    它就开始乱试 clipboard_image / load_image，把一张截图当成 PDF 读。
    错误信息必须直接指出附件真实位置。目录迁移后（.uploads →
    .ollama-agent/uploads）旧会话里的失效路径也靠这里的同名兜底自动纠正。
    """
    up = ws / UPLOAD_DIRNAME
    if up.is_dir():
        try:
            files = [f for f in up.iterdir() if f.is_file()]
        except OSError:
            files = []
        # 1) 有同名文件（忽略大小写）→ 直接给出正确路径
        for f in files:
            if f.name.lower() == p.name.lower():
                return f"。同名文件其实在 {f}，直接用这个路径重试"
        # 2) 否则列出最近附带的几个文件
        if files:
            try:
                recent = sorted(files, key=lambda f: f.stat().st_mtime, reverse=True)[:5]
            except OSError:
                recent = files[:5]
            listing = "\n".join(f"  - {f}" for f in recent)
            return (f"。用户本条消息附带的文件在 {up}，最近几个是：\n{listing}\n"
                    f"从中选一个重试；要看全部用 list_dir")
    return f"（相对路径基于工作空间 {ws}，用 list_dir 查看真实文件名）"


@tool(
    name="read_file",
    description=(
        "读取本地文件内容。支持：文本（txt/md/csv/json/代码等，按行号返回）、"
        "Word(.docx)、Excel(.xlsx/.xlsm，按工作表返回单元格)、PPT(.pptx，按页返回)、"
        "PDF(.pdf，按页返回文字，用 pages 指定页码)。"
        "PDF 抽不出文字时说明是扫描件，改用 load_image(path=…, page=N) 渲染成图看。"
        "【文档很长、一次读不完时（返回里带「超出单次可读量」），改用 read_long_doc "
        "整篇分段精读】，不要靠反复翻页硬凑；只有想精读某一两段细节时才回来用 "
        "offset/pages 取局部。"
        f"用户本条消息拖入/上传的文件都在工作空间 {HIDDEN_DIRNAME}/uploads/ 目录下，按那个路径读。"
        "相对路径在工作空间内查找；用户给了绝对路径就直接用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "文件路径。相对路径基于工作空间，绝对路径按原样",
            },
            "pages": {
                "type": "string",
                "description": "仅 PDF 生效：页码，如 \"3\"、\"1-5\"、\"2,4,7\"；不填则从第 1 页起读",
            },
            "offset": {"type": "integer", "description": "起始行号（文本模式），从 0 开始，默认 0"},
            "limit": {"type": "integer", "description": "最多返回多少行，默认 200"},
        },
        "required": ["path"],
    },
)
def read_file(ctx: ToolContext, path: str, pages: str | None = None,
              offset: int = 0, limit: int = 200) -> ToolResult:
    p = ctx.resolve(path)
    if not p.exists():
        return ToolResult(f"[错误] 文件不存在: {p}{_missing_hint(p, ctx.workspace)}")
    if p.is_dir():
        return ToolResult(f"[错误] 是目录不是文件: {p}（用 list_dir）")

    # ---- PDF（自带页码窗口，不走行号分页）----
    if p.suffix.lower() in docparse.PDF_SUFFIXES:
        # 先看页数：几百页的 PDF 直接读只会截出前 40 万字里的开头一小截，
        # 还不如当场告诉它换 read_long_doc
        est = longdoc.page_estimate(p)
        if est and longdoc.too_long(est[0] * 1200, ctx.num_ctx()):
            pages_total, page_word = est
            return ToolResult(
                f"[PDF 文档] {p}  共 {pages_total} 页\n"
                + longdoc.long_read_hint(pages_total * 1200, ctx.num_ctx())
                + f"\n（只想看某几页时仍可用 pages=\"{'1-5'}\" 精确取）")
        try:
            _label, text = docparse.extract_text(p, pages=pages)
        except ValueError as e:
            return ToolResult(f"[错误] {e}")
        except Exception as e:
            return ToolResult(f"[错误] PDF 解析失败: {type(e).__name__}: {e}")
        return ToolResult(text + _long_hint(ctx, len(text)))

    # ---- Office 文档 ----
    if p.suffix.lower() in (docparse.DOC_SUFFIXES | docparse.SHEET_SUFFIXES
                            | docparse.SLIDE_SUFFIXES | docparse.LEGACY_SUFFIXES):
        try:
            label, text = docparse.extract_text(p)
        except ValueError as e:
            return ToolResult(f"[错误] {e}")
        except Exception as e:
            return ToolResult(f"[错误] 解析失败: {type(e).__name__}: {e}")
        lines = text.splitlines() or [""]
        return ToolResult(_with_head_and_window(p, f"{label}", lines, offset, limit)
                          + _long_hint(ctx, len(text)))

    # ---- 文本（含未知扩展名试探）----
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ToolResult(
            f"[错误] 二进制文件无法按文本读取: {p.suffix or '(无扩展名)'}。"
            f"图片用 load_image；docx/xlsx/pptx/pdf 用 read_file（已支持）；其它格式暂不支持"
        )
    lines = text.splitlines()
    return ToolResult(_with_head_and_window(p, "文本", lines, offset, limit)
                      + _long_hint(ctx, len(text)))


def _with_head_and_window(p: Path, label: str, lines: list[str],
                          offset: int, limit: int) -> str:
    if not lines:
        return f"[空文件] {p}"
    offset = max(0, int(offset or 0))
    limit = max(1, int(limit or 200))
    chunk = lines[offset:offset + limit]
    numbered = "\n".join(f"{offset + i + 1}\t{l}" for i, l in enumerate(chunk))
    more = f"\n…[后续还有 {len(lines) - offset - len(chunk)} 行，用 offset 翻页]" \
        if offset + len(chunk) < len(lines) else ""
    head = (f"[{label}] {p}  共 {len(lines)} 行，显示 {offset + 1}-"
            f"{offset + len(chunk)} 行")
    return f"{head}\n{numbered}{more}"
