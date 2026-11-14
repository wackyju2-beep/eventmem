/* eventmem GUI — vanilla on purpose: one static page, no build step, no npm.
   Everything here is a thin wrapper over the daemon's /api/* routes. */

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const mb = (n) => (n / 1e6).toFixed(n > 1e8 ? 0 : 1) + " MB";
const gb = (n) => (n / 1e9).toFixed(2) + " GB";
const dur = (s) => (s == null ? "—" : s < 90 ? Math.round(s) + "s"
  : s < 5400 ? (s / 60).toFixed(0) + "min" : (s / 3600).toFixed(1) + "h");
const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false })
  : "—");

let toastTimer = null;
function toast(msg, kind = "") {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast on " + kind;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.className = "toast"; }, kind === "err" ? 8000 : 3500);
}

async function api(path, body) {
  // Relative on purpose: the page is served at "/" by the daemon and at
  // "/api/plugins/eventmem-gui/" when the dashboard proxies it, and a relative fetch is the
  // only spelling that works for both without a build-time base URL.
  const url = String(path).replace(/^\//, "");
  const opts = { headers: { "Content-Type": "application/json" } };
  if (body !== undefined) { opts.method = "POST"; opts.body = JSON.stringify(body); }
  const res = await fetch(url, opts);
  let data = {};
  try { data = await res.json(); } catch { data = { error: res.statusText }; }
  if (!res.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

/* ------------------------------------------------------------------ tabs */

const TABS = ["overview", "backends", "models", "schedule", "logs"];

function showTab(name) {
  for (const t of TABS) {
    $("tab-" + t).classList.toggle("on", t === name);
    document.querySelector(`nav button[data-tab="${t}"]`).classList.toggle("on", t === name);
  }
  if (name !== "schedule") { backfillGeneration++; clearTimeout(backfillTimer); backfillTimer = null; }
  location.hash = name;
  refresh(name);
}

function refresh(name) {
  const fn = { overview: refreshOverview, backends: refreshBackends,
               models: refreshModels, schedule: refreshSchedule, logs: refreshLogs }[name];
  if (fn) fn().catch((e) => toast(e.message, "err"));
}

document.querySelectorAll("nav button").forEach((b) => {
  b.onclick = () => showTab(b.dataset.tab);
});

/* ------------------------------------------------------------------ health */

async function refreshHealth() {
  try {
    const h = await api("/api/health");
    $("health").textContent = "ok · schema " + h.schema + " · pid " + h.pid;
    $("health").className = "pill ok";
    $("db").textContent = h.db;
  } catch (e) {
    $("health").textContent = "daemon 不可达";
    $("health").className = "pill bad";
  }
}

/* The pause switch: stops the running pass and every future one (persisted in the db) until
   pressed again. The schedule itself is configured on the 调度 tab. */
function paintPause(paused) {
  const btn = $("pause-toggle");
  if (!btn) return;
  btn.dataset.paused = paused ? "1" : "0";
  btn.textContent = paused ? "继续判断" : "暂停全部判断";
  btn.classList.toggle("danger", !paused);
  btn.classList.toggle("primary", paused);
}

$("pause-toggle").onclick = async () => {
  const btn = $("pause-toggle");
  const want = btn.dataset.paused !== "1";
  btn.disabled = true;
  try {
    const r = await api("/api/pause", { paused: want });
    toast(want ? "已暂停：这一轮停下后不再自动开新的一轮" : "已继续：按调度恢复", "ok");
    paintPause(Boolean(r.paused));
    refresh("overview");
  } catch (e) {
    toast(e.message, "err");
  } finally {
    btn.disabled = false;
  }
};

/* ------------------------------------------------------------------ overview */

/* The overview tick: 5 s while idle, 1.5 s while a pass is live, so the numbers move with
   the +1 strip instead of lagging up to five seconds behind every answered pair. */
let overviewDelay = 5000;

/* 数字翻页：只重画变了的位。逗号与没变的位原样留着，视觉上只有那一位翻了一下。 */
function setFlip(el, value) {
  if (!el) return;
  const text = value === null || value === undefined || value === "" ? "—" : String(value);
  if (el.dataset.flip === text) return;
  const before = el.dataset.flip || "";
  el.dataset.flip = text;
  if (!before) {
    el.innerHTML = [...text]
      .map((ch) => `<span class="flip-cell"><span class="to">${esc(ch)}</span></span>`)
      .join("");
    return;
  }
  const width = Math.max(before.length, text.length);
  const a = before.padStart(width, " ");
  const b = text.padStart(width, " ");
  let html = "";
  for (let i = 0; i < width; i += 1) {
    if (b[i] === " ") continue;
    const changed = a[i] !== b[i] && a[i] !== " ";
    html += `<span class="flip-cell">${
      changed ? `<span class="out">${esc(a[i])}</span>` : ""
    }<span class="to${changed ? " on" : ""}">${esc(b[i])}</span></span>`;
  }
  el.innerHTML = html;
  window.setTimeout(() => {
    el.querySelectorAll(".flip-cell .out").forEach((node) => node.remove());
    el.querySelectorAll(".flip-cell .to.on").forEach((node) => node.classList.remove("on"));
  }, 460);
}

async function refreshOverview() {
  const [stats, status, config] = await Promise.all([
    api("/api/stats"), api("/api/status"), api("/api/config")]);
  overviewDelay = status.live ? 1500 : 5000;
  paintPause(Boolean(status.paused));

  setFlip($("n-events"), stats.events == null ? null : stats.events.toLocaleString("en-US"));
  $("n-newest").textContent = stats.newest ? "最新 " + when(stats.newest) : "空库";
  setFlip($("n-edges"), stats.edges == null ? null : stats.edges.toLocaleString("en-US"));
  $("n-pairs").textContent = (stats.pairs ?? 0) + " 对已判";

  /* 待判 = 本轮开始时算出来的预计对数 − 已问；写边会退休链头，真实数量只会更少。
     没有 pass 在跑时这块退回游标口径，别让它显示一个假的 0。 */
  const cursor = status.cursor || {};
  const live = status.live || null;
  const planned = live && typeof live.pairs_planned === "number" ? live.pairs_planned : null;
  const left = live && planned !== null ? Math.max(0, planned - (live.pairs || 0)) : null;
  setFlip($("n-pending"), left == null ? null : left.toLocaleString("en-US"));
  $("n-pending-sub").textContent = live
    ? planned === null
      ? (live.events_total
          ? `第 ${live.event_index || 0}/${live.events_total} 个事件 · 正在数这一轮还要问多少对…`
          : "正在准备这一轮…")
      : `第 ${live.event_index || 0}/${live.events_total} 个事件 · 已判 ${live.pairs} 对（预计）`
    : cursor.behind == null
      ? "无游标"
      : `空闲 · 游标落后 ${cursor.events} 个事件`;

  const kinds = stats.kinds || {};
  const total = Object.values(kinds).reduce((a, b) => a + b, 0) || 1;
  const max = Math.max(1, ...Object.values(kinds));
  $("kinds").querySelector("tbody").innerHTML = Object.keys(kinds).length
    ? Object.entries(kinds).map(([k, n]) => `<tr><td>${esc(k)}</td>
        <td style="width:55%"><div class="bar"><i style="width:${(n / max) * 100}%"></i></div></td>
        <td class="num">${n}</td><td class="num sub">${((n / total) * 100).toFixed(0)}%</td></tr>`).join("")
    : `<tr><td class="sub">还没有事件</td></tr>`;

  $("projects").querySelector("tbody").innerHTML = (stats.projects || []).length
    ? stats.projects.map((p) => `<tr><td>${esc(p.name)}</td><td class="num">${p.events}</td>
        <td class="num sub">${when(p.last)}</td></tr>`).join("")
    : `<tr><td class="sub">还没有项目</td></tr>`;

  const runs = (status.runs || []).slice().reverse();
  $("runs").querySelector("tbody").innerHTML = runs.length
    ? runs.map((r) => `<tr><td class="sub">${when(r.ts)}</td>
        <td>${r.skipped ? esc(r.skipped) : "完成"}</td>
        <td class="num">${r.pairs ?? "—"}</td><td class="num">${r.edges ?? "—"}</td>
        <td class="sub">${esc(r.backend || "")}</td></tr>`).join("")
    : `<tr><td class="sub">这一进程还没跑过 pass</td></tr>`;

  const chain = (config.effective?.decision?.backends) || [];
  $("chain-overview").innerHTML = chain.length
    ? chain.map((n, i) => `<span class="item">${i + 1}. ${esc(n)}</span>`).join("")
    : `<span class="sub">空（用 decision.url 单端点）</span>`;

  /* 抽取失败：那一轮什么都没写。放在概览最上面，不让它只活在日志里。 */
  const failures = stats.failures || { count: 0, last: [] };
  const card = $("notice-card");
  card.style.display = failures.count ? "" : "none";
  if (failures.count) {
    $("notice-count").textContent = `（${failures.count} 次）`;
    $("notices").querySelector("tbody").innerHTML = (failures.last || []).map((r) => `<tr>
        <td class="sub">${when(r.ts)}</td>
        <td>${r.reason === "unparsable" ? "输出无法解析" : "调用失败"}</td>
        <td>${esc(r.detail || "—")}</td>
        <td class="sub">${esc((r.session || "—").slice(0, 10))}</td></tr>`).join("");
  }
}

$("notice-clear").onclick = async () => {
  try {
    const r = await api("/api/notices/clear", {});
    toast(`已清除 ${r.cleared ?? 0} 条警告`, "ok");
    refresh("overview");
  } catch (e) {
    toast(e.message, "err");
  }
};

/* 清空连边：边 + 已判过的对一起删、游标归零；只删边的话重跑会把每一对都跳过。 */
$("edges-clear").onclick = async () => {
  if (!window.confirm("删掉全部边和已判过的对，并让下一轮从头重判？这一步不可撤销。")) return;
  const btn = $("edges-clear");
  btn.disabled = true;
  $("edges-msg").textContent = "清空中…";
  try {
    const r = await api("/api/edges/clear", {});
    $("edges-msg").textContent =
      `已清空 ${r.edges} 条边、${r.pairs} 对判定记录；游标归零，下一轮从头重判`;
    refresh("overview");
  } catch (e) {
    $("edges-msg").textContent = "";
    toast(e.message, "err");
  } finally {
    btn.disabled = false;
  }
};

/* ------------------------------------------------------------------ backends */

const TEMPLATES = {
  cloud: `# 云端 Jev / System One 端点
protocol: systemone
model: jev-latest
base_url: https://api.typesafe.ai
api_key_env: TYPESAFE_API_KEY
license: proprietary
note: 官方托管；没 key 时探活记 needs_key
`,
  lmstudio: `# LM Studio（或任何已装好的本地服务）：我们不启动它
protocol: systemone
model: lev
external: true
base_url: http://127.0.0.1:1234
note: LM Studio 里要先加载一个 decision model
`,
  local: `# 自己托管：daemon 有活时才拉起 llama-server
protocol: systemone
model: lev
weights: ../../models/lev/lev-Q4_K_M.gguf
launch: auto
port: 0
license: apache-2.0
`,
};

let chainDraft = [];
let packData = [];
let packNames = [];

async function refreshBackends() {
  const [packs, config] = await Promise.all([api("/api/packs"), api("/api/config")]);
  packData = packs.packs || [];
  packNames = packData.map((p) => p.name);
  // The server is authoritative on every (re)load; local edits live in `renderChain` only.
  chainDraft = [...((config.effective?.decision?.backends) || [])];
  if (packs.problems?.length) toast("有 pack 加载失败：\n" + packs.problems.join("\n"), "err");
  renderChain();
  renderPacks(packs.dir);
}

function renderChain() {
  const chain = $("chain-editor");
  chain.innerHTML = chainDraft.length
    ? chainDraft.map((n, i) => `<span class="item">${i + 1}. ${esc(n)}
        <button title="上移" data-up="${i}">↑</button>
        <button title="下移" data-down="${i}">↓</button>
        <button title="移除" data-del="${i}">✕</button></span>`).join("")
    : `<span class="sub">空（用 decision.url 单端点）</span>`;
  chain.querySelectorAll("button[data-up]").forEach((b) => b.onclick = () => {
    const i = +b.dataset.up; if (i === 0) return;
    [chainDraft[i - 1], chainDraft[i]] = [chainDraft[i], chainDraft[i - 1]]; renderChain();
  });
  chain.querySelectorAll("button[data-down]").forEach((b) => b.onclick = () => {
    const i = +b.dataset.down; if (i === chainDraft.length - 1) return;
    [chainDraft[i + 1], chainDraft[i]] = [chainDraft[i], chainDraft[i + 1]]; renderChain();
  });
  chain.querySelectorAll("button[data-del]").forEach((b) => b.onclick = () => {
    chainDraft.splice(+b.dataset.del, 1); renderChain();
  });

  const free = packNames.filter((n) => !chainDraft.includes(n));
  $("chain-add").innerHTML = free.length
    ? free.map((n) => `<option>${esc(n)}</option>`).join("")
    : `<option value="">（没有可加的）</option>`;
}

function renderPacks(dir) {
  $("pack-list").innerHTML = packData.length
    ? "" : `<div class="card sub">${esc(dir || "")} 里还没有后端</div>`;
  for (const p of packData) {
    const el = document.createElement("div");
    el.className = "pack";
    const up = p.running === true ? `<span class="tag up">运行中 :${p.port ?? "?"}</span>`
      : p.kind === "self" ? `<span class="tag down">未运行</span>`
      : p.needs_key ? `<span class="tag down">缺 key</span>` : "";
    el.innerHTML = `
      <div class="head">
        <span class="name">${esc(p.name)}</span>
        <span class="tag ${p.kind === "self" ? "self" : ""}">${esc(p.kind)}</span>
        ${p.in_chain ? `<span class="tag chain">在链路</span>` : ""}
        ${up}
        <span style="flex:1"></span>
        <button class="btn ghost" data-act="probe">探活</button>
        ${p.kind === "self" ? `<button class="btn" data-act="start">启动</button>
        <button class="btn" data-act="stop">停止</button>` : ""}
        <button class="btn danger" data-act="delete">删除</button>
      </div>
      <div class="meta" style="margin-top:6px">
        ${esc(p.protocol)} · ${esc(p.model || "?")} · ${esc(p.url || p.weights || "")}
        ${p.license ? " · " + esc(p.license) : ""}
      </div>
      <details style="margin-top:8px"><summary class="sub" style="cursor:pointer">config.yaml</summary>
        <textarea spellcheck="false" data-cfg="1">${esc(p.config || "")}</textarea>
        <div class="row" style="margin-top:8px"><button class="btn primary" data-act="save">保存</button>
          <span class="hint" style="margin:0">保存的就是这份文件本身，注释会原样留下。</span></div>
      </details>`;
    el.querySelector('[data-act="probe"]').onclick = () => probe(p.name);
    el.querySelector('[data-act="delete"]').onclick = () => remove(p.name);
    if (p.kind === "self") {
      el.querySelector('[data-act="start"]').onclick = () => backend(p.name, "start");
      el.querySelector('[data-act="stop"]').onclick = () => backend(p.name, "stop");
    }
    el.querySelector('[data-act="save"]').onclick =
      () => savePack(p.name, el.querySelector("[data-cfg]").value);
    $("pack-list").appendChild(el);
  }
}

async function probe(name) {
  try {
    const r = await api(`/api/backends/${encodeURIComponent(name)}/probe`, {});
    toast(r.ok ? `${name} 可用（${r.protocol}）` : `${name} 不可用：${r.reason || "无响应"}`,
      r.ok ? "ok" : "err");
  } catch (e) { toast(e.message, "err"); }
}

async function backend(name, action) {
  try {
    const r = await api(`/api/backends/${encodeURIComponent(name)}/${action}`, {});
    toast(`${name} ${action}: ${r.error ? r.error : "ok"}`, r.error ? "err" : "ok");
    refreshBackends();
  } catch (e) { toast(e.message, "err"); }
}

async function savePack(name, text) {
  try {
    const r = await api(`/api/packs/${encodeURIComponent(name)}/config`, { text });
    toast(`已保存 ${name}` + (r.warnings?.length ? "\n" + r.warnings.join("\n") : ""), "ok");
    refreshBackends();
  } catch (e) { toast(e.message, "err"); }
}

async function remove(name) {
  if (!confirm(`删除后端 ${name} 的整个目录？`)) return;
  try {
    await api(`/api/packs/${encodeURIComponent(name)}/delete`, {});
  } catch (e) {
    if (!String(e.message).includes("链路")) return toast(e.message, "err");
    if (!confirm(e.message + "\n仍然删除？")) return;
    try { await api(`/api/packs/${encodeURIComponent(name)}/delete`, { force: true }); }
    catch (e2) { return toast(e2.message, "err"); }
  }
  toast(`已删除 ${name}`, "ok");
  refreshBackends();
}

$("chain-add-btn").onclick = () => {
  const name = $("chain-add").value;
  if (name && !chainDraft.includes(name)) { chainDraft.push(name); renderChain(); }
};

$("chain-save").onclick = async () => {
  try {
    await api("/api/config", { decision: { backends: chainDraft } });
    toast("链路已保存（下一次 pass 生效）", "ok");
    refreshBackends();
  } catch (e) { toast(e.message, "err"); }
};

$("new-template").onchange = () => {
  $("new-config").value = TEMPLATES[$("new-template").value] || "";
};
$("new-config").value = TEMPLATES.cloud;

$("new-save").onclick = async () => {
  const name = $("new-name").value.trim();
  if (!name) return toast("先起个名字", "err");
  try {
    await api("/api/packs", { name, text: $("new-config").value });
    toast(`已创建 ${name}`, "ok");
    $("new-name").value = "";
    refreshBackends();
  } catch (e) { toast(e.message, "err"); }
};

/* ------------------------------------------------------------------ models */

let jobTimer = null;

async function refreshModels() {
  const [m, j] = await Promise.all([api("/api/models"), api("/api/job")]);
  const llama = m.llama || {};
  $("llama-line").innerHTML = llama.build
    ? `llama.cpp <b>${esc(llama.build)}</b> · ${esc(llama.asset || "")}<br>${esc(llama.binary || "")}`
    : `<span class="sub">还没装 llama.cpp（点右边按钮，按硬件自动选包）</span>`;

  $("model-table").querySelector("tbody").innerHTML = (m.models || []).length
    ? m.models.map((x) => `<tr><td>${esc(x.repo)}/${esc(x.file)}</td>
        <td class="num">${gb(x.size)}</td></tr>`).join("")
    : `<tr><td class="sub">没有 GGUF</td></tr>`;

  renderJob(j.job);
  if (j.job?.state === "running") startJobPoll();
}

function renderJob(job) {
  if (!job) return;
  const label = { idle: "空闲", running: "进行中", done: "完成", error: "失败" }[job.state] || job.state;
  $("job-phase").textContent = label + (job.phase ? " · " + job.phase : "") +
    (job.error ? " · " + job.error : "");
  const pct = job.total ? Math.min(100, Math.round((job.done / job.total) * 100)) : (job.state === "done" ? 100 : 0);
  $("job-bar").style.width = pct + "%";
  $("job-num").textContent = job.total ? `${mb(job.done)} / ${mb(job.total)}` :
    (job.elapsed ? job.elapsed + "s" : "");
}

function startJobPoll() {
  if (jobTimer) return;
  jobTimer = setInterval(async () => {
    try {
      const { job } = await api("/api/job");
      renderJob(job);
      if (job.state !== "running") {
        clearInterval(jobTimer); jobTimer = null;
        if (job.state === "done") toast("安装完成", "ok");
        else if (job.state === "error") toast("安装失败：" + job.error, "err");
        refreshModels();
      }
    } catch { clearInterval(jobTimer); jobTimer = null; }
  }, 1200);
}

$("install-llama").onclick = async () => {
  try {
    await api("/api/install", { runtime_only: true });
    toast("开始下载 llama.cpp", "ok");
    startJobPoll();
  } catch (e) { toast(e.message, "err"); }
};

$("inst-go").onclick = async () => {
  try {
    await api("/api/install", {
      name: $("inst-name").value.trim() || "lev",
      repo: $("inst-repo").value.trim(),
      quant: $("inst-quant").value.trim(),
      skip_llama: $("inst-skip").checked,
      force: false,
    });
    toast("安装任务已开始（首次要下 2 GB 左右）", "ok");
    startJobPoll();
    if ($("inst-chain").checked) {
      const name = $("inst-name").value.trim() || "lev";
      const cfg = await api("/api/config");
      const chain = [...(cfg.effective?.decision?.backends || [])];
      if (!chain.includes(name)) {
        chain.push(name);
        await api("/api/config", { decision: { backends: chain } });
        toast(`装完会自动把 ${name} 加进链路`, "ok");
      }
    }
  } catch (e) { toast(e.message, "err"); }
};

/* ------------------------------------------------------------------ schedule */


const fillText = (translate, text, ...args) => translate(text).replace(/\{(\d+)\}/g, (m, i) => args[+i] ?? m);
function recalText(r, tr) {
  const f = (text, ...args) => fillText(tr, text, ...args);
  return ['改变', '否定', '重复', '因为', '细化'].map((rel) => f('{0}：新增 {1}，删除 {2}', tr(rel), r.added?.[rel] ?? '—', r.removed?.[rel] ?? '—')).concat([
    f('置信度更新的边：{0}', r.confidence_updates ?? '—'), f('跳过的事件对：{0}', r.skipped ?? '—'), f('未覆盖的 Jev 边：{0}', r.uncovered_jev_edges ?? r.uncovered_edges ?? '—'), f('记录了人工删除的事件对：{0}', r.manual_deletion_pairs ?? '—'), ...(r.legacy_deletions_unknown ? [tr('旧版人工删除记录无法确认。')] : [])]).join('\n');
}
function backfillText(r, tr) {
  return fillText(tr, '历史事件 {0} 条；当前图观察到 {1} 个候选对。该观察值不是最终数量，也不是上限或下限；新边可能改变后续候选。', r.event_count ?? '—', r.observed_pairs ?? '—');
}
function progressText(p, tr) {
  const states = {idle:'尚无补算任务', pending:'等待中', running:'运行中', done:'已结束', error:'失败', completed:'已完成', stopped:'已停止', 'jev-unavailable':'模型不可用', 'jev-error':'模型判定失败'};
  const state = p?.result?.status || p?.state || 'idle';
  return fillText(tr, '补算状态：{0}；已判 {1} 对，新增 {2} 条边', tr(states[state] || state), p?.pairs ?? 0, p?.edges ?? 0) + (p?.error ? '\n' + p.error : '');
}

let scheduleCfg = null;
let scheduleLang = {};
const st = (s) => scheduleLang[s] || s;
const SCHEDULE_ACCEPT = [
  ['margin', '全局 margin'], ['min_conf_change', '改变最低置信度'],
  ['min_conf_negate', '否定最低置信度'], ['min_conf_repeat', '重复最低置信度'],
  ['min_conf_because', '因为最低置信度'], ['min_conf_refine', '细化最低置信度'],
];
let schedulePreviews = {};
let backfillTimer = null;
let backfillGeneration = 0;
let lastBackfill = null;
const scheduleSection = $('tab-schedule');
const marginField = $('f-margin').parentElement;
const acceptCard = document.createElement('div');
acceptCard.className = 'card';
acceptCard.style.marginTop = '14px';
acceptCard.innerHTML = `<h2 data-st="关系接受条件">关系接受条件</h2><div class="grid c3" id="accept-fields"></div>
  <div class="row" style="margin-top:12px"><button class="btn primary" id="accept-save" data-st="保存接受条件"></button>
  <span class="hint" style="margin:0" data-st="保存后，从下一轮 pass 起的连边一律按这里的 margin 与最低置信度判定；已有边不动（要改历史就用下面的重校准预览）。"></span></div>`;
scheduleSection.append(acceptCard);
const recentField = document.createElement('div');
recentField.innerHTML = `<label data-st="Tier2 · 同会话近期事件数"></label><input id="f-recent" type="number" min="0" step="1" value="3">`;
$('f-session').parentElement.after(recentField);
const saveHint = document.createElement('p'); saveHint.className = 'hint';
saveHint.setAttribute('data-st', '保存未来设置会同时保存候选数量和关系规则；历史操作不会保存未来设置。');
$('sched-save').parentElement.before(saveHint);
$('accept-fields').append(marginField);
marginField.querySelector('label').setAttribute('data-st', '全局 margin');
for (const [key, label] of SCHEDULE_ACCEPT.slice(1)) {
  const div = document.createElement('div');
  div.innerHTML = `<label data-st="${esc(label)}">${esc(label)}</label><input id="f-${key}" type="number" min="0" max="1" step="0.01" value="0">`;
  $('accept-fields').append(div);
}
const historyCard = document.createElement('div');
historyCard.className = 'card';
historyCard.style.marginTop = '14px';
historyCard.innerHTML = `<h2 data-st="历史关系重校准">历史关系重校准</h2>
  <p class="hint" data-st="只重判已有概率的关系；无法使用的记录会跳过。预览不会修改历史。"></p>
  <p class="hint" data-st="历史概率格式有歧义，模型来源未知。StartLux 输出条件概率，但不能假设其他后端相同。默认跳过条件形式；仅在确认后勾选，选择只影响条件形式的记录。"></p>
  <label><input type="checkbox" id="recal-conditional"> <span data-st="将条件形式的历史概率按条件概率处理"></span></label>
  <div class="row"><button class="btn" id="recal-preview" data-st="预览历史"></button><button class="btn" id="recal-apply" data-st="应用历史重校准" disabled></button></div><pre id="recal-result"></pre>
  <h2 data-st="历史候选补算">历史候选补算</h2>
  <p class="hint" data-st="重新选择历史候选需要 Jev 模型判定；耗时和成本不确定。预览不会启动任务。"></p>
  <div class="row"><button class="btn" id="backfill-preview" data-st="预览历史"></button><button class="btn" id="backfill-start" data-st="启动历史候选补算" disabled></button></div><pre id="backfill-result"></pre><button class="btn" id="backfill-stop" data-st="停止补算" hidden></button>
  <p class="hint" data-st="补算使用已保存的关系阈值，不使用未保存的阈值编辑；候选参数使用当前表单。"></p>
  <div id="backfill-progress-label" class="hint" data-st="任务进度（服务状态）" hidden></div><pre id="backfill-progress"></pre>`;
scheduleSection.append(historyCard);
$('sched-save').setAttribute('data-st', '仅保存未来设置');
$('sched-reset').setAttribute('data-st', '恢复调度默认值');
scheduleSection.querySelectorAll('h2')[0].setAttribute('data-st', '预设（每轮问多少对）');
scheduleSection.querySelectorAll('h2')[1].setAttribute('data-st', '候选选择与成本');
scheduleSection.querySelectorAll('[data-preset]').forEach((el) => el.setAttribute('data-st', el.textContent));
for (const id of ['f-heads', 'f-cos', 'f-session', 'f-lessons', 'f-skeleton', 'f-cross', 'f-crosscos', 'f-poll']) {
  const label = $(id).parentElement.querySelector('label');
  label.setAttribute('data-st', label.textContent);
}
  const tierHint = scheduleSection.querySelector('.hint');
  tierHint.setAttribute('data-st', tierHint.textContent.trim());
function translateSchedule() {
  scheduleSection.querySelectorAll('[data-st]').forEach((el) => { el.textContent = st(el.dataset.st); });
  updateCost();
  if (lastBackfill) renderBackfill(lastBackfill);
}
translateSchedule();
api('/api/i18n').then((r) => { scheduleLang = r.map || {}; translateSchedule(); }).catch((e) => toast(e.message, 'err'));
function invalidateSchedulePreviews() {
  schedulePreviews = {};
  $('recal-apply').disabled = true; $('backfill-start').disabled = true;
  $('recal-result').textContent = ''; $('backfill-result').textContent = '';
}
scheduleSection.querySelectorAll('input').forEach((el) => el.addEventListener('input', invalidateSchedulePreviews));

const PRESETS = {
  light: { heads_vec_top_k: 5, session_heads_top_k: 3, unclosed_lessons: 1,
           decision_skeleton: 3, cross_top_k: 1 },
  normal: { heads_vec_top_k: 10, session_heads_top_k: 10, unclosed_lessons: 2,
            decision_skeleton: 5, cross_top_k: 3 },
  deep: { heads_vec_top_k: 20, session_heads_top_k: 15, unclosed_lessons: 3,
          decision_skeleton: 8, cross_top_k: 5 },
};

async function refreshSchedule() {
  scheduleCfg = await api("/api/config");
  const e = scheduleCfg.effective?.edges || {};
  $("f-heads").value = e.heads_vec_top_k ?? 10;
  $("f-cos").value = e.heads_cos_min ?? 0.35;
  $("f-margin").value = e.margin ?? 0.15;
  for (const [key] of SCHEDULE_ACCEPT.slice(1)) $('f-' + key).value = e[key] ?? 0;
  $("f-session").value = e.session_heads_top_k ?? 10;
  $("f-recent").value = e.session_recent ?? 3;
  $("f-lessons").value = e.unclosed_lessons ?? 2;
  $("f-skeleton").value = e.decision_skeleton ?? 5;
  $("f-cross").value = e.cross_top_k ?? 3;
  $("f-crosscos").value = e.cross_cos_min ?? 0.7;
  $("f-poll").value = e.poll_seconds ?? 60;
  $("f-schedule").value = e.schedule ?? "interval";
  $("f-schedule-at").value = e.schedule_at ?? "03:30";
  $("f-schedule-weekday").value = String(e.schedule_weekday ?? 0);
  $("sched-file").textContent = scheduleCfg.file || "（无配置文件）";
  updateCost();
  await refreshBackfillStatus();
}

function updateCost() {
  $('cost').textContent = st('候选来源会去重，实际数量未知；每对 8 秒仅供参考，当前模型速度未知。');
}

["f-heads", "f-session", "f-lessons", "f-skeleton", "f-cross"].forEach((id) => {
  $(id).oninput = updateCost;
});

document.querySelectorAll("button[data-preset]").forEach((b) => {
  b.onclick = () => {
    const p = PRESETS[b.dataset.preset];
    $("f-heads").value = p.heads_vec_top_k;
    $("f-session").value = p.session_heads_top_k;
    $("f-lessons").value = p.unclosed_lessons;
    $("f-skeleton").value = p.decision_skeleton;
    $("f-cross").value = p.cross_top_k;
    invalidateSchedulePreviews();
    updateCost();
    toast("已填入预设，点保存才会写盘");
  };
});

function scheduleKeys() { return ['heads_vec_top_k', 'heads_cos_min', 'margin', 'session_heads_top_k', 'session_recent', 'unclosed_lessons', 'decision_skeleton', 'cross_top_k', 'cross_cos_min', 'poll_seconds', 'schedule', 'schedule_at', 'schedule_weekday', ...SCHEDULE_ACCEPT.slice(1).map(([key]) => key)]; }
function scheduleEdges() {
  const edges = {
    heads_vec_top_k: +$("f-heads").value,
    heads_cos_min: +$("f-cos").value,
    margin: +$("f-margin").value,
    session_heads_top_k: +$("f-session").value,
    session_recent: +$("f-recent").value,
    unclosed_lessons: +$("f-lessons").value,
    decision_skeleton: +$("f-skeleton").value,
    cross_top_k: +$("f-cross").value,
    cross_cos_min: +$("f-crosscos").value,
    poll_seconds: +$("f-poll").value,
    schedule: $("f-schedule").value,
    schedule_at: $("f-schedule-at").value || "03:30",
    schedule_weekday: +$("f-schedule-weekday").value,
  };
  for (const [key] of SCHEDULE_ACCEPT.slice(1)) edges[key] = +$('f-' + key).value;
  if ([...scheduleSection.querySelectorAll('input[type=number]')].some((input) => input.value.trim() === '' || !input.validity.valid))
    throw new Error(st('请输入有效数值（置信度与 margin 为 0–1）'));
  return edges;
}

$('sched-save').onclick = async () => {
  try {
    const edges = scheduleEdges();
    await api("/api/config", { edges });
    invalidateSchedulePreviews();
    toast(st("已保存；下一轮 pass 用新值"), "ok");
    refreshSchedule();
  } catch (e) { toast(e.message, "err"); }
};

// The acceptance rule has its own save, next to the fields it writes: margin plus the five
// minimum confidences. Later edges use them; existing edges are untouched (that is what the
// recalibration preview below is for).
$('accept-save').onclick = async () => {
  try {
    const edges = acceptanceEdges(scheduleEdges());
    await api("/api/config", { edges });
    invalidateSchedulePreviews();
    toast(st('接受条件已保存：之后的连边按该标准判定'), "ok");
    refreshSchedule();
  } catch (e) { toast(e.message, "err"); }
};

$("sched-reset").onclick = async () => {
  if (!confirm(st('仅清除调度字段的覆盖？'))) return;
  try {
    if (!(scheduleCfg?.overrides || {}).edges) { invalidateSchedulePreviews(); await refreshSchedule(); return; }
    const edges = Object.fromEntries(scheduleKeys().map((key) => [key, null]));
    await api("/api/config", { edges });
    invalidateSchedulePreviews();
    toast("已恢复默认", "ok");
    refreshSchedule();
  } catch (e) { toast(e.message, "err"); }
};


function renderBackfill(p) {
  lastBackfill = p;
  $('backfill-progress-label').hidden = false;
  $('backfill-progress').textContent = progressText(p, st);
  $('backfill-stop').hidden = !['running', 'pending'].includes(p?.state);
}
async function refreshBackfillStatus() {
  clearTimeout(backfillTimer); backfillTimer = null;
  const generation = ++backfillGeneration;
  try {
    const status = await api('/api/status');
    if (generation !== backfillGeneration || !$('tab-schedule').classList.contains('on')) return;
    renderBackfill(status.backfill);
    if (['running', 'pending'].includes(status.backfill?.state)) backfillTimer = setTimeout(refreshBackfillStatus, 5000);
  } catch (e) { if ($('tab-schedule').classList.contains('on')) toast(e.message, 'err'); }
}

const candidateEdges = (edges) => Object.fromEntries(Object.entries(edges).filter(([key]) => !SCHEDULE_ACCEPT.some(([name]) => name === key) && key !== 'poll_seconds'));
const acceptanceEdges = (edges) => Object.fromEntries(SCHEDULE_ACCEPT.map(([key]) => [key, edges[key]]));
async function schedulePreview(kind) {
  const id = kind === 'recalibrate' ? 'recal' : 'backfill';
  const button = $(id + '-preview'); button.disabled = true;
  delete schedulePreviews[kind];
  $(kind === 'recalibrate' ? 'recal-apply' : 'backfill-start').disabled = true;
  try {
    const values = scheduleEdges();
    const edges = kind === 'recalibrate' ? acceptanceEdges(values) : candidateEdges(values);
    const path = kind === 'recalibrate' ? '/api/recalibrate/preview' : '/api/backfill/preview';
    const historical_scale = kind === 'recalibrate' && $('recal-conditional').checked ? 'conditional' : null;
    const result = await api(path, kind === 'recalibrate' ? { edges, historical_scale } : { edges });
    if (!result.token) throw new Error(st('预览未返回令牌，无法应用'));
    const current = scheduleEdges();
    if (JSON.stringify(edges) !== JSON.stringify(kind === 'recalibrate' ? acceptanceEdges(current) : candidateEdges(current))) throw new Error(st('参数已改变，请重新预览'));
    if (kind === 'recalibrate' && historical_scale !== ($('recal-conditional').checked ? 'conditional' : null)) throw new Error(st('参数已改变，请重新预览'));
    schedulePreviews[kind] = { edges, result, historical_scale };
    $(id + '-result').textContent = kind === 'recalibrate' ? recalText(result, st) : backfillText(result, st);
    $(kind === 'recalibrate' ? 'recal-apply' : 'backfill-start').disabled = false;
  } catch (e) { toast(e.message, 'err'); } finally { button.disabled = false; }
}
async function scheduleApply(kind) {
  const p = schedulePreviews[kind]; if (!p || !confirm(st('确认应用历史操作？此操作不会保存未来设置。'))) return;
  const button = $(kind === 'recalibrate' ? 'recal-apply' : 'backfill-start'); button.disabled = true;
  try {
    const current = scheduleEdges();
    if (JSON.stringify(p.edges) !== JSON.stringify(kind === 'recalibrate' ? acceptanceEdges(current) : candidateEdges(current))) throw new Error(st('参数已改变，请重新预览'));
    if (kind === 'recalibrate' && p.historical_scale !== ($('recal-conditional').checked ? 'conditional' : null)) throw new Error(st('参数已改变，请重新预览'));
    const path = kind === 'recalibrate' ? '/api/recalibrate/apply' : '/api/backfill/start';
    const result = await api(path, kind === 'recalibrate' ? { edges: p.edges, token: p.result.token, historical_scale: p.historical_scale } : { edges: p.edges, token: p.result.token });
    delete schedulePreviews[kind];
    toast(st('请求已提交'), 'ok');
    if (kind === 'backfill') { renderBackfill(result.backfill); await refreshBackfillStatus(); }
    else $('recal-result').textContent = recalText(result, st);
  } catch (e) { toast(e.message, 'err'); }
}
$('recal-preview').onclick = () => schedulePreview('recalibrate');
$('recal-apply').onclick = () => scheduleApply('recalibrate');
$('backfill-preview').onclick = () => schedulePreview('backfill');
$('backfill-start').onclick = () => scheduleApply('backfill');
$('backfill-stop').onclick = async () => { try { await api('/api/stop', {}); toast(st('已请求停止：本轮做完手上这一对就停'), 'ok'); await refreshBackfillStatus(); } catch (e) { toast(e.message, 'err'); } };

/* ------------------------------------------------------------------ logs */

async function refreshLogs() {
  const { logs } = await api("/api/logs");
  const body = logs.map((l) =>
    `<span class="lv-${esc(l.level)}">${when(l.ts)} ${esc(l.level[0])} ${esc(l.name.split(".").pop())}  ${esc(l.message)}</span>`
  ).join("\n");
  const pre = $("log-body");
  const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 40;
  pre.innerHTML = body || `<span class="sub">（还没有日志）</span>`;
  if (atBottom) pre.scrollTop = pre.scrollHeight;
}

$("log-refresh").onclick = () => refreshLogs().catch((e) => toast(e.message, "err"));

/* ------------------------------------------------------------------ loop */

async function tick() {
  refreshHealth();
  if ($("log-follow").checked && $("tab-logs").classList.contains("on")) refreshLogs();
  if ($("tab-overview").classList.contains("on")) {
    try { await refreshOverview(); } catch (e) { toast(e.message, "err"); }
  }
  setTimeout(tick, $("tab-overview").classList.contains("on") ? overviewDelay : 5000);
}
tick();

refreshHealth();
showTab(location.hash.replace("#", "") || "overview");
