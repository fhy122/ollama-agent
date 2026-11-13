/* Ollama Agent Web UI 前端逻辑（对话式布局） */
"use strict";

// ---------------- DOM ----------------
const $ = (id) => document.getElementById(id);
const messagesEl = $("messages");
const inputEl = $("input");
const attachRow = $("attachments");
const welcomeEl = $("welcome");
const btnSend = $("btn-send");
const shellEl = $("shell");
const outlineEl = $("session-outline");
const wsFilesEl = $("ws-files");
const btnSidebarOpen = $("btn-sidebar-open");
const sessionListEl = $("session-list");
const searchInput = $("session-search");
const btnSearchClear = $("btn-search-clear");

// ---------------- 状态 ----------------
const state = {
  busy: false,
  queue: [],    // 输入队列：{id, text, imgs, files}（生成中排队，回合结束自动续发）
  images: [],   // {name, b64}
  files: [],    // {name, path}
  config: {},
  outline: [],  // {el, text}
  sessions: [], // 历史对话元信息
  sid: "",      // 当前对话 id
  models: null, // 模型配置清单（内置 Ollama + 自定义 profiles）
  noSearchApi: false, // 后端没有 /api/sessions/search（旧进程），搜索直接给重启提示
  searchQuery: "",    // 搜索态唯一真值：只有用户主动输入才更新，绝不信 input.value
  skillsOn: true,     // skill 工具是否启用（决定技能索引是否注入）
  skillsLoaded: false,// /api/skills 是否已返回过（避免首屏把「加载中」画成「没有技能」）
  wsExpanded: new Set(),   // 已展开的目录（相对路径）：刷新后按原样恢复
  wsChildren: new Map(),   // 目录 → 子项缓存：展开/收起不该重复请求
};

// ---------------- 工具函数 ----------------
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function toast(msg, isErr = false) {
  const el = document.createElement("div");
  el.className = "toast" + (isErr ? " err" : "");
  el.textContent = msg;
  $("toasts").appendChild(el);
  setTimeout(() => el.remove(), 2800);
}

// ---------------- num_ctx 预设档位 ----------------
// 让人手填 65536 这种数字太费劲：给常见的 K 档，提交值仍是精确 token 数。
// K 一律按 1024 算，选项文案把精确值写出来，免得 8K 被当成 8000。
const CTX_PRESETS = [
  [0, "服务端默认（不传 num_ctx）"],
  [4096, "4K（4,096 tokens）"],
  [8192, "8K（8,192 tokens）"],
  [16384, "16K（16,384 tokens）"],
  [32768, "32K（32,768 tokens）"],
  [65536, "64K（65,536 tokens）"],
  [131072, "128K（131,072 tokens）"],
];
const CTX_CUSTOM = "custom";
// 每档值不值得选，一句话说清代价：num_ctx 越大，KV cache 越吃显存
const CTX_DESC = {
  0: "不传 num_ctx，交给服务端默认（Ollama 一般是 4096，长文易截断）",
  4096: "最省显存，只够短对话",
  8192: "日常问答的常见档位",
  16384: "读一两份中等文档",
  32768: "长文档 / 多文件一起读",
  65536: "大文件与长对话，显存压力明显",
  131072: "尽量塞满，8G 以下显存基本别选",
};
const CTX_CUSTOM_HINT =
  "自定义 num_ctx 要填正整数 token 数（例如 12288）；不想填就选 4K / 8K 这类预设档位。";

function fmtCtx(n) {
  const v = Number(n) || 0;
  if (v > 0 && v % 1024 === 0) return `${v / 1024}K（${v.toLocaleString("en-US")} tokens）`;
  return `${v.toLocaleString("en-US")} tokens`;
}

// 顶栏 chip 地方小，只放短标签
function fmtCtxShort(n) {
  const v = Number(n) || 0;
  return v > 0 && v % 1024 === 0 ? `${v / 1024}K` : v.toLocaleString("en-US");
}

function buildCtxSelect(sel) {
  if (sel.dataset.ctxReady === "1") return;
  sel.innerHTML = CTX_PRESETS.map(([v, label]) =>
    `<option value="${v}">${label}</option>`).join("") +
    `<option value="${CTX_CUSTOM}">自定义数值…</option>`;
  sel.dataset.ctxReady = "1";
}

// 回填：预设档位直接选中；配置文件里的非标准值单独插一项并选中，
// 保证「打开看一眼再点保存」不会悄悄把它改成某个预设值。
function setCtxValue(sel, custom, raw) {
  buildCtxSelect(sel);
  const n = Number(raw) || 0;
  sel.querySelectorAll("option[data-extra]").forEach((o) => o.remove());
  custom.value = "";
  custom.classList.add("hidden");
  if (n > 0 && !CTX_PRESETS.some(([v]) => v === n)) {
    const o = document.createElement("option");
    o.value = String(n);
    o.dataset.extra = "1";
    o.textContent = `当前值：${fmtCtx(n)}`;
    sel.insertBefore(o, sel.querySelector(`option[value="${CTX_CUSTOM}"]`));
  }
  sel.value = String(n);
}

// null = 选了自定义却没填合法值：调用方要提示，不能静默当成 0 提交
function getCtxValue(sel, custom) {
  if (sel.value !== CTX_CUSTOM) return Number(sel.value || 0);
  const n = Number(custom.value);
  return Number.isInteger(n) && n > 0 ? n : null;
}

function initCtxControl(sel, custom) {
  buildCtxSelect(sel);
  sel.addEventListener("change", () => {
    const on = sel.value === CTX_CUSTOM;
    custom.classList.toggle("hidden", !on);
    if (on) custom.focus();
  });
}

// 协议不支持时显示「不适用」并禁用，但别把真实值从表单里抹掉：
// 保存模型配置仍按原值提交，否则切一次中转站就把已存的 num_ctx 清成 0。
function setCtxCap(sel, custom, supported, reason, current) {
  setFieldCap(sel.id, supported, reason);
  const off = sel.querySelector("option[data-off]");
  if (!supported) {
    if (!off) {
      const o = document.createElement("option");
      o.value = "";
      o.dataset.off = "1";
      o.textContent = "不适用";
      sel.appendChild(o);
    }
    sel.value = "";
    custom.classList.add("hidden");
    custom.disabled = true;
  } else {
    if (off) {                       // 刚从禁用态恢复，按原值重建选中项
      off.remove();
      setCtxValue(sel, custom, current);
    }
    custom.disabled = false;
  }
}

async function api(path, body, timeoutMs) {
  const opts = body === undefined
    ? {}
    : { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) };
  // 只在指定时启用：别的接口快且不该被掐；pick_file 这种要等人挑文件才需要
  if (timeoutMs) opts.signal = AbortSignal.timeout(timeoutMs);
  const resp = await fetch(path, opts);
  return resp.json();
}

function fmtSize(n) {
  if (n == null) return "";
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(1) + " MB";
}

const EXT_ICON = {
  md: "📝", txt: "📄", json: "🧩", py: "🐍", js: "🟨", ts: "🟦", html: "🌐",
  css: "🎨", csv: "📊", xlsx: "📊", xls: "📊", png: "🖼", jpg: "🖼", jpeg: "🖼",
  webp: "🖼", gif: "🖼", pdf: "📕", zip: "🗜", sh: "⚡", yml: "⚙️", yaml: "⚙️",
  toml: "⚙️", log: "🗒",
};
function extOf(name) {
  const m = String(name || "").match(/\.([A-Za-z0-9]+)$/);
  return m ? m[1].toLowerCase() : "";
}
function fileIcon(name) {
  return EXT_ICON[extOf(name)] || "📄";
}

async function copyText(text, btnEl) {
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand("copy"); } catch { /* 忽略 */ }
    ta.remove();
  }
  if (btnEl) {
    const old = btnEl.textContent;
    btnEl.textContent = "已复制";
    btnEl.classList.add("done");
    setTimeout(() => {
      btnEl.textContent = old;
      btnEl.classList.remove("done");
    }, 1400);
  } else {
    toast("已复制");
  }
}

// ---------------- Markdown 渲染 ----------------
function inlineFmt(s) {
  return String(s)
    .replace(/`([^`\n]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(https?:\/\/[^\s<)]+)/g,
      '<a href="$1" target="_blank" rel="noopener">$1</a>');
}

function splitRow(line) {
  let s = line.trim();
  if (s.startsWith("|")) s = s.slice(1);
  if (s.endsWith("|")) s = s.slice(0, -1);
  return s.split("|").map((c) => c.trim());
}

// | 表头 | … | + |---|…| 分隔行 + 数据行 → 真表格
function tableToHtml(block) {
  const lines = block.split("\n").filter((l) => l.trim());
  if (lines.length < 2) return null;
  const sep = splitRow(lines[1]);
  if (!sep.length || !sep.every((c) => /^:?-{2,}:?$/.test(c))) return null;
  const head = splitRow(lines[0]);
  const aligns = sep.map((c) =>
    c.startsWith(":") && c.endsWith(":") ? "center"
      : c.endsWith(":") ? "right"
        : c.startsWith(":") ? "left" : "");
  const cls = (i) => (aligns[i] ? ` style="text-align:${aligns[i]}"` : "");
  const tdCls = (v) => (/^[\d\-+()\s]{7,}$/.test(v) || /^[^\s@]+@[^\s@]+$/.test(v))
    ? ' class="nw"' : "";
  let html = '<div class="table-wrap"><table class="md-table"><thead><tr>';
  head.forEach((c, i) => { html += `<th${cls(i)}>${inlineFmt(c)}</th>`; });
  html += "</tr></thead><tbody>";
  for (const line of lines.slice(2)) {
    const cells = splitRow(line);
    html += "<tr>";
    for (let i = 0; i < head.length; i += 1) {
      const v = cells[i] ?? "";
      html += `<td${cls(i)}${tdCls(v)}>${inlineFmt(v)}</td>`;
    }
    html += "</tr>";
  }
  return `${html}</tbody></table></div>`;
}

// 代码块 → 带语言标签 + 复制/换行/放大的卡片
function codeBlockHtml(raw, lang) {
  const l = (lang || "text").toLowerCase();
  const label = { py: "python", js: "javascript", ts: "typescript", sh: "shell",
    bash: "shell", yml: "yaml", md: "markdown" }[l] || l;
  return '<div class="code-card">' +
    '<div class="code-head">' +
    `<span class="code-lang">${esc(label)}</span>` +
    '<div class="code-btns">' +
    '<button class="code-btn" data-act="copy-code" title="复制代码">复制</button>' +
    '<button class="code-btn" data-act="wrap-code" title="自动换行">换行</button>' +
    '<button class="code-btn" data-act="expand-code" title="放大查看">放大</button>' +
    "</div></div>" +
    `<pre><code>${esc(raw)}</code></pre></div>`;
}

// ---------------- 数学公式（KaTeX，本地静态副本） ----------------
// 定界符必须在 esc() 之前抽走：LaTeX 里的 < > & 一旦变成 &lt; &gt; &amp;，
// KaTeX 收到的就是实体名而不是关系符，公式会被拆错。
// 只认「成对闭合」的定界符：流式输出时公式常只打了一半，未闭合的 $ 先当
// 普通文本，等补齐再渲染，免得每个增量都闪一次红色错误。
const MATH_CACHE = new Map();

function mathHtml(tex, display) {
  const key = (display ? "D:" : "I:") + tex;
  if (MATH_CACHE.has(key)) return MATH_CACHE.get(key);
  let html;
  if (typeof katex === "undefined" || !katex.renderToString) {
    // 库没加载成（文件缺失/被拦）不能静默吞掉内容，回退成原文
    html = `<code class="md-math-fallback" title="公式渲染库未加载，显示原始 LaTeX">${esc(tex)}</code>`;
  } else {
    try {
      html = katex.renderToString(tex, {
        displayMode: display,
        throwOnError: false,   // 写错的 LaTeX 显示成带标记的原文，不抛异常打断整条消息
        strict: "ignore",
        trust: false,         // 禁 \href / \includegraphics，防注入
      });
    } catch (e) {
      html = `<code class="md-math-fallback" title="公式无法渲染：${esc(e.message)}">${esc(tex)}</code>`;
    }
  }
  // 流式渲染每来一个字就全量重排一遍，公式结果要缓存；缓存本身别无上限涨
  if (MATH_CACHE.size >= 300) MATH_CACHE.clear();
  MATH_CACHE.set(key, html);
  return html;
}

function extractMath(src, out) {
  const push = (tex, display) => {
    const t = tex.trim();
    if (!t) return null;
    out.push({ display, html: mathHtml(t, display) });
    return `\u0000M${out.length - 1}\u0000`;
  };
  // 显示公式（可跨行）
  let s = src.replace(/\$\$([\s\S]+?)\$\$/g, (m, tex) => push(tex, true) ?? m);
  s = s.replace(/\\\[([\s\S]+?)\\\]/g, (m, tex) => push(tex, true) ?? m);
  // 行内公式：内容非空、不是纯价格数字，且前后不紧贴数字（$5 / 10$ 是货币）
  const inline = (m, tex, offset, str) => {
    const t = tex.trim();
    if (!t || /^[\d.,\s]+$/.test(t)) return m;
    const before = str[offset - 1] || "";
    const after = str[offset + m.length] || "";
    if (/\d/.test(before) || /\d/.test(after)) return m;
    return push(t, false) ?? m;
  };
  s = s.replace(/(?<!\\)\$([^$\n]+?)(?<!\\)\$/g, inline);
  s = s.replace(/\\\(([\s\S]+?)\\\)/g, inline);
  return s;
}

function renderMarkdown(src) {
  // Windows 换行先归一化：正则里的 $ 不吃 \r，行末多出的 \r 会让
  // 表格/标题/列表的 ^…$ 逐行匹配全部失配（实测：CRLF 的 .md 表格渲染成纯文本）
  const parts = String(src).replace(/\r\n?/g, "\n").split(/(```[\s\S]*?```)/g);
  return parts.map((part) => {
    if (part.startsWith("```")) {
      const m = part.match(/^```([\w+#.-]*)\n?([\s\S]*?)```$/);
      return codeBlockHtml(m ? m[2] : part, m ? m[1] : "");
    }
    // 行内代码先占位：里面的 $ 不是公式、** 不是粗体、URL 不是链接
    const codes = [];
    let raw = part.replace(/`([^`\n]+)`/g, (m, code) => {
      codes.push(`<code>${esc(code)}</code>`);
      return `\u0000C${codes.length - 1}\u0000`;
    });
    const maths = [];
    raw = extractMath(raw, maths);
    let s = esc(raw);

    // 表格先抽出来做占位，避免被后续行内规则和段落包裹打乱
    const tables = [];
    s = s.replace(/(?:^[ \t]*\|.*\|[ \t]*$)(?:\n[ \t]*\|.*\|[ \t]*$)*/gm, (block) => {
      const html = tableToHtml(block);
      if (!html) return block;
      tables.push(html);
      return `\u0000T${tables.length - 1}\u0000`;
    });

    s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/^#{1,4}\s+(.+)$/gm, "<h4>$1</h4>");
    s = s.replace(/^(?:[-*]|\d+\.)\s+(.+)$/gm, "<li>$1</li>");
    s = s.replace(/(<li>[\s\S]*?<\/li>)(\n(?!<li>)|$)/g, "<ul>$1</ul>$2");
    // 图片要在自动链接之前抽成占位，否则 src 里的 URL 会被自动链接拆坏标签
    const imgs = [];
    s = s.replace(/!\[([^\]]*)\]\((https?:\/\/[^\s)]+|data:image\/[^\s)]+)\)/g, (m, alt, src) => {
      imgs.push(`<img class="md-img" alt="${alt}" src="${src}" loading="lazy">`);
      return `\u0000I${imgs.length - 1}\u0000`;
    });
    s = s.replace(/(https?:\/\/[^\s<)]+)/g,
      '<a href="$1" target="_blank" rel="noopener">$1</a>');
    s = s.replace(/\u0000I(\d+)\u0000/g, (m, i) => imgs[Number(i)]);

    s = s.replace(/\u0000T(\d+)\u0000/g, (m, i) => `\n\n${tables[Number(i)]}\n\n`);
    // 块级公式换成 B 占位，让它在段落分割里独占一块、不被 <p> 包住。
    // 到这一步粗体/链接规则都跑完了，最后才把占位换成 KaTeX 的 HTML，
    // 公式内部的换行和标签就不会被任何 Markdown 规则拆坏。
    s = s.replace(/\u0000M(\d+)\u0000/g, (m, i) => maths[Number(i)].display
      ? `\n\n\u0000B${i}\u0000\n\n` : m);
    return s.split(/\n{2,}/).map((b) => {
      const t = b.trim();
      if (!t) return "";
      const bm = t.match(/^\u0000B(\d+)\u0000$/);
      if (bm) return `<div class="md-math-block">${maths[Number(bm[1])].html}</div>`;
      return (t.startsWith("<h4>") || t.startsWith("<ul>") || t.startsWith("<div"))
        ? t : `<p>${t.replace(/\n/g, "<br>")}</p>`;
    }).join("")
      .replace(/\u0000M(\d+)\u0000/g, (m, i) => maths[Number(i)].html)
      .replace(/\u0000B(\d+)\u0000/g, (m, i) => maths[Number(i)].html)
      .replace(/\u0000C(\d+)\u0000/g, (m, i) => codes[Number(i)]);
  }).join("");
}

// ---- 自动跟随（stick-to-bottom）----
// 输出中每个增量都调 scrollBottom 会把上滑回看的用户拽回底部。
// 规则：用户向上滚 → 跟随暂停；滚回底部（或点「回到底部」）→ 恢复。
// 主动作（发消息、切会话、回放历史）强制滚底并顺带恢复跟随。
// 注意：不能按「离底距离」判断回看——生成中 scroll 事件迟到、内容在
// 两次事件之间疯长，会把程序滚动/内容生长误判成用户上滑，导致
// 「滚轮失灵、点回到底也弹回」。改为按滚动方向 + 贴底判定用户意图。
let followBottom = true;
let lastTop = 0;

function distFromBottom() {
  return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight;
}

messagesEl.addEventListener("scroll", () => {
  const top = messagesEl.scrollTop;
  if (distFromBottom() < 48) {
    followBottom = true;             // 贴底 = 跟随（含内容收缩导致的 clamp 回跳）
  } else if (top < lastTop - 1) {
    followBottom = false;            // 向上滚 = 用户想回看
  }
  // 其余情况（离底且向上变小/不变，多半是内容增长）保持现状
  lastTop = top;
  updateJumpBtn();
}, { passive: true });

// 滚轮/触摸上滑是明确的回看意图，先于 scroll 事件生效：
// 主线程繁忙时 scroll 事件会迟到，等它再判就被生成中的 scrollBottom() 抵消了。
messagesEl.addEventListener("wheel", (e) => {
  if (e.deltaY < 0) followBottom = false;
}, { passive: true });

function updateJumpBtn() {
  const btn = $("jump-bottom");
  if (!btn) return;
  // 离底超过一屏的 1/8 且确有消息时才值得打扰（空态/贴底都收起）
  const hasMsgs = messagesEl.querySelector(".msg") !== null;
  btn.classList.toggle("hidden", distFromBottom() < 120 || !hasMsgs);
}

function scrollBottom(force) {
  if (!force && !followBottom) { updateJumpBtn(); return; }
  if (force) followBottom = true;
  messagesEl.scrollTop = messagesEl.scrollHeight;
  lastTop = messagesEl.scrollTop;    // 同步基准，避免自己的程序滚动被当成用户上滑
  updateJumpBtn();
}

// ---------------- 图片查看器 ----------------
function ensureViewer() {
  let box = document.getElementById("img-viewer");
  if (box) return box;
  box = document.createElement("div");
  box.id = "img-viewer";
  box.className = "img-viewer hidden";
  box.innerHTML = '<img alt="">' +
    '<div class="iv-toolbar">' +
    '<span class="iv-name"></span>' +
    '<button data-iv="zoom">放大</button>' +
    '<button data-iv="download">下载</button>' +
    '<button data-iv="close">关闭</button>' +
    "</div>";
  document.body.appendChild(box);
  box.addEventListener("click", (e) => {
    const act = e.target.closest("[data-iv]")?.dataset.iv;
    const big = box.querySelector("img");
    if (act === "zoom") { box.classList.toggle("zoomed"); return; }
    if (act === "download") {
      const a = document.createElement("a");
      a.href = big.src;
      a.download = big.dataset.name || "image.png";
      a.click();
      return;
    }
    if (act === "close") { box.classList.add("hidden"); return; }
    if (e.target.tagName === "IMG") { box.classList.toggle("zoomed"); return; }
    box.classList.add("hidden");
  });
  return box;
}

function openViewer(img) {
  const box = ensureViewer();
  const big = box.querySelector("img");
  big.src = img.currentSrc || img.src;
  const name = img.dataset.name || img.alt || "image.png";
  big.dataset.name = /\.(png|jpe?g|webp|gif|bmp)$/i.test(name) ? name : name + ".png";
  big.alt = name;
  box.querySelector(".iv-name").textContent = name;
  box.classList.remove("zoomed");
  box.classList.remove("hidden");
}

function closeViewer() {
  const box = document.getElementById("img-viewer");
  if (box && !box.classList.contains("hidden")) {
    box.classList.add("hidden");
    return true;
  }
  return false;
}

// ---------------- 文件预览 ----------------
function fileCardHTML(name, path, sub) {
  return '<div class="file-card" data-path="' + esc(path || name) + '">' +
    `<span class="fi">${fileIcon(name)}</span>` +
    '<div class="fmeta">' +
    `<div class="fname">${esc(name)}</div>` +
    `<div class="fsub">${esc(sub || "生成的文件")}</div>` +
    "</div>" +
    '<button class="fbtn" data-act="preview-file">预览</button>' +
    "</div>";
}

async function openPreview(path, name) {
  const modal = $("preview-modal");
  const display = name || String(path || "").split(/[\\/]/).pop() || "文件";
  $("pv-name").textContent = display;
  $("pv-meta").textContent = "加载中…";
  $("pv-icon").textContent = fileIcon(display);
  $("pv-body").innerHTML = '<div class="pv-note">加载中…</div>';
  $("pv-copy").hidden = true;
  $("pv-copy").onclick = null;
  // 预览里的「加入上下文」：看完内容直接把这一个文件挂到本轮对话上，
  // 关掉弹窗后 chip 就在输入框上方，无需再回侧栏找。
  $("pv-add").onclick = () => {
    const before = state.files.length;
    addFileAttachment(display, path);
    toast(state.files.length > before
      ? `已加入上下文：${display}` : "这个文件已经在上下文里了");
  };
  $("pv-download").onclick = () => {
    const a = document.createElement("a");
    a.href = "/api/file?raw=1&path=" + encodeURIComponent(path);
    a.download = display;
    a.click();
  };
  modal.classList.remove("hidden");

  let r;
  try {
    r = await api("/api/file?path=" + encodeURIComponent(path));
  } catch (e) {
    $("pv-meta").textContent = "";
    $("pv-body").innerHTML = `<div class="pv-note">读取失败：${esc(e.message)}</div>`;
    return;
  }
  if (r.error) {
    $("pv-meta").textContent = "";
    $("pv-body").innerHTML = `<div class="pv-note">${esc(r.error)}</div>`;
    return;
  }

  $("pv-meta").textContent = [r.kind === "image" ? "图片" : (r.lang || r.kind),
    fmtSize(r.size)].filter(Boolean).join(" · ");

  if (r.kind === "image") {
    $("pv-body").innerHTML =
      `<div class="pv-img-wrap"><img src="data:${r.mime || "image/png"};base64,${r.b64}" alt="${esc(display)}"></div>`;
    return;
  }
  if (r.kind === "table") {
    const rows = r.rows || [];
    let html = "<table><thead><tr>" +
      (rows[0] || []).map((c) => `<th>${esc(c)}</th>`).join("") + "</tr></thead><tbody>";
    for (const row of rows.slice(1)) {
      html += "<tr>" + row.map((c) => `<td>${esc(c)}</td>`).join("") + "</tr>";
    }
    html += "</tbody></table>";
    if (r.truncated) html += '<div class="pv-note">仅显示前 200 行</div>';
    $("pv-body").innerHTML = html;
    $("pv-copy").hidden = false;
    $("pv-copy").onclick = (e) => {
      const text = rows.map((row) => row.join("\t")).join("\n");
      copyText(text, e.currentTarget);
    };
    return;
  }
  if (r.kind === "text") {
    if (r.markdown) {
      $("pv-body").innerHTML = `<div class="pv-md">${renderMarkdown(r.text || "")}</div>`;
    } else {
      $("pv-body").innerHTML = `<pre>${esc(r.text || "")}</pre>`;
    }
    $("pv-copy").hidden = false;
    $("pv-copy").onclick = (e) => copyText(r.text || "", e.currentTarget);
    return;
  }
  $("pv-body").innerHTML = '<div class="pv-note">该类型不支持在线预览，可点右上角下载。</div>';
}

// ---------------- 侧栏 ----------------
function addOutline(text, el) {
  const item = document.createElement("button");
  item.className = "ol-item";
  const plain = String(text || "")
    .replace(/```[\s\S]*?```/g, " [代码] ")
    .replace(/[`*#>|\[\]]/g, "")
    .replace(/\s+/g, " ")
    .trim();
  item.textContent = plain.slice(0, 40) || "新消息";
  item.title = plain.slice(0, 120);
  item.onclick = () => {
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    outlineEl.querySelectorAll(".ol-item").forEach((x) => x.classList.remove("active"));
    item.classList.add("active");
  };
  const empty = outlineEl.querySelector(".sb-empty");
  if (empty) empty.remove();
  outlineEl.appendChild(item);
  state.outline.push({ el, item });
}

function clearOutline() {
  outlineEl.innerHTML = '<p class="sb-empty">还没有提问</p>';
  state.outline = [];
}

// ---------------- 工作空间文件（目录树） ----------------

// 每层缩进 14px：侧栏本来就窄，再多就没地方显示文件名了
const WS_INDENT = 14;
const wsPad = (depth) => 8 + depth * WS_INDENT;

function wsEntryHtml(e, depth) {
  const pad = wsPad(depth);
  if (e.dir) {
    // 只有「拉过子项且仍在展开集合里」才画成展开态。刷新会把缓存清掉，
    // 这里若照着 wsExpanded 画成展开却留一个空容器，看起来就像空目录。
    const open = state.wsChildren.has(e.path) && state.wsExpanded.has(e.path);
    return `<button class="ws-row ws-dir${open ? " open" : ""}"` +
      ` data-dir="${esc(e.path)}" data-depth="${depth}"` +
      ` style="padding-left:${pad}px" title="${esc(e.path)}">` +
      `<span class="caret">${open ? "▾" : "▸"}</span>` +
      `<span class="wico">${open ? "📂" : "📁"}</span>` +
      `<span class="nm">${esc(e.name)}</span></button>` +
      `<div class="ws-kids" data-kids="${esc(e.path)}"${open ? "" : " hidden"}></div>`;
  }
  // 文件行外面套一层：点行=预览，右侧「+」=加入上下文（两个动作不能挤在一个
  // button 里——button 套 button 是非法 HTML，浏览器会把它拆开）
  return `<div class="ws-row-wrap">` +
    `<button class="ws-item" data-path="${esc(e.path)}"` +
    ` style="padding-left:${pad}px" title="${esc(e.path)}">` +
    `<span class="fi">${fileIcon(e.name)}</span>` +
    `<span class="nm">${esc(e.name)}</span></button>` +
    `<button class="ws-add" data-add="${esc(e.path)}"` +
    ` title="加入对话上下文" aria-label="${esc(e.name)} 加入上下文">+</button>` +
    `</div>`;
}

function paintDir(container, data, depth) {
  const entries = data.entries || [];
  if (!entries.length) {
    const msg = data.error ? esc(data.error) : (depth ? "空目录" : "工作空间没有文件");
    container.innerHTML = depth
      ? `<p class="ws-kid-empty" style="padding-left:${wsPad(depth)}px">${msg}</p>`
      : `<p class="sb-empty">${msg}</p>`;
    return;
  }
  container.innerHTML = entries.map((e) => wsEntryHtml(e, depth)).join("") +
    (data.truncated
      ? `<p class="ws-kid-empty" style="padding-left:${wsPad(depth)}px">还有更多未显示</p>`
      : "");
}

// 按 dataset 精确匹配，而不是拼 CSS 属性选择器：路径里带引号/反斜杠时
// 拼进选择器会直接抛 SyntaxError，整个侧栏白屏。
const wsFind = (sel, key, val) =>
  [...wsFilesEl.querySelectorAll(sel)].find((el) => el.dataset[key] === val);

async function toggleDir(path, wantOpen) {
  const row = wsFind(".ws-dir", "dir", path);
  const kids = wsFind("[data-kids]", "kids", path);
  if (!row || !kids) return;
  const open = wantOpen === undefined ? !state.wsExpanded.has(path) : wantOpen;
  if (!open) {
    state.wsExpanded.delete(path);
    row.classList.remove("open");
    row.querySelector(".caret").textContent = "▸";
    row.querySelector(".wico").textContent = "📁";
    kids.hidden = true;
    return;
  }
  state.wsExpanded.add(path);
  row.classList.add("open");
  row.querySelector(".caret").textContent = "▾";
  row.querySelector(".wico").textContent = "📂";
  kids.hidden = false;
  if (state.wsChildren.has(path)) return;      // 拉过就复用，收起再展开不再请求
  kids.innerHTML = '<p class="ws-kid-empty">加载中…</p>';
  const depth = Number(row.dataset.depth || 0) + 1;
  try {
    const data = await api("/api/files/tree?path=" + encodeURIComponent(path));
    state.wsChildren.set(path, data);
    paintDir(kids, data, depth);
  } catch {
    kids.innerHTML = '<p class="ws-kid-empty">读取失败</p>';
  }
}

// 刷新后按「上次展开的目录」逐层重拉。父目录必须先于子目录：
// 外层重绘会把旧的 kids 容器一起换掉，子目录的 DOM 那时还不存在。
async function restoreExpanded(paths) {
  const ordered = [...paths].sort((a, b) => a.split("/").length - b.split("/").length);
  for (const p of ordered) {
    if (state.wsExpanded.has(p)) await toggleDir(p, true);
  }
}

// reset=true：工作空间换了，旧的展开路径全部作废（否则一直攒着上一个目录的键）
async function loadWorkspaceFiles(reset) {
  if (reset) state.wsExpanded.clear();
  const keep = new Set(state.wsExpanded);
  state.wsChildren.clear();
  try {
    const root = await api("/api/files/tree?path=");
    paintDir(wsFilesEl, root, 0);
    await restoreExpanded(keep);
  } catch {
    wsFilesEl.innerHTML = '<p class="sb-empty">读取失败</p>';
  }
}

// ---------------- 历史对话（会话列表） ----------------

function fmtWhen(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const now = new Date();
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  if (d.toDateString() === now.toDateString()) return hm;
  const y = new Date(now);
  y.setDate(now.getDate() - 1);
  if (d.toDateString() === y.toDateString()) return "昨天";
  const md = `${d.getMonth() + 1}-${String(d.getDate()).padStart(2, "0")}`;
  return d.getFullYear() === now.getFullYear() ? md : `${d.getFullYear()}-${md}`;
}

function renderSessionList(list, currentId) {
  state.sessions = list || [];
  if (!state.sessions.length) {
    sessionListEl.innerHTML = '<p class="sb-empty">还没有历史对话</p>';
    return;
  }
  const st = state.statuses || {};
  sessionListEl.innerHTML = state.sessions.map((s) => {
    const b = st[s.id] || {};
    // 并行会话通知（参考 DSH agent/status 投影）：●=生成中 ✦=后台完成待查看
    const badge = b.busy
      ? '<span class="ss-badge ss-run" title="正在生成">●</span>'
      : (b.attention ? '<span class="ss-badge ss-done" title="已完成，点开查看">✦</span>' : "");
    return `<div class="ss-item${s.id === currentId ? " active" : ""}" data-id="${esc(s.id)}">` +
      `<button class="ss-main" data-open="${esc(s.id)}" title="${esc(s.title)}">` +
      `<span class="ss-title">${badge}${esc(s.title)}</span>` +
      `<span class="ss-time">${fmtWhen(s.updated)}</span></button>` +
      `<button class="ss-del" data-del="${esc(s.id)}" title="删除这条对话" aria-label="删除对话">×</button>` +
      "</div>";
  }).join("");
}

async function loadSessions() {
  try {
    const r = await api("/api/sessions");
    state.sid = (r.current && r.current.id) || "";
    state.sessions = r.sessions || [];
    syncSearchUI();                 // 顺手把被浏览器/扩展改过的显示拉回真值
    if (state.searchQuery) await runSearch(state.searchQuery);
    else renderSessionList(r.sessions, state.sid);
    return r;
  } catch {
    sessionListEl.innerHTML = '<p class="sb-empty">读取失败</p>';
    return null;
  }
}

// ---------------- 搜索历史对话 ----------------
const RE_META = /[.*+?^${}()|[\]\\]/g;
let searchTimer = 0;
let searchSeq = 0;   // 竞态令牌：慢响应回来时若已有新输入就丢弃
let searchReady = false;  // window.load 之后才把 input 事件当用户输入（躲开加载期自动填充）

// 先转义再包 <mark>：esc 过的词才能和 esc 过的正文对上
function highlight(text, terms) {
  const safe = esc(text);
  if (!terms || !terms.length) return safe;
  const pat = terms.map((t) => esc(t).replace(RE_META, "\\$&")).filter(Boolean).join("|");
  if (!pat) return safe;
  return safe.replace(new RegExp(`(${pat})`, "gi"), "<mark>$1</mark>");
}

function renderSearchResults(results, kw) {
  const terms = kw.split(/\s+/).filter(Boolean);
  if (!results.length) {
    sessionListEl.innerHTML = `<p class="sb-empty">没有找到含「${esc(kw)}」的对话</p>`;
    return;
  }
  sessionListEl.innerHTML = results.map((s) =>
    `<div class="ss-item ss-result${s.id === state.sid ? " active" : ""}" data-id="${esc(s.id)}">` +
    `<button class="ss-main" data-open="${esc(s.id)}" title="${esc(s.title)}">` +
    `<span class="ss-r1"><span class="ss-title">${highlight(s.title, terms)}</span>` +
    `<span class="ss-time">${fmtWhen(s.updated)}</span></span>` +
    (s.snippet
      ? `<span class="ss-snip"><span class="ss-field">${esc(s.field || "内容")}</span>` +
        `${highlight(s.snippet, terms)}</span>`
      : "") +
    "</button>" +
    `<button class="ss-del" data-del="${esc(s.id)}" title="删除这条对话" aria-label="删除对话">×</button>` +
    "</div>").join("");
}

async function runSearch(raw) {
  const kw = String(raw || "").trim();
  state.searchQuery = kw;   // 任何路径进入搜索，都把真值同步成实际生效的关键词
  btnSearchClear.hidden = !kw;
  if (!kw) {
    renderSessionList(state.sessions, state.sid);
    return;
  }
  if (state.noSearchApi) {
    sessionListEl.innerHTML =
      '<p class="sb-empty">搜索不可用：后端还是旧进程<br>重启服务（Ctrl+C 后重跑启动命令）即可</p>';
    return;
  }
  const mine = ++searchSeq;
  try {
    const r = await api("/api/sessions/search?q=" + encodeURIComponent(kw));
    if (mine !== searchSeq) return;      // 已被更新的输入取代
    // 404 / 老后端会回 {"error": "not found"}，不能当成「零结果」
    if (r.error) throw new Error(r.error);
    renderSearchResults(r.results || [], kw);
  } catch (e) {
    if (mine !== searchSeq) return;
    state.noSearchApi = /not found/i.test(e.message || "");
    sessionListEl.innerHTML = state.noSearchApi
      ? '<p class="sb-empty">搜索不可用：后端还是旧进程<br>重启服务（Ctrl+C 后重跑启动命令）即可</p>'
      : '<p class="sb-empty">搜索失败，稍后再试</p>';
  }
}

function clearSearch(focus = true) {
  clearTimeout(searchTimer);
  searchSeq++;                            // 让在飞的请求作废
  state.searchQuery = "";
  searchInput.value = "";
  btnSearchClear.hidden = true;
  renderSessionList(state.sessions, state.sid);
  if (focus) searchInput.focus();
}

// 列表被局部刷新时（删除/切换/新建），搜索中要重新搜，否则会闪回全量列表
function refreshSessionPane(list) {
  syncSearchUI();
  if (state.searchQuery) runSearch(state.searchQuery);
  else renderSessionList(list, state.sid);
}

// 浏览器刷新/会话恢复会把上次的查询词塞回 input，且不派发 input 事件
// （autocomplete="new-password" 只是降低概率）。搜索态只认 state.searchQuery，
// 这里把界面显示强行拉回真值，避免框里留着一段已经不生效的旧字。
function syncSearchUI() {
  const q = state.searchQuery || "";
  // 用 trim 比较：用户正输入的 "Excel " 不该被中途改写成 "Excel"
  if (searchInput.value.trim() !== q) searchInput.value = q;
  btnSearchClear.hidden = !q;
}

function clearView() {
  messagesEl.querySelectorAll(".msg").forEach((m) => m.remove());
  welcomeEl.style.display = "";
  clearOutline();
}

function closeSidebarIfMobile() {
  if (window.innerWidth <= 900) setSidebar(false);
}

// 把服务端记下的流事件按原样重放，还原气泡 / 代码块 / 工具卡片
function replayTurns(turns) {
  for (const t of turns || []) {
    if (t.role === "user") {
      const files = (t.files || []).map((p) => ({
        path: p, name: String(p).split(/[\\/]/).pop(),
      }));
      addUserMsg(t.text || "", [], files, t.imgs || 0);
    } else if (t.role === "assistant") {
      const turn = newAssistantTurn();
      for (const ev of t.events || []) applyEvent(turn, ev);
      turn.finalize();
    }
  }
  scrollBottom(true);   // 打开会话/回放历史 = 用户要看最新
}

async function openSessionById(id) {
  if (id === state.sid && messagesEl.querySelector(".msg")) return;
  const r = await api("/api/sessions/open", { id });
  if (r.error) return toast(r.error, true);
  detachStream(r.id);                // 旧会话的流若有，转孤儿：渲染与 busy 都让位
  state.sid = r.id;
  setBusy(false);                    // busy 属于「正在看的会话」；目标忙则下面 attachLive 再置回
  clearView();
  const turns = r.turns || [];
  if (r.busy) {
    // 目标会话正在后台生成：不回放最后一轮，交给 attachLive 从快照接管
    const cut = turns.length && turns[turns.length - 1].role === "assistant"
      ? turns.length - 1 : turns.length;
    replayTurns(turns.slice(0, cut));
    const live = await fetch("/api/live").then((x) => x.json()).catch(() => null);
    if (live && live.busy) {
      attachLive(live, turns.slice(cut));
    } else {
      // 接管前恰好跑完：补画被切掉的最后一轮
      replayTurns(turns.slice(cut));
    }
  } else {
    replayTurns(turns);
  }
  await loadSessions();
  closeSidebarIfMobile();
}

async function newChat() {
  // 并行：当前在生成也放行——它转后台继续跑，列表上会挂「生成中 ●」
  const r = await api("/api/sessions/new", {});
  if (r.error) return toast(r.error, true);
  detachStream(r.id || "");          // 旧会话的流转孤儿：事件不再渲染、busy 让位
  state.sid = r.id || "";
  setBusy(false);                    // 新对话是空闲的；旧流在后端照跑
  clearView();
  await loadSessions();
  closeSidebarIfMobile();
  inputEl.focus();
  toast("已开始新对话");
}

async function clearCurrentChat() {
  if (state.busy) return toast("正在生成，先点停止", true);
  if (!messagesEl.querySelector(".msg") && !state.sid) {
    return toast("当前对话本来就是空的");
  }
  // 把影响范围写清楚：这条会连存档一起删掉，其他历史对话一条不动
  const others = state.sessions.filter((s) => s.id !== state.sid).length;
  const body = others
    ? `只删当前这一条，另外 ${others} 条历史对话不受影响。清空后无法恢复。`
    : "清空后无法恢复。";
  if (!(await askYesNo("清空当前对话？", body, "清空"))) return;
  const r = await api("/api/reset", {});
  if (r.error) return toast(r.error, true);
  state.sid = (r.current && r.current.id) || "";
  state.sessions = r.sessions || state.sessions;
  clearView();
  refreshSessionPane(r.sessions);
  inputEl.focus();
  toast("本条对话已清空，其他对话保留");
}

async function deleteSession(id) {
  if (state.busy) return toast("正在生成，先点停止", true);
  const cur = state.sessions.find((s) => s.id === id);
  const name = cur ? cur.title : "这条对话";
  if (!(await askYesNo("删除这条对话？", `「${name}」将从历史里移除，删除后无法恢复。`, "删除"))) return;
  const r = await api("/api/sessions/delete", { id });
  if (r.error) return toast(r.error, true);
  if (id === state.sid) {
    state.sid = (r.current && r.current.id) || "";
    clearView();
  }
  state.sessions = r.sessions || state.sessions;   // 搜索态下不经过 renderSessionList，得手动同步
  refreshSessionPane(r.sessions);
  toast("对话已删除");
}

function setSidebar(open) {
  shellEl.classList.toggle("sb-collapsed", !open);
  const scrim = $("sb-scrim");
  if (scrim) scrim.classList.toggle("hidden", !(open && window.innerWidth <= 900));
  // 同一颗按钮承担收起/展开，提示文字与 aria 同步
  const tg = $("btn-sidebar-toggle");
  if (tg) {
    tg.title = open ? "收起侧栏（Ctrl+B）" : "展开侧栏（Ctrl+B）";
    tg.setAttribute("aria-label", tg.title);
    tg.setAttribute("aria-expanded", open ? "true" : "false");
  }
  if (btnSidebarOpen) {
    btnSidebarOpen.title = "展开侧栏（Ctrl+B）";
    btnSidebarOpen.setAttribute("aria-label", "展开侧栏");
  }
  try { localStorage.setItem("sbOpen", open ? "1" : "0"); } catch { /* 隐私模式忽略 */ }
}

function toggleSidebar() {
  setSidebar(shellEl.classList.contains("sb-collapsed"));
}

// ---------------- 消息渲染 ----------------
function addUserMsg(text, imgs, files, imgCount = 0) {
  welcomeEl.style.display = "none";
  // 旧中断回合的「⚡ 继续生成」随新消息过期收掉（用户已经自己接上了话头）
  document.querySelectorAll(".continue-btn").forEach(
    (b) => b.closest(".msg-actions")?.remove());
  const wrap = document.createElement("div");
  wrap.className = "msg user";
  let inner = "";
  if (imgs.length || files.length || imgCount) {
    const pics = imgs.map((im) =>
      `<img src="data:image/png;base64,${im.b64}" alt="${esc(im.name)}" data-name="${esc(im.name)}">`).join("");
    const fchips = files.map((f) =>
      `<div class="file-chip" data-path="${esc(f.path)}">` +
      `<span>${fileIcon(f.name)}</span><span class="nm">${esc(f.name)}</span></div>`).join("");
    const past = imgCount
      ? `<div class="file-chip ghost"><span>🖼</span>` +
        `<span class="nm">图片 ×${imgCount}（历史里只留文字）</span></div>`
      : "";
    inner += `<div class="bubble">${pics ? `<div class="img-grid">${pics}</div>` : ""}${past}${fchips}</div>`;
  }
  if (text) inner += `<div class="bubble">${renderMarkdown(text)}</div>`;
  wrap.innerHTML = `<div class="body">${inner}</div>`;
  messagesEl.appendChild(wrap);
  addOutline(text || (imgs.length || imgCount ? "[图片]" : "[文件]"), wrap);
  scrollBottom(true);   // 用户发消息 = 明确想看底部
}

function newAssistantTurn() {
  welcomeEl.style.display = "none";
  const wrap = document.createElement("div");
  wrap.className = "msg assistant";
  wrap.innerHTML = '<div class="avatar" aria-hidden="true">◎</div>' +
    '<div class="body"><div class="bubble"></div></div>';
  const bubble = wrap.querySelector(".bubble");
  const body = wrap.querySelector(".body");
  wrap.dataset.rawAll = "";
  messagesEl.appendChild(wrap);
  scrollBottom(true);   // 新回合从用户刚发的内容开始，贴底
  return {
    root: bubble,
    body,
    currentText: null,
    currentThink: null,
    appendRaw(t) { wrap.dataset.rawAll = (wrap.dataset.rawAll || "") + t; },
    ensureText() {
      if (!this.currentText || !this.currentText.isConnected) {
        this.currentText = document.createElement("div");
        this.root.appendChild(this.currentText);
      }
      return this.currentText;
    },
    ensureThink() {
      if (!this.currentThink) {
        const d = document.createElement("details");
        d.className = "think";
        d.open = true;
        d.innerHTML = '<summary><span class="name">思考中…</span><span class="dt"></span></summary><div class="body"></div>';
        this.root.appendChild(d);
        this.currentThink = d.querySelector(".body");
        this._thinkEl = d;
      }
      return this.currentThink;
    },
    closeThink() {
      if (this._thinkEl) {
        this._thinkEl.querySelector(".name").textContent = "思考过程";
        this._thinkEl.open = false;
        this.currentThink = null;
      }
    },
    // 模型正在生成、但流上一个字都还没来时的进度条（长工具参数生成阶段）。
    // 必须能就地更新而不是每 100ms 新加一行，否则 200 秒的工具调用会堆出几百个元素。
    updateWaiting(elapsed, chars) {
      if (!this._waitEl || !this._waitEl.isConnected) {
        const d = document.createElement("div");
        d.className = "gen-wait";
        d.innerHTML = '<span class="dots"><i></i><i></i><i></i></span>' +
          '<span class="txt"></span>';
        this._waitEl = d;
      }
      this._waitEl.querySelector(".txt").textContent =
        `正在生成（已等 ${elapsed.toFixed(0)}s` +
        (chars ? `，已生成 ${chars} 字符` : "") + "）";
      this.root.appendChild(this._waitEl);      // 永远贴在最后，跟随新内容下移
      scrollBottom();
    },
    clearWaiting() {
      if (this._waitEl) {
        this._waitEl.remove();
        this._waitEl = null;
      }
    },
    addStep(n) {
      if (n > 1) {
        const d = document.createElement("div");
        d.className = "step-divider";
        d.textContent = `第 ${n} 步`;
        this.root.appendChild(d);
        this.currentText = null;
      }
    },
    addToolCard(name, args) {
      if (this.currentText) this.currentText.classList.remove("streaming-cursor");
      this.currentText = null;
      this.closeThink();
      const d = document.createElement("details");
      d.className = "tool";
      d.dataset.tool = name;
      d.dataset.path = (args && (args.path || args.file)) || "";
      if (name === "todo_write" && Array.isArray(args && args.todos)) {
        // 任务清单不用 JSON 卡片：直接渲染成勾选列表（args 即完整清单，
        // 实时与历史回放走同一渲染路径，零新增事件类型）。
        const icon = { pending: "○", in_progress: "▶", completed: "✓" };
        const items = args.todos.map((t) => {
          const s = icon[t.status] || "○";
          const cls = t.status === "completed" ? " todo-done"
            : t.status === "in_progress" ? " todo-active" : "";
          return `<li class="${cls.trim()}"><span class="todo-icon">${s}</span>${esc(t.content || "")}</li>`;
        }).join("");
        const active = args.todos.filter((t) => t.status === "in_progress").length;
        d.innerHTML = `<summary><span class="name">todo_write</span>` +
          `<span class="args">任务清单 · ${args.todos.length} 项${active ? ` · 进行中 ${active}` : ""}</span>` +
          `<span class="dt"></span></summary>` +
          `<div class="body"><ul class="todo-list">${items}</ul></div>`;
        this.root.appendChild(d);
        scrollBottom();
        return d;
      }
      const brief = esc(JSON.stringify(args)).slice(0, 160);
      d.innerHTML = `<summary><span class="name">${esc(name)}</span>` +
        `<span class="args">${brief}</span><span class="dt"></span></summary>` +
        '<div class="body">执行中…</div>';
      this.root.appendChild(d);
      scrollBottom();
      return d;
    },
    finishTool(card, name, text, dt, images) {
      const body = card.querySelector(".body");
      if (card.dataset.tool === "todo_write") {
        // 清单本体已在 tool_call 渲染成勾选列表，tool_result 只是计数回执，
        // 不能用回执文本覆盖掉清单。
        card.querySelector(".dt").textContent = `${dt}s`;
        return;
      }
      body.textContent = text.length > 4000 ? text.slice(0, 4000) + "\n…[截断]" : text;
      card.querySelector(".dt").textContent = `${dt}s`;
      const path = card.dataset.path;
      if (path && /(write_file|edit_file|append_file|render_sheet)$/.test(name)) {
        const holder = document.createElement("div");
        const sub = name === "render_sheet" ? "渲染源文件" : "本轮生成";
        holder.innerHTML = fileCardHTML(path.split(/[\\/]/).pop(), path, sub);
        card.after(holder.firstElementChild);
      }
      if (images && images.length) {
        const th = document.createElement("div");
        th.className = "tool-thumbs";
        th.innerHTML = images.map((b64) =>
          `<img src="data:image/png;base64,${b64}" alt="工具返回图" data-name="render.png">`).join("");
        card.after(th);
      }
      scrollBottom();
    },
    renderStream() {
      const el = this.ensureText();
      el.classList.add("streaming-cursor");
      el.innerHTML = renderMarkdown(el.dataset.raw || "");
      scrollBottom();
    },
    finalize() {
      this.clearWaiting();
      if (this.currentText) {
        this.currentText.classList.remove("streaming-cursor");
        this.currentText.innerHTML = renderMarkdown(this.currentText.dataset.raw || "");
      }
      this.root.querySelectorAll(".streaming-cursor")
        .forEach((el) => el.classList.remove("streaming-cursor"));
      this.closeThink();
      this.addActions();
      if (this.interrupted) this.addContinue();
    },
    // 断点续跑：重试用尽/手动打断的回合尾部给一个一键「继续」，
    // 免去用户重新打字。点击走正常发送路径（与手敲「继续」完全等价）。
    addContinue() {
      if (this._continue) return;
      const bar = document.createElement("div");
      bar.className = "msg-actions";
      bar.innerHTML = '<button class="act-btn continue-btn" data-act="continue-turn"' +
        ' title="从断点继续这个任务">⚡ 继续生成</button>';
      this.body.appendChild(bar);
      this._continue = bar;
    },
    addActions() {
      if (this._actions) return;
      const raw = (wrap.dataset.rawAll || "").trim();
      if (!raw) return;
      const bar = document.createElement("div");
      bar.className = "msg-actions";
      bar.innerHTML = '<button class="act-btn" data-act="copy-msg">复制</button>' +
        `<span class="act-btn" style="cursor:default">${raw.length} 字</span>`;
      this.body.appendChild(bar);
      this._actions = bar;
    },
  };
}

// ---------------- 发送 + 流式接收 + 可打断 ----------------
let abortCtl = null;
// 流的会话归属：runTurnStream / attachLive 各自认领。切换会话时旧流标记为
// 孤儿（detached）——它的事件不再渲染（回合在后端照跑，回来看走 /api/live
// 接管），也不再抢全局 busy，否则新会话的第一条消息会被误判成「生成中排队」，
// 永远发不出去（实测：忙时点「新对话」，新会话发消息全进了队列坞）。
let streamSid = "";
let streamDetached = false;

function detachStream(newSid) {
  if (streamSid && streamSid !== newSid) streamDetached = true;
}

function setBusy(busy) {
  state.busy = busy;
  if (busy) {
    btnSend.disabled = false;            // 不禁用：生成中它就是「停止」按钮
    btnSend.classList.add("stop");
    btnSend.title = "打断本轮回答";
    btnSend.querySelector(".send-label").textContent = "停止";
    btnSend.onclick = stopGeneration;
  } else {
    btnSend.classList.remove("stop");
    btnSend.title = "发送（Enter）";
    btnSend.querySelector(".send-label").textContent = "发送";
    btnSend.onclick = sendMessage;
    abortCtl = null;
  }
}

async function stopGeneration() {
  // 顺序很关键：先让后端停（它会 close 连接、释放会话锁），再断本地流。
  // 反过来做的话，本地 finally 会抢在前面跑 setBusy(false)——按钮已经变回
  // 「发送」，后端却还锁着，此时点新建对话必被 409 挡回，界面上又没有停止按钮，
  // 用户就卡死了。这是「已打断」却发不出消息的真正原因。
  try { await api("/api/stop", {}); } catch { /* 忽略 */ }
  if (abortCtl) abortCtl.abort();        // 兜底：确保本地流也断开
  const modal = $("confirm-modal");
  modal.classList.add("hidden");         // 有待确认弹窗也一并关掉（后端已全部拒绝）
  $("btn-confirm-yes").onclick = null;
  $("btn-confirm-all").onclick = null;
  $("btn-confirm-no").onclick = null;
  toast("已打断本轮回答");
}

async function sendMessage() {
  const text = inputEl.value.trim();
  if (state.busy) {                       // 生成中 Enter/发送 → 排队，不打断模型
    enqueueCurrent();
    return;
  }
  if (!text && !state.images.length && !state.files.length) return;
  const imgs = state.images.slice();
  const files = state.files.slice();
  inputEl.value = "";
  autoGrow();
  clearAttachments();
  await runTurnStream(text, imgs, files);
}

// 断点续跑：「⚡ 继续生成」按钮 → 与手敲「继续」完全等价，复用正常发送路径
async function continueTurn() {
  if (state.busy) {
    // 罕见竞态（按钮在的话回合基本已结束）：按队列语义排队
    state.queue.push({
      id: `q${Date.now()}${Math.random().toString(36).slice(2, 6)}`,
      sid: state.sid,
      text: "继续", imgs: [], files: [],
    });
    renderQueue();
    toast("已排队，本回合结束后自动发送");
    return;
  }
  await runTurnStream("继续", [], []);
}

// ---------------- 输入队列（生成中排队，参考 DeepSeek Harness inbox）----------------
// 队列在前端：撤回编辑/删除纯本地；「立即发送」走 /api/steer 注入运行中的回合
//（步边界生效，模型不中断）；回合结束自动依次续发。

function enqueueCurrent() {
  const text = inputEl.value.trim();
  if (!text && !state.images.length && !state.files.length) return;
  state.queue.push({
    id: `q${Date.now()}${Math.random().toString(36).slice(2, 6)}`,
    sid: state.sid,        // 归属会话：切换后只回该会话才续发/注入
    text,
    imgs: state.images.slice(),
    files: state.files.slice(),
  });
  inputEl.value = "";
  autoGrow();
  clearAttachments();
  renderQueue();
  toast("已排队，本回合结束后自动发送；点 ✈ 可立即注入");
}

function renderQueue() {
  const dock = $("queue-dock");
  if (!dock) return;
  dock.innerHTML = state.queue.map((q) =>
    `<div class="queue-item" data-id="${q.id}">` +
    `<span class="q-text">${esc(q.text || "[附件]")}</span>` +
    `<span class="q-actions">` +
    `<button class="q-btn" data-act="steer" title="立即发送（注入正在进行的回答，不打断）">✈</button>` +
    `<button class="q-btn" data-act="edit" title="撤回继续编辑">✎</button>` +
    `<button class="q-btn q-del" data-act="del" title="删除">🗑</button>` +
    `</span></div>`).join("");
}

async function steerQueued(id) {
  const i = state.queue.findIndex((q) => q.id === id);
  if (i < 0) return;
  const q = state.queue[i];
  if (q.sid !== state.sid) {
    return toast("该消息属于另一条对话，切回去再注入", true);
  }
  const r = await api("/api/steer", { text: q.text });
  if (r.error === "idle") {
    // 回合恰好结束了 → 退化为普通发送（带附件原样走）
    state.queue.splice(i, 1);
    renderQueue();
    await runTurnStream(q.text, q.imgs, q.files);
    return;
  }
  if (r.error) return toast(r.error, true);
  state.queue.splice(i, 1);
  renderQueue();
  // 用户气泡由后端 user 事件统一渲染（实时与回放同一路径）
}

function editQueued(id) {
  const i = state.queue.findIndex((q) => q.id === id);
  if (i < 0) return;
  const q = state.queue[i];
  state.queue.splice(i, 1);
  renderQueue();
  inputEl.value = q.text;
  autoGrow();
  inputEl.focus();
  if (q.imgs.length || q.files.length) toast("该条附带的图片/文件不随撤回恢复");
}

function pumpQueue() {
  if (state.busy || !state.queue.length) return;
  const idx = state.queue.findIndex((q) => q.sid === state.sid);
  if (idx < 0) return;              // 队列里都是别的会话的消息：不抢发
  const q = state.queue.splice(idx, 1)[0];
  renderQueue();
  runTurnStream(q.text, q.imgs, q.files);
}

// 刷新后接管进行中的回合：后端断开连接不再打断（事件照记），前端轮询
// /api/live 拿事件快照做增量渲染，接上刷新前的画面直到回合结束。
// first = boot 时拿到的首个快照，作为增量基线（该部分尚未被回放渲染）。
// fallbackTurns = 接管前回合恰好结束时要补画的最后一轮（正常为空数组）。
async function attachLive(first, fallbackTurns = []) {
  setBusy(true);
  abortCtl = new AbortController();
  streamSid = state.sid;
  streamDetached = false;
  const turn = newAssistantTurn();
  let drawn = 0;
  let prev = (first && first.events) || [];
  let fails = 0;
  const sleep = (ms) => new Promise((res) => setTimeout(res, ms));
  while (true) {
    await sleep(600);
    let r;
    try {
      r = await fetch("/api/live").then((x) => x.json());
      fails = 0;
    } catch {
      if (++fails > 20) {
        const el = turn.ensureText();
        el.dataset.raw = (el.dataset.raw || "") + "\n[错误] 与服务端失去联系，回合可能仍在后台运行";
        turn.renderStream();
        break;
      }
      continue;
    }
    if (!r.busy) break;
    const evs = r.events || [];
    for (let i = 0; i < evs.length; i++) {
      const ev = evs[i];
      if (i < prev.length) {
        // 旧位置：RecordingEmitter 对 text/think 逐 token 合并，只有变长的
        // 那个（同位置、同类型）需要补增量，其余早已渲染过且不可变。
        // 注意不能只看「新增的下标」——两快照间只变长不加新事件时数量不变。
        const old = prev[i];
        if (old && old.type === ev.type && (ev.type === "text" || ev.type === "think") &&
            (ev.text || "").length > (old.text || "").length) {
          applyEvent(turn, Object.assign({}, ev, { text: ev.text.slice((old.text || "").length) }));
        }
      } else {
        applyEvent(turn, ev);
        drawn++;
      }
    }
    prev = evs;
  }
  if (!drawn && !prev.length && fallbackTurns.length) {
    // 接管前回合已结束且一无所获：收掉空盒，补画真实内容
    turn.root.remove();
    replayTurns(fallbackTurns);
  } else {
    turn.finalize();
  }
  setBusy(false);
  scrollBottom();
  loadSessions();
  pumpQueue();
}

async function runTurnStream(text, imgs, files) {
  closePal();
  addUserMsg(text, imgs, files);
  setBusy(true);
  abortCtl = new AbortController();
  streamSid = state.sid;
  streamDetached = false;

  const turn = newAssistantTurn();
  try {
    const resp = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: text || "看一下我附带的内容",
        images: imgs.map((i) => i.b64),
        files: files.map((f) => f.path),
      }),
      signal: abortCtl.signal,
    });
    if (!resp.ok || !resp.body) throw new Error(`HTTP ${resp.status}`);

    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let idx;
      while ((idx = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, idx).trim();
        buf = buf.slice(idx + 1);
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line); } catch { continue; }
        if (streamDetached) continue;      // 已切走：事件由 /api/live 接管渲染
        applyEvent(turn, ev);
      }
    }
  } catch (e) {
    if (!streamDetached) {
      const el = turn.ensureText();
      if (e.name === "AbortError") {
        el.dataset.raw = (el.dataset.raw || "") + "\n\n[⏹ 已打断]";
      } else {
        el.dataset.raw = (el.dataset.raw || "") + `\n[错误] ${e.message}`;
      }
      turn.renderStream();
    }
  } finally {
    if (streamDetached) {
      // 孤儿流收尾：回合在后端照常落盘，列表刷新一下（标题/排序）即可；
      // 绝不能动全局 busy/pump——那是新会话视图的状态
      streamDetached = false;
      loadSessions();
      return;
    }
    turn.finalize();
    setBusy(false);
    scrollBottom();
    loadSessions();          // 标题/排序可能变了
    pumpQueue();             // 队列非空 → 自动续发下一条（串行，直到排空）
  }
}

// 统一的流事件处理：实时接收与历史回放共用同一套渲染
// 步骤时间戳：epoch 毫秒 → 「19:05:12.345」（本地时区，毫秒三位）
function fmtClock(ms) {
  const d = new Date(ms);
  const p = (n, l = 2) => String(n).padStart(l, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}.${p(d.getMilliseconds(), 3)}`;
}

function applyEvent(turn, ev) {
  // 除了心跳本身，任何真实事件都说明「生成阶段的等待」结束了，先收掉进度条，
  // 否则它会一直挂在回合末尾，看起来像还在等。
  if (ev.type !== "thinking" && turn.clearWaiting) turn.clearWaiting();
  switch (ev.type) {
    case "step":
      turn.addStep(ev.n);
      break;
    case "step_time": {
      // 每步时间窗（参考 DeepSeek Harness：真实开始/结束时刻 + 耗时 + TTFT）。
      // 出现在该步工具卡片之后、下一步分隔线之前——正是这个步的实际跨度。
      const m = document.createElement("div");
      m.className = "meta";
      const tag = ev.step ? `第 ${ev.step} 步 · ` : "";
      let line = `${tag}⏱ ${fmtClock(ev.t_start)} → ${fmtClock(ev.t_end)} · ${ev.duration}s`;
      if (ev.ttft != null) line += ` · TTFT ${ev.ttft}s`;
      m.textContent = line;
      m.title = `开始 ${fmtClock(ev.t_start)}（epoch 毫秒 ${ev.t_start}）\n` +
                `结束 ${fmtClock(ev.t_end)}（epoch 毫秒 ${ev.t_end}）`;
      turn.root.appendChild(m);
      break;
    }
    case "thinking":
      turn.updateWaiting(ev.elapsed || 0, ev.chars || 0);
      break;
    case "think": {
      const el = turn.ensureThink();
      el.textContent += ev.text;
      break;
    }
    case "text": {
      turn.closeThink();
      turn.appendRaw(ev.text);
      const el = turn.ensureText();
      el.dataset.raw = (el.dataset.raw || "") + ev.text;
      turn.renderStream();
      break;
    }
    case "tool_call":
      turn.addToolCard(ev.name, ev.args);
      break;
    case "tool_result": {
      const cards = turn.root.querySelectorAll("details.tool");
      const card = cards[cards.length - 1];
      if (card) turn.finishTool(card, ev.name, ev.text, ev.dt, ev.images);
      break;
    }
    case "stats":
      if (ev.gen_tokens != null) {
        const m = document.createElement("div");
        m.className = "meta";
        // 多步回合里每一步都各报一次：不标「第几步」的话，界面上会出现好几个
        // 长得一样的 token 行，谁也认不出哪个是生成 9000 字符工具参数那一步。
        const tag = ev.step ? `第 ${ev.step} 步 · ` : "";
        m.textContent = tag + `prompt ${ev.prompt_tokens ?? "?"} · 生成 ${ev.gen_tokens} tokens` +
          (ev.duration ? ` · ${ev.duration}s` : "");
        turn.root.appendChild(m);
      }
      break;
    case "usage": {
      // 累计口径：包含重试、warmup、长文精读/摘要/外脑子请求；
      // 后端没返回 usage 的请求只计次数、不算 token，行尾明确标出来。
      const t = ev.turn || {};
      const all = ev.total || {};
      const m = document.createElement("div");
      m.className = "meta";
      const miss = t.missing_usage ? ` · ${t.missing_usage} 次未返回 usage（未计入）` : "";
      m.textContent = `本回合累计 ${t.requests ?? 0} 次调用 · prompt ${t.prompt_tokens ?? 0}` +
        ` · 生成 ${t.gen_tokens ?? 0} · 合计 ${t.total_tokens ?? 0} tokens${miss}` +
        ` ｜ 会话累计 ${all.requests ?? 0} 次 · ${all.total_tokens ?? 0} tokens`;
      turn.root.appendChild(m);
      break;
    }
    case "notice": {
      const el = turn.ensureText();
      el.dataset.raw = (el.dataset.raw || "") + `\n${ev.text}`;
      turn.renderStream();
      // 「继续生成」按钮的判据：只认最终收尾的两种——
      // 「回答中断：」= 自动重试用尽/不可重试断流的收尾（续写进行中的那条
  // 带「自动续写」字样，不匹配）；「已打断本轮回答」= 用户手动停止。
      if (ev.text.includes("回答中断：") || ev.text.includes("已打断本轮回答")) {
        turn.interrupted = true;
      }
      break;
    }
    case "user":
      // 步边界注入的引导消息（输入队列「立即发送」）——实时与历史回放同一路径
      addUserMsg(ev.text || "", [], []);
      break;
    case "confirm":
      askConfirm(ev);
      break;
    case "error":
      toast(ev.text, true);
      break;
    case "done":
      break;
  }
  scrollBottom();
}

// ---------------- 执行确认 ----------------
function askConfirm(ev) {
  const modal = $("confirm-modal");
  $("confirm-q").textContent = ev.question || "(未提供参数信息)";
  modal.classList.remove("hidden");
  const done = () => {
    modal.classList.add("hidden");
    $("btn-confirm-yes").onclick = null;
    $("btn-confirm-all").onclick = null;
    $("btn-confirm-no").onclick = null;
  };
  const reply = (approved, allowRest) => {
    done();
    api("/api/confirm", { id: ev.id, approved, allow_rest: allowRest })
      .then((r) => { if (!r.ok) toast("确认已过期（可能超时），工具未执行", true); })
      .catch((e) => toast("确认失败: " + e.message, true));
  };
  $("btn-confirm-yes").onclick = () => reply(true, false);
  $("btn-confirm-all").onclick = () => reply(true, true);
  $("btn-confirm-no").onclick = () => reply(false, false);
  toast("有工具等待确认，请查看弹窗");
}

// ---------------- 二次确认（清空 / 删除对话） ----------------
let ynResolve = null;

// 比原生 confirm 好看、也跟工具确认弹窗同一套视觉；返回 Promise<boolean>
function askYesNo(title, body, okText = "确定") {
  $("yn-title").textContent = title;
  $("yn-body").textContent = body;
  $("yn-ok").textContent = okText;
  $("yesno-modal").classList.remove("hidden");
  return new Promise((resolve) => { ynResolve = resolve; });
}

function closeYesNo(ok) {
  const modal = $("yesno-modal");
  if (!modal || modal.classList.contains("hidden")) return false;
  modal.classList.add("hidden");
  const r = ynResolve;
  ynResolve = null;
  if (r) r(ok);
  return true;
}

$("yn-ok").onclick = () => closeYesNo(true);
$("yn-cancel").onclick = () => closeYesNo(false);
$("yesno-modal").addEventListener("click", (e) => {
  if (e.target.id === "yesno-modal") closeYesNo(false);
});

// ---------------- 附件 ----------------
function renderAttachments() {
  attachRow.innerHTML = "";
  state.images.forEach((im, i) => {
    const el = document.createElement("div");
    el.className = "attach";
    el.innerHTML = `<img src="data:image/png;base64,${im.b64}" alt="">` +
      `<span>${esc(im.name)}</span><button title="移除" aria-label="移除">✕</button>`;
    el.querySelector("button").onclick = () => { state.images.splice(i, 1); renderAttachments(); };
    attachRow.appendChild(el);
  });
  state.files.forEach((f, i) => {
    const el = document.createElement("div");
    el.className = "attach";
    el.innerHTML = `<span>${fileIcon(f.name)} ${esc(f.name)}</span>` +
      '<button title="移除" aria-label="移除">✕</button>';
    el.querySelector("button").onclick = () => { state.files.splice(i, 1); renderAttachments(); };
    attachRow.appendChild(el);
  });
}

function clearAttachments() {
  state.images = [];
  state.files = [];
  renderAttachments();
}

function addImageAttachment(name, b64) {
  state.images.push({ name, b64 });
  renderAttachments();
}

function addFileAttachment(name, path) {
  if (state.files.some((f) => f.path === path)) return;
  state.files.push({ name, path });
  renderAttachments();
}

function fileToB64(file) {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => resolve(String(r.result).split(",")[1]);
    r.onerror = reject;
    r.readAsDataURL(file);
  });
}

// ---------------- 剪贴板 ----------------
async function grabClipboard() {
  try {
    const r = await api("/api/paste");
    if (r.error) { toast(r.error, true); return; }
    if (r.image) {
      addImageAttachment(r.cache ? r.cache.split(/[\\/]/).pop() : "剪贴板截图.png", r.image);
      toast("已附加剪贴板截图");
    }
    (r.files || []).forEach((p) => addFileAttachment(p.split(/[\\/]/).pop(), p));
    if ((r.files || []).length) toast(`已附加 ${r.files.length} 个剪贴板文件`);
    if (!r.image && !(r.files || []).length) toast("剪贴板里没有图片或文件");
  } catch (e) {
    toast("读剪贴板失败: " + e.message, true);
  }
}

async function pollPending() {
  try {
    const r = await api("/api/pending");
    (r.items || []).forEach((item) => {
      if (item.error) { toast(item.error, true); return; }
      if (item.image) addImageAttachment("Alt+V截图.png", item.image);
      (item.files || []).forEach((p) => addFileAttachment(p.split(/[\\/]/).pop(), p));
      if (item.image || (item.files || []).length) toast("Alt+V：剪贴板内容已带入对话");
    });
  } catch { /* 忽略 */ }
}

// 并行会话运行态轮询：会话列表上的「生成中 ●」「已完成 ✦」徽标。
// 状态签名没变就不重画，避免每 2.5s 重置列表的悬停/滚动状态。
let statusesSig = "";
async function pollStatuses() {
  try {
    const r = await api("/api/statuses");
    const st = r.statuses || {};
    // 本页打开之后新建的会话（或后台刚落盘的）不在 state.sessions 里，
    // 而徽标挂在 renderSessionList(state.sessions) 上——列表不更新，
    // 「后台完成 ✦」就永远没地方显示（实测坑：新会话在后台跑完不出徽标）。
    // 所以会话集合变了就重拉一次列表；平时仍只用本地状态重画，
    // 不动悬停/滚动，也不打断搜索结果。
    const ids = Object.keys(st);
    const known = new Set(state.sessions.map((s) => s.id));
    const listChanged = ids.length !== state.sessions.length
      || ids.some((i) => !known.has(i));
    if (listChanged) {
      const lr = await api("/api/sessions").catch(() => null);
      if (lr) {
        state.sessions = lr.sessions || [];
        state.sid = (lr.current && lr.current.id) || state.sid;
      }
    }
    const sig = JSON.stringify(st);
    if (sig === statusesSig && !listChanged) return;
    statusesSig = sig;
    state.statuses = st;
    // 搜索态下不能用全量列表覆盖结果（原来每 2.5s 会把搜索结果冲掉）；
    // 会话集合变了才重搜一次，保证结果不过期，也不打断列表交互。
    if (state.searchQuery) {
      if (listChanged) runSearch(state.searchQuery);
    } else {
      renderSessionList(state.sessions, state.sid);
    }
  } catch { /* 忽略 */ }
}

// ---------------- 设置 ----------------

// 「使用中」胶囊前的对勾：选中信号的主力，绿胶囊 + 对勾一个色系
const CHECK_SVG =
  '<svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true">' +
  '<path d="M4.5 12.5l5 5L19.5 7" fill="none" stroke="currentColor" ' +
  'stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/></svg>';

// 非选中行的「切换」提示箭头：hover 才显形（CSS .m-go），告诉用户整行可点
const SWAP_SVG =
  '<svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true">' +
  '<path d="M4 12h13M13 6l6 6-6 6" fill="none" stroke="currentColor" ' +
  'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>';

// 切换请求进行中的转圈：切换要重建后端实例（可能还要探测 /api/tags），
// 不是瞬时操作，不给反馈就是「点了没反应」
const SPIN_SVG =
  '<svg class="spin" viewBox="0 0 24 24" width="11" height="11" aria-hidden="true">' +
  '<circle cx="12" cy="12" r="8.5" fill="none" stroke="currentColor" ' +
  'stroke-width="2.6" stroke-linecap="round" stroke-dasharray="40 14"/></svg>';

// 给被点的行挂「切换中…」过渡态，返回还原函数（失败路径用；
// 成功路径 loadModels 会整块重画，无需还原）
function markSwitching(row) {
  if (!row) return () => {};
  row.classList.add("switching");
  const tag = row.querySelector(".m-tag");
  const old = tag ? tag.innerHTML : "";
  if (tag) tag.innerHTML = SPIN_SVG + "切换中…";
  return () => {
    row.classList.remove("switching");
    if (tag) tag.innerHTML = old;
  };
}

// 模型配置清单（内置 Ollama + 自定义）。服务端返回的 Key 一律已打码，
// 完整 Key 永远不下发到浏览器。
// opts.refresh=true 才让服务端真正探测本地 Ollama（打开设置默认走缓存，
// 实测探测会把抽屉卡住数秒——刷新交给「刷新」按钮，用户点才算数）。
async function loadModels(opts = {}) {
  try {
    const r = await api("/api/models" + (opts.refresh ? "?refresh=1" : ""));
    state.models = r;
    renderBuiltin(r.builtin || {});
    renderCustom(r.profiles || [], r.active || "");
    $("cfg-path").textContent = r.config_path || "~/.ollama-agent/config.json";
    $("cfg-path").title = r.config_path || "";
    if (r.recovered) toast(r.recovered, true);   // 配置文件坏了要让人知道
  } catch (e) {
    $("builtin-models").innerHTML =
      `<p class="sb-empty">读取失败：${esc(e.message)}<br>后端是旧进程？重启服务再刷</p>`;
  }
}

function renderBuiltin(b) {
  const box = $("builtin-models");
  const note = $("builtin-note");
  const models = b.models || [];
  if (b.stale) {
    // 还从没拉过列表（打开设置不再自动探测）：明说怎么拿，别让人以为是坏了
    note.textContent = "尚未加载本地模型列表：点右上「刷新」获取。";
    note.title = "打开设置不再自动探测本地 Ollama（避免卡顿），点「刷新」手动加载。";
    box.innerHTML = '<p class="sb-empty">未加载——点右上「刷新」</p>';
    return;
  }
  if (b.error) {
    // 本地没开 Ollama 是常态（用中转站的人不需要它），不该渲染成红色错误。
    // 但要说清「为什么空」和「怎么恢复」。
    note.textContent = "没连上本地 Ollama（不影响用自定义模型）。装了 Ollama 后点刷新。";
    note.title = b.error;
    box.innerHTML = '<p class="sb-empty">未发现本地模型</p>';
    return;
  }
  note.textContent = models.length
    ? `${models.length} 个已安装模型（来自 Ollama /api/tags）`
    : "Ollama 在跑，但一个模型都没装：ollama pull <模型名>";
  note.title = "";
  box.innerHTML = models.map((m) => {
    // 选中判据只用服务端下发的权威字段（b.active = cfg.active 是不是内置条目，
    // b.model = 当前生效模型名），不要拿 state.config 自己猜——
    // 两个判据各说各话，就会出现内置和自定义同时亮「使用中」的事故。
    const on = !!(b.active && b.model === m);
    return `<button class="model-row${on ? " on" : ""}" data-ollama-model="${esc(m)}"` +
      ` title="点击切换到这个模型">` +
      `<span class="m-name">${esc(m)}</span>` +
      `<span class="m-tag">${on ? CHECK_SVG + "使用中" : SWAP_SVG + "切换"}</span></button>`;
  }).join("");
}

function renderCustom(rows, activeId) {
  const box = $("custom-models");
  if (!rows.length) {
    box.innerHTML = '<p class="sb-empty">还没有自定义模型。点「＋ 新建」填 /v1 地址和 Key。</p>';
    return;
  }
  box.innerHTML = rows.map((p) => {
    const on = p.id === activeId;
    const where = p.provider === "openai" ? "OpenAI 兼容" : "Ollama";
    const keyState = p.has_key ? `Key ${esc(p.api_key_masked || "")}` : "无 Key";
    return `<div class="model-row${on ? " on" : ""}" data-profile="${esc(p.id)}">` +
      `<button class="m-main" data-activate="${esc(p.id)}" title="点击切换到这个配置">` +
      `<span class="m-name">${esc(p.name)}</span>` +
      `<span class="m-sub">${esc(p.model || "未填模型")} · ${where}</span>` +
      `<span class="m-sub">${esc(p.host || "未填地址")} · ${keyState}</span>` +
      `</button>` +
      `<span class="m-tag">${on ? CHECK_SVG + "使用中" : SWAP_SVG + "切换"}</span>` +
      `<button class="icon-btn m-edit" data-edit="${esc(p.id)}" title="编辑">` +
      `<svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true"><path d="M4 20h4L20 8l-4-4L4 16v4z" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linejoin="round"/></svg>` +
      `</button></div>`;
  }).join("");
}

// ---------------- 新建 / 编辑表单 ----------------

const mf = { open: false, id: "", hasKey: false, ctx: 0 };

function openModelForm(p) {
  mf.open = true;
  mf.id = (p && p.id) || "";
  mf.hasKey = !!(p && p.has_key);
  mf.ctx = (p && p.num_ctx) || 0;
  $("model-form-title").textContent = p ? "编辑模型配置" : "新建自定义模型";
  $("mf-name").value = (p && p.name) || "";
  $("mf-provider").value = (p && p.provider) || "openai";
  $("mf-host").value = (p && p.host) || "";
  $("mf-key").value = "";
  $("mf-key").placeholder = mf.hasKey
    ? `已保存（${p.api_key_masked || "已打码"}），留空=不修改`
    : "sk-…；Ollama 不需要，可留空";
  $("mf-model").value = (p && p.model) || "";
  $("mf-think").value = (p && p.think) || "off";
  setCtxValue($("mf-ctx"), $("mf-ctx-custom"), mf.ctx);
  $("mf-temp").value = (p && p.temperature != null) ? p.temperature : 0.7;
  $("mf-repeat-penalty").value = (p && p.repeat_penalty != null) ? p.repeat_penalty : "";
  $("mf-repeat-last-n").value = (p && p.repeat_last_n != null) ? p.repeat_last_n : "";
  $("mf-model-options").innerHTML = "";
  $("mf-probe-result").textContent = "";
  $("mf-probe-result").className = "probe-result";
  $("btn-mf-delete").classList.toggle("hidden", !p);
  $("model-form").classList.remove("hidden");
  updateMfNote();
  // 表单展开后必须让用户看见它。原来用 scrollIntoView({block:"nearest"})，
  // 表单在视口下方时它只滚「最小必要距离」，实测保存按钮仍压在底部固定操作栏
  // 下面就看不见 —— 点了「新建」却像没反应。这里显式滚到表单末尾，
  // 把「保存/取消」一并带进视野（用 requestAnimationFrame 等布局算完再滚）。
  requestAnimationFrame(() => {
    const form = $("model-form");
    const body = form.closest(".drawer-body");
    if (!body) return;
    const br = body.getBoundingClientRect();
    const fr = form.getBoundingClientRect();
    body.scrollTop += Math.round(fr.bottom - br.bottom + 16);
  });
  $("mf-name").focus({ preventScroll: true });   // 别让聚焦把刚滚好的位置又拉回顶部
}

function closeModelForm() {
  mf.open = false;
  mf.id = "";
  $("model-form").classList.add("hidden");
}

// 协议说明跟着下拉框走：说清「这个协议认哪些字段」「地址该不该带 /v1」
function updateMfNote() {
  const p = $("mf-provider").value;
  const host = ($("mf-host").value || "").trim();
  const note = $("mf-note");
  if (!note) return;
  if (p === "openai") {
    note.textContent = "走 /v1/chat/completions，需要 API Key；" +
      (host && !/\/v1/i.test(host) ? "当前地址不含 /v1，多数模型服务端会 404。" : "地址要指到 /v1。") +
      " num_ctx / think 不会下发（OpenAI 兼容接口不认，填了也不生效）。";
  } else {
    note.textContent = "走 /api/chat，不需要 Key。别人的 Ollama 要让对方设 " +
      "OLLAMA_HOST=0.0.0.0 才能外部访问。";
  }
  // num_ctx / 重复惩罚只对 Ollama 有意义：显式禁用并说明，免得人填了以为生效
  setCtxCap($("mf-ctx"), $("mf-ctx-custom"), p === "ollama", CTX_OFF_HINT, mf.ctx);
  setFieldCap("mf-think", p === "ollama", CTX_OFF_HINT);
  setFieldCap("mf-repeat-penalty", p === "ollama", CTX_OFF_HINT);
  setFieldCap("mf-repeat-last-n", p === "ollama", CTX_OFF_HINT);
}

async function probeModel() {
  const btn = $("btn-mf-probe");
  const out = $("mf-probe-result");
  const payload = {
    id: mf.id,
    provider: $("mf-provider").value,
    host: $("mf-host").value.trim(),
    api_key: $("mf-key").value.trim(),      // 留空时后端回查已保存的那把
    model: $("mf-model").value.trim(),
  };
  btn.disabled = true;
  out.className = "probe-result";
  out.textContent = "测试中…";
  try {
    const r = await api("/api/models/probe", payload);
    const models = r.models || [];
    $("mf-model-options").innerHTML = models.map((m) =>
      `<option value="${esc(m)}"></option>`).join("");
    if (r.ok) {
      out.className = "probe-result ok";
      out.textContent = r.model_missing ? "⚠ " + (r.note || "连通正常")
                                        : (r.note || `连通正常，${models.length} 个模型`);
      // 只填了地址没填模型名时，顺手把第一个模型填上（省一次手打，能改）
      if (!payload.model && models.length === 1) $("mf-model").value = models[0];
    } else {
      out.className = "probe-result bad";
      // 错误文案必须带下一步：只说「失败」等于让人自己猜
      out.textContent = r.error + (r.hint ? " — " + r.hint : "");
    }
  } catch (e) {
    out.className = "probe-result bad";
    out.textContent = "测试请求本身失败：" + e.message;
  } finally {
    btn.disabled = false;
  }
}

async function saveModel() {
  const name = $("mf-name").value.trim();
  const host = $("mf-host").value.trim();
  const model = $("mf-model").value.trim();
  const provider = $("mf-provider").value;
  // 前置校验：错误要指路，不是只说「必填」
  if (!host) return toast("填服务地址，例如 https://api.deepseek.com/v1", true);
  if (provider === "openai" && !mf.hasKey && !$("mf-key").value.trim()) {
    return toast("OpenAI 兼容协议需要 API Key：粘贴到 Key 框，或先点测试连通性", true);
  }
  if (!model) return toast("填模型名称；不确定就先点「测试连通性」，会列出该站的模型", true);
  // OpenAI 协议下这个控件是禁用的：按打开表单时的原值提交，
  // 既不让「不适用」被当成 0，也不让禁用的控件改动已存配置。
  const ctx = provider === "ollama"
    ? getCtxValue($("mf-ctx"), $("mf-ctx-custom"))
    : mf.ctx;
  if (ctx === null) return toast(CTX_CUSTOM_HINT, true);
  const payload = {
    id: mf.id,
    name: name || model,
    provider,
    host,
    api_key: $("mf-key").value.trim(),
    model,
    think: $("mf-think").value,
    num_ctx: ctx,
    temperature: Number($("mf-temp").value || 0.7),
    repeat_penalty: $("mf-repeat-penalty").value.trim() === ""
      ? null : Number($("mf-repeat-penalty").value),
    repeat_last_n: $("mf-repeat-last-n").value.trim() === ""
      ? null : Number($("mf-repeat-last-n").value),
  };
  try {
    await api("/api/models/save", payload);
    toast("已保存并启用：" + payload.model);
    closeModelForm();
    await loadModels();
    await loadConfig();
  } catch (e) {
    toast("保存失败：" + e.message, true);
  }
}

async function deleteModel() {
  if (!mf.id) return;
  const ok = await askYesNo("删除模型配置",
    "删掉后这个配置里的地址和 Key 就没了，需要重新填。确定删除？");
  if (!ok) return;
  try {
    const r = await api("/api/models/delete", { id: mf.id });
    if (r.error) return toast(r.error, true);
    toast("已删除");
    closeModelForm();
    await loadModels();
    await loadConfig();
  } catch (e) {
    toast("删除失败：" + e.message, true);
  }
}

async function activateModel(id, row) {
  const unmark = markSwitching(row);
  try {
    await api("/api/models/activate", { id });
    await loadModels();
    await loadConfig();
    toast("已切换模型配置");
  } catch (e) {
    unmark();
    toast("切换失败：" + e.message, true);
  }
}

async function activateOllama(name, row) {
  // 点内置模型 = 把当前配置切到 Ollama 原生协议 + 这个模型名。
  // 走 /api/config 而不是 profile：内置条目不落盘成 profile（模型列表是实时的）
  const cur = (state.models && state.models.profiles || []).find(
    (p) => p.id === (state.models.active || ""));
  const host = (cur && cur.provider === "ollama" && cur.host)
    || (state.config && state.config.provider === "ollama" && state.config.host)
    || "http://localhost:11434";
  const unmark = markSwitching(row);
  try {
    await api("/api/config", { provider: "ollama", host, model: name });
    await loadConfig();
    await loadModels();
    toast("已切换到本地模型：" + name);
  } catch (e) {
    unmark();
    toast("切换失败：" + e.message, true);
  }
}

async function loadConfig() {
  try {
    const c = await api("/api/config");
    state.config = c;
    $("chip-model").textContent = c.model || "未选模型";
    // ctx / think 是 Ollama 专有参数：当前协议不下发它们时，
    // 顶栏显示「不适用」而不是一个假的 65536（用户会以为调过上下文）
    const caps = c.caps || {};
    applyCap($("chip-think"), caps.think, "think", c.think);
    applyCap($("chip-ctx"), caps.num_ctx, "ctx",
             c.num_ctx == null ? "默认" : fmtCtxShort(c.num_ctx));
    $("chip-ws").textContent = c.workspace || ".";
    $("sb-meta").textContent = c.workspace || "";
    $("sb-meta").title = c.workspace || "";
    $("set-think").value = c.think || "off";
    // 不适用时下拉显示「不适用」，别让禁用的控件里留个 0 —— 看着像
    // 「上下文=0」，也容易被误当成当前生效值
    setCtxValue($("set-ctx"), $("set-ctx-custom"), caps.num_ctx ? (c.num_ctx ?? 0) : 0);
    // 不支持的参数直接禁用，并说明原因，免得人改了半天以为生效了
    setFieldCap("set-think", caps.think, CTX_OFF_HINT);
    setCtxCap($("set-ctx"), $("set-ctx-custom"), caps.num_ctx,
              CTX_OFF_HINT, c.num_ctx ?? 0);
    $("prefs-cap-note").textContent = caps.think
      ? ""
      : "当前是 OpenAI 兼容协议：think / num_ctx / repeat_penalty / repeat_last_n 属于 Ollama 原生协议，不会下发。要调它们请切换到 Ollama 模型。";
    $("set-temp").value = c.temperature;
    // 留空 = 不下发（用服务端默认）；别把 null 渲成 0，0 在 repeat_last_n 里是「禁用」，语义完全不同
    $("set-repeat-penalty").value = c.repeat_penalty ?? "";
    $("set-repeat-last-n").value = c.repeat_last_n ?? "";
    setFieldCap("set-repeat-penalty", caps.repeat_penalty, CTX_OFF_HINT);
    setFieldCap("set-repeat-last-n", caps.repeat_penalty, CTX_OFF_HINT);
    $("set-steps").value = c.max_steps;
    $("set-confirm").value = c.confirm ? "on" : "off";
    $("set-ws").value = c.workspace || "";
    renderToolPrefs(c);
    renderSkillPrefs();
    syncQuickChips(c);
    syncFeatureGate(c.features || []);
  } catch (e) {
    toast("加载配置失败: " + e.message, true);
  }
}

// 后端能力比对：页面比后端新时，别让人对着搜索框猜「为什么搜不到」
function syncFeatureGate(features) {
  state.noSearchApi = !features.includes("sessions.search");
  searchInput.disabled = state.noSearchApi;
  searchInput.placeholder = state.noSearchApi ? "后端是旧进程，重启后可搜索" : "搜索对话内容";
  searchInput.title = state.noSearchApi
    ? "后端还没有搜索接口：先停止服务（Ctrl+C），再重新启动即可"
    : "";
  // 模型管理接口是这一版新增的：后端没这接口时，「模型管理」标签页会一片空白，
  // 得说清是进程旧了，而不是让人以为功能坏了
  const noModels = !features.includes("models.profiles");
  $("tab-models").classList.toggle("hidden", false);
  if (noModels) {
    $("builtin-models").innerHTML =
      '<p class="sb-empty">后端是旧进程，没有模型管理接口<br>重启服务（Ctrl+C 后重跑启动命令）即可</p>';
    $("custom-models").innerHTML = "";
  }
  // 工具启停同理由：旧进程没有 tools.policy，别让人对着一个空清单猜
  const noToolPolicy = !features.includes("tools.policy");
  $("set-tool-mode").disabled = noToolPolicy;
  if (noToolPolicy) {
    $("set-tool-list").innerHTML =
      '<p class="qp-note">后端是旧进程，没有工具启停接口。重启服务即可。</p>';
  }
}

// 偏好：只提交偏好段。模型参数在「模型管理」里逐配置改，
// 两边都提交会把用户在表单里改了一半的值冲掉。
async function applyConfig() {
  const patch = {
    max_steps: Number($("set-steps").value || 12),
    confirm: $("set-confirm").value === "on",
    workspace: $("set-ws").value.trim(),
    // 工具/技能策略：模式和勾选一起提交。模式是 all/chat/none 时白名单也照传 ——
    // 它正是「以后切回自定义用哪几个」，不传等于每次切模式都清空一次。
    tool_mode: $("set-tool-mode").value,
    tools_enabled: collectTools("#set-tool-list"),
    skills_disabled: [...document.querySelectorAll('#set-skill-list input[data-skill]')]
      .filter((b) => !b.checked).map((b) => b.dataset.skill),
  };
  const caps = (state.config && state.config.caps) || {};
  if (caps.think) patch.think = $("set-think").value;
  if (caps.num_ctx) {
    const ctx = getCtxValue($("set-ctx"), $("set-ctx-custom"));
    if (ctx === null) return toast(CTX_CUSTOM_HINT, true);
    patch.num_ctx = ctx;
  }
  patch.temperature = Number($("set-temp").value || 0.7);
  if (caps.repeat_penalty) {
    // 空串 = 不下发（用服务端默认）。不能 Number("")=0：repeat_last_n 的 0 是「禁用」，语义相反
    patch.repeat_penalty = $("set-repeat-penalty").value.trim() === ""
      ? null : Number($("set-repeat-penalty").value);
    patch.repeat_last_n = $("set-repeat-last-n").value.trim() === ""
      ? null : Number($("set-repeat-last-n").value);
  }
  const wsBefore = (state.config && state.config.workspace) || "";
  try {
    await api("/api/config", patch);
    toast("偏好已应用");
    await loadConfig();
    await loadSkills();          // skills_on 可能随工具模式变了（skill 工具被关）
    const wsNow = (state.config && state.config.workspace) || "";
    loadWorkspaceFiles(wsNow !== wsBefore);
  } catch (e) {
    toast("设置失败: " + e.message, true);
  }
}

// ============ 输入框下方快捷入口 ============
// 模型 / 执行权限 / 工具 / 思考 / 上下文都是一步到位，不进设置抽屉。
// 前三项是「换什么」，后两项是 Ollama 上最常拧的两个旋钮。
// 只有「配置自定义模型」要填地址、Key、模型名，才走完整表单。
let quickKind = "";              // "" | "model" | "perm" | "tools" | "think" | "ctx"
let quickModelsFetched = false;  // 本次页面会话是否已真探测过 /api/tags

const THINK_LABEL = {
  off: "off — 不思考", on: "on — 思考", low: "low", medium: "medium",
  high: "high", max: "max", auto: "auto — 不传该字段",
};

// chip 上只有 4~6 个字的空间，标签另配一套短的
const THINK_SHORT = {
  off: "不思考", on: "思考", low: "思考·低", medium: "思考·中",
  high: "思考·高", max: "思考·最高", auto: "思考·自动",
};
const THINK_DESC = {
  off: "直接回答，省时间也省上下文", on: "按模型默认强度思考",
  low: "轻度推理", medium: "中等推理", high: "较强推理", max: "尽量充分",
  auto: "不下发该字段，交给模型/服务端决定",
};

function syncQuickChips(c) {
  $("quick-model-name").textContent = c.model || "未选模型";
  $("quick-perm-label").textContent = c.confirm ? "默认权限" : "完全访问";
  // 工具 chip 直接显示当前策略：改了模式不用点开就能看见生效了没有
  $("quick-tools-label").textContent = c.tool_mode_label || "全部";
  // 思考模式只对 Ollama 原生协议有意义，别的协议照实说「不适用」而不是显示 off
  const thinkOk = (c.caps || {}).think;
  const think = c.think || "off";
  $("quick-think-label").textContent = thinkOk
    ? (THINK_SHORT[think] || think) : "思考不适用";
  $("quick-think").classList.toggle("off", !thinkOk);
  $("quick-think").title = thinkOk
    ? "思考模式（当前：" + (THINK_SHORT[think] || think) + "）"
    : "思考模式属于 Ollama 原生协议，当前模型接的不是 Ollama";
  // 上下文窗口同理：不支持时明说「不适用」，不拿「默认」冒充一个会生效的值
  const ctxOk = (c.caps || {}).num_ctx;
  const nctx = Number(c.num_ctx) || 0;
  $("quick-ctx-label").textContent = ctxOk
    ? (nctx > 0 ? fmtCtxShort(nctx) : "默认上下文") : "上下文不适用";
  $("quick-ctx").classList.toggle("off", !ctxOk);
  $("quick-ctx").title = ctxOk
    ? "上下文窗口 num_ctx（当前：" + (nctx > 0 ? fmtCtx(nctx) : "服务端默认") + "）"
    : "num_ctx 属于 Ollama 原生协议，当前模型接的不是 Ollama";
}

function closeQuick() {
  quickKind = "";
  const pop = $("quick-pop");
  pop.classList.add("hidden");
  pop.innerHTML = "";
  document.querySelectorAll(".quick-chip").forEach((b) => b.classList.remove("on"));
}

async function openQuick(kind) {
  if (quickKind === kind) return closeQuick();
  quickKind = kind;
  document.querySelectorAll(".quick-chip").forEach((b) => b.classList.remove("on"));
  $("quick-" + kind).classList.add("on");
  $("quick-pop").classList.remove("hidden");
  renderQuick();
  // 第一次打开模型面板才真探测 /api/tags（可能几秒），之后走缓存秒开。
  // 先用已有缓存渲染一版，探测完再重绘，别让用户对着空白等。
  if (kind === "model") {
    await loadModels(quickModelsFetched ? {} : { refresh: true });
    quickModelsFetched = true;
    if (quickKind === "model") renderQuick();
  }
}

function renderQuick() {
  const pop = $("quick-pop");
  if (!pop || !quickKind) return;
  const c = state.config || {};
  if (quickKind === "model") pop.innerHTML = qpModelHtml(c);
  else if (quickKind === "perm") pop.innerHTML = qpPermHtml(c);
  else if (quickKind === "tools") pop.innerHTML = qpToolsHtml(c);
  else if (quickKind === "think") pop.innerHTML = qpThinkHtml(c);
  else if (quickKind === "ctx") pop.innerHTML = qpCtxHtml(c);
}

function qpCtxOptions(cur) {
  const n = Number(cur) || 0;
  let html = CTX_PRESETS.map(([v, label]) =>
    `<option value="${v}"${v === n ? " selected" : ""}>${esc(label)}</option>`).join("");
  // 配置文件里的非标准值单独插一项，保证「打开看一眼」不会把它改成某个预设
  if (n > 0 && !CTX_PRESETS.some(([v]) => v === n)) {
    html = `<option value="${n}" selected>当前值：${esc(fmtCtx(n))}</option>` + html;
  }
  return html;
}

function qpModelHtml(c) {
  const mv = state.models || {};
  const b = mv.builtin || {};
  const caps = c.caps || {};
  const models = b.models || [];
  const profiles = mv.profiles || [];
  const builtinOn = !!b.active;

  const localRows = models.length
    ? models.map((name) => {
      const on = builtinOn && c.model === name;
      return `<button class="qp-row${on ? " on" : ""}" data-qp="pick-ollama" data-name="${esc(name)}" title="切换到 ${esc(name)}">` +
        `<span class="qp-name">${esc(name)}</span><span class="qp-sub">本地</span>` +
        (on ? '<span class="qp-check">✓</span>' : "") + `</button>`;
    }).join("")
    : `<p class="qp-note">${b.stale ? "正在读取本地模型…" : "未发现本地模型（ollama pull <模型名> 后点刷新）"}</p>`;

  const customRows = profiles.length
    ? profiles.map((p) => {
      const on = p.id === mv.active;
      return `<button class="qp-row${on ? " on" : ""}" data-qp="pick-profile" data-id="${esc(p.id)}" title="切换到 ${esc(p.name)}">` +
        `<span class="qp-name">${esc(p.name)}</span>` +
        `<span class="qp-sub">${esc(p.model || "未填模型")}</span>` +
        (on ? '<span class="qp-check">✓</span>' : "") + `</button>`;
    }).join("")
    : `<p class="qp-note">还没有自定义模型。</p>`;

  const thinks = (c.think_values || Object.keys(THINK_LABEL)).map((k) =>
    `<option value="${esc(k)}"${k === c.think ? " selected" : ""}>${esc(THINK_LABEL[k] || k)}</option>`).join("");
  const thinkField = caps.think
    ? `<label class="qp-field">思考模式<select data-qp="think">${thinks}</select></label>`
    : `<label class="qp-field field-off">思考模式<select disabled><option>不适用（当前非 Ollama 协议）</option></select></label>`;
  const ctxField = caps.num_ctx
    ? `<label class="qp-field">上下文窗口<select data-qp="ctx">${qpCtxOptions(c.num_ctx)}</select></label>`
    : `<label class="qp-field field-off">上下文窗口<select disabled><option>不适用</option></select></label>`;

  const head = `<div class="qp-head"><span>选择模型</span><span>` +
    `<button class="qp-btn ghost" data-qp="refresh-models" style="margin:0">刷新</button> ` +
    `<button class="qp-close" data-qp="close" title="关闭">✕</button></span></div>`;
  const left = `<div class="qp-list">` +
    `<div class="qp-sec">本地 Ollama · 已安装</div>${localRows}` +
    `<div class="qp-sec">自定义模型配置</div>${customRows}` +
    `<button class="qp-row action" data-qp="new-model">配置自定义模型（新增 / 编辑）</button>` +
    `</div>`;
  const right = `<div class="qp-detail">` +
    `<div class="qp-title">当前：${esc(c.model || "未选模型")}</div>` +
    `<p class="qp-note">${esc(c.provider || "")} · ${c.confirm ? "执行前确认" : "完全访问"}</p>` +
    thinkField + ctxField +
    `<p class="qp-note">思考模式与上下文窗口改动即时生效。改 num_ctx 会触发模型重载，下一次调用可能变慢。</p>` +
    `<p class="qp-note">切换模型会记住选择，重启后仍显示你挑的那个。</p>` +
    `</div>`;
  return head + `<div class="qp-grid">${left}${right}</div>`;
}

function qpPermHtml(c) {
  const full = !c.confirm;
  return `<div class="qp-head"><span>执行权限</span>` +
    `<button class="qp-close" data-qp="close" title="关闭">✕</button></div>` +
    `<div class="qp-body">` +
    `<p class="qp-note" style="margin-top:0">${full
      ? "当前为完全访问：写/删文件、跑命令、联网会直接执行，不再逐次确认。"
      : "当前为默认权限：所有操作都在安全约束内进行，写/删文件、跑命令、联网前会请求你的允许。"}</p>` +
    `<label class="qp-switch"><span>允许完全访问<small class="qp-note" style="display:block">仅建议在本地可信场景开启</small></span>` +
    `<input type="checkbox" data-qp="perm-full"${full ? " checked" : ""}></label>` +
    `<p class="qp-note">切换即时生效并会记住（重启后仍保持）。</p></div>`;
}

function qpThinkHtml(c) {
  const caps = c.caps || {};
  const head = `<div class="qp-head"><span>思考模式</span>` +
    `<button class="qp-close" data-qp="close" title="关闭">✕</button></div>`;
  if (!caps.think) {
    return head + `<div class="qp-body"><p class="qp-note" style="margin-top:0">` +
      "当前接的是 OpenAI 兼容协议：think 属于 Ollama 原生字段，不会下发。" +
      "要调它请把模型切回 Ollama。</p></div>";
  }
  const cur = c.think || "off";
  const keys = c.think_values || Object.keys(THINK_LABEL);
  const rows = keys.map((k) => {
    const on = k === cur;
    return `<button class="qp-row${on ? " on" : ""}" data-qp="think-pick" data-value="${esc(k)}">` +
      `<span class="qp-name">${esc(THINK_SHORT[k] || k)}</span>` +
      `<span class="qp-sub">${esc(THINK_DESC[k] || "")}</span>` +
      (on ? '<span class="qp-check">✓</span>' : "") + `</button>`;
  }).join("");
  return head + `<div class="qp-body">` +
    `<p class="qp-note" style="margin-top:0">思考过程会让模型先推理再回答，慢一些但复杂任务更稳。改动即时生效。</p>` +
    `<div class="qp-list">${rows}</div></div>`;
}

function qpCtxHtml(c) {
  const caps = c.caps || {};
  const head = `<div class="qp-head"><span>上下文窗口</span>` +
    `<button class="qp-close" data-qp="close" title="关闭">✕</button></div>`;
  if (!caps.num_ctx) {
    return head + `<div class="qp-body"><p class="qp-note" style="margin-top:0">` +
      "当前接的是 OpenAI 兼容协议：num_ctx 属于 Ollama 原生字段，不会下发。" +
      "要调它请把模型切回 Ollama。</p></div>";
  }
  const cur = Number(c.num_ctx) || 0;
  // 配置文件里的非预设值也单独列一行，否则「打开看一眼」就把它显示成某个档位了
  const vals = CTX_PRESETS.map(([v]) => v);
  const extra = cur > 0 && !vals.includes(cur) ? [cur] : [];
  const rows = extra.concat(vals).map((v) => {
    const on = v === cur;
    return `<button class="qp-row${on ? " on" : ""}" data-qp="ctx-pick" data-value="${v}">` +
      `<span class="qp-name">${esc(v > 0 ? fmtCtxShort(v) : "默认")}</span>` +
      `<span class="qp-sub">${esc(CTX_DESC[v] || fmtCtx(v) + " tokens")}</span>` +
      (on ? '<span class="qp-check">✓</span>' : "") + `</button>`;
  }).join("");
  return head + `<div class="qp-body">` +
    `<p class="qp-note" style="margin-top:0">上下文越大能一起读的东西越多，占的显存也越多；` +
    `换档会触发模型重载，下一次调用会慢几秒。</p>` +
    `<div class="qp-list">${rows}</div>` +
    `<div class="qp-custom">` +
    `<input type="number" min="512" step="512" placeholder="自定义 token 数，如 12288" data-qp="ctx-custom-input">` +
    `<button class="qp-btn" data-qp="ctx-apply">应用</button>` +
    `</div></div>`;
}

// ---------------- 工具 / 技能 ----------------
// 模式只是「快捷组合」，真正下发给模型的是勾选出来的白名单：
// 选「只对话」= 把白名单设成那四件，选「自定义」= 按下面的勾选来。
const TOOL_MODES = [
  ["all", "全部工具", "读写、命令、联网全给（默认）"],
  ["chat", "只对话", "只留读文件 / 读图 / 联网"],
  ["custom", "自定义", "按下面勾选的来"],
  ["none", "纯聊天", "一个工具都不给"],
];

function toolRowsHtml(groups, forPrefs) {
  if (!groups || !groups.length) return '<p class="qp-note">没有可配置的工具</p>';
  return groups.map((g) => `<div class="${forPrefs ? "tool-group" : "qp-sec"}">${esc(g.label)}</div>` +
    g.tools.map((t) => `<label class="${forPrefs ? "tool-item" : "qp-row"}" title="${esc(t.description || "")}">` +
      `<input type="checkbox" data-qp="tool-toggle" data-name="${esc(t.name)}"${t.enabled ? " checked" : ""}>` +
      `<span class="${forPrefs ? "t-name" : "qp-name"}">${esc(t.name)}</span></label>`).join("")).join("");
}

function qpToolsHtml(c) {
  const mode = c.tool_mode || "all";
  const rows = TOOL_MODES.map(([k, label, sub]) =>
    `<button class="qp-row${mode === k ? " on" : ""}" data-qp="tool-mode" data-mode="${k}">` +
    `<span class="qp-name">${esc(label)}</span>` +
    `<span class="qp-sub">${esc(sub)}</span>` +
    (mode === k ? '<span class="qp-check">✓</span>' : "") + `</button>`).join("");
  const hint = mode === "custom"
    ? "只下发下面勾选的工具。"
    : `当前是「${esc((TOOL_MODES.find((m) => m[0] === mode) || [0, mode])[1])}」，` +
      "下面勾选在切到「自定义」后生效。";
  return `<div class="qp-head"><span>工具与技能</span>` +
    `<button class="qp-close" data-qp="close" title="关闭">✕</button></div>` +
    `<div class="qp-grid"><div class="qp-list">` +
    `<div class="qp-sec">模式</div>${rows}` +
    `<p class="qp-note">关掉的工具不会把 schema 下发给模型 —— 这是省上下文的主要来源。</p></div>` +
    `<div class="qp-detail"><div class="qp-title">单独开关</div>` +
    `<p class="qp-note" style="margin-top:2px">${hint}</p>` +
    `<div class="tool-list">${toolRowsHtml(c.tool_groups, false)}</div>` +
    `<button class="qp-btn ghost" data-qp="tools-more">更多工具与技能设置</button>` +
    `</div></div>`;
}

function collectTools(scopeSel) {
  return [...document.querySelectorAll(scopeSel + ' input[data-qp="tool-toggle"]')]
    .filter((b) => b.checked).map((b) => b.dataset.name);
}

function renderToolPrefs(c) {
  const list = $("set-tool-list");
  if (!list) return;
  list.innerHTML = toolRowsHtml(c.tool_groups, true);
  $("set-tool-mode").value = c.tool_mode || "all";
  const mode = c.tool_mode || "all";
  $("tool-mode-note").textContent = mode === "custom"
    ? "只把下面勾选的工具下发给模型。"
    : `当前模式「${(TOOL_MODES.find((m) => m[0] === mode) || [0, mode])[1]}」会覆盖下面的勾选；`
      + "勾选任何一项会自动切到「自定义」。";
}

function renderSkillPrefs() {
  const box = $("set-skill-list");
  if (!box) return;
  const on = state.skillsOn !== false;
  if (!skillsCache.length) {
    box.innerHTML = state.skillsLoaded
      ? '<p class="qp-note">这个工作空间没有技能。</p>'
      : '<p class="qp-note">加载中…</p>';
  } else {
    box.innerHTML = skillsCache.map((s) =>
      `<label class="tool-item${s.enabled === false ? " off" : ""}" title="${esc(s.desc || "")}">` +
      `<input type="checkbox" data-skill="${esc(s.name)}"${s.enabled === false ? "" : " checked"}>` +
      `<span class="t-name">${esc(s.display || s.name)}</span>` +
      `<span class="t-tag">${esc(s.name)}</span></label>`).join("");
  }
  box.classList.toggle("disabled", !on);
  $("skill-note").textContent = on
    ? "只有「名字 + 一句话描述」进系统提示词，正文由模型按需加载。关掉的技能不会出现在索引里。"
    : "当前 «skill» 工具已关闭：技能索引不再注入，模型也没有办法加载正文。"
      + "要恢复请把工具模式改回「全部」或勾上 skill。";
}

async function quickPatch(patch, note) {
  try {
    await api("/api/config", patch);
    await loadConfig();            // 内部会 syncQuickChips，chip 文字跟着变
    // 工具模式变了 → skill 可能被一起关掉，技能清单的置灰状态要跟着变
    if ("tool_mode" in patch || "tools_enabled" in patch) await loadSkills();
    if (note) toast(note);
    renderQuick();
  } catch (e) {
    toast("设置失败：" + e.message, true);
  }
}

function openSettingsWithModelForm() {
  $("btn-settings").click();      // 打开抽屉并刷新配置 / 模型清单
  switchTab("models");
  openModelForm(null);
}

// 能力标记：不适用时 chip 保留参数名并标注，而不是留个假数值。
// 名字必须留着——两个都写「不适用」的话，看不出哪个是 think 哪个是 ctx。
function applyCap(el, supported, label, value) {
  if (!el) return;
  el.textContent = supported ? `${label} ${value}` : `${label} 不适用`;
  el.classList.toggle("cap-off", !supported);
  el.title = supported ? "" : "当前后端协议不支持该参数（不会下发，改了也没用）";
}

function setFieldCap(id, supported, reason) {
  const el = $(id);
  if (!el) return;
  el.disabled = !supported;
  el.title = supported ? "" : reason;
  // 占位符跟着走：禁用的框里写「0 = 服务端默认」会让人以为还能调。
  // select 没有 placeholder（禁用态由 setCtxCap 插入的「不适用」选项负责）
  if (el.tagName !== "SELECT") {
    if (el.dataset.ph0 === undefined) el.dataset.ph0 = el.placeholder || "";
    el.placeholder = supported ? el.dataset.ph0 : "不适用";
  }
  const field = el.closest(".field");
  if (field) field.classList.toggle("field-off", !supported);
}

// 协议不支持 num_ctx / think 时给的理由：要包含「为什么」和「怎么才能改」。
// 只写「不支持」的话，用户只能靠猜。
const CTX_OFF_HINT =
  "当前是 OpenAI 兼容协议：think / num_ctx / repeat_penalty / repeat_last_n " +
  "属于 Ollama 原生协议的 options 参数，不会下发。" +
  "要调它们请把「协议类型」切回 Ollama。";

// 标签页切换：模型管理 / 偏好
function switchTab(name) {
  document.querySelectorAll(".set-tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === name));
  $("tab-models").classList.toggle("hidden", name !== "models");
  $("tab-prefs").classList.toggle("hidden", name !== "prefs");
  $("btn-apply").classList.toggle("hidden", name !== "prefs");
}

// ---------------- / 技能 · @ 文件 面板 ----------------
const palEl = $("palette");
const pal = { open: false, mode: "", items: [], flat: [], sel: 0, start: -1, query: "" };
const COMMANDS = [
  { kind: "cmd", name: "/reset", label: "/reset", desc: "清空对话历史" },
  { kind: "cmd", name: "/think off", label: "/think off", desc: "关闭思考（默认）" },
  { kind: "cmd", name: "/think on", label: "/think on", desc: "开启思考" },
];
let skillsCache = [];

async function loadSkills() {
  try {
    const d = await api("/api/skills");
    // 后端会连「已关闭」的技能一起返回（带 enabled=false）：设置面板要能重新勾上，
    // 「查不到」和「被关掉」在界面上必须能区分，否则用户会以为技能丢了。
    skillsCache = d.skills || [];
    state.skillsOn = d.skills_on !== false;
  } catch {
    skillsCache = [];
  }
  state.skillsLoaded = true;
  renderSkillPrefs();
}

function closePal() {
  pal.open = false;
  palEl.classList.add("hidden");
}

function renderPal() {
  const groups = [];
  if (pal.mode === "/") {
    const q = pal.query.toLowerCase();
    const cmds = COMMANDS.filter((c) =>
      !q || c.name.includes(q) || c.desc.toLowerCase().includes(q));
    const hit = [], miss = [];
    // 被关掉的技能不出现在 / 面板里：列出来点了也加载不了，只会让人以为坏了
    for (const s of skillsCache.filter((x) => x.enabled !== false)) {
      const n = s.name.toLowerCase(), d = (s.display || "").toLowerCase();
      (q && !(n.startsWith(q) || d.startsWith(q) || n.includes(q) || d.includes(q)
        || (s.desc || "").toLowerCase().includes(q)) ? miss : hit).push(s);
    }
    if (cmds.length) groups.push(["指令", cmds]);
    if (hit.length || miss.length) {
      groups.push(["技能", hit.concat(miss).map((s) => ({
        kind: "skill", name: s.name, label: s.display || s.name, desc: s.desc,
      }))]);
    }
  } else {
    groups.push(["上下文文件（选中即附到下一条消息）", pal.items.map((f) => ({
      kind: "file", path: f.path, label: f.name, desc: f.path,
    }))]);
  }
  pal.flat = [];
  let html = "";
  for (const [title, list] of groups) {
    html += `<div class="pal-head">${esc(title)} (${list.length})</div>`;
    for (const it of list) {
      const i = pal.flat.length;
      pal.flat.push(it);
      html += `<div class="pal-item${i === pal.sel ? " sel" : ""}" data-i="${i}">` +
        `<span class="pal-name">${esc(it.label)}</span>` +
        `<span class="pal-desc">${esc(it.desc || "")}</span></div>`;
    }
  }
  if (!pal.flat.length) html = '<div class="pal-empty">无匹配</div>';
  palEl.innerHTML = html;
  palEl.classList.remove("hidden");
  pal.open = true;
  palEl.querySelectorAll(".pal-item").forEach((el) => {
    el.onmousedown = (e) => { e.preventDefault(); pickPal(Number(el.dataset.i)); };
    el.onmousemove = () => {
      const i = Number(el.dataset.i);
      if (i !== pal.sel) { pal.sel = i; renderPal(); }
    };
  });
}

function pickPal(i) {
  const it = (pal.flat || [])[i];
  if (!it) return;
  const v = inputEl.value;
  const pos = inputEl.selectionStart ?? v.length;
  const before = v.slice(0, pal.start);
  const after = v.slice(pos);
  if (it.kind === "cmd") {
    inputEl.value = (before + it.name).trimStart();
    closePal();
    sendMessage();
    return;
  }
  if (it.kind === "skill") {
    inputEl.value = before + "/skill " + it.name + " " + after;
    closePal();
    inputEl.focus();
    return;
  }
  if (it.kind === "file") {
    addFileAttachment(it.label, it.path);
    inputEl.value = before + after;
    closePal();
    inputEl.focus();
    toast("已添加上下文: " + it.label);
  }
}

let fileTimer = null;
function fetchFiles(q) {
  clearTimeout(fileTimer);
  fileTimer = setTimeout(async () => {
    try {
      const r = await api("/api/files?q=" + encodeURIComponent(q));
      pal.items = r.files || [];
    } catch { pal.items = []; }
    renderPal();
  }, 120);
}

function autoGrow() {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + "px";
}

inputEl.addEventListener("input", (e) => {
  autoGrow();
  if (e.isComposing) return;
  const pos = inputEl.selectionStart ?? inputEl.value.length;
  const before = inputEl.value.slice(0, pos);
  const m = before.match(/(^|\s)([/@])([^\s@/]*)$/);
  if (!m) { closePal(); return; }
  pal.mode = m[2];
  pal.query = m[3];
  pal.start = pos - m[2].length - m[3].length;
  pal.sel = 0;
  if (pal.mode === "/") renderPal();
  else fetchFiles(pal.query);
});

// ---------------- 全局点击：代码复制 / 预览 / 图片 ----------------
document.addEventListener("click", (e) => {
  const t = e.target;
  if (!(t instanceof Element)) return;

  // 模型管理：编辑按钮优先于「切换」—— 编辑按钮在行内，
  // 先判它，否则点铅笔也会顺带切过去（渲染后点击穿透到父级行）
  const editBtn = t.closest("[data-edit]");
  if (editBtn) {
    const pid = editBtn.dataset.edit;
    const row = ((state.models && state.models.profiles) || [])
      .find((p) => p.id === pid);
    if (row) openModelForm(row);
    return;
  }
  const actBtn = t.closest("[data-activate]");
  if (actBtn) {
    activateModel(actBtn.dataset.activate, actBtn.closest(".model-row"));
    return;
  }
  const ollamaBtn = t.closest("[data-ollama-model]");
  if (ollamaBtn) {
    activateOllama(ollamaBtn.dataset.ollamaModel, ollamaBtn.closest(".model-row"));
    return;
  }

  const act = t.closest("[data-act]")?.dataset.act;

  // 历史对话：删除按钮优先于打开
  const delBtn = t.closest("[data-del]");
  if (delBtn) {
    e.stopPropagation();
    deleteSession(delBtn.dataset.del);
    return;
  }
  const openBtn = t.closest("[data-open]");
  if (openBtn) {
    openSessionById(openBtn.dataset.open);
    return;
  }

  if (act === "copy-code" || act === "wrap-code" || act === "expand-code") {
    const card = t.closest(".code-card");
    const code = card.querySelector("pre code");
    if (act === "copy-code") copyText(code.textContent, t);
    else if (act === "wrap-code") card.classList.toggle("wrap");
    else if (act === "expand-code") {
      openCodePreview(card.querySelector(".code-lang").textContent, code.textContent);
    }
    return;
  }

  if (act === "copy-msg") {
    const msg = t.closest(".msg");
    const rawEl = msg.querySelector("[data-raw]");
    const text = (msg.dataset.rawAll || "")
      || (rawEl ? rawEl.dataset.raw : "")
      || (msg.querySelector(".bubble")?.textContent || "");
    copyText(text.trim(), t);
    return;
  }

  if (act === "continue-turn") {
    continueTurn();
    return;
  }

  if (act === "preview-file") {
    const card = t.closest(".file-card");
    if (card) openPreview(card.dataset.path, card.querySelector(".fname").textContent);
    return;
  }

  // 树里的「+」：把该文件加进本轮对话上下文（和 @ 选文件走同一个 addFileAttachment，
  // 去重、公告栏 chip、发送时注入附件指引都不用另写一份）
  const wsAdd = t.closest(".ws-add");
  if (wsAdd) {
    const p = wsAdd.dataset.add;
    const before = state.files.length;
    addFileAttachment(p.split("/").pop(), p);
    toast(state.files.length > before
      ? `已加入上下文：${p}` : "这个文件已经在上下文里了");
    return;
  }

  const wsDir = t.closest(".ws-dir");
  if (wsDir) {
    toggleDir(wsDir.dataset.dir);
    return;
  }

  const wsItem = t.closest(".ws-item");
  if (wsItem) {
    openPreview(wsItem.dataset.path, wsItem.querySelector(".nm").textContent);
    return;
  }

  const chip = t.closest(".file-chip");
  if (chip && chip.dataset.path) {
    openPreview(chip.dataset.path, chip.querySelector(".nm").textContent);
    return;
  }

  if (t.tagName === "IMG" && messagesEl.contains(t)) {
    e.preventDefault();
    openViewer(t);
    return;
  }
});

function openCodePreview(lang, text) {
  const modal = $("preview-modal");
  $("pv-name").textContent = "代码 · " + (lang || "text");
  $("pv-meta").textContent = text.split("\n").length + " 行";
  $("pv-icon").textContent = "⌨️";
  $("pv-body").innerHTML = `<pre>${esc(text)}</pre>`;
  $("pv-copy").hidden = false;
  $("pv-copy").onclick = (e) => copyText(text, e.currentTarget);
  $("pv-download").onclick = () => {
    const blob = new Blob([text], { type: "text/plain;charset=utf-8" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "snippet." + (lang === "text" ? "txt" : lang);
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  };
  modal.classList.remove("hidden");
}

// ---------------- 事件绑定 ----------------
btnSend.onclick = sendMessage;
$("queue-dock").addEventListener("click", (e) => {
  const btn = e.target.closest(".q-btn");
  const item = e.target.closest(".queue-item");
  if (!btn || !item) return;
  const id = item.dataset.id;
  if (btn.dataset.act === "steer") steerQueued(id);
  else if (btn.dataset.act === "edit") editQueued(id);
  else if (btn.dataset.act === "del") {
    state.queue = state.queue.filter((q) => q.id !== id);
    renderQueue();
  }
});
inputEl.addEventListener("keydown", (e) => {
  if (pal.open && pal.flat.length) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      pal.sel = (pal.sel + 1) % pal.flat.length;
      renderPal();
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      pal.sel = (pal.sel - 1 + pal.flat.length) % pal.flat.length;
      renderPal();
      return;
    }
    if (e.key === "Enter" || e.key === "Tab") {
      e.preventDefault();
      pickPal(pal.sel);
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      closePal();
      return;
    }
  }
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    sendMessage();
  }
});

$("btn-clip").onclick = grabClipboard;
$("btn-file").onclick = () => $("file-input").click();
$("file-input").onchange = async (e) => {
  for (const file of e.target.files) await handleDroppedFile(file);
  e.target.value = "";
};

// 从路径添加本机文件（不复制）：服务和浏览器同一台机器，
// 拖拽/选文件拿不到真实路径（浏览器安全模型）。
// 主通道是后端 tkinter 弹系统原生文件选择框（点选，不用记不用粘），
// 手动输入路径行只当兜底：Shift+点 📁、或选择器开不成时自动展开
function togglePathAttach(show) {
  const row = $("path-attach");
  row.classList.toggle("hidden", !show);
  if (show) {
    $("path-input").focus();
    $("path-input").select();
  }
}

let _picking = false;
async function browseFiles() {
  if (_picking) return;
  _picking = true;
  try {
    // 对话框里人工挑文件可能很久，超时给足 5 分钟
    const r = await api("/api/pick_file", {}, 5 * 60 * 1000);
    if (r.error) throw new Error(r.error);
    const paths = r.paths || [];
    paths.forEach((p) => addFileAttachment(p.split(/[\\/]/).pop(), p));
    if (paths.length) toast(`已附加 ${paths.length} 个文件（原路径直读）`);
  } catch (e) {
    toast(`文件选择器没开成（${e.message}），改用手动输入`, true);
    togglePathAttach(true);
  } finally {
    _picking = false;
  }
}

async function confirmPathAttach() {
  const raw = $("path-input").value.trim();
  if (!raw) return;
  try {
    const r = await api("/api/attach_path", { path: raw });
    if (r.error) throw new Error(r.error);
    addFileAttachment(r.name, r.path);
    $("path-input").value = "";
    togglePathAttach(false);
  } catch (e) {
    toast(`添加失败: ${e.message}`, true);
  }
}

$("btn-path").onclick = (e) => {
  if (e.shiftKey) { togglePathAttach(true); return; }  // 兜底：手动输入
  browseFiles();                                        // 主通道：系统选择框
};
$("btn-path-cancel").onclick = () => togglePathAttach(false);
$("btn-path-ok").onclick = confirmPathAttach;
$("path-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); confirmPathAttach(); }
  if (e.key === "Escape") {
    e.preventDefault();
    e.stopPropagation();          // 只关输入行，别让全局 Esc 顺手关抽屉/弹窗
    togglePathAttach(false);
  }
});

$("btn-settings").onclick = () => {
  $("settings").classList.remove("hidden");
  loadConfig();
  loadModels();
};
$("btn-close-settings").onclick = () => {
  $("settings").classList.add("hidden");
  closeModelForm();      // 关抽屉时收起表单，下次打开不残留上次编辑的内容
};
$("btn-apply").onclick = applyConfig;
// 在偏好里勾/取消任何工具 → 模式自动切到「自定义」，
// 否则用户勾完了、模式还停在「全部」，改了半天不生效
$("set-tool-list").addEventListener("change", (e) => {
  if (e.target.dataset.qp === "tool-toggle") $("set-tool-mode").value = "custom";
});
$("btn-refresh-models").onclick = () => {
  $("builtin-note").textContent = "刷新中…";
  loadConfig();
  loadModels({refresh: true});      // 「刷新全部」是显式动作，允许真探测
};
$("btn-refresh-builtin").onclick = () => {
  $("builtin-note").textContent = "刷新中…";
  loadModels({refresh: true});
};
$("btn-new-model").onclick = () => openModelForm(null);
$("btn-mf-probe").onclick = probeModel;
$("btn-mf-save").onclick = saveModel;
$("btn-mf-cancel").onclick = closeModelForm;
$("btn-mf-delete").onclick = deleteModel;
$("mf-provider").onchange = updateMfNote;
$("mf-host").oninput = updateMfNote;
// ---- 输入框下方快捷入口的事件 ----
$("quick-model").onclick = () => openQuick("model");
$("quick-perm").onclick = () => openQuick("perm");
$("quick-tools").onclick = () => openQuick("tools");
$("quick-think").onclick = () => openQuick("think");
$("quick-ctx").onclick = () => openQuick("ctx");
$("quick-settings").onclick = () => $("btn-settings").click();

$("quick-pop").addEventListener("click", async (e) => {
  const t = e.target.closest("[data-qp]");
  if (!t) return;
  const act = t.dataset.qp;
  if (act === "close") return closeQuick();
  if (act === "pick-ollama") {
    closeQuick();
    await activateOllama(t.dataset.name, null);
    return;
  }
  if (act === "pick-profile") {
    closeQuick();
    await activateModel(t.dataset.id, null);
    return;
  }
  if (act === "new-model") {
    closeQuick();
    openSettingsWithModelForm();
    return;
  }
  if (act === "refresh-models") {
    await loadModels({ refresh: true });
    quickModelsFetched = true;
    renderQuick();
    return;
  }
  if (act === "think-pick") {
    // 不关浮层：勾选要跟着变，用户能立刻看到生效的是哪一档
    return quickPatch({ think: t.dataset.value }, "思考模式已更新");
  }
  if (act === "ctx-pick") {
    return quickPatch({ num_ctx: Number(t.dataset.value) },
      "上下文窗口已更新（可能触发模型重载）");
  }
  if (act === "ctx-apply") {
    const inp = document.querySelector('#quick-pop [data-qp="ctx-custom-input"]');
    const n = Number(inp && inp.value);
    // 整数字节数校验放前端：后端也能拦，但等到报错再回来改体验太差
    if (!Number.isInteger(n) || n < 512) {
      return toast("请填 512 以上的整数 token 数", true);
    }
    if (n > 1048576) return toast("num_ctx 最大 1,048,576 tokens", true);
    return quickPatch({ num_ctx: n }, "上下文窗口已更新（可能触发模型重载）");
  }
  if (act === "tool-mode") {
    const m = t.dataset.mode;
    if (m === "custom") {
      // 「自定义」= 把当前已启用的那几个固化成白名单，否则切过去会变成一个都不给
      const cur = ((state.config || {}).tool_groups || [])
        .flatMap((g) => g.tools).filter((x) => x.enabled).map((x) => x.name);
      return quickPatch({ tool_mode: "custom", tools_enabled: cur }, "已切到自定义模式");
    }
    return quickPatch({ tool_mode: m }, "工具模式已更新");
  }
  if (act === "tools-more") {
    closeQuick();
    $("btn-settings").click();
    switchTab("prefs");
    return;
  }
});

$("quick-pop").addEventListener("change", (e) => {
  const act = e.target.dataset.qp;
  if (act === "think") return quickPatch({ think: e.target.value }, "思考模式已更新");
  if (act === "ctx") {
    return quickPatch({ num_ctx: Number(e.target.value) },
      "上下文窗口已更新（可能触发模型重载）");
  }
  if (act === "perm-full") {
    return quickPatch({ confirm: !e.target.checked },
      e.target.checked ? "已允许完全访问" : "已恢复执行前确认");
  }
  if (act === "tool-toggle") {
    // 勾任何一个都意味着「按勾选来」，所以顺带切到自定义模式
    return quickPatch({ tool_mode: "custom", tools_enabled: collectTools("#quick-pop") },
      "工具已更新");
  }
});

// 自定义 token 数敲回车就应用，不用挪手去点按钮
$("quick-pop").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  if (e.target.dataset.qp !== "ctx-custom-input") return;
  e.preventDefault();
  const btn = document.querySelector('#quick-pop [data-qp="ctx-apply"]');
  if (btn) btn.click();
});

// 点浮层外 / Esc 关闭；chip 自身不算「外部」，否则刚打开就被关掉
document.addEventListener("click", (e) => {
  if (!quickKind) return;
  if (e.target.closest("#quick-pop") || e.target.closest(".quick-chip")) return;
  closeQuick();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && quickKind) closeQuick();
});

// 两个 num_ctx 下拉的选项由同一份预设生成，事件只绑一次
initCtxControl($("set-ctx"), $("set-ctx-custom"));
initCtxControl($("mf-ctx"), $("mf-ctx-custom"));
document.querySelectorAll(".set-tab").forEach((b) => {
  b.onclick = () => switchTab(b.dataset.tab);
});
// 不要直接绑 loadWorkspaceFiles：onclick 会把 Event 当第一个实参传进去，
// 而 reset 只认「工作空间变了」——刷新时不该丢掉展开状态
$("btn-refresh-files").onclick = () => loadWorkspaceFiles();
$("btn-refresh-sessions").onclick = () => loadSessions();

// 搜索历史对话：输入即搜（220ms 防抖），搜索态下刷新/删除都保持搜索。
// 浏览器密码管理器会把保存的值灌进输入框，且可能派发 input 事件；只有加载完成
// 且输入框正被用户聚焦时，才把 input 当成搜索意图，其余一律回滚显示。
searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  if (!searchReady || document.activeElement !== searchInput) {
    syncSearchUI();    // 显示拉回真值，state.searchQuery 不动
    return;
  }
  const q = searchInput.value;
  state.searchQuery = q.trim();
  btnSearchClear.hidden = !state.searchQuery;
  searchTimer = setTimeout(() => runSearch(q), 220);
});
searchInput.addEventListener("focus", syncSearchUI);
searchInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    clearTimeout(searchTimer);
    runSearch(searchInput.value);   // runSearch 会把 trim 后的词写回 state.searchQuery
  } else if (e.key === "Escape") {
    e.preventDefault();
    e.stopPropagation();          // 别让全局 Esc 顺手关掉侧栏/弹窗
    clearSearch(false);
    searchInput.blur();
  }
});
btnSearchClear.onclick = () => clearSearch();
$("btn-refresh-outline").onclick = () => scrollBottom(true);

// 「回到底部」悬浮按钮：恢复跟随 + 滚底（scroll 事件会顺带收起自己）
$("jump-bottom").onclick = () => scrollBottom(true);
$("pv-close").onclick = () => $("preview-modal").classList.add("hidden");
$("preview-modal").addEventListener("click", (e) => {
  if (e.target.id === "preview-modal") e.currentTarget.classList.add("hidden");
});

$("btn-new-chat").onclick = newChat;
$("btn-reset").onclick = clearCurrentChat;

$("btn-sidebar-toggle").onclick = () => toggleSidebar();
if (btnSidebarOpen) btnSidebarOpen.onclick = () => setSidebar(true);
$("sb-scrim").onclick = () => setSidebar(false);

document.querySelectorAll(".sugg").forEach((b) => {
  b.onclick = () => {
    inputEl.value = b.textContent;
    inputEl.focus();
    autoGrow();
  };
});

// Ctrl+V / Alt+V（页面聚焦时）
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "b") {
    e.preventDefault();
    toggleSidebar();
    return;
  }
  // Ctrl/⌘+K：聚焦历史对话搜索（浏览器默认是地址栏搜索，这里抢过来）
  if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "k") {
    e.preventDefault();
    if (shellEl.classList.contains("sb-collapsed")) setSidebar(true);
    searchInput.focus();
    searchInput.select();
    return;
  }
  if (e.key === "Escape") {
    if (closeYesNo(false)) {
      e.preventDefault();
      return;
    }
    if (!$("preview-modal").classList.contains("hidden")) {
      $("preview-modal").classList.add("hidden");
      e.preventDefault();
      return;
    }
    if (closeViewer()) {
      e.preventDefault();
      return;
    }
    if (!$("settings").classList.contains("hidden")) {
      $("settings").classList.add("hidden");
      e.preventDefault();
      return;
    }
  }
  if (e.altKey && !e.ctrlKey && !e.shiftKey && e.key.toLowerCase() === "v") {
    e.preventDefault();
    grabClipboard();
  }
});

document.addEventListener("paste", async (e) => {
  const items = e.clipboardData?.items || [];
  let got = 0;
  for (const item of items) {
    if (item.type.startsWith("image/")) {
      const file = item.getAsFile();
      if (file) {
        const b64 = await fileToB64(file);
        addImageAttachment(file.name || "粘贴图片.png", b64);
        got++;
      }
    }
  }
  if (got) {
    e.preventDefault();
    toast(`已附加 ${got} 张粘贴图片`);
  }
});

// 拖拽文件进窗口
async function handleDroppedFile(file) {
  if (file.type.startsWith("image/")) {
    const b64 = await fileToB64(file);
    addImageAttachment(file.name, b64);
    return;
  }
  try {
    const b64 = await fileToB64(file);
    const r = await api("/api/upload", { name: file.name, data: b64 });
    if (r.error) throw new Error(r.error);
    addFileAttachment(file.name, r.path);
  } catch (e) {
    toast(`上传 ${file.name} 失败: ${e.message}`, true);
  }
}
["dragover", "drop"].forEach((ev) =>
  document.addEventListener(ev, (e) => {
    e.preventDefault();
    if (ev === "drop") {
      for (const file of e.dataTransfer?.files || []) handleDroppedFile(file);
    }
  })
);

// 窗口变化：从宽变窄时把侧栏收成抽屉
let wasNarrow = window.innerWidth <= 900;
function syncSidebarForWidth() {
  const narrow = window.innerWidth <= 900;
  if (narrow && !wasNarrow) setSidebar(false);
  wasNarrow = narrow;
}
window.addEventListener("resize", syncSidebarForWidth);

// ---------------- 启动 ----------------
// 浏览器有时在本脚本跑完之后才恢复表单值，还可能顺带派发 input 事件；
// load 之前不信任任何 input，load 时把显示同步回搜索态真值。
function markSearchReady() {
  searchReady = true;
  syncSearchUI();
}
if (document.readyState === "complete") markSearchReady();
else window.addEventListener("load", markSearchReady, { once: true });

let savedSb = null;
try { savedSb = localStorage.getItem("sbOpen"); } catch { /* 隐私模式忽略 */ }
if (window.innerWidth <= 900 || savedSb === "0") setSidebar(false);

(async function boot() {
  // 浏览器刷新会把输入框旧值还原回来（autocomplete 也挡不住），
  // 所以开机把搜索态复位成「没在搜索」，并同步界面显示。
  state.searchQuery = "";
  syncSearchUI();
  const r = await loadSessions();
  // 刷新时回合可能还在跑（后端不再因断连打断）：先问 /api/live
  const live = await fetch("/api/live").then((x) => x.json()).catch(() => null);
  const turns = (r && r.current && r.current.turns) || [];
  if (live && live.busy) {
    // 进行中的回合交给 attachLive 从快照完整接管（否则回放+增量会画两份）
    const cut = turns.length && turns[turns.length - 1].role === "assistant"
      ? turns.length - 1 : turns.length;
    replayTurns(turns.slice(0, cut));
    attachLive(live, turns.slice(cut));
  } else if (turns.length) {
    // 后端会自动接上最近一次对话，这里把它画出来
    replayTurns(turns);
  }
})();
loadConfig();
loadSkills();
loadWorkspaceFiles();
setInterval(pollPending, 2500);
setInterval(pollStatuses, 2500);   // 并行会话：列表徽标（生成中/后台完成）
autoGrow();
inputEl.focus();
