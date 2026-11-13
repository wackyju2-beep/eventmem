/**
 * entry.mjs — mount the real eventmem console in a browser, against a fake ctx.
 *
 * The smoke harness renders once through react-dom/server; it cannot show whether the
 * overview really refreshes, because that lives in effects and intervals. Here the page
 * runs in Chromium with a fake `ctx.rest`, so both the layout (real CSS, real viewport)
 * and the polling behaviour are observable.
 *
 *   ?page=overview|backends|models|schedule|logs
 *   &live=1        /api/stats counts up on every call (a pass in progress)
 *   &slim=1        the "empty library" payloads
 */
import React from 'react'
import { createRoot } from 'react-dom/client'
import plugin, { Console } from './plugin.patched.js'

const query = new URLSearchParams(location.search)
const page = query.get('page') || 'overview'
const live = query.get('live') === '1'
const slim = query.get('slim') === '1'

const T0 = 1794419000
let calls = 0
let edges = 2381
let pairs = 3254

const kindBase = slim ? {} : { 结果: 151, 变更: 104, 教训: 81, 决定: 54, 尝试: 12 }
const projects = slim ? [] : [
  { name: 'eventmem', events: 266, last: T0 + 60 },
  { name: '仪器分析实验报告', events: 64, last: T0 - 800000 },
  { name: '未归类', events: 32, last: T0 - 1000000 },
  { name: '中级有机实验报告', events: 23, last: T0 - 1100000 },
  { name: '化学工程基础', events: 17, last: T0 - 900000 }
]

const edgesConfig = {
  heads_vec_top_k: 10, heads_cos_min: 0.35,
  session_heads_top_k: 10, session_recent: 3, unclosed_lessons: 2, decision_skeleton: 5,
  cross_top_k: 3, cross_cos_min: 0.7, poll_seconds: 60,
  margin: 0.15, min_conf_change: 0, min_conf_negate: 0, min_conf_repeat: 0,
  min_conf_because: 0, min_conf_refine: 0
}

const STATUS = {
  cursor: { events: 0, behind: 480 },
  runs: [
    { ts: T0 + 60, skipped: 'idle' }, { ts: T0, skipped: 'idle' }, { ts: T0 - 60, skipped: 'idle' },
    { ts: T0 - 120, skipped: 'idle' }, { ts: T0 - 180, skipped: 'idle' }
  ],
  backends: [{ name: 'startlux', kind: 'self', running: true, port: 18090, in_chain: true }],
  live: null, backfill: { state: 'idle' }, busy: false, stopping: false, down: {}
}

const PAYLOAD = {
  '/api/health': { status: 'ok', pid: 873078, schema: 9, db: '/home/wacky_ju2/.hermes/eventmem/events.db' },
  '/api/i18n': { lang: 'zh', map: {} },
  '/api/config': { effective: { edges: edgesConfig, decision: { backends: ['startlux', 'win-autothink'] } },
                   file: '/home/wacky_ju2/.hermes/eventmem/settings.yaml', overrides: { edges: {} }, writable: true },
  '/api/status': null,
  '/api/logs': { logs: [{ ts: T0, level: 'INFO', name: 'eventmem.daemon', message: 'eventmem daemon: ok' }] },
  '/api/packs': { packs: [], problems: [], dir: '/home/wacky_ju2/.hermes/eventmem/backends' },
  '/api/models': { llama: { build: 'b12284', binary: '/home/x/llama-server' }, models: [] },
  '/api/job': { job: null },
  '/api/stats': null
}

const ctx = {
  rest: (path, opts) => {
    calls += 1
    if (path === '/api/stats') {
      if (live) {
        // Every poll looks like the pass answered another pair and wrote an edge.
        edges += 3
        pairs += 3
        return Promise.resolve({
          events: 402, edges, pairs, newest: T0 + 120,
          kinds: kindBase, projects
        })
      }
      return Promise.resolve({
        events: slim ? 0 : 402, edges: slim ? 0 : 2381, pairs: slim ? 0 : 3254,
        newest: slim ? 0 : T0 + 120, kinds: kindBase, projects
      })
    }
    if (path === '/api/status') {
      // live=1 means "a pass is running right now": the strip ticks and the overview is
      // supposed to tighten its poll to the same tempo.
      return Promise.resolve(live
        ? { ...STATUS, busY: true, busy: true, live: { backend: 'startlux', pairs: Math.round((pairs - 3254) / 3), edges: edges - 2381, since: Date.now() / 1000 - 42 } }
        : { ...STATUS })
    }
    if (path === '/api/config' && opts && opts.method === 'POST') {
      const body = opts.body || {}
      ;(window.__posts = window.__posts || []).push({ path, body })
      if (body.edges) Object.assign(edgesConfig, body.edges)
      return Promise.resolve({ ok: true })
    }
    return Promise.resolve(PAYLOAD[path] ?? {})
  },
  register: () => {},
  onDispose: () => {},
  socket: () => () => {},
  storage: { get: (k, d) => d, set: () => {} },
  i18n: { t: (s) => s },
  os: {},
  pet: {},
  runAction: () => Promise.resolve(),
  listActions: () => [],
  preview: {},
  files: {}
}

// register() is what injects the plugin's <style>; the chip is harmless in the harness.
const root = createRoot(document.getElementById('root'))
root.render(React.createElement(Console, { ctx }))

// Give scripts a handle on the tab strip and on the block geometry.
const ACCEPT_KEYS = ['margin', 'min_conf_change', 'min_conf_negate', 'min_conf_repeat', 'min_conf_because', 'min_conf_refine']

window.__harness = {
  page,
  calls: () => calls,
  tab: (label) => {
    const button = [...document.querySelectorAll('.emg-tab')].find((node) => node.textContent.trim() === label)
    if (button) button.click()
    return Boolean(button)
  },
  /** Top-level blocks of the scrolling body: class + rect, in document order. */
  blocks: () => {
    const body = document.querySelector('.emg-body')
    if (!body) return []
    // Each page returns one wrapper div, so the blocks are its children.
    const host = body.querySelector('.emg-page') || body.firstElementChild || body
    return [...host.children].map((node) => {
      const r = node.getBoundingClientRect()
      return { cls: node.className, top: Math.round(r.top), bottom: Math.round(r.bottom), height: Math.round(r.height) }
    })
  },
  /** Vertical gaps between consecutive top-level blocks — the number the eye judges. */
  gaps: () => {
    const list = window.__harness.blocks()
    return list.slice(1).map((node, i) => Math.round(node.top - list[i].bottom))
  },
  text: () => (document.querySelector('.emg-body') || {}).innerText || '',
  posts: () => window.__posts || [],
  /** Click a button by its visible label. */
  click: (label) => {
    const button = [...document.querySelectorAll('button')].find((node) => node.textContent.trim() === label)
    if (button) button.click()
    return Boolean(button)
  },
  /** Type into the nth number input inside the acceptance card. */
  type: (key, value) => {
    const input = [...document.querySelectorAll('.emg-card')]
      .find((card) => card.querySelector('h3') && card.querySelector('h3').textContent.includes('关系接受条件'))
    if (!input) return false
    const field = input.querySelectorAll('input')[ACCEPT_KEYS.indexOf(key)]
    if (!field) return false
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set
    setter.call(field, String(value))
    field.dispatchEvent(new Event('input', { bubbles: true }))
    return true
  }
}

plugin.register({ ...ctx, register: () => {}, onDispose: () => {} })
