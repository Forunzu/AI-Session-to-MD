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
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) throw new Error((data && data.error) || "请求失败: " + r.status);
  return data;
}

function post(url, body) {
  return api(url, { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}) });
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
    <button class="btn ghost" onclick="migrateCurrent()" title="把这个会话迁到别的 CLI 里接着聊">↪ 迁移</button>
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

// ================= 迁移弹窗 =================
let MIG_TARGET = "claude";
let VAULT = { entries: [], rows: {}, home: "" };
let RS_PLAN = null;              // 还原干跑结果，改写要用它的 root_target 当 roots
let JOB_TIMER = null;

function openMigrate() {
  $("migMask").classList.add("show");
  renderMigSrc();
  if (!$("bkDest").value) $("bkDest").value = STATE.backup_dir || "";
  if (!$("rsSrc").value) $("rsSrc").value = STATE.backup_dir || "";
  if (!VAULT.entries.length) loadRegistry();
}

function closeMigrate() { $("migMask").classList.remove("show"); stopPoll(); }

function bindTabs(wrapId, map) {
  const w = $(wrapId);
  w.querySelectorAll("button").forEach(b => {
    b.onclick = () => {
      w.querySelectorAll("button").forEach(x => x.classList.remove("on"));
      b.classList.add("on");
      Object.keys(map).forEach(k => $(map[k]).classList.toggle("on", k === b.dataset.t));
    };
  });
}

function bindSeg(id, cb) {
  const w = $(id);
  w.querySelectorAll("button").forEach(b => {
    b.onclick = () => {
      w.querySelectorAll("button").forEach(x => x.classList.remove("on"));
      b.classList.add("on");
      if (cb) cb(b.dataset.v);
    };
  });
}
function segVal(id) { const b = $(id).querySelector("button.on"); return b ? b.dataset.v : ""; }

async function copyTxt(t) {
  try { await navigator.clipboard.writeText(t); }
  catch (e) {
    const ta = document.createElement("textarea");
    ta.value = t; document.body.appendChild(ta); ta.select();
    document.execCommand("copy"); ta.remove();
  }
  toast("已复制到剪贴板");
}

function arm(btn, label) {
  // 大动作不弹原生 confirm（pywebview 里不一定可靠），改成同一个按钮点两次
  if (btn._armed) {
    clearTimeout(btn._t); btn._armed = false; btn.textContent = btn._orig;
    return true;
  }
  btn._orig = btn._orig || btn.textContent;
  btn._armed = true;
  btn.textContent = label;
  btn._t = setTimeout(() => { btn._armed = false; btn.textContent = btn._orig; }, 20000);
  return false;
}
// ---------- 跨 CLI 续聊 ----------
const MIG_NOTE_TAIL = "工具活动折叠成 <code>〔工具 …〕</code> 文本，不生成真实调用块（缺配对的结果块会让目标 CLI 下一轮直接报错）。只新建文件，不改动任何已有会话。";
const MIG_NOTES = {
  claude: "写入 <code>~/.claude/projects/&lt;项目slug&gt;/</code> 下一个新会话文件，之后 <code>claude --resume</code> 就能在列表里选到它。",
  codex: "写入 <code>~/.codex/sessions/年/月/日/rollout-….jsonl</code>，之后 <code>codex resume &lt;路径&gt;</code> 载入。",
  handoff: "生成 <code>handoff_…</code> 目录（会话记录.md + 交接提示词.txt）到顶部设的输出目录，任何 CLI 都能用。"
};

function migItems() {
  const sel = SESSIONS.filter(s => s.checked);
  if (sel.length) return sel;
  const a = findSession(ACTIVE);
  return a ? [a] : [];
}

function migrateCurrent() {
  const s = findSession(ACTIVE);
  if (!s) { toast("请先在左侧选一个会话"); return; }
  // 预览标题栏点进来就是「迁这一个」：勾选状态会让 migItems 优先取勾选的，先清掉
  if (SESSIONS.some(x => x.checked)) {
    SESSIONS.forEach(x => x.checked = false);
    $("selAll").checked = false;
    renderList(); updateSelCount();
  }
  $("migCwd").value = s.cwd || "";
  $("migOut").innerHTML = "";
  $("migTabs").querySelector('button[data-t="cross"]').click();
  openMigrate();
}

function renderMigSrc() {
  const items = migItems();
  const box = $("migSrc");
  if (!items.length) {
    box.innerHTML = '<span class="fixed">未选择 —— 请在左侧勾选若干会话，或先点开一个会话</span>';
  } else if (items.length === 1) {
    const s = items[0];
    box.innerHTML = `<span class="fixed" title="${esc(s.path)}">${esc(s.title)}</span>
      <span class="badge ${s.source}">${s.source === "claude" ? "Claude" : "Codex"}</span>`;
    if (!$("migCwd").value) $("migCwd").value = s.cwd || "";
  } else {
    box.innerHTML = `<span class="fixed">已勾选 ${items.length} 个会话，将逐个迁移</span>`;
  }
  $("migNote").innerHTML = MIG_NOTES[MIG_TARGET] + " " + MIG_NOTE_TAIL;
  $("fldCwd").style.display = MIG_TARGET === "handoff" ? "none" : "flex";
}

function syncMigScope() {
  const v = $("migScope").value;
  $("migLastN").style.display = v === "last" ? "" : "none";
  $("migCap").style.display = v === "chars" ? "" : "none";
}

async function pickMigCwd() {
  const p = await nativePick();
  if (p) $("migCwd").value = p;
}

async function runMigrate() {
  const items = migItems();
  if (!items.length) { toast("请先选择要迁移的会话"); return; }
  if (MIG_TARGET === "handoff" && !STATE.output_dir) { toast("交接包要写到输出目录，请先在顶部选一个"); return; }
  const btn = $("migGo");
  btn.disabled = true; btn.textContent = "迁移中…";
  try {
    const res = await post("/api/migrate/cross", {
      items: items.map(s => ({ path: s.path, source: s.source, title: s.title,
        project: s.project, cwd: s.cwd, created: s.created, modified: s.modified,
        mode: s.mode })),
      target: MIG_TARGET, scope: $("migScope").value,
      last_n: +$("migLastN").value || 20, char_cap: +$("migCap").value || 60000,
      with_tools: $("migTools").checked, target_cwd: $("migCwd").value.trim(),
      output_dir: STATE.output_dir });
    renderMigResult(res);
    toast(`迁移完成：成功 ${res.ok} 个${res.fail ? "，失败 " + res.fail + " 个" : ""}`);
  } catch (e) {
    toast("迁移失败：" + e.message);
  } finally {
    btn.disabled = false; btn.textContent = "开始迁移";
  }
}

function renderMigResult(res) {
  const box = $("migOut");
  box.innerHTML = "";
  (res.results || []).forEach(r => {
    const row = document.createElement("div");
    row.className = "res-row" + (r.ok ? "" : " bad");
    if (!r.ok) {
      row.innerHTML = `<div class="t">✕ ${esc(r.title || "会话")}</div>
        <div class="p">${esc(r.error || "未知错误")}</div>`;
    } else {
      row.innerHTML = `<div class="t">✓ ${esc(r.title || "会话")}
        <span class="mode-tag">${r.target === "handoff" ? "交接包" : (r.target === "codex" ? "Codex" : "Claude")}${r.turns ? " · " + r.turns + "/" + r.total_turns + " 轮" : ""}</span></div>
        <div class="p">${esc(r.path)}</div>
        <div class="cmd"><code>${esc(r.resume || "")}</code></div>`;
      const cmd = row.querySelector(".cmd");
      const b1 = document.createElement("button");
      b1.className = "btn ghost"; b1.textContent = "复制命令";
      b1.onclick = () => copyTxt(r.resume || "");
      cmd.appendChild(b1);
      const b2 = document.createElement("button");
      b2.className = "btn ghost"; b2.textContent = "复制路径";
      b2.onclick = () => copyTxt(r.path || "");
      cmd.appendChild(b2);
    }
    box.appendChild(row);
  });
}
// ---------- 备份：CLI 目录清单 ----------
const SCOPE_LABEL = { sessions: "仅会话数据", root: "整个根目录（智能排除）", full: "完整不排除" };

async function loadRegistry() {
  const box = $("bkList");
  box.textContent = "正在探测本机 AI CLI…";
  try {
    const r = await api("/api/vault/registry");
    VAULT.entries = r.entries || [];
    VAULT.home = r.home || "";
    VAULT.entries.forEach(e => {
      if (!VAULT.rows[e.key]) {
        VAULT.rows[e.key] = { on: e.key === "claude" || e.key === "codex",
          scope: "sessions", secrets: true, size: "" };
      }
    });
    renderVaultList();
    for (const e of VAULT.entries) {   // 勾上的先把体积填出来，其余点一下再算
      if (VAULT.rows[e.key].on) await measureRow(e.key);
    }
  } catch (e) {
    box.textContent = "读取 CLI 列表失败：" + e.message;
  }
}

function renderVaultList() {
  const box = $("bkList");
  box.innerHTML = "";
  if (!VAULT.entries.length) { box.textContent = "没有探测到 AI CLI 目录"; return; }
  VAULT.entries.forEach(e => {
    const st = VAULT.rows[e.key];
    const row = document.createElement("div");
    row.className = "vault-row" + (st.on ? "" : " off");
    const tag = e.builtin ? '<span class="tag">内置规则</span>'
      : e.custom ? '<span class="tag auto">自定义</span>'
      : e.guessed ? '<span class="tag auto">疑似</span>' : '<span class="tag auto">探测</span>';
    const opts = Object.keys(SCOPE_LABEL).map(v =>
      `<option value="${v}" ${st.scope === v ? "selected" : ""}>${SCOPE_LABEL[v]}</option>`).join("");
    row.innerHTML = `<input type="checkbox" ${st.on ? "checked" : ""}>
      <div class="info">
        <div class="nm">${esc(e.label)} ${tag}</div>
        <div class="p" title="${esc(e.root)}">${esc(e.root)}${e.sessions.length ? " · 会话：" + esc(e.sessions.slice(0, 4).join(" ")) : ""}</div>
      </div>
      <select>${opts}</select>
      ${e.secrets.length ? `<label class="chk" title="${esc(e.secrets.join(" "))}"><input type="checkbox" ${st.secrets ? "checked" : ""}> 带凭证</label>` : ""}
      <span class="sz" title="点击测算体积（智能排除后 / 全部）">${st.size || "点此测体积"}</span>`;
    const cbs = row.querySelectorAll("input[type=checkbox]");
    cbs[0].onchange = () => { st.on = cbs[0].checked; row.classList.toggle("off", !st.on);
      if (st.on && !st.size) measureRow(e.key); };
    if (cbs[1]) cbs[1].onchange = () => { st.secrets = cbs[1].checked; };
    row.querySelector("select").onchange = ev => { st.scope = ev.target.value; };
    row.querySelector(".sz").onclick = () => measureRow(e.key);
    box.appendChild(row);
  });
}

async function measureRow(key) {
  const i = VAULT.entries.findIndex(e => e.key === key);
  if (i < 0) return;
  const e = VAULT.entries[i], st = VAULT.rows[key];
  const cell = $("bkList").children[i] && $("bkList").children[i].querySelector(".sz");
  if (cell) cell.textContent = "测算中…";
  try {
    const m = await api(`/api/vault/size?key=${encodeURIComponent(key)}&root=${encodeURIComponent(e.root)}`);
    st.size = `${m.keep_size} / 全 ${m.size}`;
  } catch (err) { st.size = "算不出"; }
  if (cell) cell.textContent = st.size;
}

function bkEntries() {
  return VAULT.entries.filter(e => VAULT.rows[e.key].on).map(e => ({
    key: e.key, scope: VAULT.rows[e.key].scope,
    include_secrets: VAULT.rows[e.key].secrets }));
}

function bkScopeAllRows(v) {
  if (!v) return;
  const on = VAULT.entries.filter(e => VAULT.rows[e.key].on);
  (on.length ? on : VAULT.entries).forEach(e => VAULT.rows[e.key].scope = v);
  $("bkScopeAll").value = "";
  renderVaultList();
  toast(`已把 ${(on.length ? on : VAULT.entries).length} 个目录设为「${SCOPE_LABEL[v]}」`);
}

function bkPickPreset(v) {
  const keys = v === "all" ? VAULT.entries.map(e => e.key) : (v ? v.split(",") : []);
  VAULT.entries.forEach(e => VAULT.rows[e.key].on = keys.indexOf(e.key) >= 0);
  renderVaultList();
  VAULT.entries.forEach(e => { if (VAULT.rows[e.key].on && !VAULT.rows[e.key].size) measureRow(e.key); });
}

async function addVaultCustom() {
  const p = await nativePick();
  if (!p) return;
  const cur = VAULT.entries.filter(e => e.custom).map(e => ({ label: e.label, root: e.root }));
  cur.push({ label: p, root: p });
  await post("/api/config", { vault_custom: cur });
  await loadRegistry();
  toast("已添加自定义目录：" + p);
}

async function pickBkDest() { const p = await nativePick(); if (p) $("bkDest").value = p; }
async function pickRsSrc() { const p = await nativePick(); if (p) $("rsSrc").value = p; }
async function pickRsHome() { const p = await nativePick(); if (p) $("rsHome").value = p; }
// ---------- 备份：干跑 / 执行 ----------
const SZ_UNIT = { B: 1, KB: 1024, MB: 1048576, GB: 1073741824, TB: 1099511627776 };

function szNum(s) {
  // 后端给的是 "335 MB" 这种字符串，排序时换回字节；直接 parseFloat 会把 GB 排在 MB 后面
  const m = /([\d.]+)\s*([KMGT]?B)/i.exec(s || "");
  return m ? parseFloat(m[1]) * (SZ_UNIT[m[2].toUpperCase()] || 1) : 0;
}

async function bkPlan(quiet) {
  const entries = bkEntries();
  const dest = $("bkDest").value.trim();
  if (!entries.length) { toast("请先勾选要备份的目录"); return null; }
  if (!dest) { toast("请先选择备份到哪个目录"); return null; }
  $("bkDry").innerHTML = '<p class="dry-sum">正在统计…</p>';
  try {
    const p = await post("/api/vault/plan", { action: "backup", dest: dest, entries: entries });
    renderBkDry(p);
    if (!quiet) toast(`干跑完成：${p.files} 个文件 / ${p.size}`);
    return p;
  } catch (e) {
    $("bkDry").innerHTML = `<p class="dry-sum warn hint" style="margin-left:0">${esc(e.message)}</p>`;
    return null;
  }
}

function renderBkDry(p) {
  let h = `<p class="dry-sum">合计 <b>${p.files}</b> 个文件 / <b>${p.size}</b>；
    其中 ${p.skip_files} 个与目标端一致可跳过（${p.skip_size}），本次实际约写入 <b>${p.net_size}</b>。</p>`;
  h += `<table class="dry-table"><tr><th>目录</th><th>范围</th><th>凭证</th>
    <th class="num">文件</th><th class="num">体积</th><th class="num">可跳过</th></tr>`;
  (p.entries || []).forEach(e => {
    h += `<tr><td title="${esc(e.root)}">${esc(e.label)}</td><td>${SCOPE_LABEL[e.scope] || e.scope}</td>
      <td>${e.secrets.length ? esc(e.secrets.join(" ")) : "—"}</td>
      <td class="num">${e.files}</td><td class="num">${e.size}</td><td class="num">${e.skip_files}</td></tr>`;
  });
  h += "</table>";
  if ((p.secrets || []).length) {
    h += `<p class="hint warn" style="margin-left:0">本次会带上凭证：<code>${esc(p.secrets.join("、"))}</code>
      —— 备份盘/云盘泄露等于账号泄露，可在上面逐行关掉「带凭证」。</p>`;
  }
  const top = (p.entries || []).reduce((a, e) => a.concat(e.top || []), [])
    .sort((x, y) => szNum(y.size) - szNum(x.size)).slice(0, 6);
  if (top.length) {
    h += `<p class="hint" style="margin-left:0">最大的几个：` +
      top.map(t => `${esc(t.path)} <b>${esc(t.size)}</b>`).join("　·　") + "</p>";
  }
  $("bkDry").innerHTML = h;
}

async function bkStart() {
  const btn = $("bkGo");
  const p = await bkPlan(true);
  if (!p) { btn._armed = false; btn.textContent = btn._orig || "开始备份"; return; }
  if (!arm(btn, `确认备份 ${p.files} 个文件 / ${p.size}（再点一次）`)) {
    toast("已生成干跑清单，请核对后再点一次开始；备份前请先关掉对应 CLI");
    return;
  }
  try {
    const r = await post("/api/vault/backup", { dest: p.dest, entries: bkEntries(),
      zip: $("bkZip").checked });
    await post("/api/config", { backup_dir: p.dest });
    STATE.backup_dir = p.dest;
    pollJob(r.job_id, "bkProg", j => {
      const z = j.result && j.result.zip;
      toast(`备份${j.state === "done" ? "完成" : j.state === "canceled" ? "已取消" : "出错"}：` +
        `${j.done_files} 个文件 / ${j.done_size}，跳过 ${j.skipped}${z ? "，已打包 zip" : ""}`);
    });
  } catch (e) { toast("备份启动失败：" + e.message); }
}

// ---------- 进度轮询 ----------
function stopPoll() { if (JOB_TIMER) { clearInterval(JOB_TIMER); JOB_TIMER = null; } }

function pollJob(id, boxId, onDone) {
  const box = $(boxId);
  box.classList.add("on");
  box.innerHTML = `<div class="bar"><i style="width:0%"></i></div>
    <div class="txt"><span class="st">准备中…</span><span class="cur"></span>
      <button class="btn ghost">取消</button></div>`;
  box.querySelector("button").onclick = async () => {
    try { await post("/api/vault/cancel", { id: id }); toast("已请求取消，正在收尾…"); }
    catch (e) { toast("取消失败：" + e.message); }
  };
  stopPoll();
  JOB_TIMER = setInterval(async () => {
    let j;
    try { j = await api("/api/vault/job?id=" + encodeURIComponent(id)); }
    catch (e) { stopPoll(); box.querySelector(".st").textContent = "任务丢失：" + e.message; return; }
    box.querySelector(".bar i").style.width = Math.min(100, j.percent) + "%";
    box.querySelector(".st").textContent =
      `${j.percent}% · ${j.done_files}/${j.total_files} 文件 · ${j.done_size}/${j.total_size}` +
      ` · 跳过 ${j.skipped} · 错误 ${j.error_count} · ${j.elapsed}s`;
    box.querySelector(".cur").textContent = j.current || "";
    if (j.state === "running") return;
    stopPoll();
    box.querySelector("button").remove();
    let tail = `<p class="dry-sum" style="margin-top:8px">状态：<b>${j.state}</b> · ${j.done_files} 个文件 / ${j.done_size} · 跳过 ${j.skipped} · 用时 ${j.elapsed}s</p>`;
    if (j.error_count) {
      tail += `<p class="hint warn" style="margin-left:0">${j.error_count} 处错误（最近几条）：<br>` +
        j.errors.slice(-5).map(esc).join("<br>") + "</p>";
    }
    const rw = j.result && j.result.rewrite;
    if (rw) {
      tail += `<p class="hint" style="margin-left:0">路径改写：目录 ${rw.dir_count} 个 ·
        JSONL ${rw.cwd_lines} 行（${rw.cwd_files} 个文件） · history ${rw.history_lines} 行 ·
        .claude.json ${rw.claude_json_keys} 个键，改前都留了 .bak-时间戳。</p>`;
    }
    box.insertAdjacentHTML("beforeend", tail);
    if (onDone) onDone(j);
  }, 500);
}
// ---------- 还原 ----------
function rsOverrides() {
  const o = {};
  document.querySelectorAll("#rsDry input[data-key]").forEach(i => {
    const v = i.value.trim();
    if (v) o[i.dataset.key] = v;
  });
  return o;
}

async function rsPlan(quiet) {
  const src = $("rsSrc").value.trim();
  if (!src) { toast("请先选择备份目录或 zip 包"); return null; }
  if (/\.zip$/i.test(src)) {
    RS_PLAN = null;
    $("rsDry").innerHTML = `<p class="hint" style="margin-left:0">选的是 zip 包，没法先干跑清点。
      点「开始还原」会自动解到临时目录再还原；想先看清单就手动解压后选那个目录。</p>`;
    return null;
  }
  $("rsDry").innerHTML = '<p class="dry-sum">正在读取 manifest 并统计…</p>';
  try {
    const p = await post("/api/vault/plan", { action: "restore", backup_dir: src,
      home: $("rsHome").value.trim(), overrides: rsOverrides(),
      conflict: segVal("rsConflict") });
    RS_PLAN = p;
    renderRsDry(p);
    if (!quiet) toast(`可还原 ${p.files} 个文件 / ${p.size}`);
    return p;
  } catch (e) {
    RS_PLAN = null;
    $("rsDry").innerHTML = `<p class="dry-sum warn hint" style="margin-left:0">${esc(e.message)}</p>`;
    return null;
  }
}

function renderRsDry(p) {
  const m = p.manifest || {};
  let h = `<p class="dry-sum">备份于 <b>${esc(m.created || "—")}</b>，来自
    <b>${esc(m.host || "—")}</b> 的 <code>${esc(p.old_home)}</code>${m.includes_secrets ? "（含凭证）" : ""}；
    共 <b>${p.files}</b> 个文件 / <b>${p.size}</b>，其中 ${p.exists} 个目标端已存在，按当前策略
    <b>${segVal("rsConflict") === "skip" ? "跳过" : "覆盖（先存 .bak）"}</b>。</p>`;
  h += `<table class="dry-table"><tr><th>目录</th><th class="num">文件</th><th class="num">体积</th>
    <th>还原到（可改）</th></tr>`;
  (p.entries || []).forEach(e => {
    h += `<tr><td title="${esc(e.backup_source || "")}">${esc(e.label)}</td>
      <td class="num">${e.files}</td><td class="num">${e.size}</td>
      <td><input data-key="${esc(e.key)}" value="${esc(e.root_target)}"></td></tr>`;
  });
  $("rsDry").innerHTML = h + "</table>";
}

function syncRw() {
  const on = $("rsRw").checked;
  $("rsRwAdd").disabled = !on;
  $("rsRwPlan").disabled = !on;
  if (on && !$("rsMap").children.length) {
    addMapRow(STATE.home ? STATE.home.replace(/\\+$/, "") : "", $("rsHome").value.trim() || "");
  }
}

function addMapRow(from, to) {
  const row = document.createElement("div");
  row.className = "map-row";
  row.innerHTML = `<input placeholder="旧路径，如 E:\\在办项目" value="${esc(from || "")}">
    <span>→</span><input placeholder="新路径，如 D:\\Projects" value="${esc(to || "")}">
    <span class="x" title="删掉这条">×</span>`;
  row.querySelector(".x").onclick = () => row.remove();
  $("rsMap").appendChild(row);
}

function rwMapping() {
  const map = {};
  $("rsMap").querySelectorAll(".map-row").forEach(r => {
    const i = r.querySelectorAll("input");
    const a = i[0].value.trim(), b = i[1].value.trim();
    if (a && b && a !== b) map[a] = b;
  });
  return map;
}

async function rwPlan() {
  const map = rwMapping();
  if (!Object.keys(map).length) { toast("请先填一条「旧路径 → 新路径」"); return null; }
  const p = RS_PLAN || await rsPlan(true);
  if (!p) return null;
  try {
    const r = await post("/api/vault/rewrite/plan", { mapping: map,
      home: $("rsHome").value.trim(), roots: (p.entries || []).map(e => e.root_target) });
    const txt = `按当前映射会改：目录名 ${r.dir_count} 个 · JSONL 的 cwd ${r.cwd_lines} 行（${r.cwd_files} 个文件） · history.jsonl ${r.history_lines} 行 · .claude.json ${r.claude_json_keys} 个键。`;
    $("rsDry").insertAdjacentHTML("beforeend",
      `<p class="hint" style="margin-left:0">${esc(txt)}${r.dir_count + r.cwd_lines + r.history_lines + r.claude_json_keys === 0
        ? " 全是 0 —— 还原前目标端还没有文件属于正常，还原后会按同一映射改。" : ""}</p>`);
    toast(txt);
    return r;
  } catch (e) { toast("改写预览失败：" + e.message); return null; }
}

async function rsStart() {
  const btn = $("rsGo");
  const src = $("rsSrc").value.trim();
  if (!src) { toast("请先选择备份目录或 zip 包"); return; }
  const isZip = /\.zip$/i.test(src);
  const p = isZip ? null : await rsPlan(true);
  if (!isZip && !p) { btn._armed = false; btn.textContent = btn._orig || "开始还原"; return; }
  const rw = $("rsRw").checked ? { enabled: true, mapping: rwMapping() } : null;
  if (rw && !Object.keys(rw.mapping).length) { toast("勾了路径改写但没填映射，请填一条或取消勾选"); return; }
  const label = isZip ? "确认解包并还原（再点一次）"
    : `确认还原 ${p.files} 个文件 / ${p.size}${rw ? " + 路径改写" : ""}（再点一次）`;
  if (!arm(btn, label)) {
    toast(isZip ? "zip 会先解到临时目录，确认请再点一次；还原前请先关掉对应 CLI"
      : "已生成还原清单，请核对「还原到」再点一次；还原前请先关掉对应 CLI");
    return;
  }
  try {
    const r = await post("/api/vault/restore", { backup_dir: src,
      home: $("rsHome").value.trim(), overrides: rsOverrides(),
      conflict: segVal("rsConflict"), rewrite: rw });
    if (r.backup_dir && r.backup_dir !== src) $("rsSrc").value = r.backup_dir;
    pollJob(r.job_id, "rsProg", j => {
      toast(`还原${j.state === "done" ? "完成" : j.state === "canceled" ? "已取消" : "出错"}：` +
        `${j.done_files} 个文件 / ${j.done_size}，跳过 ${j.skipped}`);
    });
  } catch (e) { toast("还原启动失败：" + e.message); }
}

// ---------- 迁移弹窗事件绑定 ----------
bindTabs("migTabs", { cross: "paneCross", vault: "paneVault" });
bindTabs("vaultTabs", { backup: "paneBackup", restore: "paneRestore" });
bindSeg("migTarget", v => { MIG_TARGET = v; renderMigSrc(); });
bindSeg("rsConflict", () => { if (RS_PLAN) renderRsDry(RS_PLAN); });
syncMigScope();
$("migMask").addEventListener("click", e => { if (e.target.id === "migMask") closeMigrate(); });

let _inited = false;
async function boot() {
  if (_inited) return;
  _inited = true;
  await init();
}
window.addEventListener("pywebviewready", boot);
setTimeout(boot, 900);

