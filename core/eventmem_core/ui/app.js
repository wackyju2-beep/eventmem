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

$("run-once").onclick = async () => {
  const btn = $("run-once");
  btn.disabled = true;
  try {
    const r = await api("/api/run-once", {});
    toast(r.skipped ? "跳过：" + r.skipped : `跑完：${r.pairs} 对，写 ${r.edges} 条边` +
      (r.backend ? `（${r.backend}）` : ""), "ok");
    refresh("overview");
  } catch (e) {
    toast(e.message, "err");
  } finally {
    btn.disabled = false;
  }
};

/* ------------------------------------------------------------------ overview */

async function refreshOverview() {
  const [stats, status, config] = await Promise.all([
    api("/api/stats"), api("/api/status"), api("/api/config")]);

  $("n-events").textContent = stats.events ?? "—";
  $("n-newest").textContent = stats.newest ? "最新 " + when(stats.newest) : "空库";
  $("n-edges").textContent = stats.edges ?? "—";
  $("n-pairs").textContent = (stats.pairs ?? 0) + " 对已判";
  const cursor = status.cursor || {};
  $("n-behind").textContent = cursor.events == null ? "—" : cursor.events;
  $("n-lag").textContent = cursor.behind == null ? "无游标" : "游标落后 " + dur(cursor.behind);

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
}

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

let scheduleCfg = null;

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
  $("f-session").value = e.session_heads_top_k ?? 10;
  $("f-lessons").value = e.unclosed_lessons ?? 2;
  $("f-skeleton").value = e.decision_skeleton ?? 5;
  $("f-cross").value = e.cross_top_k ?? 3;
  $("f-crosscos").value = e.cross_cos_min ?? 0.7;
  $("f-poll").value = e.poll_seconds ?? 60;
  $("sched-file").textContent = scheduleCfg.file || "（无配置文件）";
  updateCost();
}

function updateCost() {
  const pairs = ["f-heads", "f-session", "f-lessons", "f-skeleton", "f-cross"]
    .map((id) => +$(id).value || 0).reduce((a, b) => a + b, 0);
  const secs = pairs * 8;
  $("cost").textContent = `每事件 ≈ ${pairs} 对 ≈ ${dur(secs)}（按 8 s/对估算）`;
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
    updateCost();
    toast("已填入预设，点保存才会写盘");
  };
});

$("sched-save").onclick = async () => {
  const edges = {
    heads_vec_top_k: +$("f-heads").value,
    heads_cos_min: +$("f-cos").value,
    margin: +$("f-margin").value,
    session_heads_top_k: +$("f-session").value,
    unclosed_lessons: +$("f-lessons").value,
    decision_skeleton: +$("f-skeleton").value,
    cross_top_k: +$("f-cross").value,
    cross_cos_min: +$("f-crosscos").value,
    poll_seconds: +$("f-poll").value,
  };
  try {
    await api("/api/config", { edges });
    toast("已保存；下一轮 pass 用新值", "ok");
    refreshSchedule();
  } catch (e) { toast(e.message, "err"); }
};

$("sched-reset").onclick = async () => {
  if (!confirm("清掉 settings.yaml 里所有覆盖，回到默认值？")) return;
  try {
    await api("/api/config", { edges: null, decision: null, recall: null });
    toast("已恢复默认", "ok");
    refreshSchedule();
  } catch (e) { toast(e.message, "err"); }
};

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

setInterval(() => {
  refreshHealth();
  if ($("log-follow").checked && $("tab-logs").classList.contains("on")) refreshLogs();
  if ($("tab-overview").classList.contains("on")) refreshOverview();
}, 5000);

refreshHealth();
showTab(location.hash.replace("#", "") || "overview");
