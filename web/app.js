// ---------- 状态 ----------
let SESSIONS = [];
let STATE = { sources: [], output_dir: "", defaults: [] };
let FILTER = "all";
let SORT = "mod_desc";        // mod/cre + _desc/_asc
let ACTIVE = null;            // 当前预览的会话 id(path)
let ACTIVE_EVENTS = null;
let EDIT_SOURCES = [];        // 设置弹窗里的临时来源列表
const MODES = { plain: "纯对话", tools: "对话+工具", full: "完整原始" };

const $ = (id) => document.getElementById(id);

async function api(url, opts) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error("请求失败: " + r.status);
  return r.json();
}

function toast(msg) {
  const t = $("toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove("show"), 2600);
}

async function nativePick() {
  // 通过 pywebview 调原生文件夹对话框
  if (window.pywebview && window.pywebview.api && window.pywebview.api.pick_directory) {
    return await window.pywebview.api.pick_directory();
  }
  return prompt("请输入目录完整路径：") || "";
}

// ---------- 初始化 ----------
async function init() {
  try {
    STATE = await api("/api/state");
    setOutPath(STATE.output_dir);
    await loadSessions();
  } catch (e) {
    toast("初始化失败：" + e.message);
  }
}

function setOutPath(p) {
  STATE.output_dir = p || "";
  $("outPath").textContent = p || "（未设置）";
  $("outPath").title = p || "";
}

async function loadSessions() {
  $("count").textContent = "扫描中…";
  const data = await api("/api/sessions");
  SESSIONS = data.map(s => ({ ...s, mode: "tools", checked: false }));
  renderList();
  updateSelCount();
}

async function reload() {
  await loadSessions();
  toast("已重新扫描：共 " + SESSIONS.length + " 个会话");
}
// ---------- 列表渲染 ----------
function sortSessions(arr) {
  const [key, dir] = SORT.split("_");
  const f = key === "cre" ? "created_ts" : "modified_ts";
  const out = arr.slice().sort((a, b) => (a[f] || 0) - (b[f] || 0));
  if (dir === "desc") out.reverse();
  return out;
}

function setSort(v) { SORT = v; renderList(); }

function filtered() {
  const kw = $("search").value.trim().toLowerCase();
  const rows = SESSIONS.filter(s => {
    if (FILTER !== "all" && s.source !== FILTER) return false;
    if (kw) {
      const hay = (s.title + " " + (s.project || "") + " " + (s.created || "") + " " + (s.modified || "") + " " + s.group_label).toLowerCase();
      if (!hay.includes(kw)) return false;
    }
    return true;
  });
  return sortSessions(rows);
}

function esc(t) {
  return (t || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function renderList() {
  const list = $("list");
  const rows = filtered();
  list.innerHTML = "";
  let lastGroup = null;
  if (!rows.length) {
    list.innerHTML = '<div class="empty" style="padding:40px">没有匹配的会话</div>';
    $("count").textContent = "共 " + SESSIONS.length + " 个会话";
    return;
  }
  rows.forEach(s => {
    if (s.group_label !== lastGroup) {
      lastGroup = s.group_label;
      const gl = document.createElement("div");
      gl.className = "group-label";
      gl.textContent = s.group_label;
      list.appendChild(gl);
    }
    const item = document.createElement("div");
    item.className = "item" + (s.id === ACTIVE ? " active" : "");
    const opts = Object.entries(MODES).map(([v, l]) =>
      `<option value="${v}" ${s.mode === v ? "selected" : ""}>${l}</option>`).join("");
    const badge = s.source === "claude" ? "Claude" : "Codex";
    item.innerHTML = `
      <input type="checkbox" ${s.checked ? "checked" : ""} data-id="${esc(s.id)}" class="cb">
      <div class="body">
        <div class="title" title="${esc(s.title)}">${esc(s.title)}</div>
        <div class="meta">
          <span class="badge ${s.source}">${badge}</span>
          ${s.project ? `<span>${esc(s.project)}</span><span>·</span>` : ""}
          <span title="创建时间">创 ${esc((s.created || "").slice(0, 10))}</span><span>·</span>
          <span title="最后改动">改 ${esc((s.modified || "").slice(0, 10))}</span><span>·</span><span>${s.size}</span>
        </div>
        <select class="modesel" data-id="${esc(s.id)}">${opts}</select>
      </div>`;
    item.querySelector(".cb").onclick = (e) => { e.stopPropagation(); setChecked(s.id, e.target.checked); };
    const sel = item.querySelector(".modesel");
    sel.onclick = (e) => e.stopPropagation();
    sel.onchange = (e) => setSessionMode(s.id, e.target.value);
    item.onclick = () => selectSession(s.id);
    list.appendChild(item);
  });
  $("count").textContent = `共 ${SESSIONS.length} 个 · 显示 ${rows.length}`;
}
function findSession(id) { return SESSIONS.find(s => s.id === id); }

function setChecked(id, v) { const s = findSession(id); if (s) s.checked = v; updateSelCount(); }

function setSessionMode(id, v) {
  const s = findSession(id); if (!s) return;
  s.mode = v;
  if (id === ACTIVE) renderPreview();
}

function toggleAll(v) {
  filtered().forEach(s => s.checked = v);
  renderList(); updateSelCount();
}

function batchSetMode(v) {
  const sel = filtered().filter(s => s.checked);
  const targets = sel.length ? sel : filtered();
  targets.forEach(s => s.mode = v);
  renderList();
  if (ACTIVE) renderPreview();
  toast(`已把${sel.length ? "选中的 " + sel.length : "当前 " + targets.length}个会话设为「${MODES[v]}」`);
}

function updateSelCount() {
  const n = SESSIONS.filter(s => s.checked).length;
  $("btnSel").textContent = `导出选中 (${n})`;
  $("btnSel").disabled = n === 0;
}

// ---------- 预览 ----------
async function selectSession(id) {
  ACTIVE = id;
  renderList();
  const s = findSession(id);
  $("conv").innerHTML = '<div class="empty">加载中…</div>';
  try {
    const data = await api(`/api/session?path=${encodeURIComponent(s.path)}&source=${s.source}`);
    ACTIVE_EVENTS = data.events || [];
    renderPreview();
  } catch (e) {
    $("conv").innerHTML = '<div class="empty">读取失败：' + esc(e.message) + '</div>';
  }
}

function renderPreview() {
  const s = findSession(ACTIVE); if (!s) return;
  const badge = s.source === "claude" ? "Claude" : "Codex";
  $("pvHead").innerHTML = `<h2><span class="ttl">${esc(s.title)}</span>
    <span class="mode-tag">导出模式：${MODES[s.mode]}</span>
    <button class="btn primary" onclick="convertCurrent()" title="按当前模式直接导出这一个会话">⬇ 导出本会话</button>
    <button class="btn ghost" onclick="toggleOutline()" title="显示/隐藏指令目录">📑 目录</button></h2>
    <div class="meta"><span class="badge ${s.source}">${badge}</span>
    ${s.project ? `<span>${esc(s.project)}</span>` : ""}<span>创建：${esc(s.created || "—")}</span><span>改动：${esc(s.modified || "—")}</span><span>${s.size}</span></div>`;
  const showTools = s.mode !== "plain";
  const showExtra = s.mode === "full";
  const conv = $("conv");
  conv.innerHTML = "";
  const outlineItems = [];
  (ACTIVE_EVENTS || []).forEach((e, i) => {
    const k = e.kind;
    let div = document.createElement("div");
    if (k === "user" || k === "assistant") {
      div.className = "turn " + k;
      if (k === "user") {
        div.id = "turn-" + i;
        outlineItems.push({ id: "turn-" + i, label: firstLine(e.text) });
      }
      div.innerHTML = `<div class="role">${k === "user" ? "🧑 我" : "🤖 AI"}</div><div class="bubble">${esc(e.text)}</div>`;
    } else if (k === "tool_call" && showTools) {
      div.className = "tool";
      div.innerHTML = `<div class="tool-name">🔧 ${esc(e.name)}</div>${esc(typeof e.input === "string" ? e.input : JSON.stringify(e.input, null, 2))}`;
    } else if (k === "tool_result" && showTools) {
      div.className = "tool result"; div.textContent = "↳ " + (e.output || "");
    } else if (k === "thinking" && showExtra) {
      div.className = "thinking"; div.textContent = "💭 " + e.text;
    } else if (k === "system" && showExtra) {
      div.className = "tool"; div.textContent = "⚙ " + e.text;
    } else { return; }
    conv.appendChild(div);
  });
  if (!conv.children.length) conv.innerHTML = '<div class="empty">该模式下无可显示内容</div>';
  renderOutline(outlineItems);
}

// ---------- 右侧指令目录 ----------
function firstLine(text, cap = 46) {
  const lines = (text || "").split("\n");
  for (let ln of lines) {
    ln = ln.replace(/^[#>\-\*\s`]+/, "").trim();
    if (ln) return ln.length > cap ? ln.slice(0, cap) + "…" : ln;
  }
  return "（空指令）";
}

let _io = null;
function renderOutline(items) {
  const box = $("outline");
  if (_io) { _io.disconnect(); _io = null; }
  if (!items.length) {
    box.innerHTML = '<div class="ol-head">📑 指令目录</div><div class="ol-empty">该模式下没有可导航的指令</div>';
    return;
  }
  let html = `<div class="ol-head">📑 指令目录 <span>(${items.length})</span></div>`;
  items.forEach((it, n) => {
    html += `<div class="ol-item" data-target="${it.id}"><span class="ol-num">${n + 1}</span><span class="ol-txt" title="${esc(it.label)}">${esc(it.label)}</span></div>`;
  });
  box.innerHTML = html;
  box.querySelectorAll(".ol-item").forEach(el => { el.onclick = () => jumpTo(el.dataset.target); });
  setupSpy(items);
}

function jumpTo(id) {
  const t = document.getElementById(id);
  if (t) t.scrollIntoView({ behavior: "smooth", block: "start" });
}

function setupSpy(items) {
  const conv = $("conv");
  _io = new IntersectionObserver(entries => {
    entries.forEach(en => { if (en.isIntersecting) markActive(en.target.id); });
  }, { root: conv, rootMargin: "0px 0px -70% 0px", threshold: 0 });
  items.forEach(it => { const el = document.getElementById(it.id); if (el) _io.observe(el); });
}

function markActive(id) {
  const box = $("outline");
  box.querySelectorAll(".ol-item").forEach(el => el.classList.toggle("on", el.dataset.target === id));
  const on = box.querySelector(".ol-item.on");
  if (on) on.scrollIntoView({ block: "nearest" });
}

function toggleOutline() { document.body.classList.toggle("no-outline"); }
// ---------- 筛选 ----------
$("filter").querySelectorAll("button").forEach(b => {
  b.onclick = () => {
    $("filter").querySelectorAll("button").forEach(x => x.classList.remove("on"));
    b.classList.add("on");
    FILTER = b.dataset.f;
    renderList();
  };
});

// ---------- 输出目录 ----------
async function pickOutput() {
  const p = await nativePick();
  if (p) {
    setOutPath(p);
    await api("/api/config", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ output_dir: p }) });
    toast("输出目录已设为：" + p);
  }
}

// ---------- 导出 ----------
async function doConvert(items) {
  if (!STATE.output_dir) { toast("请先选择输出目录"); return; }
  if (!items.length) { toast("没有要导出的会话"); return; }
  toast(`正在导出 ${items.length} 个会话…`);
  const payload = items.map(s => ({ path: s.path, source: s.source, mode: s.mode,
    title: s.title, project: s.project, created: s.created, modified: s.modified }));
  try {
    const res = await api("/api/convert", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ items: payload, output_dir: STATE.output_dir }) });
    toast(`导出完成：成功 ${res.ok} 个${res.fail ? "，失败 " + res.fail + " 个" : ""} → ${res.output_dir}`);
  } catch (e) { toast("导出失败：" + e.message); }
}
function convertSelected() { doConvert(SESSIONS.filter(s => s.checked)); }
function convertAll() { doConvert(filtered()); }
function convertCurrent() {
  const s = findSession(ACTIVE);
  if (!s) { toast("请先在左侧选择一个会话"); return; }
  doConvert([s]);
}

// ---------- 来源目录设置 ----------
function openSettings() {
  EDIT_SOURCES = STATE.sources.map(s => ({ ...s }));
  renderSources();
  $("mask").classList.add("show");
}
function closeSettings() { $("mask").classList.remove("show"); }

function renderSources() {
  const box = $("srcList");
  box.innerHTML = "";
  EDIT_SOURCES.forEach((s, i) => {
    const type = s.type === "claude" ? "CL" : s.type === "codex" ? "CX" : "?";
    const color = s.type === "claude" ? "var(--claude)" : s.type === "codex" ? "var(--codex)" : "#7a828e";
    const row = document.createElement("div");
    row.className = "src-row";
    row.innerHTML = `<div class="ico" style="background:${color}">${type}</div>
      <div class="info"><div>${esc(s.label || s.type || "自定义目录")}</div>
      <div class="p" title="${esc(s.path)}">${esc(s.path)}</div></div>
      <span class="x" title="移除">×</span>`;
    row.querySelector(".x").onclick = () => { EDIT_SOURCES.splice(i, 1); renderSources(); };
    box.appendChild(row);
  });
}

async function addSource() {
  const p = await nativePick();
  if (p) { EDIT_SOURCES.push({ type: "auto", label: "自定义目录", path: p }); renderSources(); }
}

async function saveSettings() {
  STATE.sources = EDIT_SOURCES;
  await api("/api/config", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sources: EDIT_SOURCES }) });
  closeSettings();
  await loadSessions();
  toast("已保存并重新扫描：共 " + SESSIONS.length + " 个会话");
}

$("mask").addEventListener("click", e => { if (e.target.id === "mask") closeSettings(); });

let _inited = false;
async function boot() {
  if (_inited) return;
  _inited = true;
  await init();
}
window.addEventListener("pywebviewready", boot);
setTimeout(boot, 900);

