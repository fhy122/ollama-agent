# 第 8 章 · 能力层：Office、图片、技能

**本章产出**：把"能操作文件"升级成"能产出文件、能看东西、能加载工作流"。这一章不是逐行代码，而是**六种典型能力的实现套路** + 一套"加工具"的固定流程。

---

## 一、能力层的共同约束

写工具时反复撞到的三堵墙，先记住它们的解法：

| 墙 | 表现 | 解法 |
|---|---|---|
| 模型只会写字 | 让它"生成 Excel"，它会输出一段 CSV 文本存成 `.txt` | 扩展名决定真实格式：`write_file` 里按后缀路由到 openpyxl / python-docx |
| 上下文有限 | 5000 行文件一次全读进来，模型开始胡说 | 行窗口 + `offset` 翻页 + `clip_text` 掐头去尾 |
| 模型会犯错 | 少写一列、路径写错、参数类型不对 | **校验并抛错回传**，让它重写；绝不做"尽力而为"的修补 |

第三条最反直觉，下面用 xlsx 的案例讲透。

---

## 二、`write_file`：一个工具，三种文件

```python
@tool(
    name="write_file",
    description=(
        "把内容写入本地文件（覆盖写，自动创建父目录）。相对路径基于工作空间。"
        "按扩展名自动选择格式："
        ".xlsx/.xlsm = 生成真正的 Excel 表格（content 推荐 JSON 二维数组，首行是表头，"
        "每行列数必须与表头一致、缺值用空字符串占位）；"
        ".docx = 生成真正的 Word 文档（content 传 Markdown 子集）；"
        "其他扩展名 = 纯文本原样写入。"
        "不要把 CSV/Markdown 存成 .txt 冒充 Excel/Word，扩展名决定了真正的文件格式。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目标路径；扩展名决定输出格式"},
            "content": {"type": "string", "description": "完整内容"},
        },
        "required": ["path", "content"],
    },
    confirm=True,
)
def write_file(ctx: ToolContext, path: str, content: str) -> ToolResult:
    p = ctx.resolve(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    content = content or ""
    suf = p.suffix.lower()

    if suf in docparse.LEGACY_SUFFIXES:          # .xls/.doc/.ppt
        return ToolResult(
            f"[错误] 旧版二进制格式 {suf} 不支持写入，会是伪文件。"
            f"请改用 {'xlsx' if suf == '.xls' else 'docx' if suf == '.doc' else 'pptx'} 扩展名")

    if suf in docparse.SHEET_SUFFIXES:           # .xlsx/.xlsm
        try:
            summary = docgen.write_xlsx(p, content)
        except ValueError as e:
            return ToolResult(f"[错误] 表格内容解析失败: {e}")
        return ToolResult(f"[已写入] {p}（{summary}，真 xlsx 可直接用 Excel 打开）")

    if suf in docparse.DOC_SUFFIXES:             # .docx
        try:
            summary = docgen.write_docx(p, content)
        except ValueError as e:
            return ToolResult(f"[错误] 文档内容解析失败: {e}")
        return ToolResult(f"[已写入] {p}（{summary}，真 docx 可直接用 Word 打开）")

    p.write_text(content, encoding="utf-8")
    return ToolResult(f"[已写入] {p}（{len(content)} 字符）")
```

三个决策：

**① 为什么用扩展名路由，不加个 `format` 参数？**

因为模型对扩展名的理解比对参数名的理解可靠得多。说"存成 report.xlsx"，它一定传 `.xlsx`；说"format=excel"，它可能传 `"excel"`、`"xlsx"`、`"Excel"`。**能用文件系统语义表达的，就不要发明参数。**

**② 为什么明确拒绝 `.xls` / `.doc`？**

旧格式是 OLE 复合文档，openpyxl / python-docx 都写不了。如果硬写，会得到一个扩展名是 `.xls` 但内容是纯文本的**伪文件** —— Excel 打开时提示"格式与扩展名不符"，用户以为文件坏了。**明确报错比生成一个看起来成功的坏文件好。**

**③ 为什么 `confirm=True`？**

写盘是"改变用户磁盘状态"的操作。这是安全边界，不是体验问题。

---

## 三、xlsx 的核心：宁可报错，不要错位

`docgen.write_xlsx` 里有这么一段校验，是整个项目里**最有价值的一处防御**：

```python
def _validate_rectangular(sheets: list[tuple[str, list[list[str]]]]) -> None:
    """校验每个表的每行列数都与首行（表头）一致。

    参差行几乎都是上游漏列（如 Excel 里同一项目的续行没写首列项目名），
    左对齐写入会把整行数据整体挪到错列、且不报错 —— 这类静默错位比直接失败
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
            f'推荐 content 直接传 JSON 二维数组：[["列1","列2","列3"],["值1","","值3"]]'
        )
```

**为什么这件事必须拦？**

模型生成表格时最常见的错误是"同一分组只写第一行的分组名"：

```
| 部门 | 姓名 | 电话 |
| 技术 | 张三 | 111  |
|      | 李四 | 222  |     ← 模型觉得"上面已经写了技术"，省掉第一格
```

但真实数据是 `["", "李四", "222"]` —— 左对齐写入 Excel 就变成「姓名=李四、电话=222、部门=空」。**没有报错，看起来成功了，数据全错。** 这种静默错位比崩溃危险十倍。

所以：

1. 系统提示词第 8 条明确要求"每条都写全各列"
2. `_validate_rectangular` 兜底，行列数不一致直接抛错
3. 错误信息里**附带正确写法示例**，模型下一次就会照做

> 这是 agent 工具设计的通用套路：**校验、报错、在错误里给示例。** 三步缺一不可 —— 只报错不给示例，模型会反复用同样的错法重试。

---

## 四、读方向：`docparse.extract_text`

```python
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
            f"{'docx' if suf == '.doc' else 'xlsx' if suf == '.xls' else 'pptx'} 后再读")
    raise ValueError(f"不支持的文档格式: {suf or '(无扩展名)'}")
```

**返回 `(格式标签, 文本)` 而不是只返回文本**，因为标签会进 `read_file` 的输出头：

```
[Excel 表格] D:/proj/销售.xlsx  共 42 行，显示 1-42 行
```

模型看到"Excel 表格"就知道这是结构化数据，不会当成一段散文去总结。

xlsx 的抽取要带上**工作表名 + 单元格坐标**，否则模型分不清哪一行是表头：

```python
def _sheet_text(ws) -> str:
    lines = [f"### 工作表: {ws.title}"]
    for row in ws.iter_rows(values_only=True):
        cells = ["" if c is None else str(c) for c in row]
        if any(cells):
            lines.append("\t".join(cells))
    return "\n".join(lines)
```

---

## 五、`edit_file`：为什么必须"唯一匹配"

```python
# 伪代码，核心逻辑
count = text.count(old_string)
if count == 0:
    return ToolResult("[错误] 未找到 old_string，请先用 read_file 确认原文（含缩进）")
if count > 1:
    return ToolResult(f"[错误] old_string 出现 {count} 次，请扩大上下文使其唯一")
p.write_text(text.replace(old_string, new_string, 1))
```

模型改代码时最容易犯的错是拿 `return result` 这种通用片段去替换 —— 文件里有 8 处。如果你"替换第一处"，用户会发现改错了地方，而且**没有任何报错**。

宁可失败：报错 → 模型拿到"出现 8 次" → 它下一次会带更多上下文重试。这个循环通常两轮内收敛。

对比 `write_file`：整文件重写不会错位，但会丢掉模型没读到的部分（比如文件里 500 行它只看了 100 行，重写后剩 100 行）。所以提示词第 7 条明确：**改文件优先 `edit_file`，整文件重写才用 `write_file`**。

---

## 六、`run_command`：三件事必须做

```python
subprocess.run(cmd, shell=True, capture_output=True,
               timeout=timeout, cwd=str(ctx.workspace))
```

| 必须做 | 不做会怎样 |
|---|---|
| `timeout` | 模型跑了个 `npm install` 卡住，整个 agent 挂死 |
| `cwd=workspace` | 命令在进程启动目录执行，`ls` 出来的不是用户以为的目录 |
| 编码兜底 `utf-8 → gbk` | 中文 Windows 的 `dir` 输出是 GBK，`decode("utf-8")` 直接抛异常 |

再加上输出截断（`clip_text`，保留头 60% 尾 40%）：一个 `pytest` 输出 3000 行，全塞给模型既慢又没用，但**头尾都是关键**（头部有失败列表，尾部有汇总）。

```python
def decode_text(raw: bytes) -> tuple[str, str]:
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"
```

`run_command` 也带 `confirm=True` —— 这是**最高危**的工具，`rm -rf` 和 `git push --force` 都从这儿走。

---

## 七、图片：两种完全不同的需求

### 需求 A：模型要看图

```python
MAGIC = {
    b"\x89PNG\r\n\x1a\n": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"GIF87a": ".gif", b"GIF89a": ".gif",
    b"RIFF": ".webp",       # 还要再看 [8:12] == b"WEBP"
    b"BM": ".bmp",
}

def sniff(head: bytes) -> str | None:
    for sig, ext in MAGIC.items():
        if head.startswith(sig):
            return ext
    return None
```

**为什么要校验魔数？** 因为模型会把 `.xlsx` 传给 `load_image`（用户说"看一下这个表格"，它理解成"看图"）。xlsx 是 zip，base64 塞进 `images` 之后 Ollama 会返回 400 `invalid image data`。提前拦下来，回一句：

```
[错误] 这不是图片：report.xlsx。文档请用 read_file 解析文字，
要看表格长什么样请用 render_sheet。
```

一句话把模型引到正确的工具上。

### 需求 B：模型要看"表格长什么样"

这是另一个需求：用户说"这个表排版对不对"，模型需要**视觉**而非文字。

`render_sheet` 的做法是：读 xlsx → 生成 HTML 表格 → Playwright 截图 → 把 PNG 的 base64 放进 `ToolResult.images`。

```python
# 简化流程
rows, title, cut = _load_rows(p, sheet, max_rows)
html = _build_html(rows, title, cut)          # 带斑马纹、表头加粗、列宽自适应
png = _screenshot(html)                       # playwright 单例
return ToolResult("已渲染为图片，见附图", images=[b64(png)])
```

**为什么不让模型"自己写 python 画图"？** 因为它会写出一堆 matplotlib 代码，样式随机、中文乱码、还慢。工具化之后行为稳定，而且截图可以直接贴进报告。

### 需求 C：模型要看"网页长什么样"

`web_screenshot` 是同一套截图能力的延伸：`url` 或 `html` → 视口截图 → base64 回传，复用 `browse_url` 的浏览器单例（连带继承那套 `--disable-gpu`，避免和本地模型抢显存）。

两个坑值得记下来：

- **`clip` 必须配 `full_page=True`**。只给 `clip` 时超出视口的区域会被裁掉——想截 5000px 长图，实测只回了一屏 900px 高。
- **长图会撑爆 context**。像素上把整页高度夹到 5000px，字节上超过 1.2MB 自动退化成 JPEG(q=80)。两道保险都发生在 `screenshot()` 之前，不是截完再补救。

---

## 八、剪贴板与热键：平台绑定的代价

`clipboard.py`（338 行）和 `hotkeys.py`（110 行）都是 Win32 `ctypes` 直调：

| 文件 | 干什么 | 关键 API |
|---|---|---|
| `clipboard.py` | 读剪贴板里的图片（DIB→PNG）、Ctrl+C 复制的文件列表 | `OpenClipboard` / `GetClipboardData` / `CF_DIB` |
| `hotkeys.py` | 全局 Alt+V：把剪贴板内容附到下一条消息 | `RegisterHotKey` + 消息循环线程 |

三条设计原则，任何平台相关功能都适用：

1. **失败必须可降级**：热键注册失败只打印警告，提示用 `/paste` 命令，绝不 `raise`
2. **不进主路径**：`hotkeys.start()` 在 `try/except` 里，失败也不影响对话
3. **平台判断前置**：`if os.name != "nt": return False`，别等到 ctypes 报错

> 这就是为什么仓库在 Linux/macOS 上也能跑 —— 只是没有全局热键和剪贴板图片，其它功能全在。

---

## 九、技能（skill）：不改代码就扩展行为

技能 = 一个 `SKILL.md`，带 YAML frontmatter，放在三个位置之一：

```
{workspace}/.workbuddy/skills/<name>/SKILL.md     # 项目级
~/.workbuddy/skills/<name>/SKILL.md               # 用户级
~/.workbuddy/user-*/skills/<name>/SKILL.md        # 账号级
```

```markdown
---
name: pdf-report
description: 把 PDF 报告转成结构化摘要，输出 markdown
---

# 步骤
1. 用 read_file 读 PDF（会自动抽取文本）
2. 按"结论/数据/风险"三段归纳
...
```

**核心机制是"索引常驻、正文按需"**：

```python
def skill_index(workspace: str | None) -> str:
    items = _discover(Path(workspace) if workspace else None)
    if not items:
        return ""
    lines = ["可用技能（任务命中技能主题时，先调用 skill(name=\"...\") 加载指令全文再执行）："]
    for name, it in sorted(items.items()):
        lines.append(f"- {name}: {it['desc'][:60]}…")
    return "\n".join(lines)
```

- **索引**（名字 + 60 字描述）拼进 system prompt —— 常驻，占几百 token，让模型知道"有哪些技能"
- **正文**（可能 2 万字）只有在模型主动调 `skill(name=...)` 时才加载进历史

如果 20 个技能正文全塞进 system prompt，上下文直接爆掉。

还有一处很实用的细节 —— 技能文档里的工具名映射：

```
[环境提示] 技能文档中的工具名在本环境的对应关系：
Read→read_file、Write→write_file、Edit→edit_file、Glob→glob_file、
Grep→grep_file、List→list_dir、Bash→run_command、WebSearch→web_search、
WebFetch→browse_url、Image→load_image。
Task/子代理本环境没有：改为连续调用工具逐步完成，不要编造子代理。
```

因为大部分现成技能是按 Claude 的工具名写的，加这段映射就能直接用，不用逐个改写。

---

## 十、加一个工具：六步流程

这是本章最该背下来的部分。

**① 起名**：动词_名词，全小写，和已有工具不冲突（`grep_file`、`list_dir`）

**② 写 description**（三句话）：什么时候用 / 什么时候不用 / 参数怎么填

**③ 写 parameters**：JSON Schema，中文 description，`required` 只放真的必填

**④ 写 handler**：

```python
@tool(name="...", description="...", parameters={...}, confirm=False)
def my_tool(ctx: ToolContext, a: str, b: int = 10) -> ToolResult:
    p = ctx.resolve(a)              # ① 路径一律走 resolve
    if not p.exists():
        return ToolResult(f"[错误] ...（相对路径基于工作空间 {ctx.workspace}）")
    try:
        ...
    except Exception as e:
        return ToolResult(f"[错误] ...: {type(e).__name__}: {e}")
    return ToolResult(f"[结果] {clip_text(text)}")   # ② 长文本要截断
```

**⑤ 自测**：`python -c "import tools; tools.dispatch('my_tool', {...}, ctx)"`，先不接模型

**⑥ 验证模型会不会调**：换个说法问三次，看它是否在正确场景调用、是否在不该调的时候乱调

**反面清单**（写工具时最容易犯的 5 个错）：

| 错误 | 后果 |
|---|---|
| 用 `print` 输出 | 用户看不到，模型也拿不到（WebUI 下直接丢失） |
| 返回 `None` | `ToolResult(str(None))` → 模型收到 `"None"` |
| 返回上万字符 | 吃掉上下文，模型开始丢失前文 |
| 错误信息只有 `失败` | 模型无法自我修正，会重试同样的参数 |
| 自己 `open(os.getcwd() + ...)` | 工作空间切换失效，用户以为读的是自己目录 |

---

## 十一、过关自测

1. 为什么 `write_file` 拒绝 `.xls`，而不是"尽力写一个"？
2. `_validate_rectangular` 抛错时，为什么错误信息里要带正确写法的示例？
3. 为什么 `read_file` 返回 `(格式标签, 文本)` 两个值？
4. `load_image` 校验魔数时，如果模型把 `.docx` 传进来，应该回什么错误？
5. 为什么技能只把索引放进 system prompt，正文要模型主动调？
6. `edit_file` 的 `old_string` 出现 3 次时，为什么不能"替换第一处"？

<details>
<summary>答案要点</summary>

1. 因为只能写出"扩展名是 .xls、内容是纯文本"的伪文件，Excel 打开会提示格式不符。**明确失败胜过看起来成功的坏结果。**
2. 模型不会推理"我应该怎么改"，只会模仿示例。带示例的错误信息能让它一轮改对；只报错则常常反复犯同一个错。
3. 标签进输出头，告诉模型"这是什么类型的数据"，影响它后续的处理方式（结构化数据 vs 散文）。
4. 应该明确指路："这不是图片（docx）。文档用 read_file，要看排版用 render_sheet。" —— 错误信息就是下一步指令。
5. 上下文预算。索引 20 个技能约 600 token；20 份正文可能 40 万字符，直接爆。
6. 因为无法确认该改哪一处，静默改错位置比失败更糟。报错让模型带更多上下文重试，通常两轮收敛。

</details>

---

下一章：模型侧 —— 描述和提示词怎么写才有用 → [第 9 章](09-model-side.md)
