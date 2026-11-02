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
  images: [],   // {name, b64}
  files: [],    // {name, path}
  config: {},
  outline: [],  // {el, text}
  sessions: [], // 历史对话元信息
  sid: "",      // 当前对话 id
  noSearchApi: false, // 后端没有 /api/sessions/search（旧进程），搜索直接给重启提示
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

async function api(path, body) {
  const opts = body === undefined
    ? {}
    : { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) };
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

function renderMarkdown(src) {
  const parts = String(src).split(/(```[\s\S]*?```)/g);
  return parts.map((part) => {
    if (part.startsWith("```")) {
      const m = part.match(/^```([\w+#.-]*)\n?([\s\S]*?)```$/);
      return codeBlockHtml(m ? m[2] : part, m ? m[1] : "");
    }
    let s = esc(part);

    // 表格先抽出来做占位，避免被后续行内规则和段落包裹打乱
    const tables = [];
    s = s.replace(/(?:^[ \t]*\|.*\|[ \t]*$)(?:\n[ \t]*\|.*\|[ \t]*$)*/gm, (block) => {
      const html = tableToHtml(block);
      if (!html) return block;
      tables.push(html);
      return `\u0000T${tables.length - 1}\u0000`;
    });

    s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
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
    return s.split(/\n{2,}/).map((b) =>
      b.trim() ? (b.startsWith("<h4>") || b.startsWith("<ul>") || b.startsWith("<div")
        ? b : `<p>${b.replace(/\n/g, "<br>")}</p>`) : ""
    ).join("");
  }).join("");
}

function scrollBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
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

async function loadWorkspaceFiles() {
  try {
    const r = await api("/api/files?q=");
    const files = r.files || [];
    if (!files.length) {
      wsFilesEl.innerHTML = '<p class="sb-empty">工作空间没有文件</p>';
      return;
    }
    wsFilesEl.innerHTML = files.slice(0, 40).map((f) =>
      `<button class="ws-item" data-path="${esc(f.path)}" title="${esc(f.path)}">` +
      `<span class="fi">${fileIcon(f.name)}</span>` +
      `<span class="nm">${esc(f.name)}</span></button>`).join("");
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
  sessionListEl.innerHTML = state.sessions.map((s) =>
    `<div class="ss-item${s.id === currentId ? " active" : ""}" data-id="${esc(s.id)}">` +
    `<button class="ss-main" data-open="${esc(s.id)}" title="${esc(s.title)}">` +
    `<span class="ss-title">${esc(s.title)}</span>` +
    `<span class="ss-time">${fmtWhen(s.updated)}</span></button>` +
    `<button class="ss-del" data-del="${esc(s.id)}" title="删除这条对话" aria-label="删除对话">×</button>` +
    "</div>").join("");
}

async function loadSessions() {
  try {
    const r = await api("/api/sessions");
    state.sid = (r.current && r.current.id) || "";
    state.sessions = r.sessions || [];
    if (searchInput.value.trim()) await runSearch(searchInput.value);
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
  searchInput.value = "";
  btnSearchClear.hidden = true;
  renderSessionList(state.sessions, state.sid);
  if (focus) searchInput.focus();
}

// 列表被局部刷新时（删除/切换/新建），搜索中要重新搜，否则会闪回全量列表
function refreshSessionPane(list) {
  if (searchInput.value.trim()) runSearch(searchInput.value);
  else renderSessionList(list, state.sid);
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
  scrollBottom();
}

async function openSessionById(id) {
  if (state.busy) return toast("正在生成，先点停止", true);
  if (id === state.sid && messagesEl.querySelector(".msg")) return;
  const r = await api("/api/sessions/open", { id });
  if (r.error) return toast(r.error, true);
  state.sid = r.id;
  clearView();
  replayTurns(r.turns);
  await loadSessions();
  closeSidebarIfMobile();
}

async function newChat() {
  if (state.busy) return toast("正在生成，先点停止", true);
  const r = await api("/api/sessions/new", {});
  if (r.error) return toast(r.error, true);
  state.sid = r.id || "";
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
  scrollBottom();
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
  scrollBottom();
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
      if (this.currentText) {
        this.currentText.classList.remove("streaming-cursor");
        this.currentText.innerHTML = renderMarkdown(this.currentText.dataset.raw || "");
      }
      this.root.querySelectorAll(".streaming-cursor")
        .forEach((el) => el.classList.remove("streaming-cursor"));
      this.closeThink();
      this.addActions();
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
  if (abortCtl) abortCtl.abort();        // 断开流式连接（后端断连也会自动停）
  try { await api("/api/stop", {}); } catch { /* 忽略 */ }
  const modal = $("confirm-modal");
  modal.classList.add("hidden");         // 有待确认弹窗也一并关掉（后端已全部拒绝）
  $("btn-confirm-yes").onclick = null;
  $("btn-confirm-all").onclick = null;
  $("btn-confirm-no").onclick = null;
  toast("已打断本轮回答");
}

async function sendMessage() {
  const text = inputEl.value.trim();
  if (state.busy) return;
  if (!text && !state.images.length && !state.files.length) return;
  closePal();

  const imgs = state.images.slice();
  const files = state.files.slice();
  addUserMsg(text, imgs, files);
  inputEl.value = "";
  autoGrow();
  clearAttachments();
  setBusy(true);
  abortCtl = new AbortController();

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
        applyEvent(turn, ev);
      }
    }
  } catch (e) {
    const el = turn.ensureText();
    if (e.name === "AbortError") {
      el.dataset.raw = (el.dataset.raw || "") + "\n\n[⏹ 已打断]";
    } else {
      el.dataset.raw = (el.dataset.raw || "") + `\n[错误] ${e.message}`;
    }
    turn.renderStream();
  } finally {
    turn.finalize();
    setBusy(false);
    scrollBottom();
    loadSessions();          // 标题/排序可能变了
  }
}

// 统一的流事件处理：实时接收与历史回放共用同一套渲染
function applyEvent(turn, ev) {
  switch (ev.type) {
    case "step":
      turn.addStep(ev.n);
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
        m.textContent = `prompt ${ev.prompt_tokens ?? "?"} · 生成 ${ev.gen_tokens} tokens` +
          (ev.duration ? ` · ${ev.duration}s` : "");
        turn.root.appendChild(m);
      }
      break;
    case "notice": {
      const el = turn.ensureText();
      el.dataset.raw = (el.dataset.raw || "") + `\n${ev.text}`;
      turn.renderStream();
      break;
    }
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

// ---------------- 设置 ----------------
async function loadConfig() {
  try {
    const c = await api("/api/config");
    state.config = c;
    $("chip-model").textContent = c.model;
    $("chip-think").textContent = "think " + c.think;
    $("chip-ctx").textContent = "ctx " + (c.num_ctx ?? "默认");
    $("chip-ws").textContent = c.workspace || ".";
    $("sb-meta").textContent = c.workspace || "";
    $("sb-meta").title = c.workspace || "";
    const sel = $("set-model");
    sel.innerHTML = (c.models || []).map((m) =>
      `<option value="${esc(m)}" ${m === c.model ? "selected" : ""}>${esc(m)}</option>`
    ).join("") || `<option value="${esc(c.model)}">${esc(c.model)}</option>`;
    $("set-think").value = c.think;
    $("set-ctx").value = c.num_ctx ?? 0;
    $("set-temp").value = c.temperature;
    $("set-steps").value = c.max_steps;
    $("set-confirm").value = c.confirm ? "on" : "off";
    $("set-ws").value = c.workspace || "";
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
}

async function applyConfig() {
  const patch = {
    model: $("set-model").value,
    think: $("set-think").value,
    num_ctx: Number($("set-ctx").value || 0),
    temperature: Number($("set-temp").value || 0.7),
    max_steps: Number($("set-steps").value || 12),
    confirm: $("set-confirm").value === "on",
    workspace: $("set-ws").value.trim(),
  };
  try {
    const c = await api("/api/config", patch);
    state.config = c;
    toast("设置已应用");
    loadConfig();
    loadWorkspaceFiles();
  } catch (e) {
    toast("设置失败: " + e.message, true);
  }
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
  try { skillsCache = (await api("/api/skills")).skills || []; }
  catch { skillsCache = []; }
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
    for (const s of skillsCache) {
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

  if (act === "preview-file") {
    const card = t.closest(".file-card");
    if (card) openPreview(card.dataset.path, card.querySelector(".fname").textContent);
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

$("btn-settings").onclick = () => {
  $("settings").classList.remove("hidden");
  loadConfig();
};
$("btn-close-settings").onclick = () => $("settings").classList.add("hidden");
$("btn-apply").onclick = applyConfig;
$("btn-refresh-models").onclick = loadConfig;
$("btn-refresh-files").onclick = loadWorkspaceFiles;
$("btn-refresh-sessions").onclick = () => loadSessions();

// 搜索历史对话：输入即搜（220ms 防抖），搜索态下刷新/删除都保持搜索
searchInput.addEventListener("input", () => {
  clearTimeout(searchTimer);
  const q = searchInput.value;
  btnSearchClear.hidden = !q.trim();
  searchTimer = setTimeout(() => runSearch(q), 220);
});
searchInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    clearTimeout(searchTimer);
    runSearch(searchInput.value);
  } else if (e.key === "Escape") {
    e.preventDefault();
    e.stopPropagation();          // 别让全局 Esc 顺手关掉侧栏/弹窗
    clearSearch(false);
    searchInput.blur();
  }
});
btnSearchClear.onclick = () => clearSearch();
$("btn-refresh-outline").onclick = () => scrollBottom();
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
let savedSb = null;
try { savedSb = localStorage.getItem("sbOpen"); } catch { /* 隐私模式忽略 */ }
if (window.innerWidth <= 900 || savedSb === "0") setSidebar(false);

(async function boot() {
  const r = await loadSessions();
  // 后端会自动接上最近一次对话，这里把它画出来
  if (r && r.current && (r.current.turns || []).length) replayTurns(r.current.turns);
})();
loadConfig();
loadSkills();
loadWorkspaceFiles();
setInterval(pollPending, 2500);
autoGrow();
inputEl.focus();
