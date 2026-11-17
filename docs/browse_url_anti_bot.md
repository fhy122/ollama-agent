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
2. **`identity` 参数**（`guest` 默认 / `user`）：
   - `guest`：原行为，独立无登录 headless 浏览器，向后兼容；
   - `user`：通过 CDP（`connect_over_cdp`，默认 `http://127.0.0.1:9222`）接管**本机
     已登录的 Chrome**，用真实登录态过风控。用完只 `disconnect` 新标签页，绝不
     `close()` 用户的浏览器。
   - CDP 端点可用环境变量 `OLLAMA_AGENT_CDP` 覆盖。
3. `pw_close()` 增加 CDP 连接清理（同样只断开、不杀进程）。
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

### 第 4 档 · 端到端（guest 路径已实测，2026-11-17）

```bash
python -c "import sys;sys.path.insert(0,'.');import tools;from agent.registry import ToolContext,dispatch;print(dispatch('browse_url',{'url':'https://zhuanlan.zhihu.com/p/123211148'},ToolContext()).text[:700])"
```

真实输出（实测）：返回 `[已拦截] ... [拦截特征] 40362` + 已验证片段（知乎错误 JSON），
**不再把错误 JSON 冒充正文**。

### 第 4 档 · user 路径（**端到端实测通过**，2026-11-17）

本机实测环境：**Edge 157**（本机无 Chrome），Chromium 内核，CDP 完全兼容。

**踩坑（默认 profile 走不通）**：用日常 Edge 的 `--user-data-dir="...\Edge\User Data"`
带 9222 启动两次均失败——`taskkill` 后 Edge 的 startup-boost 会秒级拉起一个
`--no-startup-window` 后台进程接管启动请求，参数被吞，9222 始终"积极拒绝"。
叠加 Edge 136+ 对默认 profile 远程调试的安全限制。**结论：别在默认 profile 上耗。**

**可行做法（独立 profile）**：在 **CMD**（不是 PowerShell，避免 `--` 被解析成自减运算符）里：

```cmd
"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" --remote-debugging-port=9222 --user-data-dir="C:\Users\admin\AppData\Local\Temp\agent-edge-cdp" --no-first-run --no-default-browser-check https://www.zhihu.com
```

不用先 taskkill（不同 profile 互不冲突）。在弹出窗口登录知乎，然后：

```bash
python -c "import sys;sys.path.insert(0,'.');import tools;from agent.registry import ToolContext,dispatch;print(dispatch('browse_url',{'url':'https://zhuanlan.zhihu.com/p/123211148','identity':'user'},ToolContext()).text[:500])"
```

**实测结果（全通过）**：
- `http://127.0.0.1:9222/json/version` → `Edg/157.0.4341.60`；
- CDP session cookie `z_c0`（domain `.zhihu.com`，长度 187）存在 → 登录态确凿；
- `identity=user` 返回 `[网页] ... [identity] user` + 文章正文，标题
  **《史上最详细循环神经网络讲解（RNN/LSTM/GRU）》**，40362 被真实登录态绕过。

**通过标准**：返回 `[网页]` 开头且含文章正文。若仍返回 `[已拦截]`，说明知乎对该
profile/IP 也有风控，此时唯一诚实路径是你在浏览器读完把正文贴过来——不要让 agent
换渠道抓个摘要冒充正文。

## 已知风险

- `identity=user` 依赖本机装有 Chrome/Edge 且能以调试端口启动；部分企业策略禁用调试端口，
  新版 Edge 对**默认 profile** 的调试有限制，务必用独立 `--user-data-dir`。
- 登录态会过期；`z_c0` 消失时需用户在独立 profile 窗口重新登录。
- 知乎风控策略会变，`_BLOCK_SIGNALS` 需按实测补充。
- 全部档位（0/2/4 guest/4 user）已于 2026-11-17 实测通过，输出见上。
