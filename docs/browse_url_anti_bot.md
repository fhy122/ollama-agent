# browse_url 反爬绕过：改动与验证判据

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
