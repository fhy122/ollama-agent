"""长文分段精读：需求分析 → 逐段记笔记 → 按笔记作答。

为什么必须有这一层：read_file 一次只吐一段，20 万字的文档在 64K 上下文里
根本读不完。如果指望模型自己规划「读几段、读到哪了」，实测它会连续 read_file
把 12 步工具预算耗光，然后凭开头几页的印象编答案（同 loop.py 里
_delegate_to_expert 记录的问题：小模型擅长写字，不擅长规划）。

所以：切段、进度、笔记收敛全部由代码驱动，模型只做两件它擅长的事——
提炼关注点、看完一段写一段笔记。最后把笔记交回主循环作答。

预算怎么算（关键，算错就会上下文溢出）：
    num_ctx 是 token 数，中文按最坏情况 1 字 ≈ 1 token 折算（实际约 1.5 字/token，
    所以这一步本身已留了安全余量）。扣掉「已有笔记 + 输出余量 + 模板」之后，
    剩下的才是能塞进原文的字数。
    笔记上限按 num_ctx 比例给（不写死），因为小上下文模型如果把笔记撑到 1 万字，
    就没有空间放原文了。
"""
from __future__ import annotations

import bisect
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from agent import docparse, notebook

# 中文按最坏情况 1 字 = 1 token（千问/多数中文 tokenizer 实际约 1.5 字/token）
CHARS_PER_TOKEN = 1.0
# 每次子请求的固定开销：提示模板 + 生成余量 + 安全垫（不含原文和笔记）
RESERVE_TOKENS = 3500
# 单段笔记字数上限（再多就变成在抄原文了）
NOTES_PER_SEG = 1200
# 单段原文的字数下限：再小就切得太碎，接头信息全丢，读起来反而更不准
MIN_CHUNK = 1500
# 段数硬上限：几千页的 PDF 不该让用户干等一小时，宁可如实说「只精读了前 N 段」
MAX_SEGMENTS = 60
# 没指定 num_ctx 时的兜底（与服务端默认上下文同量级）
FALLBACK_CTX = 32768

# 模型偶尔会把笔记包在 ``` 代码围栏里，剥掉再存
_FENCE = re.compile(r"^\s*```[a-zA-Z]*\s*\n(.*?)\n```\s*$", re.S)

# ---------------- 预算 ----------------

def notes_budget(num_ctx: int | None) -> int:
    """笔记总量上限：随上下文缩放，占 18%，夹在 1500~10000 字之间。

    写死一个值会两头错：小上下文（8K）会被笔记吃掉一半原文空间；
    大上下文（128K）又会让笔记长得没有意义。
    """
    ctx = num_ctx or FALLBACK_CTX
    return max(1500, min(10000, int(ctx * 0.18)))


def chunk_budget(num_ctx: int | None) -> int:
    """单段原文的字数预算。

    按最坏笔记量预留，这样切段时算出的段数就是最终段数，
    不会出现「笔记越写越多、后面某段突然塞不进上下文」的翻车。
    """
    ctx = num_ctx or FALLBACK_CTX
    room = ctx - RESERVE_TOKENS - notes_budget(ctx) / CHARS_PER_TOKEN
    return max(MIN_CHUNK, int(room * CHARS_PER_TOKEN))


# ---------------- 切段 ----------------

@dataclass
class Chunk:
    index: int          # 1 起
    label: str          # 出处标注，如「行120–480」「第3页」
    text: str


@dataclass
class LongDocResult:
    text: str                       # 回给主循环的最终文本
    chars: int = 0
    segments: int = 0
    done: int = 0
    failed: list[str] = field(default_factory=list)
    cancelled: bool = False
    cached: bool = False            # True = 这次没读原文，用的是缓存笔记


_ATOM_DELIM = re.compile(r"[。！？；!?;\n]")


def _sentence_atoms(text: str) -> list[tuple[int, str]]:
    """按句子/换行切成原子片段，带原文偏移量（偏移量用来算行号）。

    换行也算分隔符是有意的：代码、清单类文档没有中文句号，
    只按句子切会退化成整篇一个原子，最后只能硬切。
    """
    out: list[tuple[int, str]] = []
    pos = 0
    for m in _ATOM_DELIM.finditer(text):
        end = m.end()
        if end > pos:
            out.append((pos, text[pos:end]))
            pos = end
    if pos < len(text):
        out.append((pos, text[pos:]))
    return out


def _pack(atoms: list[tuple[int, str]], budget: int) -> list[tuple[int, int, str]]:
    """把原子按顺序攒成不超过 budget 的段，返回 (首偏移, 尾偏移, 文本)。

    单句就超预算（无标点的长文本、贴了一屏的 JSON）只能硬切，
    但绝不跨段拼接，保证每段都是原文的连续片段。
    """
    out: list[tuple[int, int, str]] = []
    cur: list[str] = []
    cur_len = 0
    first = last = 0

    def flush() -> None:
        nonlocal cur, cur_len, first, last
        if cur:
            out.append((first, last, "".join(cur)))
        cur, cur_len, first, last = [], 0, 0, 0

    for off, atom in atoms:
        if not atom:
            continue
        if len(atom) > budget:
            flush()
            for i in range(0, len(atom), budget):
                out.append((off + i, off + i + len(atom[i:i + budget]),
                            atom[i:i + budget]))
            continue
        if cur_len + len(atom) > budget:
            flush()
        if not cur:
            first = off
        last = off + len(atom) - 1
        cur.append(atom)
        cur_len += len(atom)
    flush()
    return out


def _split_long_text(text: str, budget: int) -> list[str]:
    """把一段超预算的文本（如单页 PDF、单张幻灯片）拆成若干片。"""
    if len(text) <= budget:
        return [text] if text.strip() else []
    return [t for _a, _b, t in _pack(_sentence_atoms(text), budget) if t.strip()]


def _line_label(nl: list[int], start: int, end: int) -> str:
    """字符偏移 → 「行A–B」。中文字符偏移和行号必须对得上，才谈得上引用。"""
    lo = bisect.bisect_left(nl, start) + 1
    hi = bisect.bisect_left(nl, max(start, end)) + 1
    return f"行{lo}–{hi}" if lo != hi else f"行{lo}"


def _page_label(a: int, b: int) -> str:
    return f"第{a}页" if a == b else f"第{a}–{b}页"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        pass
    try:
        return path.read_text(encoding="gbk")     # 中文 Windows 的老文件
    except UnicodeDecodeError:
        raise ValueError(
            f"二进制文件无法按文本读取: {path.suffix or '(无扩展名)'}。"
            f"图片用 load_image；docx/xlsx/pptx/pdf 用 read_long_doc（已支持）"
        ) from None


def build_chunks(path: Path, budget: int) -> tuple[list[Chunk], int, str]:
    """切段。返回 (段列表, 总字数, 文档类型标签)。

    budget 是硬预算：调用方给了多少就用多少（显式传 chunk_chars 时不该被悄悄放大）。
    「别切太碎」的下限由 chunk_budget() 在自动算预算时保证，不在这里二次夹紧。
    """
    suf = path.suffix.lower()
    budget = max(200, int(budget))

    if suf in docparse.SHEET_SUFFIXES:
        raise ValueError(
            "Excel 表格不走分段精读：用 read_file 按工作表返回单元格，"
            "需要全表统计再按表名/行区间分批读"
        )
    if suf in docparse.LEGACY_SUFFIXES:
        raise ValueError(
            f"旧版二进制格式 {suf} 无法解析，请用 Office/WPS 另存为新格式后再读"
        )

    # ---- PDF / PPT：按页切（天然的语义边界，页码也是最好的出处标注）----
    if suf in docparse.PDF_SUFFIXES or suf in docparse.SLIDE_SUFFIXES:
        iter_pages = (docparse.iter_pdf_pages if suf in docparse.PDF_SUFFIXES
                      else docparse.iter_pptx_slides)
        atoms: list[tuple[int, str]] = []
        total = 0
        pages = 0
        for no, txt in iter_pages(path):
            pages += 1
            total += len(txt)
            for piece in _split_long_text(txt, budget):
                atoms.append((no, piece))
        if not atoms:
            if pages:
                raise ValueError(
                    f"这份文档共 {pages} 页但全部抽不到文字，基本可断定是扫描件/纯图 —— "
                    f"用 load_image(path=…, page=N) 按页渲染成图给视觉模型看"
                )
            raise ValueError("文档没有可用文字内容")
        chunks = [Chunk(i, _page_label(a, b), t)
                  for i, (a, b, t) in enumerate(_pack(atoms, budget), 1)]
        return chunks, total, ("PDF 文档" if suf in docparse.PDF_SUFFIXES else "PPT 幻灯片")

    # ---- Word / 纯文本：按句子切，再折算行号 ----
    if suf in docparse.DOC_SUFFIXES:
        label, text = docparse.extract_text(path)
    else:
        label, text = "文本", _read_text(path)
    if not text.strip():
        raise ValueError("文件是空的")
    packed = _pack(_sentence_atoms(text), budget)
    nl = [i for i, ch in enumerate(text) if ch == "\n"]
    chunks = [Chunk(i, _line_label(nl, a, b), t)
              for i, (a, b, t) in enumerate(packed, 1)]
    return chunks, len(text), label


# ---------------- 子请求 ----------------

class _Cancelled(Exception):
    pass


def _clean(text: str) -> str:
    """去掉模型爱加的外壳：代码围栏、开场的「好的，以下是……」。"""
    t = (text or "").strip()
    m = _FENCE.match(t)
    if m:
        t = m.group(1).strip()
    return t


def _ask(client, prompt: str, cancel=None) -> str:
    """发一条独立的子请求（不挂工具，避免模型在记笔记时去调工具）。"""
    resp = client.chat([{"role": "user", "content": prompt}], tools=None, cancel=cancel)
    if resp.get("cancelled"):
        raise _Cancelled()
    if resp.get("error"):
        raise RuntimeError(resp["error"])
    return _clean(resp.get("content") or "")


_FOCUS_PROMPT = """你在精读一份长文档。文档太长一次读不完，必须分成 {n} 段逐段读。
先只做一件事：根据用户的问题，列出读每一段时要盯住找的信息（关注点）。

用户的问题：
{question}

文档：{name}（{chars} 字，{label}，共 {n} 段）

输出要求：
- 3-6 行，每行以「- 」开头，一行一个关注点；
- 要具体到可检索（例如「各型号的功率与价格」「对交付周期的要求」），
  不要写「重要内容」「核心信息」这类抓不住东西的空话；
- 问题很笼统（如「总结全文」）时，改用这几个通用维度：全文结构、核心结论、
  关键数据与指标、重要定义与术语；
- 只输出清单，不要解释、不要前言、不要复述文档内容。"""

_SEG_PROMPT = """你在分段精读一份长文档，这是第 {i}/{n} 段（{where}）。

【要盯住的关注点】
{focus}

【前面各段已记的笔记】（不要重复，只补充新信息）
{notes}

【本段原文】
{chunk}

输出要求：
- 只写本段里与关注点相关的内容，用「- 」列表，每条先标出处【{where}】；
- 保留原文的具体数字、名称、术语、结论，不要泛泛概括；
- 与关注点无关的不要写；整段都无关时只输出一行：[无关]；
- 全部不超过 {max} 字，不要写前言、不要复述任务。"""

_COMPRESS_PROMPT = """把下面的分段读书记录压缩到 {budget} 字以内：
保留所有具体数字、名称、结论，以及每条的出处标注【…】；删掉重复、铺垫和过渡语。
直接输出压缩后的笔记，不要解释。

{notes}"""


def _focus(client, question: str, name: str, chars: int, label: str,
           n: int, cancel=None) -> str:
    """第一步：把用户问题翻译成「每段要找什么」。

    失败不致命 —— 退化成用户原话，比整轮失败强。
    """
    if n <= 1:
        focus = ("- " + question.strip()) if question.strip() else "- 全文要点"
    else:
        try:
            focus = _ask(client, _FOCUS_PROMPT.format(
                n=n, question=question.strip() or "（用户没写具体问题）",
                name=name, chars=chars, label=label), cancel=cancel)
        except _Cancelled:
            raise
        except Exception:
            focus = ""
        if len(focus) < 10:            # 模型没照格式写，或用原话兜底
            focus = (f"- {question.strip()}\n- 全文结构与核心结论\n"
                     f"- 关键数据与指标") if question.strip() else \
                    "- 全文结构\n- 核心结论\n- 关键数据与指标"
    # 关注点本身也不能太长，否则每段都要重复付这笔 token
    return focus[:800]


_SAME_QUESTION_PROMPT = """判断下面两个问题是不是在问同一件事。

问题A：{a}
问题B：{b}

只输出一个词：
- 一样   （问的是同一件事，换了个说法也算一样）
- 不一样 （问的是不同的事）

判断标准：如果这两个问题能在同一批笔记里得到回答，就是「一样」。
只要其中一个问到了另一个不关心的方面，就算「不一样」。
不要解释，只输出「一样」或「不一样」。"""


def same_question(client, a: str, b: str, cancel=None) -> bool:
    """让模型判断两个问题是否同义（缓存第三层）。

    为什么不能靠字符串相似度：实测「找出所有关于X的内容」vs「文中哪些地方
    提到了X」字面重合度为 0，而同义改写的分数又和真·不同问题重叠
    （0.27 是同义改写，0.11 是不同问题），没有任何阈值能分开。
    只有模型懂「经营情况」和「业绩表现」是一回事。

    判不准就当「不一样」：误判成一样会把不相干的笔记喂给模型，
    比重新读一遍更糟。任何异常也一律返回 False。
    """
    try:
        out = _ask(client, _SAME_QUESTION_PROMPT.format(
            a=a.strip()[:300], b=b.strip()[:300]), cancel=cancel)
    except _Cancelled:
        raise
    except Exception:
        return False
    # 只认明确的「一样」；模型啰嗦输出（「一样。理由…」）也接受前缀
    return out.strip().startswith("一样")


_PICK_TOPIC_PROMPT = """一份文档的笔记里已经记了这些主题：

{topics}

现在用户问：{question}

这个问题的答案，是否已经包含在上面某个主题里？

只输出一个词：
- 有  （已经包含，输出那个主题名，一行一个词，如：有 商户经营）
- 无  （没有包含，需要新读）

判断要严格：只有「现有主题能直接回答这个问题」才算「有」。
只是沾边、需要补充新内容，都算「无」。"""


def pick_topic(client, nb, question: str, cancel=None) -> str:
    """问模型：现有主题里有没有能回答这个问题的。返回主题名，没有返回 ""。

    这是「笔记检索」的核心一步，但有意做得很轻：不做向量库、不做 embedding，
    因为一份文档的主题通常只有 5~20 个，直接把主题名列表给模型看就行。
    上向量检索属于过度设计——那套东西是为「几百万份文档」准备的。
    """
    if not nb or not nb.topics:
        return ""
    names = list(nb.topics)
    # 主题太多时只报最近的前 30 个，避免提示词自己撑大
    listing = "\n".join(f"- {n}" for n in names[-30:])
    try:
        out = _ask(client, _PICK_TOPIC_PROMPT.format(
            topics=listing, question=question.strip()[:300]), cancel=cancel)
    except _Cancelled:
        raise
    except Exception:
        return ""
    out = out.strip()
    if not out.startswith("有"):
        return ""
    # 从输出里认出主题名：直接匹配已有主题，避免模型自由发挥写出新名字
    rest = out[1:].strip(" :：-")
    for n in names:
        if n and n in rest:
            return n
    return ""


_ORGANIZE_PROMPT = """把下面这段读书笔记归到一个主题下，并起一个简短的主题名。

已有的主题（可以复用其中一个，也可以新起一个）：
{existing}

这段笔记是围绕这个问题记的：{question}

笔记内容：
{notes}

输出格式（严格两行，不要多余的话）：
主题：<主题名，不超过 12 个字，概括这段笔记讲什么>
位置：<这些内容在文档里的位置，如「第3页」「行120–480」，从笔记里提取>

主题名要和已有主题保持一致的颗粒度（如「商户经营」「违约与赔偿」），
不要起「其他」「杂项」这类没信息量的名字。"""


def organize_notes(client, notes: str, question: str, existing: list[str],
                   where: str, cancel=None) -> tuple[str, str]:
    """把新读到的笔记归到一个主题下。返回 (主题名, 位置)。

    归主题而不是按问题另存：这是「笔记越用越厚」能成立的前提——
    同一个主题被问两次，笔记应该合并成一节，而不是散成两份。
    模型判不出来就退回用问题当主题名，至少不会丢内容。
    """
    fallback = notebook.clean_topic_name(question) or where or "未分类"
    try:
        out = _ask(client, _ORGANIZE_PROMPT.format(
            existing="\n".join(f"- {n}" for n in existing[-20:]) or "（还没有任何主题）",
            question=question.strip()[:200],
            notes=notes.strip()[:4000]), cancel=cancel)
    except _Cancelled:
        raise
    except Exception:
        return fallback, where

    name = ""
    pos = where
    for line in out.splitlines():
        s = line.strip()
        if s.startswith(("主题：", "主题:")):
            name = notebook.clean_topic_name(s.split("：", 1)[-1].split(":", 1)[-1])
        elif s.startswith(("位置：", "位置:")):
            pos = s.split("：", 1)[-1].split(":", 1)[-1].strip() or where
    return (name or fallback), pos


def _compress(client, notes: str, budget: int, cancel=None) -> str:
    """笔记超限时压缩。

    只接受「像笔记、且确实变短了」的输出：模型偶尔会返回一句「好的，已压缩」，
    把几万字笔记顶成一句空话，那比不压缩更糟。
    """
    try:
        out = _ask(client, _COMPRESS_PROMPT.format(budget=budget, notes=notes),
                   cancel=cancel)
    except _Cancelled:
        raise
    except Exception:
        return notes          # 压缩失败就用原笔记，宁可费 token 也别丢信息
    return out if len(out) >= 20 and len(out) < len(notes) else notes


# ---------------- 主流程 ----------------

def _lookup_topic(client, nb, question: str, cancel) -> tuple[str, str]:
    """在已有笔记里找能回答这个问题的主题。

    两级，先便宜后贵：
      1. FAQ 精确查（归一化后相同）—— 0 次模型调用，纯字典查找；
      2. 让模型看主题清单判断 —— 1 次调用。主题通常只有 5~20 个，
         直接把清单给模型看，比上向量检索轻得多。
    返回 (主题名, 命中方式)，没命中返回 ("", "")。

    不要图省事在「只有一个主题」时直接返回它：那样这份文档的**任何**
    新问题都会被判成命中，笔记从此不再增长，还会拿不相干的主题作答。
    验证过：加这条捷径后，「交付周期」会被「功率与价格」的主题吃掉。
    """
    t = nb.find_faq(question)
    if t and t in nb.topics:
        return t, "问过同一个问题"
    picked = pick_topic(client, nb, question, cancel=cancel)
    if picked and picked in nb.topics:
        return picked, "已有主题可回答"
    return "", ""


def run_long_doc(client, path: Path, question: str, num_ctx: int | None = None,
                 notice=None, cancel=None, chunk_chars: int = 0,
                 max_segments: int = MAX_SEGMENTS, refresh: bool = False,
                 workspace: str | Path | None = None,
                 _progress=None) -> LongDocResult:
    """精读一份超长文档。

    流程固定四步：需求分析（关注点）→ 逐段读+记笔记 → 笔记超限就压缩 → 交回主循环。
    切段和预算全部由这里决定，模型不参与调度 —— 小模型一旦拿到调度权就会乱跳。

    refresh=True 跳过缓存强制重读（笔记质量不满意时用）。
    workspace: 笔记落在 <workspace>/.ollama-agent/notes/。不传就用当前目录——
        宁可放到一个明确的地方，也别猜。
    _progress: 测试钩子，每段开始前回调 (i, n, label)。
    """
    say = notice or (lambda _t: None)
    ctx = num_ctx or FALLBACK_CTX

    if ctx < 12000:
        say(f"[提示] 上下文只有 {ctx}，单段能读的原文很少，会切得很碎；"
            f"能把 --ctx 调大就调大")

    budget = chunk_chars if (isinstance(chunk_chars, int) and chunk_chars > 0) \
        else chunk_budget(num_ctx)

    # 先算哈希、查笔记库，再切段：命中时连抽取全文都省了（几百页 PDF 抽取不便宜）。
    # 哈希失败不拦路——交给 build_chunks 报出更具体的错误（文件不存在/无权限等）。
    digest = ""
    book = None
    if not refresh:
        try:
            digest = notebook.file_digest(path)
        except Exception:
            digest = ""
        if digest:
            book = notebook.load(digest, workspace)
            if book:
                hit_topic, how = _lookup_topic(client, book, question, cancel)
                if hit_topic:
                    say(f"[长文精读] 命中已有笔记（{how}：{hit_topic}），跳过重读；"
                        f"要重新精读请加 refresh=true")
                    # 命中后顺手把这个问题记进 FAQ：下次同样问法就不用再问模型了。
                    # 不记的话，「换了说法」每次都要多花一次调用去重新判断。
                    if how != "问过同一个问题":
                        book.add_faq(question, hit_topic)
                        notebook.save(book, workspace)
                    return LongDocResult(
                        text=book.render(hit_topic, question=question),
                        chars=0, segments=len(book.topics), done=len(book.topics),
                        cached=True,
                    )

    chunks, total_chars, label = build_chunks(path, budget)
    if not chunks:
        raise ValueError("文档没有可用文字内容")

    truncated = False
    all_segments = len(chunks)
    if len(chunks) > max_segments:
        truncated = True
        say(f"[长文精读] 共 {len(chunks)} 段，超过单次上限 {max_segments} 段，"
            f"本次精读前 {max_segments} 段（约 {max_segments * budget} 字）")
        chunks = chunks[:max_segments]

    n = len(chunks)
    name = path.name

    if n == 1:
        # 整篇本来就装得下：直接给全文，别为了走流程把 3 万字压成 1200 字笔记
        say(f"[长文精读] {name}｜{total_chars} 字｜{label}｜一次装得下，直接整篇返回")
        return LongDocResult(
            text=(f"[全文已读完] {name}｜{total_chars} 字｜{label}｜一次装得下，无需分段。\n"
                  f"请直接依据下面的原文回答用户问题；引用时标注位置（如「第3页」「行120」）。\n\n"
                  f"{chunks[0].text}"),
            chars=total_chars, segments=1, done=1,
        )

    say(f"[长文精读] {name}｜{total_chars} 字｜{label}｜切成 {n} 段"
        f"（每段约 {budget} 字）→ 先分析需求，再逐段记笔记")

    focus = _focus(client, question, name, total_chars, label, n, cancel=cancel)
    say("[长文精读] 关注点：" + " / ".join(
        ln.lstrip("- ").strip() for ln in focus.splitlines() if ln.strip())[:300])

    nb = notes_budget(num_ctx)
    note_blocks: list[str] = []
    failed: list[str] = []
    done = 0
    used_notes = 0
    cancelled = False

    for ch in chunks:
        if cancel is not None and getattr(cancel, "is_set", lambda: False)():
            cancelled = True
            break
        if _progress:
            _progress(ch.index, n, ch.label)
        t0 = time.time()
        notes_so_far = "\n".join(note_blocks) or "（还没有笔记，这是第一段）"
        prompt = _SEG_PROMPT.format(
            i=ch.index, n=n, where=ch.label, focus=focus,
            notes=notes_so_far, chunk=ch.text, max=NOTES_PER_SEG)
        try:
            out = _ask(client, prompt, cancel=cancel)
        except _Cancelled:
            cancelled = True
            break
        except Exception as e:
            failed.append(f"{ch.label}（{type(e).__name__}: {e}）")
            say(f"[长文精读 {ch.index}/{n}] {ch.label} → 失败：{type(e).__name__}，跳过")
            continue

        if not out:
            failed.append(f"{ch.label}（模型没返回笔记）")
            say(f"[长文精读 {ch.index}/{n}] {ch.label} → 空笔记，跳过")
            continue

        done += 1
        if out.strip() == "[无关]" or out.strip().startswith("[无关]"):
            note_blocks.append(f"【{ch.label}】与关注点无关")
            say(f"[长文精读 {ch.index}/{n}] {ch.label} → 无关")
        else:
            note_blocks.append(out)
            used_notes += len(out)
            say(f"[长文精读 {ch.index}/{n}] {ch.label} → 笔记 {len(out)} 字"
                f"（{time.time() - t0:.1f}s）")
            # 笔记涨到上限就压缩：不压缩的话后面每段的提示词会越来越长，
            # 单段预算被吃掉，最后几段反而读不动
            if used_notes > nb and len(note_blocks) > 1:
                merged = "\n".join(note_blocks)
                say(f"[长文精读] 笔记已达 {used_notes} 字，压缩到 {int(nb * 0.65)} 字以内")
                compressed = _compress(client, merged, int(nb * 0.65), cancel=cancel)
                note_blocks = [compressed]
                used_notes = len(compressed)

    notes = "\n".join(note_blocks)

    # 只把「干净读完」的结果写进笔记库：有段失败就说明笔记缺了内容，
    # 存下来等于把这个缺口固化，下次命中就再也不会去补那几段了。
    if digest and not cancelled and not failed and done == n and notes.strip():
        _save_to_notebook(client, digest, name, question, notes, label, book,
                          say, cancel, workspace)

    return LongDocResult(
        text=_format(name, total_chars, label, n, done, notes, focus,
                     failed=failed, cancelled=cancelled, truncated=truncated,
                     budget=budget, all_segments=all_segments),
        chars=total_chars, segments=n, done=done, failed=failed,
        cancelled=cancelled,
    )


def _save_to_notebook(client, digest: str, name: str, question: str, notes: str,
                      where: str, book, say, cancel, workspace=None) -> None:
    """把这次精读的笔记归入主题并写回笔记库。

    归主题而不是按问题另存：同一个主题被问两次要合并成一节，
    这是「笔记越用越厚」能成立的前提。合并逻辑在 Notebook.add_topic。
    """
    try:
        existing = list(book.topics) if book else []
        topic, pos = organize_notes(client, notes, question, existing, where,
                                    cancel=cancel)
        target = book or notebook.Notebook(file=name, digest=digest)
        if not target.file:
            target.file = name
        target.digest = digest
        target.add_topic(topic, notes, pos)
        target.add_faq(question, topic)
        saved = notebook.save(target, workspace)
        if saved:
            verb = "合并进" if topic in existing else "新增主题"
            say(f"[长文精读] 笔记已{verb}「{topic}」（{saved.name}），"
                f"下次问同一主题直接复用")
    except _Cancelled:
        raise
    except Exception:
        pass          # 笔记存不下来不该影响这次回答


def _format(name: str, chars: int, label: str, n: int, done: int, notes: str,
            focus: str, failed: list[str], cancelled: bool, truncated: bool,
            budget: int, all_segments: int) -> str:
    """把笔记整理成主循环能直接作答的形状。

    措辞是硬约束：不写清楚「只能依据这些笔记」，小模型会拿常识补全空缺，
    把没读到的部分编得有鼻子有眼。
    """
    if cancelled:
        head = (f"[长文精读·已取消] {name}｜{chars} 字｜{label}｜"
                + (f"读到第 {done}/{n} 段被打断，下面是已读部分的笔记。"
                   if done else "刚开头就被打断，还没有任何笔记。"))
    elif done == 0:
        return (f"[错误] 长文精读失败：{name} 共 {n} 段，没有一段成功生成笔记。"
                f"原因：{'; '.join(failed[:3]) or '模型无输出'}。"
                f"可以先用 read_file(offset=…, limit=…) 直接读关键页，或换更大的模型重试。")
    else:
        head = (f"[长文精读完成] {name}｜{chars} 字｜{label}｜"
                f"{done}/{n} 段已逐段读完（每段约 {budget} 字）")

    tail: list[str] = []
    if truncated:
        tail.append(f"⚠️ 文档实际超过 {all_segments} 段，本次只精读了前 {n} 段，后面没读。"
                    f"如果需要后面的内容，请把文件拆小后分别精读，或用 read_file 直接取指定页。")
    if failed:
        tail.append("⚠️ 这些段没读到：" + "；".join(failed[:5]) +
                    ("…" if len(failed) > 5 else ""))
    if cancelled:
        tail.append(f"⚠️ 用户打断了本轮，剩余段落未读。回答时说明「只读到第 {done} 段」"
                    f"或「还没读到内容」。")
    if done:
        tail.append(
            "请只依据上面的笔记回答用户的问题：\n"
            "1. 笔记覆盖不到的地方，直接说「文档中没有提到」，绝不凭常识补全；\n"
            "2. 引用结论时带上笔记里的出处标注（如【行120–480】【第3页】），方便用户回查；\n"
            "3. 笔记里互相矛盾的地方要指出来，不要替文档圆场。"
        )

    return (f"{head}\n\n【读前确定的关注点】\n{focus}\n\n"
            f"【分段笔记】\n{notes or '（无）'}\n\n【说明】\n" + "\n".join(tail))


def too_long(chars: int, num_ctx: int | None = None) -> bool:
    """这段文字是否超出单次能读的量（超过一个段预算就算超）。

    判据就是切段预算本身：既然一次塞得下的只有 budget，
    那超过 budget 的内容一次性全给模型必然被截断或被上下文挤掉。
    """
    return chars > chunk_budget(num_ctx)


def long_read_hint(chars: int, num_ctx: int | None = None) -> str:
    """超长时给模型的下一步指令。

    这段文字要能让模型立刻放弃「多调几次 read_file 凑」的念头 ——
    实测它会一直翻页，把步数预算耗光，然后凭前几页印象编答案。
    """
    budget = chunk_budget(num_ctx)
    est = max(2, -(-chars // max(1, budget)))
    return (f"\n\n⚠️ 这份文档约 {chars} 字，超出单次可读量（约 {budget} 字）。"
            f"不要用 read_file 反复翻页（会耗光工具步数，且读到后面忘了前面）。"
            f"改用 read_long_doc(path=…, question=…)：它会先分析需求、把文档切成约 {est} 段，"
            f"逐段读并记笔记，最后按笔记作答。")


def page_estimate(path: Path) -> tuple[int, str] | None:
    """超长文档的粗估页数/字数（只给提示文案用，不解析正文）。"""
    suf = path.suffix.lower()
    try:
        if suf in docparse.PDF_SUFFIXES:
            pages = docparse.pdf_page_count(path)
            return pages, f"{pages} 页"
        if suf in docparse.SLIDE_SUFFIXES:
            slides = docparse.pptx_slide_count(path)
            return slides, f"{slides} 页幻灯片"
    except Exception:
        return None
    return None


# 判断「附件大到需要精读」的阈值。这里故意用保守的粗指标（页数/文件体积），
# 因为这条提示在每回合开始前跑，不能为了精确而解析整个文档。
_BIG_BYTES = 120_000        # 约 4 万字以上的中文文本 / 更长的英文
_BIG_PDF_PAGES = 40
_BIG_SLIDES = 40
# 会走长文精读的后缀：文本类（含代码/配置，长篇日志同样适用）+ Office 文档。
# 表格不在此列——它按工作表读，整篇塞进上下文没有意义。
_LONG_DOC_SUFFIXES = (docparse.TEXT_SUFFIXES | docparse.DOC_SUFFIXES
                      | docparse.SLIDE_SUFFIXES | docparse.PDF_SUFFIXES
                      | docparse.SHEET_SUFFIXES)
_PATH_LINE = re.compile(r"^\s*-\s+(.+?)\s*$", re.M)


def _big_attachment(p: Path) -> str | None:
    """这个附件是否大到单次读不完。返回一句尺寸描述，否则 None。"""
    suf = p.suffix.lower()
    if suf in docparse.SHEET_SUFFIXES:
        return None          # 表格本来就要按工作表/行区间读，不走整篇精读
    try:
        if suf in docparse.PDF_SUFFIXES:
            n = docparse.pdf_page_count(p)
            return f"{n} 页" if n >= _BIG_PDF_PAGES else None
        if suf in docparse.SLIDE_SUFFIXES:
            n = docparse.pptx_slide_count(p)
            return f"{n} 页幻灯片" if n >= _BIG_SLIDES else None
        size = p.stat().st_size
        return f"{size // 1024} KB" if size >= _BIG_BYTES else None
    except Exception:
        return None


def attachment_hint(text: str, workspace: Path, num_ctx: int | None = None) -> str:
    """用户本条消息附带了超大文档时，补一条「必须用 read_long_doc」的硬指令。

    为什么要抢在模型开口之前：它一旦先 read_file 读了开头两百行，
    就会顺着「再翻一页」的路走到黑（实测 9B 能连翻十几次直到步数耗尽），
    然后在只见过前 3% 的情况下把全文总结写得像模像样。
    在消息里点名文件、点明页数、指定工具，比任何系统提示都管用。
    """
    if "[我附带" not in text:
        return ""
    rows: list[str] = []
    for m in _PATH_LINE.finditer(text):
        raw = m.group(1).strip().strip('"').strip("'")
        if not raw or raw.startswith("["):
            continue
        p = Path(raw)
        if not p.is_absolute():
            p = workspace / p
        try:
            if not p.is_file() or p.suffix.lower() not in _LONG_DOC_SUFFIXES:
                continue
        except OSError:
            continue
        size = _big_attachment(p)
        if size:
            rows.append(f'- {raw}（{size}）')
    if not rows:
        return ""
    budget = chunk_budget(num_ctx)
    return ("\n\n[系统指令] 本次附带的是长文档，单次读不完（每段上限约 "
            f"{budget} 字）：\n" + "\n".join(rows) +
            "\n回答前必须先用 read_long_doc(path=…, question=<用户的原问题>) "
            "整篇分段精读（它会边读边记笔记）；不要用 read_file 反复翻页，"
            "那样读不完全文，答案会不准。")
