# 第 11 章 · 和真实仓库对账

**本章产出**：一张差异表、一份排错手册、一份下一步清单。到这里你可以放心改那份 7000 行的代码了。

---

## 一、你写的东西 vs 仓库里的东西

| 你的文件 | 仓库对应 | 仓库多了什么 | 重要度 |
|---|---|---|---|
| `agent/config.py` | 同名 | 系统提示词扩到 9 条（含 xlsx / 图片 / 删除的专门约束） | ★★★ |
| `agent/client.py` | 同名 | 图片无效 → 剥离重试的分支（`_payload_has_images` / `_strip_images`） | ★★ |
| `agent/registry.py` | 同名 | 无差别（这份几乎逐行一致） | — |
| `agent/loop.py` | 同名 | `new_history` 里拼技能索引（`skill_index`） | ★★ |
| `agent/events.py` | 同名 | 无差别 | — |
| `agent/console.py` | 同名 | 无差别 | — |
| `agent/cli.py` | 同名 | `@` 面板接线、`/paste`、`/skill` 快捷键、palette 集成 | ★ |
| `tools/read_file.py` | 同名 | Office 分支（`docparse.extract_text`） | ★★★ |
| `tools/list_dir.py` | 同名 | 大小格式化、隐藏项统计 | ★ |
| — | `agent/palette.py` | 445 行行内面板（raw console + ANSI 重绘） | 可选 |
| — | `agent/docgen.py` | 399 行：xlsx / docx 生成 + 矩形校验 | ★★★ |
| — | `agent/docparse.py` | 150 行：docx / xlsx / pptx 读取 | ★★★ |
| — | `agent/clipboard.py` | 338 行 Win32 剪贴板 | 可选 |
| — | `agent/hotkeys.py` | 110 行全局热键 | 可选 |
| — | `tools/` 其余 12 个 | `edit_file` `delete_file` `run_command` `grep_file` `glob_file` `load_image` `clipboard_image` `web_search` `browse_url` `web_chat` `render_sheet` `skill` | ★★ |
| — | `webui/static/` | 712 行 `app.js`：工具卡片、`@` 面板、拖拽上传、设置面板、markdown 渲染 | ★ |
| — | `tests/` | 6 个脚本，约 1800 行 | ★★★ |

**先补哪三个？** 按"能立刻提升可用性"排序：

1. `docgen` + `docparse`（不然模型写不出能打开的 Excel，这是最常被要求的能力）
2. `edit_file` + `grep_file` + `glob_file`（不然改代码只能整文件重写，会丢内容）
3. 系统提示词扩到 9 条（不然模型会拿 `load_image` 去开 xlsx）

---

## 二、三个最值得回读的函数

代码你都写过了，但仓库里这三处的处理比教程更细，值得逐行读一遍：

### ① `client.chat` 里的 `_raw` 合并

```python
args = fn.get("arguments", {})
merged = tool_calls[i]["function"].setdefault("arguments", {})
if isinstance(args, dict):
    merged.update(args)
elif isinstance(args, str) and args.strip():
    merged["_raw"] = merged.get("_raw", "") + args
```

同一个模型，可能这次给你 `{"path": "a.txt"}`，下次给你 `"{\"path\": \"a"` + `".txt\"}"` 两片。**两种都要活。**

### ② `loop.pad_unanswered_tool_calls`

```python
idx = len(history) - 1
answered = 0
while idx >= 0 and history[idx].get("role") == "tool":
    answered += 1
    idx -= 1
```

从后往前数"已经有几条 tool 响应"，再和 assistant 声明的 `tool_calls` 数量对账。**为什么从后往前？** 因为被打断时缺的一定是**末尾**那几条，前面的一定是齐的。

### ③ `webui/session.py` 的 `ConfirmGate.answer`

```python
def answer(self, cid, approved, allow_rest=False) -> bool:
    with self._lock:                     # ← gate 自己的小锁，不是 session.lock
        entry = self._pending.get(cid)
        if entry is None:
            return False                 # 已超时/已应答 → 返回 False 而不是抛
        ...
```

三个细节都值得抄：**不取会话锁**（防死锁）、**id 不存在返回 False**（幂等）、**`allow_rest` 只在本回合有效**（`reset()` 会清掉）。

---

## 三、400 排错手册

Ollama 的 400 里 90% 是这五种，每种都能从消息文本直接定位：

| 报错关键词 | 真实原因 | 修法 |
|---|---|---|
| `tool_calls` / `tool message` / `must be followed by` | assistant 声明了工具但缺 `tool` 消息 | 检查所有打断路径是否都补了占位；`pad_unanswered_tool_calls` |
| `invalid tool call` / `arguments` | `arguments` 是字符串，或不是合法 JSON | `client` 的 `_raw` 解析；循环里的 `isinstance(args, dict)` |
| `does not support tools` | 模型不支持工具调用（小模型常见） | `--no-tools`，或换模型 |
| `invalid image` / `Failed to load image` | 把 xlsx/docx 当图片塞进 `images` | `load_image` 加魔数校验；`client` 的剥离重试 |
| `model requires more system memory` / `out of memory` | 上下文或模型太大 | 降 `num_ctx`、换小模型、`keep_alive` 调短 |

再补两个不是 400 但更常见的：

| 现象 | 原因 | 修法 |
|---|---|---|
| 模型回答里出现"根据文件内容…"但没调工具 | 幻觉 | 提示词第 3 条；或在 `read_file` 返回里强调"这是真实内容" |
| 回答突然变短、忘记前文 | 上下文满了，旧消息被静默截断 | `stats` 里看 `prompt_eval_count`；做历史压缩（下面 P0） |

---

## 四、下一步：按投入产出排序

### P0（一天内能做，收益明显）

**① 历史压缩**

现在 `history` 只增不减，20 轮之后每轮都要重发几万 token。最小可行方案：

```python
def compact_if_needed(history, client, limit=40):
    """消息超过 limit 条时，把中间部分摘要成一条。"""
    if len(history) <= limit:
        return
    head, middle, tail = history[:2], history[2:-6], history[-6:]
    summary = client.chat([{"role": "user", "content":
        "把下面的对话压缩成 200 字要点，保留文件路径和结论：\n" + render(middle)}])
    history[:] = head + [{"role": "user",
                          "content": f"[早期对话摘要]\n{summary['content']}"}] + tail
```

**注意**：只能压缩到 `assistant` 边界，绝不能把 `tool_calls` 和它的 `tool` 响应拆开（不变量 1）。

**② 工具增量容错**

现在 `tool_calls` 按**下标**合并。如果模型分片发的顺序不是 0,1,2 而是 0,1,0…，会错位。加个 `index` 字段兜底：

```python
idx = tc.get("index", i)      # 有 index 用 index，没有才用位置
```

### P1（一周，工程化）

| 事项 | 为什么 |
|---|---|
| 工具沙箱（限制在 workspace 内） | 现在 `ctx.resolve("../..")` 能跑出去，`run_command` 更危险 |
| WebUI 多会话 | 现在两个标签页排队；按 session id 分桶即可 |
| 工具调用的"预演模式" | `--dry-run`：只打印要调什么，不执行 |
| 结构化日志 | 每次工具调用写一行 JSON，便于复盘 |
| pytest 化 | 想在 CI 跑就得有 `exit code` 和并行 |

### P2（想做再做）

- 流式 Markdown 渲染（现在 `app.js` 是纯文本追加）
- 多模型对比（同一问题并发问两个模型）
- 会话持久化到 SQLite（现在只有 `/save` 手动存）
- 文件变更监听（工作空间里的文件被改了，自动提示）

---

## 五、你要改代码时的三步

```bash
# 1. 先看它属于哪一层（第 0 章的判据表）
#    改的是"怎么做" → tools/    改的是"什么时候做" → loop.py
#    改的是"怎么显示" → events/  改的是"怎么连模型" → client.py

# 2. 找同类实现照抄结构
grep -n "@tool" tools/*.py | wc -l        # 看看别人怎么写的

# 3. 改完跑这三条
python tests/test_tools.py                # 工具没坏
python tests/test_webui.py                # 前端协议没坏
python -m agent "读一下 README 前 10 行"    # 端到端
```

---

## 六、毕业验收清单

能全部答"是"，就可以在这份代码上做二次开发了：

- [ ] 能画出一张消息序列图：user → assistant(tool_calls) → tool ×N → assistant
- [ ] 能说出 4 个"必须保持历史自洽"的出口，以及各自怎么收尾
- [ ] 能解释 `think` 三态，以及为什么 `auto` 是 `None` 而不是 `False`
- [ ] 能解释 `arguments` 的两种形态和 `_raw` 的存在理由
- [ ] 能不看代码写出一个工具（含 description 的三要素）
- [ ] 能说出为什么 `answer_confirm` 不能取会话锁
- [ ] 能说出"改一个工具要动几个文件"，以及为什么
- [ ] 能用桩 client 给 `run_turn` 写一个离线测试
- [ ] 遇到 400 能按第三节的表定位

---

## 七、最后一句

这套代码里最值钱的不是那 15 个工具，是四个决策：

1. **循环自己写** —— 所以每一步都能观测、能打断
2. **工具自注册** —— 所以扩展是"加文件"而不是"改代码"
3. **状态显式传参** —— 所以同一个内核能同时服务 CLI 和 WebUI
4. **输出走事件接口** —— 所以加前端不改内核

工具会过时（模型升级、API 变了），这四个决策不会。

---

回到 [教程目录](README.md) ｜ 回看 [源码阅读指南](../CODE_READING.md)
