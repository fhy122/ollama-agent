"""主题笔记库：一份长文档对应一本可生长的笔记。

为什么不按「问题」存笔记（第一版做法）：一次精读要 5~8 次模型调用、十几分钟，
按问题存意味着同一份文件问三个问题就读三遍。而且同一件事的答案会散在
三份笔记里，互相不知道对方存在。

改按「文件」存、内部按「主题」分节之后：
  - 笔记成了这份文档的知识积累载体，越用越厚；
  - 追问命中的是**某个主题**而不是整本笔记，所以笔记总量可以无限增长，
    每次只取相关的那一节塞进上下文（这才是能撑住长文的关键）；
  - 目录负责「定位」，FAQ 负责「答过没有」，主题笔记负责「内容」。

文件结构（Markdown，人能直接看、能进 git）：

    ---
    version: 1
    file: 白皮书.pdf
    digest: a1b2c3...
    ---

    ## 目录
    - 商户经营 …… 第3页

    ## 主题笔记
    ### 商户经营
    - 2024 年 GMV 1.2 亿【第3页】

    ## 已答问题
    - 商户经营情况如何？ → 商户经营

为什么用 Markdown 而不是 JSON：老板要能打开看、要能 diff、要能手工改。
JSON 里那些转义和嵌套会让「人可读」这件事直接失效。

为什么解析要写得这么谨慎：这个文件是**用户可编辑**的，改坏了不能让精读崩掉。
任何解析失败都退化成「没有笔记」，重新读一遍就是了。
"""
from __future__ import annotations

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

# 切段规则或提示词改了就把版本 +1，旧笔记自动作废（宁可重读，不要读错的笔记）
NOTEBOOK_VERSION = 1

# 笔记跟着工作空间走：<工作空间>/.ollama-agent/notes/
# 为什么不放全局目录（~/.ollama-agent）或项目包目录：笔记是「这份文档读出了什么」，
# 天然属于某个工作空间。放全局会让 A 项目的笔记被 B 项目读到；放包目录则会在
# 每个 checkout 里各存一份、彼此不认，而且用户根本不知道它在哪。
NOTEBOOK_SUBDIR = Path(".ollama-agent") / "notes"

NOTEBOOK_ENV = "OLLAMA_AGENT_NOTEBOOK"          # 设 off/0 可整体关掉
NOTEBOOK_DIR_ENV = "OLLAMA_AGENT_NOTEBOOK_DIR"  # 显式指定目录（测试/排障用）

# 目录条目 / FAQ 条目的数量上限：再多人也不会看，还会把上下文吃掉
MAX_TOC = 60
MAX_FAQ = 200
# 单个主题的笔记上限：防止一个主题无限膨胀，最后没法塞进上下文
MAX_TOPIC_CHARS = 6000

_SEC = TOC, TOPICS, FAQ = "## 目录", "## 主题笔记", "## 已答问题"


def enabled() -> bool:
    v = os.environ.get(NOTEBOOK_ENV, "").strip().lower()
    return v not in ("0", "off", "false", "no")


def notebook_dir(workspace: str | Path | None = None) -> Path:
    """笔记目录：<工作空间>/.ollama-agent/notes/

    环境变量 OLLAMA_AGENT_NOTEBOOK_DIR 可整体改道（测试要绝对隔离时用）。
    没给工作空间就退回当前目录——宁可放到一个明确的地方，也别猜。
    """
    override = os.environ.get(NOTEBOOK_DIR_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    base = Path(workspace).expanduser() if workspace else Path.cwd()
    return base / NOTEBOOK_SUBDIR


# ---------------- 数据结构 ----------------

@dataclass
class Notebook:
    """一本笔记。topics 有序（保持插入顺序，新主题追加在末尾）。"""
    file: str = ""
    digest: str = ""
    topics: dict[str, str] = field(default_factory=dict)   # 主题名 → 笔记正文
    toc: dict[str, str] = field(default_factory=dict)      # 主题名 → 位置（第3页 / 行1–99）
    faq: list[tuple[str, str]] = field(default_factory=list)   # (问题, 主题名)
    updated: str = ""
    path: Path | None = None

    # ---- 读 ----

    def topic_names(self) -> list[str]:
        return list(self.topics)

    def get_topic(self, name: str) -> str:
        return self.topics.get(name, "")

    def find_faq(self, question: str) -> str:
        """按归一化问题精确查 FAQ，返回主题名；没有返回 ""。"""
        key = norm_question(question)
        if not key:
            return ""
        for q, topic in self.faq:
            if norm_question(q) == key:
                return topic
        return ""

    # ---- 写 ----

    def add_topic(self, name: str, notes: str, where: str = "") -> None:
        """新增或**合并**同主题笔记。

        合并而不是覆盖：追问「违约金上限」时读到的新事实，不该把
        上次记的「逾期利率」冲掉。去重按行做——同一行原文重复记两次
        在笔记里毫无意义，只会白占预算。
        """
        name = clean_topic_name(name)
        if not name:
            return
        notes = (notes or "").strip()
        old = self.topics.get(name, "")
        if not old:
            self.topics[name] = notes
        elif notes and notes not in old:
            merged = _merge_notes(old, notes)
            self.topics[name] = merged[:MAX_TOPIC_CHARS]
        if where:
            prev = self.toc.get(name, "")
            self.toc[name] = _merge_where(prev, where)

    def add_faq(self, question: str, topic: str) -> None:
        """记下「这个问题问过，答案在那个主题」。同问题只留一条，指向最新主题。"""
        q = re.sub(r"\s+", " ", str(question or "")).strip()
        if not q or not topic:
            return
        self.faq = [(a, b) for a, b in self.faq if norm_question(a) != norm_question(q)]
        self.faq.append((q[:120], topic))
        if len(self.faq) > MAX_FAQ:
            self.faq = self.faq[-MAX_FAQ:]

    # ---- 序列化 ----

    def dumps(self) -> str:
        lines: list[str] = [
            "---",
            f"version: {NOTEBOOK_VERSION}",
            f"file: {self.file}",
            f"digest: {self.digest}",
            f"updated: {self.updated or time.strftime('%Y-%m-%d %H:%M:%S')}",
            "---",
            "",
            _SEC[0],        # ## 目录
        ]
        for name in self.topics:
            where = self.toc.get(name, "")
            lines.append(f"- {name} …… {where}" if where else f"- {name}")
        if not self.topics:
            lines.append("（空）")

        lines += ["", _SEC[1]]      # ## 主题笔记
        if not self.topics:
            lines.append("（空）")
        for name, body in self.topics.items():
            lines += ["", f"### {name}", body.rstrip()]

        lines += ["", _SEC[2]]      # ## 已答问题
        if not self.faq:
            lines.append("（空）")
        for q, topic in self.faq:
            lines.append(f"- {q} → {topic}")
        return "\n".join(lines) + "\n"

    @classmethod
    def loads(cls, raw: str, path: Path | None = None) -> "Notebook":
        """解析。任何结构问题都尽力容忍，不抛异常。"""
        nb = cls(path=path)
        raw = raw or ""
        meta: dict[str, str] = {}
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", raw, re.S)
        if m:
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    meta[k.strip()] = v.strip()
            raw = m.group(2)
        if meta.get("version") and meta["version"] != str(NOTEBOOK_VERSION):
            # 版本不符：当作没有笔记，让调用方重新精读
            return cls(path=path, file=meta.get("file", ""))
        nb.file = meta.get("file", "")
        nb.digest = meta.get("digest", "")
        nb.updated = meta.get("updated", "")

        # 按二级标题切三段
        blocks: dict[str, list[str]] = {}
        cur = ""
        for line in raw.splitlines():
            s = line.rstrip()
            if s in _SEC:
                cur = s
                blocks.setdefault(cur, [])
                continue
            if cur:
                blocks[cur].append(s)

        # 目录
        for line in blocks.get(TOC, []):
            t = line.strip()
            if not t.startswith("- "):
                continue
            body = t[2:].strip()
            if "……" in body:
                name, where = body.split("……", 1)
                nb.toc[clean_topic_name(name)] = where.strip()
            elif body and body != "（空）":
                nb.toc.setdefault(clean_topic_name(body), "")

        # 主题笔记：### 标题 切块
        cur_topic = ""
        buf: list[str] = []

        def _flush() -> None:
            if cur_topic:
                body = "\n".join(buf).strip()
                nb.topics[cur_topic] = body

        for line in blocks.get(TOPICS, []):
            s = line.rstrip()
            if s.startswith("### "):
                _flush()
                cur_topic = clean_topic_name(s[4:])
                buf = []
            elif cur_topic:
                if s.strip() == "（空）":
                    continue
                buf.append(s)
        _flush()

        # FAQ
        for line in blocks.get(FAQ, []):
            t = line.strip()
            if not t.startswith("- ") or "→" not in t:
                continue
            q, topic = t[2:].split("→", 1)
            q = q.strip()
            topic = clean_topic_name(topic)
            if q and topic:
                nb.faq.append((q, topic))

        # 目录里有、主题里没有的条目：主题丢了就一起丢掉，别留悬空目录
        for name in list(nb.toc):
            if name not in nb.topics:
                nb.toc.pop(name, None)
        return nb

    # ---- 渲染给模型 ----

    def render(self, topic: str, *, question: str = "") -> str:
        """把某个主题渲染成给模型读的文本。

        只给这一节（外加目录当上下文），不是整本笔记——这是笔记能无限
        增长还塞得进上下文的原因。
        """
        body = self.get_topic(topic)
        toc = "\n".join(
            f"- {n}" + (f"（{w}）" if (w := self.toc.get(n, "")) else "")
            for n in self.topics)
        head = f"【主题】{topic}"
        if question:
            head += f"（对应问题：{question}）"
        return (f"{head}\n\n{body}\n\n"
                f"【本笔记已有的其他主题】（如需这些内容，可再精读对应页）\n{toc}")


# ---------------- 工具函数 ----------------

def clean_topic_name(name: str) -> str:
    """主题名清理：去掉 markdown 噪音、压成一行、限长。

    主题名是 key，必须稳定：模型每次写的名字略有出入就会分裂成两个主题，
    所以这里统一去标点收尾、去空白。
    """
    s = re.sub(r"[`*_#>\[\]]", "", str(name or ""))
    s = re.sub(r"\s+", " ", s).strip()
    s = s.strip("：:，,。.、-—…")
    return s[:40]


def norm_question(q: str) -> str:
    """问题归一化：剥标点、空白与提问套话。

    「商户经营情况如何？」和「商户经营的怎么样？」剥完都是「商户经营」，
    所以能命中同一条 FAQ。只剥不承载信息的词，不动业务名词。
    """
    s = re.sub(r"\s+", "", str(q or ""))
    s = re.sub(r"[，。！？；：、,.!?;:\"'“”‘’（）()【】\[\]]", "", s)
    s = s.lower()
    for w in _FILLER:
        s = s.replace(w, "")
    return s


_FILLER = (
    "怎么样", "怎样", "如何", "是怎么", "是怎样的", "是什么样",
    "有哪些", "有什么", "是什么", "哪些", "什么",
    "情况", "样子", "吗", "呢", "吧", "啊",
    "请", "帮我", "帮忙", "麻烦", "我想知道", "想知道",
    "的", "了", "是",
)


def _merge_notes(old: str, new: str) -> str:
    """合并两段笔记：按行去重，保留先后顺序。

    新内容里已经出现在旧笔记中的行直接丢掉——重复行既占预算又没信息量。
    反过来，旧笔记的行全部保留，因为新读到的内容不该覆盖旧结论。
    """
    seen = {ln.strip() for ln in old.splitlines() if ln.strip()}
    out = old.rstrip().splitlines()
    for ln in new.splitlines():
        t = ln.strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(ln.rstrip())
    return "\n".join(out).strip()


def _merge_where(old: str, new: str) -> str:
    """合并位置标注（第3页 + 第12–15页 → 第3页、第12–15页）。"""
    if not old:
        return new
    if not new or new in old:
        return old
    if old in new:
        return new
    return f"{old}、{new}"


def file_digest(path: Path, chunk: int = 1 << 20) -> str:
    """文件内容哈希（sha256，分块读）。

    为什么不用 mtime+size：改完又改回来、cp -p 保留 mtime 都会误判。
    代价是每次多算一遍哈希（20 万字 pdf 约几十毫秒），可接受。
    """
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ---------------- 存取 ----------------

def notebook_path(digest: str, workspace: str | Path | None = None) -> Path:
    return notebook_dir(workspace) / f"{digest[:16]}.md"


def load(digest: str, workspace: str | Path | None = None) -> Notebook | None:
    """按文件哈希读笔记。没有 / 坏了 / 版本不符都返回 None。"""
    if not enabled() or not digest:
        return None
    p = notebook_path(digest, workspace)
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8")
    except Exception:
        return None
    nb = Notebook.loads(raw, path=p)
    if nb.digest and nb.digest != digest:
        # 哈希对不上说明文件被替换过，这份笔记不能用
        return None
    if not nb.topics:
        return None
    return nb


def save(nb: Notebook, workspace: str | Path | None = None) -> Path | None:
    """写笔记（临时文件 + 原子替换）。失败返回 None，绝不抛——缓存坏了不能影响回答。"""
    if not enabled() or not nb.digest or not nb.topics:
        return None
    try:
        notebook_dir(workspace).mkdir(parents=True, exist_ok=True)
        p = notebook_path(nb.digest, workspace)
        tmp = p.with_suffix(".md.tmp")
        nb.updated = time.strftime("%Y-%m-%d %H:%M:%S")
        tmp.write_text(nb.dumps(), encoding="utf-8")
        os.replace(tmp, p)
        nb.path = p
        return p
    except Exception:
        return None


def list_topics(nb: Notebook) -> str:
    """给模型看的主题清单（一句话）。"""
    if not nb or not nb.topics:
        return "（空）"
    return "、".join(nb.topics)
