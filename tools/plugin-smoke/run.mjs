// Smoke-render the eventmem-gui plugin outside the app: stub the SDK, feed canned
// payloads, and render every tab through react-dom/server. Catches render-time crashes
// (bad hooks, undefined children, wrong props) that a static syntax check cannot.
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { readFileSync } from 'node:fs'

globalThis.__canned = null
// register() injects a <style>; a three-line stub is enough to let it run outside a DOM.
globalThis.document = {
  head: { append: () => {} },
  createElement: () => ({ dataset: {}, textContent: '', remove: () => {} })
}
globalThis.window = globalThis
const plugin = (await import('./plugin.js')).default
const parts = await import('./plugin.js')

const entries = []
const ctx = {
  rest: (path) => Promise.resolve((globalThis.__canned || {})[path] ?? {}),
  register: (entry) => entries.push(entry),
  onDispose: () => {},
  setInterval: () => () => {},
  setInterval_: null
}

plugin.register(ctx)

const CANNED = {
  '/api/health': { status: 'ok', pid: 4242, schema: 8, db: '/home/x/.hermes/eventmem/events.db' },
  '/api/stats': {
    events: 306,
    edges: 1712,
    pairs: 905,
    newest: 1794419860,
    kinds: { 决定: 41, 结果: 120, 教训: 33 },
    projects: [{ name: 'eventmem', events: 120, last: 1794419860 }, { name: 'unfiled', events: 8, last: 1794419000 }]
  },
  '/api/status': {
    cursor: { events: 0, behind: 42 },
    runs: [{ ts: 1794419860, pairs: 2, edges: 1, backend: 'startlux' }, { ts: 1794419000, skipped: 'no work' }],
    // A pass in flight: this is what the live strip reads (backend, counters, stop flag).
    live: { backend: 'win-autothink', pairs: 12, edges: 9, since: 1794420000, updated: 1794420030 },
    busy: true,
    stopping: false
  },
  '/api/config': {
    file: '/home/x/.hermes/eventmem/settings.yaml',
    effective: {
      decision: { backends: ['startlux', 'typesafe'] },
      edges: {
        heads_vec_top_k: 10, heads_cos_min: 0.35, margin: 0.15, session_heads_top_k: 10,
        unclosed_lessons: 2, decision_skeleton: 5, cross_top_k: 3, cross_cos_min: 0.7, poll_seconds: 60
      }
    }
  },
  '/api/packs': {
    dir: '/home/x/.hermes/eventmem/backends',
    problems: [],
    packs: [
      {
        name: 'startlux', kind: 'external', protocol: 'systemone', model: 'jev',
        url: 'http://127.0.0.1:18090', in_chain: true, running: null, needs_key: false,
        license: 'apache-2.0', config: 'protocol: systemone\nmodel: jev\n'
      },
      {
        name: 'lev', kind: 'self', protocol: 'systemone', model: 'lev',
        weights: '../../models/lev/lev-Q4_K_M.gguf', in_chain: false, running: true, port: 43111,
        needs_key: false, license: 'apache-2.0', config: 'protocol: systemone\n'
      },
      {
        name: 'typesafe', kind: 'remote', protocol: 'systemone', model: 'jev-latest',
        url: 'https://api.typesafe.ai', in_chain: false, running: null, needs_key: true,
        license: 'proprietary', config: 'protocol: systemone\n'
      }
    ]
  },
  '/api/models': {
    llama: { build: 'b12284', asset: 'llama-b12284-bin-ubuntu-x64.tar.gz', binary: '/runtimes/llama.cpp/b12284/llama-server' },
    models: [{ repo: 'ggml-org/lev-GGUF', file: 'lev-Q4_K_M.gguf', size: 2.4e9 }]
  },
  '/api/job': { job: { state: 'running', phase: 'lev-Q4_K_M.gguf', done: 1.1e9, total: 2.4e9 } },
  '/api/logs': {
    logs: [
      { ts: 1794419860, level: 'INFO', name: 'eventmem.daemon', message: 'pass done pairs=2 edges=1 backend=startlux' },
      { ts: 1794419800, level: 'WARNING', name: 'eventmem.decision', message: 'typesafe unavailable (needs_key)' },
      { ts: 1794419700, level: 'ERROR', name: 'eventmem.worker', message: 'boom\nsecond line' }
    ]
  },
  '/api/i18n': { lang: 'zh', map: {} },
  '/api/graph': { events: [], edges: [] }
}

// The dictionary is Python's (core/eventmem_core/ui_strings.py, written here as map.json by
// run.sh) — one source for both UIs, so drift is impossible by construction.
let EN_MAP = null
try {
  EN_MAP = JSON.parse(readFileSync(new URL('./map.json', import.meta.url), 'utf8'))
} catch {
  EN_MAP = null
}

const tabs = { Overview: parts.Overview, Backends: parts.Backends, Models: parts.Models, Schedule: parts.Schedule, Logs: parts.Logs, GraphTab: parts.GraphTab }

function render(name, element) {
  try {
    const html = renderToStaticMarkup(element)
    const label = html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()
    console.log(`OK   ${name.padEnd(10)} ${String(html.length).padStart(6)}B  ${label.slice(0, 70)}`)
    return true
  } catch (error) {
    console.log(`FAIL ${name}: ${error && error.stack ? error.stack.split('\n').slice(0, 4).join(' | ') : error}`)
    return false
  }
}

let bad = 0
const page = entries.find((entry) => entry.id === 'eventmem-gui-page')
const chip = entries.find((entry) => entry.id === 'eventmem-gui-chip')
if (!page) throw new Error('page route was not registered')
if (!chip) throw new Error('status-bar chip was not registered')

console.log('entry points:', entries.map((entry) => `${entry.id}@${entry.area}${entry.data ? entry.data.path : ''}`).join(', '))

for (const phase of ['empty', 'canned']) {
  globalThis.__canned = phase === 'canned' ? CANNED : null
  console.log(`--- ${phase} payloads`)
  if (!render(`page/${phase}`, React.createElement(page.render))) bad += 1
  for (const [name, component] of Object.entries(tabs)) {
    if (name === 'GraphTab') continue
    const props = name === 'Overview' || name === 'Logs' ? { ctx } : { ctx, notify: () => {} }
    if (!render(`${name}/${phase}`, React.createElement(component, props))) bad += 1
  }
  if (!render(`chip/${phase}`, React.createElement(chip.render))) bad += 1
}

// The graph tab without the handshake must show the hint, not crash.
globalThis.__eventmemGraph = undefined
if (!render('GraphTab/none', React.createElement(tabs.GraphTab, { notify: () => {} }))) bad += 1

// With the handshake in place the tab must render the graph plugin's own component.
globalThis.__eventmemGraph = { GraphPage: () => React.createElement('div', { className: 'fake-graph' }, 'GRAPH'), ctx }
if (!render('GraphTab/on', React.createElement(tabs.GraphTab, { notify: () => {} }))) bad += 1

// ---- English pass: the dictionary comes from ui_strings.py, so this checks the real
// translations (and that no Chinese literal was left behind).
if (!EN_MAP) {
  console.log('FAIL en pass: map.json missing')
  bad += 1
} else {
  globalThis.__canned = { ...CANNED, '/api/i18n': { lang: 'en', map: EN_MAP } }
  // Console assigns LANG/MAP; the tab components rendered after it come out in English.
  // The page is checked too — the live strip lives there, not in a tab.
  try {
    const html = renderToStaticMarkup(React.createElement(page.render))
    const text = html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').replace(/决定|尝试|结果|变更|教训/g, ' ')
    const hit = text.match(/[\u4e00-\u9fff]/)
    if (hit) {
      const at = text.indexOf(hit[0])
      console.log(`FAIL page/en still Chinese: …${text.slice(Math.max(0, at - 20), at + 24)}…`)
      bad += 1
    } else {
      console.log(`OK   page/en     ${String(html.length).padStart(6)}B  no Chinese left`)
    }
  } catch (error) {
    console.log(`FAIL page/en threw: ${error && error.message}`)
    bad += 1
  }
  for (const [name, component] of Object.entries(tabs)) {
    if (name === 'GraphTab') continue
    const props = name === 'Overview' || name === 'Logs' ? { ctx } : { ctx, notify: () => {} }
    let html = ''
    try {
      html = renderToStaticMarkup(React.createElement(component, props))
    } catch (error) {
      console.log(`FAIL ${name}/en threw: ${error && error.message}`)
      bad += 1
      continue
    }
    // Kind and relation names are stored data (deliberately never translated), so they are
    // removed before the check instead of being whitelisted one by one.
    const text = html
      .replace(/<[^>]+>/g, ' ')
      .replace(/\s+/g, ' ')
      .replace(/决定|尝试|结果|变更|教训/g, ' ')
    const hit = text.match(/[\u4e00-\u9fff]/)
    if (hit) {
      const at = text.indexOf(hit[0])
      console.log(`FAIL ${name}/en still Chinese: …${text.slice(Math.max(0, at - 20), at + 24)}…`)
      bad += 1
    } else {
      console.log(`OK   ${name}/en     ${String(html.length).padStart(6)}B  no Chinese left`)
    }
  }
}

console.log(bad === 0 ? 'SMOKE PASS' : `SMOKE FAIL (${bad})`)
process.exit(bad === 0 ? 0 : 1)
