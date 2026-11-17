# browse_url 反爬绕过：改动与验证判据

## ⚠️ 当前最终架构（2026-11-17 定稿，先看这段）

**`browse_url` 现在是"哑管道"，零语义判断。** 工具只做机械动作：打开页面 → 读正文 →
原样返回 `[网页] URL / [标题] / [identity] / 正文`。它**不判断**读回来的东西是不是
登录页、风控页、人机验证、需点击确认——任何"障碍类型"的语义判断都由**模型读正文时
自己完成**（老板定调："用大模型判断，不是 Python 写死"；"也可能是人机验证之类需要
人工点击，不一定是登录"）。

模型的工作流（**工具调用全由 agent 自己做，不甩给用户**）：读到内容与预期不符 →
自己看出是登录页/验证页/需点击 → ① agent 自己调 `browse_url(url, identity='user')`
弹出并置顶浏览器窗口、停在目标页 → ② 告诉用户"窗口已打开，请在窗口里完成登录/验证/
点击，好了告诉我"（用户只做这个无法代劳的物理动作，**不告诉用户任何工具名/参数**）→
③ 用户说好后 agent 再自己调一次 `browse_url(url, identity='user')` 读正文回答。
就是真实 session `20261117-143252-70qn.json` 里发生的流程。

> **提示词引导修复（2026-11-17）**：初版 description 写"用户弄好后用 identity=user 再读
> 一次"措辞含糊，模型（实测 qwen）理解成"让用户自己调工具"，输出"您可以使用
> identity=user 重新打开浏览器窗口…"把活甩给用户。修法：description 明确三步流程 +
> "全程由你主动调用工具、不要把工具名/参数写给用户"，并在 `agent/config.py`
> `SYSTEM_PROMPT_CORE` 第2条加通用约束"需要工具的事自己做、不要让用户调用工具/执行命令，
> 用户只做扫码/点击等物理动作"。工具零判断的架构不变，只改引导模型行为的文案。
>
> **第二轮（重启后仍犯）**：只读工具 description 不够——模型用 guest 读完、拿到登录页后，
> 推理已不在"选工具"语境，没回头翻工具说明就用一句话"请登录"结束回合（guest 是无头
> 浏览器，用户根本没有窗口可操作）。修法：加系统提示第8条 `SYSTEM_PROMPT_BROWSE_RULE`
> （每轮常驻，按 browse_url 可用性拼接），硬规定"读到需人工操作的页面，下一步必须是
> `browse_url(identity='user')` 工具调用弹窗，不要只用一句话告知用户就结束"。配套回归
> 测试 `test_prompt_drives_browse_action_not_just_telling_user`。**教训**：引导 agent 行动
> 的规则要放系统提示（每轮在），不能只放工具 description（仅选工具时看）。

**以下历史章节中，这些已被删除、勿再恢复：**
- `_BLOCK_SIGNALS` / `_detect_block()`（风控特征表）
- `_detect_login_wall()`（登录墙特征表）—— 曾加又被推翻，误伤正常短文本且追不上新站点
- `_page_changed()`（标题/长度变化判断）
- `_wait_login_loop()` / `wait_for_login` 工具（轮询等待）

保留的机械能力：Playwright 常驻 worker 线程、`identity=guest/user/auto`（只选浏览器、
不判断内容）、CDP 会话复用与窗口置顶、正文不可见空格归一化、`_read_page` 导航重试。
这些是"怎么读"，不是"读到的是什么"。

---

> 以下是历史演进记录（含上述已废弃方案），留档说明为什么走到最终架构。

> 状态：**已实测通过**（2026-11-17，沙箱恢复后）。第 0/2/4 档均跑过，命令与真实输出见下。
> 中途故障：WorkBuddy 命令沙箱曾报 `sandbox creation failed 0xFFFFF826`（含绕过开关在内），
> 恢复后完成全部验证。

## 问题

`https://zhuanlan.zhihu.com/p/123211148` 被知乎风控拦截，返回 **HTTP 200 + 错误 JSON**
（`code: 40362`，"您当前请求存在异常，暂时限制本次访问"）。

因为 HTTP 200，`page.goto()` 不抛异常；旧 `browse_url` 直接取 `body.innerText`，
于是把这段错误 JSON **当成正文交给了模型**。这是根因，不是"网络不通"。

## 改动（`tools/browse_url.py`）

1. **拦截检测** `_detect_block()`：短正文（≤1200 字符）命中已知风控特征（40362、
   请求存在异常、访问受限、Cloudflare 验证页等）就返回 `[已拦截]` + 已验证片段，
   明确报"卡在哪"，**不把错误 JSON 伪装成正文**，也不编内容。长文命中关键词不误伤。
2. **`identity` 参数**（`auto` 默认 / `guest` / `user`）——**全自动，无需人执行命令**：
   - `auto`（默认）：先用 guest 无登录浏览器读；若被拦截，**agent 自动启动带调试端口
     的浏览器 + 连 CDP + 重试**，全程不用人执行任何命令。若会话未登录，返回
     `[需要登录]`——窗口 agent 已开好并停在目标页，人只需扫码（唯一 human-only）。
     登录态存在固定持久化 profile，登一次长期复用。
   - `guest`：只用无登录 headless 浏览器（明确要匿名时用）。
   - `user`：直接用浏览器会话（跳过 guest 探测）。
   - `_auto_start_cdp_browser()`：探活 9222 → 没有就找本机 Chrome/Edge → 以固定独立
     profile（`OLLAMA_AGENT_CDP_PROFILE`，默认 `%LOCALAPPDATA%\ollama-agent\cdp-profile`）
     + DETACHED 启动 → 轮询等就绪。端点可用 `OLLAMA_AGENT_CDP` 覆盖。
   - 只用 `disconnect` 关 CDP 连接，绝不 `close()` 用户的浏览器窗口。
3. **单一 Playwright driver**：sync API 一个进程只能有一个 event loop（实测 auto 同轮
   guest→CDP 会抛 "using Sync API inside asyncio loop"）。全模块共享一个
   `sync_playwright()` 实例，guest 的 `launch` 与 CDP 的 `connect_over_cdp` 都从它出。
   `pw_close()` 断开 CDP 只 disconnect、最后统一 stop 这个 driver。
4. **`_read_page()` 导航重试**：实测知乎在首屏 500ms 时 JS 导航未稳，`page.evaluate`
   抛 `Execution context was destroyed`（旧代码在这里直接报"打开失败"，拦截检测
   根本没机会跑）。改为遇导航类异常等 1s 重试、最多 3 次；非导航类异常立即抛出。
5. **`_normalize_text()` 不可见空格归一化**：网页 HTML 的 `&nbsp;` 经 `innerText`
   解码成 U+00A0（实测 CSDN 那篇 spill 有 89 个），编辑器显形成 `·`、复制粘贴易乱。
   把 16 种 Unicode 空格类字符折叠成普通空格、ZWSP/BOM 直接删；**不用 NFKC**
   （那会把全角字母/上标数字一并改掉，误伤正文）。实测真实页面残留为零。

## 验证判据（沙箱恢复后执行）

### 第 0 档 · 语法

```bash
python -c "import ast,pathlib; [ast.parse(p.read_text(encoding='utf-8')) for p in [pathlib.Path('tools/browse_url.py'), pathlib.Path('tests/test_tools.py')]]; print('syntax OK')"
```

### 第 2 档 · 纯逻辑（无网络/无浏览器）

```bash
python tests/test_tools.py
```

期望：出现 `PASS: browse_url 拦截检测 ... _read_page 导航重试 + 不可见空格归一化`，末行 PASS，退出码 0。
覆盖：40362 JSON、通用拦截、Cloudflare、长文不误伤、正常短文放行、schema 含 identity、
非法值被拒、`_read_page` 导航销毁重试 + 非导航异常不重试、不可见空格归一化（含"不用 NFKC
误伤上标/全角"的反例断言）。**实测退出码 0。**

### 第 4 档 · 端到端（**auto 全自动实测通过**，2026-11-17）

**只需一条命令，人不用执行任何东西**：

```bash
python -c "import sys;sys.path.insert(0,'.');import tools;from agent.registry import ToolContext,dispatch;print(dispatch('browse_url',{'url':'https://zhuanlan.zhihu.com/p/123211148'},ToolContext()).text[:700])"
```

默认 `identity=auto` 的实测链路与输出：
1. guest 无登录浏览器先读 → 被 40362 拦截；
2. agent 自动找到本机 Edge 157、以固定独立 profile + DETACHED 启动带 9222 的浏览器、
   轮询等 CDP 就绪（实测 0.5s）、connect_over_cdp；
3. 返回 `[网页] ... [identity] cdp` + 文章正文，标题
   **《史上最详细循环神经网络讲解（RNN/LSTM/GRU）》**。

**关键修复**：auto 同轮先 guest 后 CDP，最初报
`It looks like you are using Playwright Sync API inside the asyncio loop`——
根因是 sync API 一个进程只能有一个 event loop，两次 `start()` 撞车。改为全模块共享
一个 `sync_playwright()` 实例后解决（官方文档：同一个 `playwright.chromium` 既能
`launch` 也能 `connect_over_cdp`）。

**历史踩坑（已被自动化绕开，留档）**：早期尝试让人手动用**日常 Edge profile** 带 9222
启动，两次失败——startup-boost 秒级拉起无参数后台进程吞掉启动参数，叠加 Edge 136+
对默认 profile 调试的限制。**现在 agent 自己用独立固定 profile，这些问题都不存在了**，
PowerShell 引号坑（`--` 被当自减运算符）也不再需要人面对。

**通过标准**：返回 `[网页]` 开头且含正文；若返回 `[需要登录]`，说明该站必须登录——
窗口已由 agent 开好并停在目标页，人只需扫码一次（唯一 human-only），之后 profile
持久化会话自动复用。绝不换渠道抓摘要冒充正文。

## 已知风险

- 自动接管依赖本机装有 Chrome/Edge；已用独立固定 profile 绕开 Edge 136+ 默认 profile
  的调试限制。若企业策略彻底禁用调试端口，`_auto_start_cdp_browser` 会在 15s 超时后报错。
- 登录态会过期；`z_c0` 消失时返回 `[需要登录]`，人扫码一次即可（窗口已自动开好）。
- 知乎风控策略会变，`_BLOCK_SIGNALS` 需按实测补充。
- 全部档位（0/2/4 auto 全自动）已于 2026-11-17 实测通过，输出见上。

## 修复：WebUI 第二轮起报 `TargetClosedError: Playwright connection closed`

> 状态：已修复并实测通过（2026-11-17）。

### 症状

WebUI 里第一轮 `browse_url` 正常，**从第二轮开始必然失败**，且是 **0 秒**报错：

```
[错误] 打开失败 https://qwenpaw.agentscope.io/: TargetClosedError: Playwright connection closed
```

### 根因（两层，缺一不可）

1. **Playwright sync 对象绑定创建它的线程。** 换线程使用直接抛
   `Cannot switch to a different thread`（实测：worker 线程建的 page，主线程调
   `goto` 立刻报这个）。
2. **它所在线程一退出，底层连接就断了**，但模块级 `_DRIVER` 字典里还留着这个
   死对象。旧代码没做存活检查，于是下一次调用拿到死连接 → 0 秒抛
   `TargetClosedError`。

WebUI 用 `ThreadingHTTPServer`（**每请求一个线程**），而 agent 循环就在请求线程里
同步跑。所以 driver 绑在「第一个请求的线程」上，那个请求一结束线程就没了，第二轮
对话必然踩到这个坑。CLI 是单线程常驻，所以只在 WebUI 复现。

### 修复（`tools/browse_url.py`）

**所有 Playwright 操作固定在一个常驻专用线程里执行**，页面/浏览器对象绝不跨线程传递：

- `run_on_pw(fn)`：唯一入口。把闭包投递进常驻 worker（`playwright-worker`）执行，
  异常原样透传，超时抛 `TimeoutError`（worker 卡死宁可报错，也不让对话挂住）。
  已在 worker 内部（嵌套调用）就直接执行，避免自己等自己死锁。
- `run_with_page(fn, via_cdp=)`：给 `web_search` / `web_screenshot` 用的门面——
  在 worker 里开新页 → 交给 `fn(page)` → 自动关页 → 返回值带回。
  **关键约定：`page` 绝不能逃出 `fn`**，拿回调用线程再用必崩。
- 删掉 `pw_new_page()` / `_pw_page()` 这类「返回 page 给外部」的接口——它们天生
  诱导跨线程使用。
- `_is_stale()` + `run_with_page` 内置重建：命中连接已死类错误（connection closed /
  `Cannot switch to a different thread` / browser has been closed）自动重建 driver
  并**重试一次**，而不是把这句底层异常甩给模型当「打不开网页」。
- `pw_close()` 改为投递到 worker 执行 `_reset_pw()`，任意线程可调。

配套改动：`web_screenshot.py`（截图流程整体移入闭包）、`web_search.py`
（百度渲染整体移入闭包）。

### 验证判据

- **第 2 档（纯逻辑，已加进 `tests/test_tools.py`）**：多线程提交 +
  主线程提交，执行线程必须始终是同一个 worker 且不等于调用方线程；worker 内异常
  必须原样透传；`_is_stale` 要认出连接类错误、且不能把 DNS 失败误判为失效连接。
- **第 4 档（端到端）**：模拟 WebUI「每请求一线程」，连开 3 个独立请求线程跑
  `identity=auto`，三轮都要返回 `[网页]`。修复前第 2 轮必挂，修复后三轮全通。
- 混合调用（同进程内 `browse_url` + `web_screenshot` 交替、各两轮）全部成功。

## 修复：`identity=user` 报"已连接但浏览器没有任何上下文"（0.03s 秒失败）

> 状态：已修复并实测通过（2026-11-17）。原始调用：
> `browse_url({"url":"https://www.baidu.com","identity":"user"})` →
> `[错误] agent 自动启动浏览器会话失败: 已连接但浏览器没有任何上下文`（0.03s，只留空白页签）。

### 根因

`_cdp_page()` 里 `connect_over_cdp` 成功后直接读 `browser.contexts`，为空就立刻
`raise RuntimeError("已连接但浏览器没有任何上下文")`，页面根本没走到 `goto()`。
但 **CDP 端点活着 ≠ 有可用 context**：实测 `/json/version` 返回 200（Edge 157），
`/json/list` 却可能只有 `about:blank` 甚至 0 个 page target（用户关掉最后一个窗口
只剩后台进程、或 target 还没注册完）。此时 0.03s 抛错，用户只看到一个空白页签，
自然"没有打开链接"。

附带两个隐藏问题：
- Playwright Python 的 `Browser` **没有 `disconnect()` 方法**（实测 `hasattr` 为 False），
  原代码 `(getattr(...,"disconnect") or lambda:None)()` 是空操作；`close()` 又会真关
  用户窗口，所以旧连接从未被安全释放。
- `is_connected()` 偶有假阳性：实测关掉所有 page 后 Edge 进程已退出
  （`/json/version` 不再响应），缓存的 browser 对象 `is_connected()` 仍返回 True。
  所以不能只靠它判活，拿到连接后还得处理"连着却拿不到 context"。

### 改动（`tools/browse_url.py`）

1. `_cdp_browser_alive()`：带异常兜底的活体判据（None / `is_connected()` 抛错 / 返回
   False 一律判死），`_cdp_page()` 据此在连接失效或端点变化时重建。
2. `_pick_cdp_context()`：把"取 context"从一锤子买卖改成三段式 ——
   ① 取现有 `contexts`；② 为空则轮询 `_CDP_CTX_WAIT_TRIES`(10)×
   `_CDP_CTX_WAIT_INTERVAL`(0.3s) 等 target 注册；③ 仍为空则 `new_context()` 兜底。
   CDP 连接下 `new_context()` 建的是隔离会话，**不碰用户默认 context / 登录态**。
   页面因此一定能走到 `goto()`，不再空抛"没有任何上下文"。
3. `_disconnect_cdp()`：仅对确实存在 `disconnect` 的实现做兼容调用，否则不动作
   （真正的连接回收靠 `_reset_pw()` 停 driver，绝不对用户窗口调 `close()`）。

### 验证判据

- **第 0 档（语法）**：`ast.parse` 解析 `tools/browse_url.py` 与 `tests/test_tools.py` 通过。
- **第 2 档（纯逻辑，已加进 `tests/test_tools.py`）**：mock browser 覆盖三条路径 ——
  contexts 前两次空第三次出现（轮询等待，不得提前 new_context）、contexts 恒空
  （new_context 兜底且只建一次）、失活连接判据（`_cdp_browser_alive` 对 None/False/
  抛错/True 四种输入的判定）。全档 PASS，退出码 0。
- **第 4 档（端到端，真实浏览器）**：`dispatch('browse_url',
  {'url':'https://www.baidu.com','identity':'user'}, ToolContext())` 返回
  `[网页] https://www.baidu.com / [标题] 百度一下，你就知道` + 正文；
  连续两轮均 OK。修复前同一环境是 0.03s 报"没有任何上下文"。

### 已知风险 / 权衡

- `new_context()` 兜底创建的是**隔离会话，不带用户已登录的 cookie**；仅当
  `browser.contexts` 真的全空时才触发（正常登录态下 `contexts[0]` 直接复用）。
  若某站必须登录且恰好走到兜底，会返回 `[需要登录]`，用户在弹出的窗口扫码即可。
- `_CDP_CTX_WAIT_TRIES × INTERVAL` 目前是 10×0.3s=3s 上限；target 注册极慢的场景
  仍可能进 new_context 兜底，但不会再秒抛错误。

## 修复：`identity=user` 跳到目标页又秒退、只剩一个 about:blank

> 状态：已修复并实测通过（2026-11-17）。接上一节"没有任何上下文"修复之后的反馈：
> 页面能打开了，但 `browse_url` 一返回目标页就被关掉，浏览器里只剩 agent 启动时
> 自带的 about:blank 空标签页 —— 用户看到"跳过去又退出来"。

### 根因

`run_with_page()` 的 `job()` 里 `finally: page.close()` 无条件关页。这个行为对
guest（web_search / web_screenshot）是对的（防标签页泄漏），但对 `identity=user`
是错的：user 的语义是"接管会话、停在当前页给人看/扫码"，页面必须留在浏览器里。
实测 `dispatch(identity=user)` 前后 `/json/list`：调用后 `AFTER: ['about:blank']`，
目标页确实被 close 掉了。

### 改动（`tools/browse_url.py`）

1. `_CDP` 增 `page` 字段做**常驻页**单例；新增 `_cdp_sticky_page()`：
   复用同一页 navigate，优先复用浏览器里已有的 about:blank 占位页（不再 new_page），
   页被外部关掉才重建。这样既留住目标页，又不产生空标签页、不累积标签页。
2. `run_with_page()` 增 `keep_page` 开关：默认 False（guest 依旧读完即关，行为不变）；
   True 时不关页。开页逻辑按 (via_cdp, keep_page) 三选一：常驻页 / CDP 新页 / guest 新页。
3. `_fetch()` 的 CDP 路径改传 `keep_page=via_cdp`，仍走 `run_with_page` 以保留
   `_is_stale` 重建重试；guest 路径零改动。
4. `_reset_pw()` 清 `_CDP["page"]`，并把旧的 `disconnect()` 空操作换成 `_disconnect_cdp()`。
   顺带修 `_cdp_sticky_page` 复用循环跳过已 close 的页（否则可能复用死页）。

### 验证判据

- **第 0/2 档**：语法 OK；`tests/test_tools.py` 新增 `_cdp_sticky_page` 回归（复用
  about:blank 不新建、二次调用复用同一页不累积、常驻页不 close、死页重建），全 PASS。
- **第 4 档（真实浏览器，干净启动）**：连跑两轮 `identity=user` 访问百度，
  每轮结果 OK，且 `/json/list` 每轮都只有 `['https://www.baidu.com/']` ——
  目标页留住、无 about:blank、无累积。修复前是 `['about:blank']`。
- **回归**：guest 模式 `browse_url(identity=guest)` 访问 example.com 正常返回正文，
  关页行为未受影响。

### 已知风险 / 权衡

- 常驻页当前复用浏览器里第一个 about:blank；若用户的 CDP 浏览器本就有多个 lab
  页且第一个 about:blank 是用户自己的，会被借用作 agent 页（navigate 后原空白丢失）。
  agent 用的是独立固定 profile，正常不与用户日常浏览器混用，影响可控。

## 新增：`wait_for_login` —— 扫码登录的自动等待闭环（方案 A）

> 状态：已实现并实测通过（2026-11-17）。

### 问题

旧流程 `browse_url` 返回 `[需要登录]` 后就"放手"：用户扫码后必须**回来再发一条指令**
才能重读。agent 不会自动等、页面窗口可能被挡住找不到。

### 改动

1. **`wait_for_login(url, timeout=120, interval=3)`**（新增工具，注册在
   `tools/browse_url.py`，归 `toolpolicy` 的 web 组）：
   - `browse_url` 报 `[需要登录]` 后用这个。CDP 常驻页 `bring_to_front()` 置顶，
     每 interval 秒重新 `goto` 目标 URL + `_read_page` + `_detect_block`，
     直到正文可读（登录成功）→ 返回 `[网页] ... [identity] user（登录后）`；
     超时 → `[等待登录超时]`（不编造正文）；`ctx.stopped()` → `[已取消]`。
   - 登录中转页导航中（`_read_page` 抛异常）等下一轮，不算失败。
   - 参数夹取：timeout 1~600s、interval 0.5~30s 且 < timeout；非法值早拒。
   - 外层 `run_on_pw(timeout=timeout+60)` 必须大于内部等待，避免外层先超时。
2. **`_bring_to_front()`**：`_cdp_sticky_page` 返回页前尝试 `bring_to_front()`，
   把登录窗口提到前面（失败不影响读取）→ 解决"窗口被挡住找不到"。
3. 配套：`tests/test_tools.py` 的 expected 集合加 `wait_for_login`；
   "模块数==注册数"断言改为 `>=`（一个模块可注册多个工具）。
   `agent/toolpolicy.py` 的 web 组加 `wait_for_login`（CHAT_TOOLS 保持 4 个不变）。

### 验证判据

- **第 0/2 档**：语法 OK；`tests/test_tools.py` 加 `_wait_login_loop` 纯逻辑回归
  （注入假时钟/停止信号）：第 3 轮登录成功返回 ok + 正文、始终被拦返回 timeout、
  取消立即返回且不导航；参数校验（ftp/非数字）早拒。全 PASS，注册 21 个工具。
  `tests/test_tool_policy.py` 36 passed。
- **第 4 档（真实浏览器，本地 mock）**：mock 页面先返回"请登录"，3s 后服务端
  会话切换为已登录（模拟扫码生效），`wait_for_login(timeout=30, interval=1)`
  在 5.1s 时自动读到正文并返回 `[网页] ... [identity] user（登录后）`；
  不切换会话则 4s 后返回 `[等待登录超时]` 且命中拦截特征，不编造。

### 已知风险 / 权衡

- **轮询占用唯一 worker 线程**：`wait_for_login` 全程占着 playwright worker，
  期间其他浏览器操作（web_search/screenshot）会排队，直到登录成功或超时。真实
  用户扫码在浏览器窗口手动完成、由浏览器写 cookie，不走 worker，故正常链路无碍；
  但用户迟迟不扫时，建议给合理 timeout（默认 120s），WebUI 取消走 `ctx.stopped()`。
- 登录态持久化依赖固定 profile 目录（`%LOCALAPPDATA%\ollama-agent\cdp-profile`）
  不被清理；实测 `Default\Network\Cookies` 已落盘。
- `bring_to_front` 对无头/远程桌面可能无效（best-effort，不影响读取）。

## 修正：`wait_for_login` 的登录墙判定 —— 从特征表改为 agent 判断

> 状态：已修正并实测通过（2026-11-17）。这是设计决策修正，留档防止回退。

### 问题（真实 session 复现）

用户以 `https://malltest.zhilusoft.com/currency/points/summary` 测试，反馈"没等我
登录，loop 就不停地调工具"。查 `sessions/20261117-143252-70qn.json`：
`browse_url(auto)` 和 `wait_for_login` 三次都秒返回 `[网页] ... [identity] guest /
user（登录后） / user`，正文都是 `Welcome to / 山海生活运营管理系统 / 登录`——
**登录壳页被当成正文了**。用户后来说"登录好了"再读，才拿到真正的
`[标题] 额度汇总 - 山海生活` + 1703 字正文。

根因：未登录的 SPA 登录壳页 HTTP 200、正文仅约 20 字、`_BLOCK_SIGNALS` 风控
特征表认不出 → `_wait_login_loop` 旧逻辑 `if not hit and text: return ok` 第一轮
就误判"已登录"，秒退，用户根本没来得及扫码。

### 一次被推翻的尝试（重要教训）

最初加了 `_detect_login_wall()`：用 "welcome to"/"请登录"/"登录后"… 特征词 +
短正文阈值判登录墙。写完立刻暴露两个硬伤：
1. **误伤**：测试样本"登录后的正文内容"这种正常短文本被判成登录墙（"登录后"
   命中）—— 特征表越宽越容易误伤，越窄越漏判。
2. **追不上新站点**：zhilusoft 这个样本旧特征表根本没覆盖，每个新站点都要补表，
   是打地鼠。

遂**整段删除**特征表方案。

### 最终设计：工具报客观信号，语义交回 agent

分工（老板定调："咱们是 Agent，直接用 agent 本身判断是否需要登录"）：
- **工具只做机器能可靠判断的事实**：`_page_changed(before, after)` 比较
  `(title, text, url)` 三元组 —— 标题变了 / URL 变了 / 正文显著变长（≥1.5倍且
  多 ≥50 字）就算"页面变了"。不猜"是不是登录页"。
- **"读回的到底是正文还是登录页、够不够回答用户"由 agent 判断**：本就是模型
  读 ToolResult 的强项（session 里模型在未登录时就正确判断要登录）。
- `wait_for_login` 三种状态 `changed/timeout/cancelled` **都带回最后读到的
  title/text + 明确提示"，请自行判断是否为正文；若仍是登录页可再等"**，绝不
  替 agent 下结论、不编造。首轮只建基线不返回（调用前提是 browse_url 已读到
  疑似登录页）。
- `_detect_block()` 风控拦截保留 —— 40362/Cloudflare 是机器可读的明确拒绝，
  不是语义猜测；命中继续等。

### 验证判据

- **第 0/2 档**：语法 OK；`tests/test_tools.py` 加 `_page_changed` 断言（真实样本
  登录壳→内容页判变化、页面没变不误报、URL 跳转判变化、无基线不判变化），
  `_wait_login_loop` 改为快照变化驱动（首轮基线→标题变化返回 changed、一直不变
  走到 timeout、取消不导航）。全 PASS。
- **第 4 档（本地 mock，真实 SPA 登录壳样本）**：未登录页标题"山海生活"、正文
  `Welcome to/…/登录`（24字），`wait_for_login(timeout=15,interval=1)` 前几轮读到
  登录壳**不返回**（不再秒退），4s 后模拟登录生效、标题变"额度汇总 - 山海生活"、
  正文变 999 字 → 6.1s 返回 `[页面已变化] ...` 带新内容；连跑两次 4.6/6.1s 稳定。
- 全量：`test_tools.py` 退出码 0、`test_tool_policy.py` 36 passed。

### 已知风险 / 权衡

- 语义判断交回 agent 意味着：agent 必须真的去看 wait_for_login 返回的内容，
  不能见 `[页面已变化]` 就当成功。返回文案已强提示这一点。
- 页面"变化"是客观信号但非充分条件：若登录后标题/正文恰好不变（罕见），
  会等到 timeout —— 此时超时文案也会带回最后内容供 agent 判断，不至于误报成功。
- 跨进程 CDP 会累积标签页（每个新进程 `_CDP` 为空→找不到 about:blank→new_page）；
  真实 agent 单进程常驻不会发生，本次未处理。
