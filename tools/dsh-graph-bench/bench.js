/**
 * In-page harness for the DSH memory-console graph.
 *
 * Loads the real `adapters/dsh/client.js`, mounts its real Console with a stub Host
 * ctx + a fetch stub serving a real `/api/mem/graph` dump, then opens the 图谱 tab.
 * Everything measured below runs in the page: rAF deltas, atom style state, and the
 * write volume the render loop performs. `window.__bench()` is driven over CDP by
 * run.mjs, which adds Performance metrics and a CPU profile on top.
 */
const hud = document.getElementById('hud')
const setHud = (text) => { hud.textContent = text }
// Ask client.js to expose its fade/camera state for diagnosis.
window.__EM_GRAPH_DEBUG = 1

// ------------------------------------------------------------------ theme vars
// The DSH host supplies these; without them every color-mix() resolves to invalid
// and the graph renders as blank boxes, so stand in a dark theme.
const HOST_VARS = {
  '--dsw-alias-bg-base': '#0b0c0f',
  '--dsw-alias-bg-layer-1': '#14161b',
  '--dsw-alias-bg-layer-2': '#191c22',
  '--dsw-alias-label-primary': '#c9cdd3',
  '--dsw-alias-label-secondary': '#9aa0a8',
  '--dsw-alias-label-tertiary': '#7c828b',
  '--dsw-alias-border-l2': '#22252b',
  '--dsw-alias-border-l3': '#2f333b',
  '--dsw-alias-state-business-primary': '#4f8cff',
  '--dsw-alias-state-error-primary': '#ff6f8e',
  '--dsw-alias-state-success-primary': '#48c979',
  '--dsw-alias-interactive-bg-hover': '#1f232a',
  '--dsw-alias-settings-card-stroke': '#2f333b',
  '--dsw-alias-label-primary-foreground': '#ffffff',
  '--dsw-elevation-prominent': '0 12px 32px rgba(0,0,0,.5)',
  '--dsw-radius-sm': '4px',
  '--dsw-radius-md': '8px',
  '--dsw-radius-lg': '12px',
  '--dsw-font-mono': 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
}
for (const [key, value] of Object.entries(HOST_VARS)) {
  document.documentElement.style.setProperty(key, value)
}

// ------------------------------------------------------------------ daemon stub
const graph = await (await fetch('/graph.json')).json()
const now = Math.floor(Date.now() / 1000)
const ROUTES = {
  health: { status: 'ok', schema: 12, pid: 424242 },
  status: {
    ok: true, paused: false, busy: false,
    live: { backend: 'local-judge', pairs: 0, edges: 0, events_total: graph.events.length, pairs_planned: 0, since: now, updated: now },
    backfill: { state: 'idle', pairs: 0, edges: 0, result: null, error: '', started: 0, finished: 0 },
    runs: [], backends: [], cursor: { behind: 0, events: 0 },
  },
  stats: {
    events: graph.events.length, edges: graph.edges.length, pairs: graph.pairs.length, newest: now,
    kinds: {}, projects: [], failures: { count: 0, last: [] },
  },
  config: { effective: { edges: {} }, overrides: { edges: {} }, file: '/tmp/settings.yaml', writable: true },
  'mem/graph': graph,
}
let callCount = 0
const originalFetch = window.fetch.bind(window)
window.fetch = async (url, init) => {
  callCount += 1
  const route = new URL(url, location.href).searchParams.get('path') || ''
  const body = ROUTES[route] ?? {}
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) }
}

// ------------------------------------------------------------------ plugin load
const source = await (await originalFetch('/client.js')).text()
if (source.length < 5000) throw new Error(`client.js fetch looks wrong: ${source.length} bytes — ${source.slice(0, 120)}`)
let definition = null
window.__ModuleLoader__ = { load(d) { definition = d } }
new Function('window', 'document', source)(window, document)
if (!definition) throw new Error(`client.js did not register a module (${source.length} bytes)`)

const React = window.React
const ReactDOM = window.ReactDOM
const requireStub = (id) => {
  if (id === 'react') return React
  if (id === 'react-dom') return ReactDOM
  throw new Error(`unexpected require(${id})`)
}
const plugin = definition.factory(requireStub)

const localeTables = {}
let consoleComponent = null
const ctx = {
  effect(fn) { return fn() },
  locale: {
    register(ns, tables) { localeTables[ns] = tables; return () => {} },
    bind(ns) {
      return (text, params) => {
        const table = (localeTables[ns] || {}).zh || {}
        let out = table[text] === undefined ? text : table[text]
        if (params && typeof params === 'object') {
          for (const [key, value] of Object.entries(params)) out = out.split(`{${key}}`).join(String(value))
        }
        return out
      }
    },
  },
  slots: {
    inject(_name, fn) { fn() },
    register(definition2, component) {
      if (definition2.name === 'main') consoleComponent = component
      return () => {}
    },
  },
}
plugin.apply(ctx)
if (!consoleComponent) throw new Error('console page was not registered')

// ------------------------------------------------------------------ mount
const root = ReactDOM.createRoot(document.getElementById('root'))
root.render(React.createElement(consoleComponent, { tr: ctx.locale.bind('eventmem') }))

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const stage = () => document.querySelector('.emc-g-stage')
const atoms = () => document.querySelectorAll('.emc-g-atom')

const waitFor = async (predicate, ms = 15000) => {
  const t0 = performance.now()
  while (performance.now() - t0 < ms) {
    const value = predicate()
    if (value) return value
    await sleep(50)
  }
  return null
}

// Open 图谱 as soon as the tab row exists.
const graphTab = await waitFor(() => [...document.querySelectorAll('.emc-tab')]
  .find((node) => node.textContent.trim() === '图谱'))
graphTab.click()
await waitFor(() => stage() && atoms().length > 0)
await sleep(1200) // first paint + opening camera

// ------------------------------------------------------------------ measurement
const quantiles = (sorted, f) => sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * f))]

/** Median/p95/max of consecutive rAF deltas, plus the scene's own state. */
function summarise(deltas) {
  const clean = deltas.filter((d) => d > 0)
  const sorted = [...clean].sort((a, b) => a - b)
  const mean = clean.reduce((a, b) => a + b, 0) / clean.length
  const m = [...atoms()].map((el) => ({
    m: Number(el.style.getPropertyValue('--m') || 0),
    f: Number(el.style.getPropertyValue('--f') || 0),
  }))
  return {
    frames: clean.length,
    // rAF in headless Chrome is not vsynced, so this is not a display frame rate — the
    // app's own draw rate (drawFps, from the canvas clears) is the number to compare.
    rafFps: +(1000 / mean).toFixed(1),
    median: +quantiles(sorted, 0.5).toFixed(2),
    p95: +quantiles(sorted, 0.95).toFixed(2),
    max: +quantiles(sorted, 0.999).toFixed(2),
    atoms: m.length,
    cards: m.filter((x) => x.m > 0.9).length,
    midMorph: m.filter((x) => x.m > 0.1 && x.m <= 0.9).length,
    dots: m.filter((x) => x.m <= 0.1).length,
    longFrames: clean.filter((d) => d > 33).length,
  }
}

/** Park the camera after a controlled number of wheel ticks, for screenshots. */
window.__pose = async (ticks = 30, step = 40) => {
  await window.__reset()
  const element = stage()
  const box = element.getBoundingClientRect()
  for (let i = 0; i < ticks; i += 1) {
    element.dispatchEvent(new WheelEvent('wheel', {
      deltaY: -step, clientX: box.left + box.width / 2, clientY: box.top + box.height / 2,
      bubbles: true, cancelable: true,
    }))
    await sleep(16)
  }
  await sleep(700)
  return { atoms: atoms().length, cards: document.querySelectorAll('.emc-g-atom[data-card="1"]').length }
}

/** Wheel all the way out and count how many atoms are on screen at the fitted view. */
window.__zoomOut = async (ticks = 120) => {
  const element = stage()
  const box = element.getBoundingClientRect()
  for (let i = 0; i < ticks; i += 1) {
    element.dispatchEvent(new WheelEvent('wheel', {
      deltaY: 60, clientX: box.left + box.width / 2, clientY: box.top + box.height / 2,
      bubbles: true, cancelable: true,
    }))
    await sleep(9)
  }
  await sleep(900)
  const all = [...document.querySelectorAll('.emc-g-atom')]
  const shown = all.filter((el) => el.style.display !== 'none')
  return {
    total: all.length,
    visible: shown.length,
    dim: shown.filter((el) => Number(el.style.opacity) < 0.9).length,
    cards: document.querySelectorAll('.emc-g-atom[data-card="1"]').length,
    morph: shown.filter((el) => Number(el.style.getPropertyValue('--m')) > 0.1).length,
  }
}

/** Apply a kind filter through the real panel and report what is on screen. */
window.__filter = async (chipText = '决定') => {
  await window.__reset()
  const open = [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === '筛选')
  if (!open) return { error: 'no filter button' }
  open.click()
  await sleep(250)
  const chip = [...document.querySelectorAll('.emc-g-panel .emc-g-chip')]
    .find((c) => c.textContent.trim() === chipText)
  if (!chip) return { error: 'no chip ' + chipText, chips: [...document.querySelectorAll('.emc-g-panel .emc-g-chip')].map((c) => c.textContent.trim()) }
  chip.click()
  await sleep(1600)
  const state1 = window.__emGraphState ? window.__emGraphState() : null
  await sleep(700)
  const state2 = window.__emGraphState ? window.__emGraphState() : null
  const atomsAll = [...document.querySelectorAll('.emc-g-atom')]
  const info = atomsAll.map((el) => {
    const m = /translate3d\(([-\d.]+)px, ([-\d.]+)px/.exec(el.style.transform || '')
    return {
      display: el.style.display,
      opacity: Number(el.style.opacity),
      m: Number(el.style.getPropertyValue('--m') || 0),
      x: m ? Number(m[1]) : NaN,
      y: m ? Number(m[2]) : NaN,
    }
  })
  const box = document.querySelector('.emc-g-stage').getBoundingClientRect()
  const onScreen = (a) => a.x > box.left - 40 && a.x < box.right + 40 && a.y > box.top - 40 && a.y < box.bottom + 40
  const matched = [...document.querySelectorAll(`.emc-g-atom[data-kind="${chipText}"]`)]
  const matchedInfo = matched.map((el) => ({
    display: el.style.display,
    opacity: Number(el.style.opacity),
    m: Number(el.style.getPropertyValue('--m') || 0),
    x: Number((/translate3d\(([-\d.]+)px/.exec(el.style.transform || '') || [0, NaN])[1]),
    y: Number((/translate3d\([-\d.]+px, ([-\d.]+)px/.exec(el.style.transform || '') || [0, NaN])[1]),
  }))
  return {
    total: atomsAll.length,
    visible: info.filter((a) => a.display !== 'none').length,
    visibleOnScreen: info.filter((a) => a.display !== 'none' && onScreen(a)).length,
    hiddenButOnScreen: info.filter((a) => a.display === 'none' && onScreen(a)).length,
    matchedTotal: matched.length,
    matchedVisible: matchedInfo.filter((a) => a.display !== 'none').length,
    matchedHiddenOnScreen: matchedInfo.filter((a) => a.display === 'none' && onScreen(a)).length,
    matchedHiddenOffScreen: matchedInfo.filter((a) => a.display === 'none' && !onScreen(a)).length,
    matchedOpacity: matchedInfo.filter((a) => a.display !== 'none').slice(0, 8).map((a) => a.opacity),
    countText: ((document.querySelector('.emc-g-count') || {}).textContent || '').trim(),
    cards: document.querySelectorAll('.emc-g-atom[data-card="1"]').length,
    state1, state2,
    atomStates: window.__emAtomState
      ? window.__emAtomState(matched.slice(0, 10).map((el) => Number(el.dataset.id)))
      : null,
    culledMatched: window.__emAtomState
      ? window.__emAtomState(matched.map((el) => Number(el.dataset.id))).filter((a) => a.culled).length
      : null,
  }
}

/** Sweep the wheel one notch at a time and record how many atoms are on screen. */
window.__sweep = async (dir = 1, steps = 40, step = 40) => {
  await window.__reset()
  const element = stage()
  const box = element.getBoundingClientRect()
  const cx = box.left + box.width / 2
  const cy = box.top + box.height / 2
  const count = () => [...document.querySelectorAll('.emc-g-atom')]
    .filter((el) => el.style.display !== 'none').length
  const trace = [{ at: 0, visible: count() }]
  for (let i = 1; i <= steps; i += 1) {
    element.dispatchEvent(new WheelEvent('wheel', {
      deltaY: -dir * step, clientX: cx, clientY: cy, bubbles: true, cancelable: true,
    }))
    await sleep(90)
    trace.push({ at: i, visible: count() })
  }
  return { dir, trace, max: Math.max(...trace.map((t) => t.visible)) }
}

// ---------------------------------------------------------------- click probes
// 三条交互规则各一个探针：点高亮邻域直接换选、平常边不可点、筛选面板先关后选。
// 全部走真实的 pointer/mouse 事件序列，和手指点出来的一致。

const atomCenter = (el) => {
  const r = el.getBoundingClientRect()
  return [r.left + r.width / 2, r.top + r.height / 2]
}

/** 完整的一次点击：pointer + mouse 两个序列都发，页面的 pointer 拖拽和 mousedown 门卫都要看到。 */
const clickAt = async (el, x, y) => {
  const opts = { button: 0, clientX: x, clientY: y, bubbles: true, cancelable: true }
  el.dispatchEvent(new PointerEvent('pointerdown', { ...opts, pointerId: 7, pointerType: 'mouse' }))
  el.dispatchEvent(new PointerEvent('pointerup', { ...opts, pointerId: 7, pointerType: 'mouse' }))
  el.dispatchEvent(new MouseEvent('mousedown', opts))
  el.dispatchEvent(new MouseEvent('mouseup', opts))
  el.dispatchEvent(new MouseEvent('click', opts))
  await sleep(60)
}

window.__closeInspector = async () => {
  for (let i = 0; i < 3; i += 1) {
    const close = document.querySelector('.emc-g-insp .emc-g-x')
    if (!close) break
    close.click()
    await sleep(500)
  }
  return Boolean(document.querySelector('.emc-g-insp'))
}

/** 舞台上一块空白：elementFromPoint 命中的就是舞台本身（不是原子/工具条/图例/检查器）。 */
window.__emptyPoint = (() => {
  const stageEl = () => document.querySelector('.emc-g-stage')
  return () => {
    const el = stageEl()
    if (!el) return null
    const box = el.getBoundingClientRect()
    for (let fy = 0.2; fy <= 0.85; fy += 0.05) {
      for (let fx = 0.25; fx <= 0.72; fx += 0.03) {
        const x = box.left + box.width * fx
        const y = box.top + box.height * fy
        const hit = document.elementFromPoint(x, y)
        if (hit === el) return [x, y]
      }
    }
    return null
  }
})()

/** 图里的度数：挑一颗「有邻居、且邻居还有二跳」的原子来验收邻域点击。 */
const DEGREE = (() => {
  const adj = new Map()
  for (const e of graph.edges) {
    if (!adj.has(e.src)) adj.set(e.src, new Set())
    if (!adj.has(e.dst)) adj.set(e.dst, new Set())
    adj.get(e.src).add(e.dst)
    adj.get(e.dst).add(e.src)
  }
  const score = (id) => {
    const direct = adj.get(id) || new Set()
    let second = 0
    for (const n of direct) for (const m of adj.get(n) || []) {
      if (m !== id && !direct.has(m)) second += 1
    }
    return { direct: direct.size, second }
  }
  return { adj, score }
})()

/** 当前可点的、度数最高的原子（初始镜头下屏内原子都可见）。 */
const bestAtom = (filter) => {
  let best = null
  for (const el of document.querySelectorAll('.emc-g-atom[data-hit="1"]')) {
    if (el.style.display === 'none') continue
    const id = Number(el.dataset.id)
    const s = DEGREE.score(id)
    if (filter && !filter(id, s)) continue
    if (!best || s.direct + s.second > best.score.direct + best.score.second) best = { el, id, score: s }
  }
  return best
}

/**
 * 需求 1：选中一颗原子后，被点亮的两层邻域原子可以一次点中。
 * 需求 2 的对照面：普通视图里点空白不会选中任何边。
 */
window.__ringPolicy = async () => {
  await window.__reset()
  await window.__closeInspector()
  const pick = bestAtom((id, s) => s.direct > 0)
  if (!pick) return { error: 'no clickable atom with neighbors' }
  const [x, y] = atomCenter(pick.el)
  await clickAt(pick.el, x, y)
  await sleep(1900)
  const layers = window.__emRingLayers ? window.__emRingLayers() : null
  const state = window.__emGraphState ? window.__emGraphState() : null
  const ring = state && state.ring ? state.ring : []
  const ringHit = ring.map((id) => {
    const el = document.querySelector(`.emc-g-atom[data-id="${id}"]`)
    return { id, hit: el ? el.dataset.hit : null, disp: el ? el.style.display : null }
  })
  // 二跳原子优先；没有二跳就用一跳，验证的是「亮着的原子一次点就换选」。
  const pool = [...(layers ? layers.second : []), ...(layers ? layers.first : [])]
  let target = null
  let tx = 0
  let ty = 0
  const stageBox = document.querySelector('.emc-g-stage').getBoundingClientRect()
  for (const id of pool) {
    const el = document.querySelector(`.emc-g-atom[data-id="${id}"]`)
    if (!el || el.style.display === 'none' || el.dataset.hit !== '1') continue
    const [cx, cy] = atomCenter(el)
    if (cx < stageBox.left + 4 || cx > stageBox.right - 4
      || cy < stageBox.top + 4 || cy > stageBox.bottom - 4) continue
    // 真实点击要真的落在这颗原子上（不被工具条/别的卡片/检查器盖住）。
    const top = document.elementFromPoint(cx, cy)
    if (!top || !top.closest || !top.closest('.emc-g-atom')) continue
    target = id
    tx = cx
    ty = cy
    break
  }
  const before = {
    inspector: Boolean(document.querySelector('.emc-g-insp')),
    title: (document.querySelector('.emc-g-insp-title') || {}).textContent || '',
    hitEdges: state ? state.hitEdges : null,
    ringSize: layers ? layers.direct.length + layers.second.length : null,
    direct: layers ? layers.direct.length : null,
    second: layers ? layers.second.length : null,
    ringHit,
  }
  if (!target) return { ...before, error: 'no highlighted neighbor on screen', selectedId: pick.id }
  // 一次点击：应当直接换成那颗邻域原子，而不是先退出。
  const el = document.querySelector(`.emc-g-atom[data-id="${target}"]`)
  await clickAt(el, tx, ty)
  await sleep(1500)
  const report = {
    ...before,
    selectedId: pick.id,
    targetId: target,
    after: {
      inspector: Boolean(document.querySelector('.emc-g-insp')),
      title: (document.querySelector('.emc-g-insp-title') || {}).textContent || '',
      selected: [...document.querySelectorAll('.emc-g-atom[data-sel="1"]')].map((n) => Number(n.dataset.id)),
    },
  }
  await window.__closeInspector()
  return report
}

/** 需求 2：平常命中表为空；选中原子后只有相连边可点，点空白不会误中一条边。 */
window.__edgePolicy = async () => {
  await window.__reset()
  await window.__closeInspector()
  const baseline = window.__emGraphState ? window.__emGraphState().hitEdges : null
  const pick = bestAtom((id, s) => s.direct > 1)
  if (!pick) return { baseline, error: 'no clickable atom with edges' }
  const [x, y] = atomCenter(pick.el)
  await clickAt(pick.el, x, y)
  await sleep(1900)
  const hits = window.__emEdgeHits ? window.__emEdgeHits() : []
  const state = window.__emGraphState ? window.__emGraphState() : null
  const own = hits.filter((h) => h.src === pick.id || h.dst === pick.id)
  if (!hits.length) return { baseline, selectedId: pick.id, hitEdges: 0, error: 'no highlighted edge' }
  const stageEl = document.querySelector('.emc-g-stage')
  const box = stageEl.getBoundingClientRect()
  // 沿曲线找一点真的落在画布上（不被别的卡片/工具条盖住）的点来点。
  const candidates = []
  for (const h of [...own, ...hits]) {
    for (const t of [0.5, 0.42, 0.58, 0.35, 0.65]) {
      const mt = 1 - t
      candidates.push({
        h,
        x: mt * mt * h.x1 + 2 * mt * t * h.cx + t * t * h.x2,
        y: mt * mt * h.y1 + 2 * mt * t * h.cy + t * t * h.y2,
      })
    }
  }
  const spot = candidates.find((c) => {
    const top = document.elementFromPoint(box.left + c.x, box.top + c.y)
    return top === stageEl
  })
  if (!spot) return { baseline, selectedId: pick.id, hitEdges: hits.length, error: 'no free spot on an edge' }
  const seg = spot.h
  const mx = spot.x
  const my = spot.y
  const move = (x, y) => stageEl.dispatchEvent(new PointerEvent('pointermove', {
    button: 0, buttons: 0, pointerId: 9, pointerType: 'mouse',
    clientX: x, clientY: y, bubbles: true, cancelable: true,
  }))
  // 悬停手续：可点的高亮边上给 pointer，离开就还原。
  move(box.left + mx, box.top + my)
  await sleep(80)
  const cursorOnEdge = stageEl.style.cursor
  const empty = window.__emptyPoint()
  if (empty) move(empty[0], empty[1])
  await sleep(80)
  const cursorOffEdge = stageEl.style.cursor
  await clickAt(stageEl, box.left + mx, box.top + my)
  await sleep(1500)
  const edgeSelected = {
    inspector: Boolean(document.querySelector('.emc-g-insp')),
    title: (document.querySelector('.emc-g-insp-title') || {}).textContent || '',
    rel: seg.rel,
    selectedAtoms: document.querySelectorAll('.emc-g-atom[data-sel="1"]').length,
  }
  // 选中边时亮着的是它的两端：点其中一颗应当一次换成原子检查器。
  let endpointTarget = null
  for (const id of [seg.src, seg.dst]) {
    const el = document.querySelector(`.emc-g-atom[data-id="${id}"]`)
    if (!el || el.style.display === 'none') continue
    const [ex, ey] = atomCenter(el)
    const top = document.elementFromPoint(ex, ey)
    if (!top || !top.closest || !top.closest('.emc-g-atom')) continue
    endpointTarget = id
    await clickAt(el, ex, ey)
    await sleep(1200)
    break
  }
  const endpointSelected = {
    target: endpointTarget,
    inspector: Boolean(document.querySelector('.emc-g-insp')),
    title: (document.querySelector('.emc-g-insp-title') || {}).textContent || '',
  }
  // 退出后（无选中）在同一个像素点再点一次：这一下不能再选中那条边。
  await window.__closeInspector()
  await sleep(900)
  const idleHits = window.__emGraphState ? window.__emGraphState().hitEdges : null
  await clickAt(stageEl, box.left + mx, box.top + my)
  await sleep(500)
  const report = {
    baseline,
    hitEdges: state ? state.hitEdges : hits.length,
    incidentHits: own.length,
    allIncident: hits.every((h) => h.src === pick.id || h.dst === pick.id),
    selectedId: pick.id,
    edgeSelected,
    endpointSelected,
    cursorOnEdge,
    cursorOffEdge,
    idleHits,
    idleAfterClickInspector: Boolean(document.querySelector('.emc-g-insp')),
  }
  await window.__closeInspector()
  return report
}

/** 需求 3：筛选面板是浮层，点它外面第一下只关面板，第二下才真正选中。 */
window.__panelPolicy = async () => {
  await window.__reset()
  await window.__closeInspector()
  const openBtn = document.querySelector('.emc-g-fbtn')
  if (!openBtn) return { error: 'no filter button' }
  openBtn.click()
  await sleep(400)
  const opened = Boolean(document.querySelector('.emc-g-panel'))
  const panelBox = (document.querySelector('.emc-g-panel') || {}).getBoundingClientRect
    ? document.querySelector('.emc-g-panel').getBoundingClientRect() : null
  const outside = (el) => {
    if (!el) return false
    const [x, y] = atomCenter(el)
    if (panelBox && x >= panelBox.left && x <= panelBox.right
      && y >= panelBox.top && y <= panelBox.bottom) return false
    if (!(x > 0 && y > 0 && x < window.innerWidth && y < window.innerHeight)) return false
    // 真实点击要落在原子自己身上，而不是被工具条/检查器挡住。
    const top = document.elementFromPoint(x, y)
    return Boolean(top && top.closest && top.closest('.emc-g-atom') === el)
  }
  const candidates = [...document.querySelectorAll('.emc-g-atom[data-hit="1"]')]
    .filter((el) => el.style.display !== 'none' && outside(el))
  if (!candidates.length) return { opened, error: 'no atom outside the panel' }
  const pick = candidates[0]
  const id = Number(pick.dataset.id)
  const [x, y] = atomCenter(pick)
  await clickAt(pick, x, y)
  await sleep(700)
  const first = {
    panelOpen: Boolean(document.querySelector('.emc-g-panel')),
    inspector: Boolean(document.querySelector('.emc-g-insp')),
    selected: [...document.querySelectorAll('.emc-g-atom[data-sel="1"]')].map((n) => Number(n.dataset.id)),
    hitEdges: window.__emGraphState ? window.__emGraphState().hitEdges : null,
  }
  await clickAt(pick, x, y)
  await sleep(1500)
  const second = {
    panelOpen: Boolean(document.querySelector('.emc-g-panel')),
    inspector: Boolean(document.querySelector('.emc-g-insp')),
    title: (document.querySelector('.emc-g-insp-title') || {}).textContent || '',
    selected: [...document.querySelectorAll('.emc-g-atom[data-sel="1"]')].map((n) => Number(n.dataset.id)),
  }
  // 收尾：关面板、清选中，后面的性能测量从干净状态开始。
  const closePanel = document.querySelector('.emc-g-fbtn')
  if (closePanel && document.querySelector('.emc-g-panel')) closePanel.click()
  await sleep(200)
  await window.__closeInspector()
  return { opened, atomId: id, first, second }
}

/** Click an atom and report what the focus path produced (fade, retreat, inspector, edges). */
window.__select = async () => {
  await window.__reset()
  const atom = [...document.querySelectorAll('.emc-g-atom[data-hit="1"]')]
    .sort((a, b) => Number(a.style.zIndex) - Number(b.style.zIndex))[0]
  if (!atom) return { error: 'no clickable atom' }
  const rect = atom.getBoundingClientRect()
  const x = rect.left + rect.width / 2
  const y = rect.top + rect.height / 2
  // A real pointer sequence: the stage ignores a click that follows a drag (movedRef), so
  // dispatch the press first exactly like a person would.
  atom.dispatchEvent(new PointerEvent('pointerdown', {
    button: 0, pointerId: 3, pointerType: 'mouse', clientX: x, clientY: y, bubbles: true, cancelable: true,
  }))
  atom.dispatchEvent(new PointerEvent('pointerup', {
    button: 0, pointerId: 3, pointerType: 'mouse', clientX: x, clientY: y, bubbles: true, cancelable: true,
  }))
  atom.click()
  await sleep(1600)
  return {
    inspector: Boolean(document.querySelector('.emc-g-insp')),
    title: (document.querySelector('.emc-g-insp-title') || {}).textContent || '',
    selected: document.querySelectorAll('.emc-g-atom[data-sel="1"]').length,
    visibleAtoms: [...document.querySelectorAll('.emc-g-atom')]
      .filter((el) => el.style.display !== 'none').length,
  }
}

/** Close whatever inspector is open and report how the atoms come back. */
window.__deselect = async () => {
  const close = document.querySelector('.emc-g-insp .emc-g-x')
  if (close) close.click()
  await sleep(1400)
  const count = () => {
    const all = [...document.querySelectorAll('.emc-g-atom')]
    const shown = all.filter((el) => el.style.display !== 'none')
    const opacities = shown.map((el) => Number(el.style.opacity))
    return {
      total: all.length, visible: shown.length,
      dim: opacities.filter((o) => o < 0.9).length,
      minOpacity: Math.min(...opacities),
    }
  }
  const afterClose = count()
  await window.__reset()
  return { afterClose, afterReset: count() }
}

/** Reset the camera to the fitted view so every measurement starts from the same frame. */
window.__reset = async () => {
  const button = [...document.querySelectorAll('button')]
    .find((node) => node.textContent.trim() === '重置视角')
  if (!button) throw new Error('reset-view button not found')
  button.click()
  await sleep(1100)
}

window.__bench = async (opts = {}) => {
  const ms = opts.ms ?? 4000
  const mode = opts.mode ?? 'zoom'
  const element = stage()
  if (!element) return { error: 'no .emc-g-stage' }
  const box = element.getBoundingClientRect()
  const cx = box.left + box.width / 2
  const cy = box.top + box.height / 2

  const deltas = []
  let last = null
  let stop = false
  const tick = (t) => {
    if (last !== null) deltas.push(t - last)
    last = t
    if (!stop) requestAnimationFrame(tick)
  }
  requestAnimationFrame(tick)
  const meter = startMeter()

  let wheelTimer = 0
  let moveTimer = 0
  let dir = -1
  let step = 0
  if (mode === 'zoom') {
    wheelTimer = setInterval(() => {
      step += 1
      if (step % (opts.flip ?? 26) === 0) dir = -dir
      element.dispatchEvent(new WheelEvent('wheel', {
        deltaY: dir * (opts.step ?? 40), clientX: cx, clientY: cy, bubbles: true, cancelable: true,
      }))
    }, 16)
  } else if (mode === 'orbit') {
    element.dispatchEvent(new PointerEvent('pointerdown', {
      button: 1, buttons: 4, pointerId: 1, pointerType: 'mouse',
      clientX: cx, clientY: cy, bubbles: true, cancelable: true,
    }))
    let angle = 0
    moveTimer = setInterval(() => {
      angle += 0.12
      element.dispatchEvent(new PointerEvent('pointermove', {
        button: 1, buttons: 4, pointerId: 1, pointerType: 'mouse',
        clientX: cx + Math.cos(angle) * 260, clientY: cy + Math.sin(angle) * 90,
        bubbles: true, cancelable: true,
      }))
    }, 16)
  }

  await sleep(ms)
  if (wheelTimer) clearInterval(wheelTimer)
  if (moveTimer) {
    clearInterval(moveTimer)
    element.dispatchEvent(new PointerEvent('pointerup', { button: 1, pointerId: 1, pointerType: 'mouse', bubbles: true }))
  }
  stop = true
  const counters = meter.stop()
  await sleep(250)
  return { mode, fps: counters.drawFps, ...summarise(deltas), ...counters, fetchCalls: callCount }
}

/** Let the page sit still: measures the idle redraw cost, not the interaction cost. */
window.__idle = async (ms = 3000) => {
  const deltas = []
  let last = null
  let stop = false
  const tick = (t) => {
    if (last !== null) deltas.push(t - last)
    last = t
    if (!stop) requestAnimationFrame(tick)
  }
  requestAnimationFrame(tick)
  const meter = startMeter()
  await sleep(ms)
  stop = true
  const counters = meter.stop()
  await sleep(200)
  return { fps: counters.drawFps, ...summarise(deltas), ...counters }
}

// ---------------------------------------------------------------- counters
/** Counts the draw loop's own work: canvas frames (2 clearRect per draw) and
 *  custom-property writes per name — the traffic that forces style recalc. */
function startMeter() {
  const proto = CSSStyleDeclaration.prototype
  const ctxProto = CanvasRenderingContext2D.prototype
  const originalSet = proto.setProperty
  const originalClear = ctxProto.clearRect
  const byName = Object.create(null)
  let draws = 0
  proto.setProperty = function patchedSet(name, value, priority) {
    byName[name] = (byName[name] || 0) + 1
    return originalSet.call(this, name, value, priority)
  }
  // Only the back canvas is cleared on every frame (the front one is lazy), so count that
  // one as the frame counter — counting both would halve the reported frame rate.
  ctxProto.clearRect = function patchedClear(...args) {
    if (this.canvas && this.canvas.classList && this.canvas.classList.contains('emc-g-back')) draws += 1
    return originalClear.apply(this, args)
  }
  const t0 = performance.now()
  return {
    stop() {
      const ms = performance.now() - t0
      proto.setProperty = originalSet
      ctxProto.clearRect = originalClear
      const writes = Object.values(byName).reduce((a, b) => a + b, 0)
      return {
        ms: Math.round(ms),
        drawFrames: draws,
        drawFps: +(draws / (ms / 1000)).toFixed(1),
        writes,
        writesPerFrame: draws ? +(writes / draws).toFixed(0) : 0,
        customM: byName['--m'] || 0,
        customF: byName['--f'] || 0,
      }
    },
  }
}

// ---------------------------------------------------------------- A/B patches
// Each patch removes exactly one cost from the render path so the delta is that
// cost's share of the frame. Only one patch is active at a time.
let activeRestore = null
window.__patch = (name) => {
  window.__unpatch()
  const style = document.createElement('style')
  style.id = 'em-bench-patch'
  const css = (text) => { style.textContent = text; document.head.appendChild(style) }
  const skipSet = (predicate) => {
    const proto = CSSStyleDeclaration.prototype
    const original = proto.setProperty
    proto.setProperty = function patched(name2, value, priority) {
      if (predicate(name2)) return undefined
      return original.call(this, name2, value, priority)
    }
    return () => { proto.setProperty = original }
  }
  const patches = {
    // The dot→sphere→card morph: width/height/border-radius/background all keyed on --m/--f.
    morph: () => skipSet((n) => n === '--m' || n === '--f'),
    // Per-frame position: compositor transforms for every atom.
    transform: () => skipSet((n) => n === 'transform'),
    // Stacking order rewrite for every atom.
    'z-index': () => skipSet((n) => n === 'z-index'),
    // Card text raster: the only text in the scene.
    text: () => { css('.emc-g-card{display:none!important}'); return () => style.remove() },
    // Layer promotion on every atom.
    willchange: () => { css('.emc-g-atom{will-change:auto!important}'); return () => style.remove() },
    shadow: () => { css('.emc-g-body{box-shadow:none!important}'); return () => style.remove() },
    edges: () => { css('.emc-g-canvas{display:none!important}'); return () => style.remove() },
    // Layer promotion: the card's scaled text, and the atom's per-frame transform.
    'card-layer': () => {
      css('.emc-g-card{will-change:transform!important;backface-visibility:hidden}')
      return () => style.remove()
    },
    'atom-layer': () => {
      css('.emc-g-atom{will-change:transform!important}')
      return () => style.remove()
    },
    // Card scale without a layer: measures what the text re-raster costs.
    'card-no-scale': () => {
      css('.emc-g-card{transform:translate(-50%,-50%)!important}')
      return () => style.remove()
    },
    // Everything the old renderer did per atom: blurred shadow, a layer per atom, and a
    // fixed-size card whose text does not scale with the box.
    legacy: () => {
      css(`.emc-g-body{box-shadow:0 2px 8px rgba(0,0,0,.18)!important}
           .emc-g-atom{will-change:transform!important}
           .emc-g-card{transform:translate(-50%,-50%)!important}`)
      return () => style.remove()
    },
    hover: () => { css('.emc-g-atom:hover .emc-g-body{box-shadow:none!important}'); return () => style.remove() },
  }
  const make = patches[name]
  if (!make) return false
  activeRestore = make()
  return true
}
window.__unpatch = () => {
  if (activeRestore) { activeRestore(); activeRestore = null }
  const style = document.getElementById('em-bench-patch')
  if (style) style.remove()
}

window.__gpu = () => {
  try {
    const gl = document.createElement('canvas').getContext('webgl')
    const info = gl.getExtension('WEBGL_debug_renderer_info')
    return gl.getParameter(info.UNMASKED_RENDERER_WEBGL)
  } catch (error) { return 'no webgl: ' + error.message }
}

/** Count style writes the render loop performs per frame (patched for one frame only). */
window.__writeProbe = async (ms = 1000) => {
  const meter = startMeter()
  await sleep(ms)
  return meter.stop()
}

setHud(`${graph.events.length} atoms / ${graph.edges.length + graph.pairs.length} edges · ready`)
window.__ready = true
