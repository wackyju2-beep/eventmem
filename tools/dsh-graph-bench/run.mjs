/**
 * Real-Chromium benchmark for the DSH memory-console graph (`adapters/dsh/client.js`).
 *
 *   node tools/dsh-graph-bench/run.mjs                 # idle + zoom + orbit + profile
 *   node tools/dsh-graph-bench/run.mjs --tag=before     # names the report
 *
 * Serves the harness, launches headless Chrome over CDP, and reports:
 *   - rAF fps / p95 / long frames while the camera is driven (zoom and orbit),
 *   - the idle redraw cost (the graph animates drift even with no input),
 *   - custom-property writes per second (the morph's style traffic),
 *   - compositor layer count (will-change on every atom = one layer per atom),
 *   - Layout / RecalcStyle / Script seconds from Performance metrics,
 *   - a sampled CPU profile of the hot frames, aggregated by function,
 *   - PNG screenshots at rest and mid-morph so the change can be eyeballed.
 */
import { spawn } from 'node:child_process'
import http from 'node:http'
import fs from 'node:fs'
import path from 'node:path'
import os from 'node:os'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const REPO = path.resolve(HERE, '..', '..')
const SMOKE = path.join(REPO, 'tools', 'dsh-console-smoke', 'node_modules')
const ARGS = Object.fromEntries(process.argv.slice(2).map((a) => {
  const [k, v] = a.replace(/^--/, '').split('=')
  return [k, v === undefined ? true : v]
}))
const TAG = ARGS.tag || 'run'
const MS = Number(ARGS.ms || 4000)
const PORT = Number(ARGS.port || 8899)
const CDP_PORT = Number(ARGS.cdp || 9333)
const WIDTH = Number(ARGS.width || 1600)
const HEIGHT = Number(ARGS.height || 1000)
const DPR = Number(ARGS.dpr || 1)
const AB = ARGS.ab !== '0'

const CHROME = ['C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe',
  path.join(process.env.LOCALAPPDATA || '', 'Google/Chrome/Application/chrome.exe')]
  .find((p) => p && fs.existsSync(p))
if (!CHROME) throw new Error('Chrome not found')

// Node 22+ ships a global WebSocket; older runtimes fall back to the copy the console
// smoke test already depends on. Either way this is a plain client, no CDP package.
const WebSocketImpl = globalThis.WebSocket
  || (await import(new URL('file://' + path.join(SMOKE, 'ws', 'index.js')).href)).WebSocket

// ------------------------------------------------------------------ static server
const MIME = {
  '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8', '.png': 'image/png',
}
const server = http.createServer((req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1')
  let file = null
  if (url.pathname === '/' || url.pathname === '/index.html') file = path.join(HERE, 'index.html')
  else if (url.pathname === '/bench.js') file = path.join(HERE, 'bench.js')
  else if (url.pathname === '/graph.json') file = path.join(HERE, 'graph.json')
  else if (url.pathname === '/client.js') file = path.join(REPO, 'adapters', 'dsh', 'client.js')
  else if (url.pathname === '/react.js') file = path.join(SMOKE, 'react', 'umd', 'react.development.js')
  else if (url.pathname === '/react-dom.js') file = path.join(SMOKE, 'react-dom', 'umd', 'react-dom.development.js')
  if (!file || !fs.existsSync(file)) {
    res.writeHead(404).end('not found')
    return
  }
  res.writeHead(200, {
    'content-type': MIME[path.extname(file)] || 'application/octet-stream',
    'cache-control': 'no-store',
  })
  fs.createReadStream(file).pipe(res)
})
await new Promise((resolve) => server.listen(PORT, '127.0.0.1', resolve))

// ------------------------------------------------------------------ chrome
const profileDir = fs.mkdtempSync(path.join(os.tmpdir(), 'em-graph-bench-'))
const chrome = spawn(CHROME, [
  '--headless=new',
  `--remote-debugging-port=${CDP_PORT}`,
  `--user-data-dir=${profileDir}`,
  '--no-first-run', '--no-default-browser-check', '--disable-extensions',
  '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
  '--hide-scrollbars', `--window-size=${WIDTH},${HEIGHT}`,
  ...(ARGS.gpu === 'off' ? ['--disable-gpu'] : []),
  'about:blank',
], { stdio: 'ignore' })

const waitHttp = async (url, ms = 20000) => {
  const t0 = Date.now()
  while (Date.now() - t0 < ms) {
    try {
      const res = await fetch(url)
      if (res.ok) return res.json()
    } catch { /* not up yet */ }
    await new Promise((r) => setTimeout(r, 150))
  }
  throw new Error(`timed out waiting for ${url}`)
}

const version = await waitHttp(`http://127.0.0.1:${CDP_PORT}/json/version`)
const ws = new WebSocketImpl(version.webSocketDebuggerUrl)
const wsOn = (event, fn) => ws.addEventListener(event, fn)
const wsOnce = (event, fn) => ws.addEventListener(event, fn, { once: true })
await new Promise((resolve, reject) => { wsOnce('open', resolve); wsOnce('error', reject) })

let nextId = 0
const pending = new Map()
const listeners = new Map()
wsOn('message', (event) => {
  const msg = JSON.parse(String(event.data))
  if (msg.id) {
    const job = pending.get(msg.id)
    if (!job) return
    pending.delete(msg.id)
    if (msg.error) job.reject(new Error(msg.error.message))
    else job.resolve(msg.result)
    return
  }
  const key = `${msg.method}|${msg.sessionId || ''}`
  for (const fn of listeners.get(key) || []) fn(msg.params)
})
const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
  const id = ++nextId
  pending.set(id, { resolve, reject })
  ws.send(JSON.stringify({ id, method, params, sessionId }))
})
const on = (method, sessionId, fn) => {
  const key = `${method}|${sessionId || ''}`
  if (!listeners.has(key)) listeners.set(key, [])
  listeners.get(key).push(fn)
}
const waitEvent = (method, sessionId, ms = 15000) => new Promise((resolve, reject) => {
  const key = `${method}|${sessionId || ''}`
  const timer = setTimeout(() => reject(new Error(`no event ${method}`)), ms)
  const fn = (params) => {
    clearTimeout(timer)
    listeners.set(key, (listeners.get(key) || []).filter((f) => f !== fn))
    resolve(params)
  }
  if (!listeners.has(key)) listeners.set(key, [])
  listeners.get(key).push(fn)
})

const { targetId } = await send('Target.createTarget', { url: 'about:blank' })
const { sessionId } = await send('Target.attachToTarget', { targetId, flatten: true })
const cmd = (method, params) => send(method, params, sessionId)

await cmd('Page.enable')
await cmd('Runtime.enable')
await cmd('Log.enable')
await cmd('Performance.enable')
await cmd('Profiler.enable')

// Page-side noise is what explains a harness failure, so keep it.
const pageLogs = []
on('Runtime.exceptionThrown', sessionId, (p) => pageLogs.push(
  'EXC ' + (p.exceptionDetails.exception?.description || p.exceptionDetails.text)))
on('Runtime.consoleAPICalled', sessionId, (p) => pageLogs.push(
  p.type + ' ' + p.args.map((a) => a.value ?? a.description ?? '').join(' ')))
on('Log.entryAdded', sessionId, (p) => pageLogs.push('LOG ' + p.entry.text))
await cmd('Emulation.setDeviceMetricsOverride', {
  width: WIDTH, height: HEIGHT, deviceScaleFactor: DPR, mobile: false,
})

const evaluate = async (expression, { awaitPromise = true } = {}) => {
  const result = await cmd('Runtime.evaluate', {
    expression, awaitPromise, returnByValue: true, allowUnsafeEvalBlockedByCSP: true,
  })
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text)
  }
  return result.result.value
}

const loaded = waitEvent('Page.loadEventFired', sessionId, 30000)
await cmd('Page.navigate', { url: `http://127.0.0.1:${PORT}/index.html` })
await loaded

const t0 = Date.now()
let ready = false
while (Date.now() - t0 < 30000) {
  ready = await evaluate('window.__ready === true', { awaitPromise: false })
  if (ready) break
  await new Promise((r) => setTimeout(r, 200))
}
if (!ready) {
  const hud = await evaluate('document.getElementById("hud") && document.getElementById("hud").textContent', { awaitPromise: false })
  throw new Error('page never became ready: ' + hud + '\n' + pageLogs.join('\n'))
}

// ------------------------------------------------------------------ metrics helpers
const readMetrics = async () => {
  const { metrics } = await cmd('Performance.getMetrics')
  return Object.fromEntries(metrics.map((m) => [m.name, m.value]))
}
const diffMetrics = (a, b) => {
  const keys = ['TaskDuration', 'ScriptDuration', 'LayoutDuration', 'RecalcStyleDuration',
    'LayoutCount', 'RecalcStyleCount', 'JSHeapUsedSize', 'Nodes']
  const out = {}
  for (const key of keys) if (a[key] !== undefined && b[key] !== undefined) out[key] = +(b[key] - a[key]).toFixed(4)
  return out
}

const layerCount = async () => {
  let last = null
  on('LayerTree.layerTreeDidChange', sessionId, (params) => { last = params })
  await cmd('LayerTree.enable')
  await new Promise((r) => setTimeout(r, 700))
  await cmd('LayerTree.disable')
  return last && last.layers ? last.layers.length : null
}

const shot = async (name) => {
  const { data } = await cmd('Page.captureScreenshot', { format: 'png' })
  const file = path.join(HERE, `${TAG}-${name}.png`)
  fs.writeFileSync(file, Buffer.from(data, 'base64'))
  return file
}

const report = { tag: TAG, chrome: path.basename(CHROME), viewport: `${WIDTH}x${HEIGHT}@${DPR}x`, when: new Date().toISOString() }

// Steady state at rest.
await new Promise((r) => setTimeout(r, 800))
// Warm up: the first wheel zoom after load pays shader/raster/layer setup for the cards;
// measuring it cold says nothing about the steady state (and made A/B arms look causal).
await evaluate('window.__bench({ ms: 1600, mode: "zoom" })')
await evaluate('window.__bench({ ms: 1200, mode: "orbit" })')
await new Promise((r) => setTimeout(r, 400))
report.gpu = await evaluate('window.__gpu()', { awaitPromise: false })
report.atoms = await evaluate('document.querySelectorAll(".emc-g-atom").length', { awaitPromise: false })
report.layers = await layerCount()
report.idle = await evaluate('window.__idle(3000)')
report.writes = await evaluate('window.__writeProbe(1000)')
report.screens = { rest: await shot('rest') }

// Two parked zoom levels: one where cards are mid-morph (the old clipping bug) and one
// close enough for full-size cards.
await evaluate('window.__pose(18)')
report.screens.mid = await shot('mid')
await evaluate('window.__pose(42)')
report.screens.close = await shot('close')

// Selection path: fade + retreat + inspector + highlighted edges on the front canvas.
report.select = await evaluate('window.__select()')
report.screens.selected = await shot('selected')
report.deselect = await evaluate('window.__deselect()')
// The three interaction rules: highlighted-neighborhood click, edge gating, filter-panel gate.
report.ringPolicy = await evaluate('window.__ringPolicy()')
report.edgePolicy = await evaluate('window.__edgePolicy()')
report.panelPolicy = await evaluate('window.__panelPolicy()')
report.zoomOut = await evaluate('window.__zoomOut()')
report.sweepOut = await evaluate('window.__sweep(1, 24)')
report.sweepIn = await evaluate('window.__sweep(-1, 24)')
report.filter = await evaluate('window.__filter("决定")')
report.screens.filtered = await shot('filtered')
report.screens.deselected = await shot('deselected')

// Interaction: wheel zoom in/out (drives the dot→sphere→card morph).
await evaluate('window.__reset()')
let before = await readMetrics()
report.zoom = await evaluate(`window.__bench(${JSON.stringify({ ms: MS, mode: 'zoom' })})`)
let after = await readMetrics()
report.zoom.metrics = diffMetrics(before, after)
report.screens.zoom = await shot('zoom')

// Interaction: middle-drag orbit (camera rotates through the cloud).
await evaluate('window.__reset()')
before = await readMetrics()
report.orbit = await evaluate(`window.__bench(${JSON.stringify({ ms: MS, mode: 'orbit' })})`)
after = await readMetrics()
report.orbit.metrics = diffMetrics(before, after)

// Same interaction, measured again at the end: if this drifts from the first measurement,
// the run itself is not comparable and the arms mean nothing.
let baseline2 = null
// A/B: remove one cost at a time and re-measure the same zoom interaction.
// The delta against `zoom` is that cost's share of a frame.
report.ab = {}
if (AB) {
  const arms = ARGS.arms ? String(ARGS.arms).split(',') : ['morph', 'transform', 'z-index', 'text',
    'willchange', 'shadow', 'edges', 'card-layer', 'atom-layer', 'card-no-scale']
  for (const patch of arms) {
    await evaluate(`window.__patch(${JSON.stringify(patch)})`, { awaitPromise: false })
    // Warm-up per arm: the first frames after a style change pay raster/layer setup.
    await evaluate('window.__reset()')
    await evaluate('window.__bench({ ms: 700, mode: "zoom" })')
    await evaluate('window.__reset()')
    const abBefore = await readMetrics()
    const run = await evaluate(`window.__bench(${JSON.stringify({ ms: Math.round(MS * 0.8), mode: 'zoom' })})`)
    run.metrics = diffMetrics(abBefore, await readMetrics())
    await evaluate('window.__unpatch()', { awaitPromise: false })
    await new Promise((r) => setTimeout(r, 300))
    report.ab[patch] = run
  }
  report.baseline2 = await (async () => {
    await evaluate('window.__reset()')
    const m0 = await readMetrics()
    const run = await evaluate(`window.__bench(${JSON.stringify({ ms: MS, mode: 'zoom' })})`)
    run.metrics = diffMetrics(m0, await readMetrics())
    return run
  })()
  baseline2 = report.baseline2
}

// CPU profile of the same zoom interaction.
await cmd('Profiler.setSamplingInterval', { interval: 200 })
await cmd('Profiler.start')
await evaluate('window.__reset()')
await evaluate(`window.__bench(${JSON.stringify({ ms: MS, mode: 'zoom' })})`)
const { profile } = await cmd('Profiler.stop')
const byFunction = new Map()
for (const node of profile.nodes) {
  const hits = node.hitCount || 0
  if (!hits) continue
  const frame = node.callFrame
  const file = (frame.url || '').split('/').pop() || '(anonymous)'
  const key = `${frame.functionName || '(anonymous)'} @ ${file}:${frame.lineNumber + 1}`
  byFunction.set(key, (byFunction.get(key) || 0) + hits)
}
const samples = [...byFunction.entries()].sort((a, b) => b[1] - a[1]).slice(0, 25)
report.profileTop = samples.map(([key, hits]) => ({
  key, hits, ms: +((hits * 200) / 1000).toFixed(1),
}))

const outFile = path.join(HERE, `${TAG}-report.json`)
fs.writeFileSync(outFile, JSON.stringify(report, null, 2))

// ------------------------------------------------------------------ console summary
const scene = JSON.parse(fs.readFileSync(path.join(HERE, 'graph.json'), 'utf8'))
const fmt = (o) => `fps ${o.fps} · median ${o.median}ms · p95 ${o.p95}ms · max ${o.max}ms · long>33ms ${o.longFrames}/${o.frames}`
const per = (o) => (o.metrics ? `· main ${(o.metrics.TaskDuration / Math.max(1, o.drawFrames) * 1000).toFixed(1)}ms/frame` : '')
console.log(`\n== ${TAG} ==  ${scene.events.length} atoms / ${scene.edges.length + scene.pairs.length} edges · ${WIDTH}x${HEIGHT}@${DPR}x · ${report.layers} compositor layers`)
console.log(`gpu      ${report.gpu}`)
console.log(`idle     ${fmt(report.idle)} · draw ${report.idle.drawFps}/s · ${report.idle.writesPerFrame} style writes/frame`)
console.log(`zoom     ${fmt(report.zoom)} ${per(report.zoom)} · draw ${report.zoom.drawFps}/s · ${report.zoom.writesPerFrame} writes/frame (--m ${report.zoom.customM}, --f ${report.zoom.customF})`)
console.log(`orbit    ${fmt(report.orbit)} ${per(report.orbit)}`)
console.log(`writes   ${report.writes.writes} style writes / ${report.writes.ms}ms · --m ${report.writes.customM} · --f ${report.writes.customF}`)
console.log(`morph state at rest: ${report.idle.cards} cards · ${report.idle.midMorph} mid · ${report.idle.dots} dots`)
console.log(`select: ${JSON.stringify(report.select)}`)
console.log(`deselect: ${JSON.stringify(report.deselect)}`)
console.log(`ringPolicy: ${JSON.stringify(report.ringPolicy)}`)
console.log(`edgePolicy: ${JSON.stringify(report.edgePolicy)}`)
console.log(`panelPolicy: ${JSON.stringify(report.panelPolicy)}`)
console.log(`zoomOut: ${JSON.stringify(report.zoomOut)}`)
console.log(`sweepOut max ${report.sweepOut.max}: ${report.sweepOut.trace.map((t) => t.visible).join(',')}`)
console.log(`sweepIn  max ${report.sweepIn.max}: ${report.sweepIn.trace.map((t) => t.visible).join(',')}`)
console.log(`filter: ${JSON.stringify(report.filter)}`)
if (AB) {
  console.log('--- A/B (zoom; draw/s is the app frame rate, higher = better) ---')
  const base = report.zoom
  console.log(`baseline            ${base.fps} draw/s  ${(base.metrics.TaskDuration / base.drawFrames * 1000).toFixed(2)} ms/frame`)
  for (const [name, run] of Object.entries(report.ab)) {
    console.log(`with ${name.padEnd(14)} ${run.fps} draw/s  ${(run.metrics.TaskDuration / run.drawFrames * 1000).toFixed(2)} ms/frame`)
  }
  if (report.baseline2) console.log(`baseline again      ${report.baseline2.fps} draw/s  ${(report.baseline2.metrics.TaskDuration / report.baseline2.drawFrames * 1000).toFixed(2)} ms/frame`)
}
console.log('--- CPU profile (self time) ---')
for (const row of report.profileTop.slice(0, 14)) console.log(`${String(row.ms).padStart(8)}ms  ${row.hits.toString().padStart(5)}  ${row.key}`)
console.log(`\nreport: ${outFile}`)
console.log(`screens: ${report.screens.rest}\n         ${report.screens.mid}\n         ${report.screens.close}\n         ${report.screens.zoom}`)

// ------------------------------------------------------------------ teardown
ws.close()
chrome.kill()
server.close()
process.exit(0)
