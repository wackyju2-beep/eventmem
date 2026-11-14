/**
 * Render smoke test for adapters/dsh/client.js — the DSH memory console.
 *
 * Loads the real client bundle in a jsdom window with the real React 18, stubs the
 * `__ModuleLoader__` require and the Host ctx, feeds canned daemon payloads through a
 * fetch stub, and asserts the things a screenshot would show: the 待判 tile counts down,
 * a judged pair flashes +N, the schedule page has the Hermes blocks, and both history
 * previews call the right daemon routes.
 *
 * Run from anywhere:  node <this file>
 */
import fs from 'node:fs'
import path from 'node:path'
import { JSDOM } from 'jsdom'
import React from 'react'
import { createRoot } from 'react-dom/client'
import { act } from 'react-dom/test-utils'

const CLIENT = new URL('../../adapters/dsh/client.js', import.meta.url)

// ------------------------------------------------------------------ jsdom window
const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  url: 'http://127.0.0.1:12467/',
  pretendToBeVisual: true,
})
const { window } = dom
globalThis.window = window
globalThis.document = window.document
Object.defineProperty(globalThis, 'navigator', { value: window.navigator, configurable: true })
globalThis.HTMLElement = window.HTMLElement
globalThis.Element = window.Element
globalThis.Node = window.Node
globalThis.Event = window.Event
globalThis.MouseEvent = window.MouseEvent
globalThis.getComputedStyle = window.getComputedStyle.bind(window)
globalThis.IS_REACT_ACT_ENVIRONMENT = true
window.IS_REACT_ACT_ENVIRONMENT = true

const failures = []
const check = (cond, message) => {
  if (!cond) failures.push(message)
  console.log(`${cond ? 'ok  ' : 'FAIL'} ${message}`)
}

// ------------------------------------------------------------------ daemon state
const now = Math.floor(Date.now() / 1000)
const state = {
  pairs: 13,
  backend: 'startlux',
  paused: false,
  backfill: { state: 'idle', pairs: 0, edges: 0, result: null, error: '', started: 0, finished: 0 },
  calls: [],
}

const EDGES = {
  heads_vec_top_k: 10, heads_cos_min: 0.35, margin: 0.2,
  min_conf_change: 0, min_conf_negate: 0, min_conf_repeat: 0,
  min_conf_because: 0, min_conf_refine: 0.9,
  session_heads_top_k: 10, session_recent: 5, unclosed_lessons: 2, decision_skeleton: 5,
  cross_top_k: 5, cross_cos_min: 0.7, poll_seconds: 60,
  schedule: 'interval', schedule_at: '03:30', schedule_weekday: 0,
}

function reply(route, body) {
  const live = {
    backend: state.backend, pairs: state.pairs, edges: 1, event_index: 2, events_total: 26,
    pairs_planned: 193, since: now - 30, updated: now,
  }
  switch (route) {
    case 'health':
      return { status: 'ok', schema: 12, pid: 181857 }
    case 'status':
      return {
        ok: true, paused: state.paused, busy: true, live,
        backfill: state.backfill,
        runs: [{ ts: now - 600, pairs: 54, edges: 3, backend: 'startlux' }],
        backends: [{ name: 'startlux', kind: 'external' }, { name: 'win-autothink-gpu', kind: 'self' }],
        cursor: { behind: 2.6, events: 9 },
      }
    case 'stats':
      return {
        events: 521, edges: 1234, pairs: 4000, newest: now - 120,
        kinds: { 决定: 120, 结果: 200, 教训: 201 }, projects: [{ name: 'eventmem', events: 521, last: now - 120 }],
        failures: { count: 0, last: [] },
      }
    case 'config':
      return {
        effective: { edges: EDGES },
        overrides: { edges: { margin: 0.2 } },
        file: '/home/user/.hermes/eventmem/settings.yaml',
        writable: true,
      }
    case 'recalibrate/preview':
      check(JSON.stringify(Object.keys(body.edges).sort()) === JSON.stringify(
        ['margin', 'min_conf_because', 'min_conf_change', 'min_conf_negate', 'min_conf_refine', 'min_conf_repeat']),
        'recalibrate/preview gets only the acceptance fields')
      return {
        token: 'tok-recal', added: { 改变: 3, 否定: 0, 重复: 1, 因为: 2, 细化: 0 },
        removed: { 改变: 1, 否定: 0, 重复: 0, 因为: 0, 细化: 0 },
        confidence_updates: 4, skipped: 7, uncovered_jev_edges: 2, manual_deletion_pairs: 1,
      }
    case 'backfill/preview':
      check(!('margin' in body.edges) && body.edges.heads_vec_top_k === 10,
        'backfill/preview gets the candidate fields only')
      return { token: 'tok-back', event_count: 521, observed_pairs: 900, exact_total: null }
    case 'pause':
      state.paused = Boolean(body.paused)
      return { ok: true, paused: state.paused, schedule: {} }
    case 'switch':
      return { ok: true, stopping: true }
    default:
      return {}
  }
}

let fetchCount = 0
globalThis.fetch = window.fetch = async (url, init = {}) => {
  const route = new URL(url, 'http://127.0.0.1:12467/').searchParams.get('path') || ''
  const body = init.body ? JSON.parse(init.body) : {}
  state.calls.push({ route, body })
  fetchCount += 1
  return {
    ok: true,
    status: 200,
    json: async () => reply(route, body),
    text: async () => JSON.stringify(reply(route, body)),
  }
}

// ------------------------------------------------------- module loader + host ctx
const source = fs.readFileSync(CLIENT, 'utf8')
let mod = null
window.__ModuleLoader__ = { load(definition) { mod = definition } }
// eslint-disable-next-line no-new-func
new Function('window', 'document', source)(window, window.document)
check(Boolean(mod && typeof mod.factory === 'function'), 'client bundle registers a factory')

const requireStub = (id) => {
  if (id === 'react') return React
  if (id === 'react-dom') return { createRoot }
  throw new Error(`unexpected require(${id})`)
}
const plugin = mod.factory(requireStub)

let consoleComponent = null
let iconComponent = null
const localeTables = {}
const ctx = {
  effect(fn) { return fn() },
  locale: {
    register(ns, tables) { localeTables[ns] = tables; return () => {} },
    bind(ns) {
      return (text, params) => {
        const lang = 'zh'
        const table = (localeTables[ns] || {})[lang] || {}
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
    register(definition, component) {
      if (definition.name === 'main') consoleComponent = component
      if (definition.name === 'sidebar.panellist') iconComponent = component
      return () => {}
    },
  },
}
plugin.apply(ctx)
check(Boolean(consoleComponent), 'plugin registers the main page')
check(Boolean(iconComponent), 'plugin registers the sidebar icon')

// ------------------------------------------------------------------ render helpers
const root = createRoot(document.getElementById('root'))
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms))
const text = () => document.getElementById('root').textContent || ''
const widgets = (selector) => [...document.querySelectorAll(selector)]
const byText = (selector, label) => widgets(selector).find((node) => node.textContent.trim() === label)
const cardOf = (title) => widgets('.emc-card').find((card) =>
  card.querySelector('.emc-card-head h3') && card.querySelector('.emc-card-head h3').textContent.trim() === title)

async function settle(ms = 120) {
  await act(async () => { await sleep(ms) })
}

async function main() {
  await act(async () => { root.render(React.createElement(consoleComponent, { tr: ctx.locale.bind('eventmem') })) })
  await settle(200)

  // ---- 概览：待判倒计时 + 数字翻页 -----------------------------------------
  const body = text()
  check(body.includes('待判'), 'overview shows the 待判 tile')
  check(body.includes('180'), `待判 counts down 193 planned − 13 judged (saw: ${body.slice(0, 0) || ''}${widgets('.emc-tile .v').map((n) => n.textContent).join(' | ')})`)
  check(!widgets('.emc-tile .v').some((node) => node.textContent.trim() === '—'), '待判/事件/边 tiles show numbers, not dashes')
  check(widgets('.emc-flip').length >= 3, 'tiles render flip-digit cells')
  check(body.includes('第 2/26 个事件'), 'pending subtitle reports the walk through this pass')
  check(body.includes('已判 13 对（预计）'), 'pending subtitle reports judged pairs')
  check(widgets('.emc-live').length === 1, 'live strip is on the page')
  check(text().includes('startlux'), 'live strip names the backend')

  // ---- 直播条：+N 增量闪 ----------------------------------------------------
  state.pairs = 15
  let flashed = null
  for (let attempt = 0; attempt < 40; attempt += 1) {
    await settle(80)
    const node = widgets('.emc-live .flash span')[0]
    if (node) { flashed = node.textContent; break }
  }
  check(flashed === '+2', `a two-pair poll flashes +2 (saw ${JSON.stringify(flashed)})`)
  await settle(700)   // let the flip settle, then read the tile like a person would
  const pendingTile = widgets('.emc-tile .v')[2]
  check(pendingTile && pendingTile.textContent.trim() === '178',
    `待判 follows the live count down to 193 − 15 (saw ${pendingTile && pendingTile.textContent})`)

  // ---- 调度页 --------------------------------------------------------------
  await act(async () => { byText('.emc-tab', '调度').click() })
  await settle(250)
  for (const title of ['自动运行', '预设（每轮问多少对）', '候选选择与成本', '关系接受条件', '历史关系重校准', '历史候选补算', '说明']) {
    check(Boolean(cardOf(title)), `schedule has the 「${title}」 card`)
  }
  check(!cardOf('Tier 明细'), 'the old single 「Tier 明细」 card is gone')
  check(Boolean(byText('.emc-btn', '暂停全部判断')), 'pause lives on the schedule page')
  check(!widgets('.emc-head .emc-btn').length, 'the header no longer duplicates the pause button')

  // 关系接受条件：margin + 五个最低置信度
  const accept = cardOf('关系接受条件')
  check(accept.querySelectorAll('input[type=number]').length === 6, 'acceptance card has six thresholds')
  check(accept.textContent.includes('有未保存的改动') === false && accept.textContent.includes('已保存'),
    'acceptance card reports its saved state')

  // 重校准预览：只发接受字段
  const recal = cardOf('历史关系重校准')
  await act(async () => { byTextIn(recal, '.emc-btn', '预览历史').click() })
  await settle(250)
  check(state.calls.some((call) => call.route === 'recalibrate/preview'), 'preview history POSTs recalibrate/preview')
  const recalPre = recal.querySelector('pre.emc-log')
  check(Boolean(recalPre) && recalPre.textContent.includes('改变：新增 3，删除 1'), 'preview renders the per-relation delta')
  check(Boolean(recalPre) && recalPre.textContent.includes('置信度更新的边：4'), 'preview renders confidence updates')
  const applyBtn = byTextIn(cardOf('历史关系重校准'), '.emc-btn', '应用历史重校准')
  check(applyBtn && !applyBtn.disabled, 'apply becomes available once a preview exists')

  // 补算预览：只发候选字段
  const backfill = cardOf('历史候选补算')
  await act(async () => { byTextIn(backfill, '.emc-btn', '预览历史').click() })
  await settle(250)
  check(state.calls.some((call) => call.route === 'backfill/preview'), 'preview history POSTs backfill/preview')
  check(backfill.textContent.includes('521 条') && backfill.textContent.includes('900 个候选对'),
    'backfill preview renders the observed candidate count')
  check(backfill.textContent.includes('尚无补算任务'), 'backfill progress line is shown')

  // 暂停：走 /api/pause
  await act(async () => { byText('.emc-btn', '暂停全部判断').click() })
  await settle(200)
  check(state.calls.some((call) => call.route === 'pause' && call.body.paused === true), 'pause POSTs /api/pause {paused:true}')

  // 保存未来设置：不发历史操作
  const before = state.calls.filter((call) => call.route === 'config' && call.body.edges).length
  await act(async () => { byTextIn(cardOf('候选选择与成本'), '.emc-btn', '仅保存未来设置').click() })
  await settle(200)
  const after = state.calls.filter((call) => call.route === 'config' && call.body.edges).length
  check(after === before + 1, 'save sends /api/config with edges')
  check(!state.calls.some((call) => call.route === 'recalibrate/apply' || call.route === 'backfill/start'),
    'saving never starts a history operation')

  // ---- 其它页签仍然渲染 -----------------------------------------------------
  for (const [label, marker] of [['概览', '待判'], ['后端', '后端链路'], ['模型', '运行时'], ['日志', '本进程日志']]) {
    await act(async () => { byText('.emc-tab', label).click() })
    await settle(150)
    check(text().includes(marker), `the ${label} tab renders`)
  }

  console.log(`\n${failures.length ? `${failures.length} FAILED` : 'SMOKE PASS'}`)
  if (failures.length) for (const failure of failures) console.log(' - ' + failure)
  await act(async () => { root.unmount() })
  process.exit(failures.length ? 1 : 0)
}

/** First button with this exact label inside `root`. */
function byTextIn(root, selector, label) {
  return [...root.querySelectorAll(selector)].find((node) => node.textContent.trim() === label)
}

main().catch((error) => {
  console.error(error)
  process.exitCode = 1
})
