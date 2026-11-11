/**
 * eventmem-gui — the whole eventmem console, inside the desktop app.
 *
 * The renderer is loaded from `file://`, so an `<iframe src="/api/…">` resolves to
 * `file:///api/…` and paints nothing (that was the blank page on 2026-11-12). The only
 * door from a plugin page to a backend is `ctx.rest` — IPC → main → HTTP with
 * credentials — and it is scoped to this plugin's own namespace. Everything below
 * therefore talks JSON to `/api/plugins/eventmem-gui/api/*`, which the dashboard plugin
 * `eventmem-gui` proxies verbatim onto the daemon's `/api/*`.
 *
 * Tabs mirror the web console one-for-one (概览 / 后端 / 模型 / 调度 / 日志) and the graph
 * from `eventmem-graph` is rendered as a sixth tab through a small handshake: that plugin
 * publishes its own component + ctx on `globalThis.__eventmemGraph`, so the graph keeps
 * one implementation and its own `/api/plugins/eventmem-graph/*` data path.
 *
 * Plain ESM, loaded uncompiled — jsx() calls, not JSX syntax.
 */

import { ROUTES_AREA, STATUSBAR_AREAS, Tip, haptic, host } from '@hermes/plugin-sdk'
import { useCallback, useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'eventmem-gui'
const PAGE = '/eventmem-gui'

/* ------------------------------------------------------------------ i18n
   One dictionary for both UIs, served by the daemon at /api/i18n and keyed by the Chinese
   source string — no build step means no key catalog to keep in sync. `t` degrades to the
   Chinese text, never to a bare key. `fmt` fills `{0}`/`{1}` placeholders.

   LANG/MAP are module-level because module-scope tables (TABS, FIELDS) are read at render
   time; the console assigns them once the fetch lands and re-renders.
*/
let LANG = 'zh'
let MAP = {}
const t = (text) => (LANG === 'en' && MAP[text]) || text
const fmt = (text, ...args) => t(text).replace(/\{(\d+)\}/g, (whole, index) => (args[Number(index)] ?? whole))
const TABS = [
  ['overview', '概览'],
  ['backends', '后端'],
  ['models', '模型'],
  ['schedule', '调度'],
  ['logs', '日志'],
  ['graph', '图谱']
]

const CSS = `
.emg{height:100%;min-height:0;display:flex;flex-direction:column;color:var(--ui-text-primary);
  font-size:12.5px;background:var(--ui-bg-primary);color-scheme:light}
.emg.dark{color-scheme:dark}
.emg-head{display:flex;align-items:center;gap:12px;padding:10px 16px;flex-wrap:nowrap;
  border-bottom:1px solid var(--ui-stroke-secondary);background:var(--ui-bg-secondary)}
.emg-title{font-size:13.5px;font-weight:600;white-space:nowrap}
.emg-title small{font-weight:400;font-size:11px;color:var(--ui-text-tertiary);margin-left:6px}
.emg-tabs{display:flex;gap:2px;flex:1;min-width:0;overflow-x:auto;scrollbar-width:none}
.emg-tab{border:0;background:transparent;color:var(--ui-text-secondary);font:inherit;font-size:12.5px;
  padding:5px 11px;border-radius:6px;cursor:pointer;white-space:nowrap}
.emg-tab:hover{background:var(--ui-control-hover-background);color:var(--ui-text-primary)}
.emg-tab.on{background:var(--ui-control-active-background);color:var(--ui-text-primary);font-weight:600}
.emg-pill{font-size:11px;padding:2px 8px;border-radius:999px;border:1px solid var(--ui-stroke-secondary);
  color:var(--ui-text-secondary);white-space:nowrap;font-variant-numeric:tabular-nums}
.emg-pill.ok{color:var(--ui-green);border-color:var(--ui-green)}
.emg-pill.bad{color:var(--ui-red);border-color:var(--ui-red)}
.emg-live{display:flex;align-items:center;gap:9px;padding:7px 16px;font-size:11.5px;
  border-bottom:1px solid var(--ui-stroke-secondary);
  background:color-mix(in srgb,var(--ui-accent) 5%,transparent)}
.emg-live.on{background:color-mix(in srgb,var(--ui-accent) 11%,transparent)}
.emg-dot{width:7px;height:7px;border-radius:50%;background:var(--ui-text-tertiary);flex:none}
.emg-dot.on{background:var(--ui-green);animation:emg-pulse 1.9s ease-out infinite}
@keyframes emg-pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--ui-green) 55%,transparent)}
  70%{box-shadow:0 0 0 7px transparent}100%{box-shadow:0 0 0 0 transparent}}
.emg-live-backend{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-weight:600;font-size:12.5px}
.emg-count{display:inline-flex;align-items:baseline;gap:3px;padding:1px 9px;border-radius:999px;
  border:1px solid var(--ui-stroke-secondary);background:var(--ui-bg-secondary)}
.emg-count b{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:13px;
  font-variant-numeric:tabular-nums;font-weight:600}
.emg-plus{color:var(--ui-green);font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-weight:600;font-size:12px;animation:emg-float 1s ease-out forwards}
@keyframes emg-float{0%{opacity:0;transform:translateY(5px)}25%{opacity:1;transform:translateY(0)}
  100%{opacity:0;transform:translateY(-10px)}}
.emg-body{flex:1;min-height:0;overflow:auto;padding:14px 16px 20px}
.emg-body.still{overflow:hidden;padding:0}
.emg-card{border:1px solid var(--ui-stroke-secondary);border-radius:9px;padding:12px 14px;
  background:var(--ui-bg-card)}
.emg-card + .emg-card{margin-top:12px}
.emg-card h3{margin:0 0 8px;font-size:12px;font-weight:600;color:var(--ui-text-secondary);letter-spacing:.02em}
.emg-grid{display:grid;gap:12px}
.emg-grid.c2{grid-template-columns:repeat(2,minmax(0,1fr))}
.emg-grid.c3{grid-template-columns:repeat(3,minmax(0,1fr))}
.emg-tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin-bottom:12px}
.emg-tile{border:1px solid var(--ui-stroke-secondary);border-radius:9px;padding:11px 14px;background:var(--ui-bg-card)}
.emg-tile .k{font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--ui-text-tertiary)}
.emg-tile .v{font-size:23px;line-height:1.3;font-variant-numeric:tabular-nums}
.emg-tile .s{font-size:11px;color:var(--ui-text-tertiary);margin-top:2px}
.emg-row{display:flex;align-items:center;gap:8px}
.emg-row.wrap{flex-wrap:wrap}
.emg-grow{flex:1;min-width:0}
.emg-sub{color:var(--ui-text-tertiary);font-size:11.5px}
.emg-mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11.5px}
.emg-trunc{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.emg-tag{font-size:10.5px;padding:1px 7px;border-radius:999px;border:1px solid var(--ui-stroke-secondary);
  color:var(--ui-text-secondary);white-space:nowrap}
.emg-tag.ok{color:var(--ui-green);border-color:var(--ui-green)}
.emg-tag.bad{color:var(--ui-red);border-color:var(--ui-red)}
.emg-tag.info{color:var(--ui-blue);border-color:var(--ui-blue)}
.emg-b{border:1px solid var(--ui-stroke-secondary);background:var(--ui-bg-tertiary);
  color:var(--ui-text-primary);border-radius:6px;padding:4px 10px;font:inherit;font-size:11.5px;cursor:pointer}
.emg-b:hover{background:var(--ui-control-hover-background)}
.emg-b:disabled{opacity:.45;cursor:default}
.emg-b.pri{background:var(--ui-accent);border-color:var(--ui-accent);color:#fff}
.emg-b.danger{color:var(--ui-red);border-color:var(--ui-red)}
.emg-b.tiny{padding:1px 6px;font-size:11px}
.emg-dd{position:relative;flex:1;min-width:0}
.emg-dd-btn{display:flex;align-items:center;gap:6px;width:100%;box-sizing:border-box;
  background:var(--ui-bg-tertiary);color:var(--ui-text-primary);border:1px solid var(--ui-stroke-secondary);
  border-radius:6px;padding:4px 8px;font:inherit;font-size:11.5px;cursor:pointer;text-align:left}
.emg-dd-btn:hover{background:var(--ui-control-hover-background)}
.emg-dd-btn:disabled{opacity:.45;cursor:default}
.emg-dd-btn .emg-trunc{flex:1}
.emg-dd-caret{color:var(--ui-text-tertiary);font-size:10px}
.emg-dd-list{position:absolute;z-index:40;left:0;right:0;top:calc(100% + 3px);padding:3px;
  border:1px solid var(--ui-stroke-secondary);border-radius:7px;background:var(--ui-bg-elevated);
  box-shadow:0 8px 24px rgba(0,0,0,.35);max-height:240px;overflow:auto}
.emg-dd-item{display:block;width:100%;text-align:left;border:0;background:transparent;
  color:var(--ui-text-primary);font:inherit;font-size:11.5px;padding:5px 8px;border-radius:5px;cursor:pointer}
.emg-dd-item:hover{background:var(--ui-control-hover-background)}
.emg-dd-item.on{background:color-mix(in srgb,var(--ui-accent) 22%,transparent)}
.emg-in,.emg-sel,.emg-ta{background:var(--ui-bg-tertiary);color:var(--ui-text-primary);
  border:1px solid var(--ui-stroke-secondary);border-radius:6px;padding:4px 8px;font:inherit;
  font-size:11.5px;min-width:0;
  /* The host's --ui-bg-input is #fcfcfc in *both* themes, so a native input would stay
     white in dark mode. appearance:none + a themed surface is the fix; without it the UA
     widget paints itself and ignores the background. */
  appearance:none;-webkit-appearance:none}
.emg-sel{padding-right:22px;background-image:url("data:image/svg+xml;utf8,<svg xmlns='http://www.w3.org/2000/svg' width='10' height='6'><path d='M1 1l4 4 4-4' fill='none' stroke='%238a8f98' stroke-width='1.4' stroke-linecap='round'/></svg>");
  background-repeat:no-repeat;background-position:right 7px center;cursor:pointer}
.emg-ta{background:var(--ui-bg-editor)}
.emg-in:focus,.emg-sel:focus,.emg-ta:focus{outline:1px solid var(--ui-accent);outline-offset:-1px}
.emg-ta{width:100%;box-sizing:border-box;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  font-size:11.5px;line-height:1.5;padding:8px;resize:vertical}
.emg-label{display:block;font-size:10.5px;letter-spacing:.06em;text-transform:uppercase;
  color:var(--ui-text-tertiary);margin-bottom:4px}
.emg-line{display:flex;align-items:center;gap:8px;padding:5px 0;border-top:1px solid var(--ui-stroke-secondary)}
.emg-line.first{border-top:0}
.emg-bar{height:6px;border-radius:999px;background:var(--ui-bg-tertiary);overflow:hidden}
.emg-bar i{display:block;height:100%;background:var(--ui-accent);border-radius:999px;transition:width .2s}
.emg-chain{display:flex;flex-wrap:wrap;gap:6px;align-items:center}
.emg-chip{display:inline-flex;align-items:center;gap:4px;border:1px solid var(--ui-stroke-secondary);
  border-radius:999px;padding:2px 6px 2px 9px;background:var(--ui-bg-secondary);font-size:11.5px}
.emg-chip b{font-weight:600}
.emg-chip button{border:0;background:transparent;color:var(--ui-text-tertiary);cursor:pointer;
  font-size:11px;padding:0 2px;line-height:1;border-radius:3px}
.emg-chip button:hover{color:var(--ui-text-primary);background:var(--ui-control-hover-background)}
.emg-details{margin-top:8px}
.emg-details summary{cursor:pointer;color:var(--ui-text-tertiary);font-size:11px}
.emg-note{font-size:11.5px;padding:5px 10px;border-radius:6px;border:1px solid var(--ui-stroke-secondary);
  color:var(--ui-text-secondary);white-space:pre-wrap;max-height:76px;overflow:auto}
.emg-note.err{color:var(--ui-red);border-color:var(--ui-red)}
.emg-note.ok{color:var(--ui-green);border-color:var(--ui-green)}
.emg-log{margin:0;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:11px;
  line-height:1.55;white-space:pre-wrap;word-break:break-word;height:calc(100vh - 220px);
  min-height:220px;overflow:auto}
.emg-log .w{color:var(--ui-orange)}.emg-log .e{color:var(--ui-red)}.emg-log .d{color:var(--ui-text-tertiary)}
.emg-graph{height:calc(100vh - 106px);min-height:320px}
/* The host bar is a fixed h-5 (20px) strip with items-stretch, and the app font scale
   grows the inherited text — 11px here is 13-15px there. Vertical padding on top of that
   pushed the line box past 20px and the bar clipped the glyphs top and bottom.
   Mirror the host's own items instead: full height, no vertical padding, line-height 1. */
.emg-chipbar{display:inline-flex;align-items:center;gap:5px;background:transparent;border:0;
  color:inherit;cursor:pointer;padding:0 6px;border-radius:5px;font:inherit;
  font-size:min(0.6875rem,13px);line-height:1;height:100%;max-height:100%;white-space:nowrap;
  flex:0 0 auto;align-self:center;overflow:hidden}
.emg-chipbar>span{display:inline-flex;align-items:center;gap:5px;line-height:1;white-space:nowrap}
.emg-chipbar .emg-gear{font-size:1.05em;line-height:1;opacity:.8}
.emg-chipbar:hover{background:var(--ui-control-hover-background)}
`

/* ------------------------------------------------------------------ helpers */

const NI = (v) => (v === null || v === undefined || v === '' ? '—' : String(v))
const num = (v) => (typeof v === 'number' ? v.toLocaleString('en-US') : NI(v))

function msg(error) {
  if (!error) return ''
  return String((error && error.message) || error)
}

function when(ts) {
  if (!ts) return '—'
  try {
    return new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false })
  } catch {
    return '—'
  }
}

function dur(seconds) {
  if (seconds === null || seconds === undefined) return '—'
  if (seconds < 90) return `${Math.round(seconds)}s`
  if (seconds < 5400) return `${Math.round(seconds / 60)}min`
  return `${(seconds / 3600).toFixed(1)}h`
}

function sizeGb(bytes) {
  if (!bytes) return '—'
  return `${(bytes / 1e9).toFixed(2)} GB`
}

function sizeMb(bytes) {
  if (!bytes) return '—'
  return `${(bytes / 1e6).toFixed(bytes > 1e8 ? 0 : 1)} MB`
}

/** GET one path (repeatedly when `every` is set). Returns {data, error, reload}. */
function useFetch(ctx, path, every = 0) {
  const [state, setState] = useState({ data: null, error: '' })
  const alive = useRef(true)

  const run = useCallback(
    () =>
      ctx
        .rest(path)
        .then((data) => alive.current && setState({ data, error: '' }))
        .catch((error) => alive.current && setState((old) => ({ ...old, error: msg(error) }))),
    [ctx, path]
  )

  useEffect(() => {
    alive.current = true
    run()
    const id = every ? window.setInterval(run, every) : 0
    return () => {
      alive.current = false
      if (id) window.clearInterval(id)
    }
  }, [run, every])

  return { data: state.data, error: state.error, reload: run }
}

/** GET several paths at once (Promise.all). `paths` must be a stable array. */
function useFetchAll(ctx, paths, every = 0) {
  const key = paths.join('|')
  const [state, setState] = useState({ data: null, error: '' })
  const alive = useRef(true)

  const run = useCallback(() => {
    const list = key.split('|').filter(Boolean)
    return Promise.all(list.map((path) => ctx.rest(path)))
      .then((data) => alive.current && setState({ data, error: '' }))
      .catch((error) => alive.current && setState((old) => ({ ...old, error: msg(error) })))
  }, [ctx, key])

  useEffect(() => {
    alive.current = true
    run()
    const id = every ? window.setInterval(run, every) : 0
    return () => {
      alive.current = false
      if (id) window.clearInterval(id)
    }
  }, [run, every])

  return { data: state.data, error: state.error, reload: run }
}

/** Is the app currently dark? Read the rendered surface, not the OS. */
function probeDark() {
  try {
    const surface = (document.body && getComputedStyle(document.body).backgroundColor) || ''
    const parts = surface.match(/[\d.]+/g)
    if (!parts || parts.length < 3) return false
    const [r, g, b] = parts.map(Number)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b < 128
  } catch {
    return false
  }
}

/** `color-scheme` is what makes native scrollbars, checkboxes and select popups match. */
function useDarkTheme() {
  const [dark, setDark] = useState(probeDark)
  useEffect(() => {
    const root = document.documentElement
    if (!root || typeof MutationObserver === 'undefined') return undefined
    const observer = new MutationObserver(() => setDark(probeDark()))
    observer.observe(root, { attributeFilter: ['class', 'style', 'data-theme'], attributes: true })
    return () => observer.disconnect()
  }, [])
  return dark
}

/**
 * A dropdown of our own, because the native `<select>` popup is an OS widget: it ignores
 * the panel's colours and stays white in dark mode no matter what `color-scheme` says.
 */
function Select({ value, options, onChange, disabled }) {
  const [open, setOpen] = useState(false)
  const box = useRef(null)

  useEffect(() => {
    if (!open) return undefined
    const close = (event) => {
      if (box.current && !box.current.contains(event.target)) setOpen(false)
    }
    const escape = (event) => {
      if (event.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', close)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', close)
      document.removeEventListener('keydown', escape)
    }
  }, [open])

  const current = options.find((option) => option.value === value)

  return jsxs('div', {
    ref: box,
    className: `emg-dd${open ? ' open' : ''}`,
    children: [
      jsx('button', {
        type: 'button',
        className: 'emg-dd-btn',
        disabled,
        onClick: () => setOpen((was) => !was),
        children: [
          jsx('span', { className: 'emg-trunc', children: current ? current.label : '—' }),
          jsx('span', { className: 'emg-dd-caret', children: '▾' })
        ]
      }),
      open
        ? jsx('div', {
            className: 'emg-dd-list',
            children: options.map((option) =>
              jsx(
                'button',
                {
                  type: 'button',
                  className: `emg-dd-item${option.value === value ? ' on' : ''}`,
                  onClick: () => {
                    setOpen(false)
                    onChange(option.value)
                  },
                  children: option.label
                },
                option.value
              )
            )
          })
        : null
    ]
  })
}

function Card({ title, children, right }) {
  return jsxs('div', {
    className: 'emg-card',
    children: [
      title || right
        ? jsxs('div', {
            className: 'emg-row',
            style: { marginBottom: 8 },
            children: [jsx('h3', { style: { margin: 0 }, children: title }), jsx('span', { className: 'emg-grow' }), right || null]
          })
        : null,
      children
    ]
  })
}

function Btn({ children, onClick, primary, danger, disabled, tiny, title }) {
  const classes = ['emg-b']
  if (primary) classes.push('pri')
  if (danger) classes.push('danger')
  if (tiny) classes.push('tiny')
  return jsx('button', { type: 'button', className: classes.join(' '), onClick, disabled, title, children })
}

function Tile({ k, v, s }) {
  return jsxs('div', {
    className: 'emg-tile',
    children: [
      jsx('div', { className: 'k', children: k }),
      jsx('div', { className: 'v', children: v }),
      s ? jsx('div', { className: 's', children: s }) : null
    ]
  })
}

function Note({ text, kind }) {
  if (!text) return null
  return jsx('div', { className: `emg-note ${kind || ''}`, children: text })
}

/* ------------------------------------------------------------------ 概览 */

function Overview({ ctx }) {
  const { data, error, reload } = useFetchAll(ctx, ['/api/stats', '/api/status', '/api/config'], 5000)
  const [stats, status, config] = data || [{}, {}, {}]
  const cursor = (status && status.cursor) || {}
  const kinds = (stats && stats.kinds) || {}
  const kindRows = Object.entries(kinds)
  const kindTotal = kindRows.reduce((sum, [, n]) => sum + n, 0) || 1
  const kindMax = Math.max(1, ...kindRows.map(([, n]) => n))
  const runs = ((status && status.runs) || []).slice().reverse()
  const chain = (config && config.effective && config.effective.decision && config.effective.decision.backends) || []

  return jsxs('div', {
    children: [
      jsxs('div', {
        className: 'emg-tiles',
        children: [
          jsx(Tile, { k: t('事件'), v: num(stats && stats.events), s: stats && stats.newest ? fmt('最新 {0}', when(stats.newest)) : t('空库') }),
          jsx(Tile, { k: t('边 / 已判对'), v: num(stats && stats.edges), s: fmt('{0} 对已判', NI(stats && stats.pairs)) }),
          jsx(Tile, { k: t('连边落后'), v: num(cursor.events), s: cursor.behind == null ? t('无游标') : fmt('落后 {0}', dur(cursor.behind)) })
        ]
      }),
      error ? jsx(Note, { text: error, kind: 'err' }) : null,

      jsxs('div', {
        className: 'emg-grid c2',
        children: [
          jsx(Card, {
            title: t('kind 分布'),
            children: kindRows.length
              ? kindRows.map(([k, n]) =>
                  jsxs(
                    'div',
                    {
                      className: 'emg-line first',
                      children: [
                        jsx('span', { style: { width: 44 }, children: k }),
                        jsx('span', { className: 'emg-grow', children: jsx('div', { className: 'emg-bar', children: jsx('i', { style: { width: `${(n / kindMax) * 100}%` } }) }) }),
                        jsx('span', { className: 'emg-mono', style: { width: 46, textAlign: 'right' }, children: num(n) }),
                        jsx('span', { className: 'emg-sub', style: { width: 38, textAlign: 'right' }, children: `${Math.round((n / kindTotal) * 100)}%` })
                      ]
                    },
                    k
                  )
                )
              : jsx('div', { className: 'emg-sub', children: t('还没有事件') })
          }),
          jsx(Card, {
            title: t('项目（按事件数）'),
            children: (stats && stats.projects && stats.projects.length) || []
              ? (stats.projects || []).map((p) =>
                  jsxs(
                    'div',
                    {
                      className: 'emg-line first',
                      children: [
                        jsx('span', { className: 'emg-grow emg-trunc', children: p.name }),
                        jsx('span', { className: 'emg-mono', children: num(p.events) }),
                        jsx('span', { className: 'emg-sub', children: when(p.last) })
                      ]
                    },
                    p.name
                  )
                )
              : jsx('div', { className: 'emg-sub', children: t('还没有项目') })
          })
        ]
      }),

      jsx(Card, {
        title: t('最近几轮 pass'),
        right: jsx(Btn, { tiny: true, onClick: reload, children: t('刷新') }),
        children: runs.length
          ? runs.slice(0, 8).map((r, i) =>
              jsxs(
                'div',
                {
                  className: `emg-line ${i ? '' : 'first'}`,
                  children: [
                    jsx('span', { className: 'emg-sub', style: { width: 132 }, children: when(r.ts) }),
                    jsx('span', { className: 'emg-grow', children: r.skipped ? r.skipped : t('完成') }),
                    jsx('span', { className: 'emg-mono', children: fmt('{0} 对 / {1} 边', NI(r.pairs), NI(r.edges)) }),
                    jsx('span', { className: 'emg-tag', children: r.backend || '—' })
                  ]
                },
                String(i)
              )
            )
          : jsx('div', { className: 'emg-sub', children: t('这一进程还没跑过 pass') })
      }),

      jsx(Card, {
        title: t('后端链路（decision.backends）'),
        children: chain.length
          ? jsx('div', {
              className: 'emg-chain',
              children: chain.map((name, i) =>
                jsx('span', { className: 'emg-chip', children: jsxs('span', { children: [jsx('b', { children: `${i + 1}.` }), ` ${name}`] }) }, name)
              )
            })
          : jsx('div', { className: 'emg-sub', children: t('空（用 decision.url 单端点）') })
      })
    ]
  })
}

/* ------------------------------------------------------------------ 后端 */

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
`
}

const TEMPLATES_EN = {
  cloud: `# Hosted Jev / System One endpoint
protocol: systemone
model: jev-latest
base_url: https://api.typesafe.ai
api_key_env: TYPESAFE_API_KEY
license: proprietary
note: official hosting; without a key the probe reports needs_key
`,
  lmstudio: `# LM Studio (or any server that is already running): we do not start it
protocol: systemone
model: lev
external: true
base_url: http://127.0.0.1:1234
note: load a decision model in LM Studio first
`,
  local: `# Self-hosted: the daemon launches llama-server when there is work
protocol: systemone
model: lev
weights: ../../models/lev/lev-Q4_K_M.gguf
launch: auto
port: 0
license: apache-2.0
`
}

/** The config scaffolds are prose for the reader, so they follow the UI language. */
const templates = () => (LANG === 'en' ? TEMPLATES_EN : TEMPLATES)

function PackCard({ pack, ctx, onChanged, notify }) {
  const [open, setOpen] = useState(false)
  const [text, setText] = useState(pack.config || '')
  const [armed, setArmed] = useState(false)
  const [busy, setBusy] = useState(false)

  const call = async (path, body, okText) => {
    setBusy(true)
    try {
      const result = await ctx.rest(path, body === undefined ? undefined : { method: 'POST', body })
      if (result && result.ok === false) notify(fmt('{0}：{1}', okText || t('失败'), result.reason || result.error || t('不可用')), 'err')
      else notify(okText ? `${okText}：ok` : 'ok', 'ok')
      onChanged()
    } catch (error) {
      notify(msg(error), 'err')
    } finally {
      setBusy(false)
    }
  }

  const state = pack.running === true
    ? { text: fmt('运行中 :{0}', pack.port ?? '?'), cls: 'ok' }
    : pack.kind === 'self'
      ? { text: t('未运行'), cls: 'bad' }
      : pack.needs_key
        ? { text: t('缺 key'), cls: 'bad' }
        : { text: t('外部'), cls: '' }

  return jsxs('div', {
    className: 'emg-card',
    children: [
      jsxs('div', {
        className: 'emg-row wrap',
        children: [
          jsx('span', { style: { fontWeight: 600 }, children: pack.name }),
          jsx('span', { className: 'emg-tag', children: pack.kind }),
          pack.in_chain ? jsx('span', { className: 'emg-tag info', children: t('在链路') }) : null,
          jsx('span', { className: `emg-tag ${state.cls}`, children: state.text }),
          jsx('span', { className: 'emg-grow' }),
          jsx(Btn, { tiny: true, disabled: busy, onClick: () => call(`/api/backends/${encodeURIComponent(pack.name)}/probe`, {}, pack.name), children: t('探活') }),
          pack.kind === 'self'
            ? jsx(Btn, { tiny: true, disabled: busy, onClick: () => call(`/api/backends/${encodeURIComponent(pack.name)}/start`, {}), children: t('启动') })
            : null,
          pack.kind === 'self'
            ? jsx(Btn, { tiny: true, disabled: busy, onClick: () => call(`/api/backends/${encodeURIComponent(pack.name)}/stop`, {}), children: t('停止') })
            : null,
          jsx(Btn, {
            tiny: true,
            danger: true,
            disabled: busy,
            onClick: () => {
              if (!armed) return setArmed(true)
              setArmed(false)
              call(`/api/packs/${encodeURIComponent(pack.name)}/delete`, {})
            },
            children: armed ? t('确认删除') : t('删除')
          })
        ]
      }),
      jsx('div', {
        className: 'emg-sub emg-mono emg-trunc',
        style: { marginTop: 6 },
        children: [pack.protocol, pack.model || '?', pack.url || pack.weights || '', pack.license || ''].filter(Boolean).join(' · ')
      }),
      jsxs('details', {
        className: 'emg-details',
        children: [
          jsx('summary', { children: 'config.yaml' }),
          jsx('textarea', {
            className: 'emg-ta',
            spellCheck: false,
            rows: 9,
            value: text,
            onChange: (event) => setText(event.target.value)
          }),
          jsxs('div', {
            className: 'emg-row',
            style: { marginTop: 8 },
            children: [
              jsx(Btn, { primary: true, tiny: true, disabled: busy, onClick: () => call(`/api/packs/${encodeURIComponent(pack.name)}/config`, { text }), children: t('保存') }),
              jsx('span', { className: 'emg-sub', children: t('保存的就是这份文件本身，注释原样留下。') }),
              jsx('span', { className: 'emg-grow' }),
              jsx(Btn, { tiny: true, onClick: () => setText(pack.config || ''), children: t('还原') })
            ]
          })
        ]
      })
    ]
  })
}

function Backends({ ctx, notify }) {
  const packs = useFetch(ctx, '/api/packs')
  const config = useFetch(ctx, '/api/config')
  const [draft, setDraft] = useState([])
  const [pick, setPick] = useState('')
  const [template, setTemplate] = useState('cloud')
  const [newName, setNewName] = useState('')
  const [newText, setNewText] = useState(() => templates().cloud)
  const [busy, setBusy] = useState(false)
  const seeded = useRef('')

  // The server is authoritative on every reload; local ordering lives in `draft` only,
  // so "add to the end" is never wiped by a background refresh.
  useEffect(() => {
    const chain = (config.data && config.data.effective && config.data.effective.decision && config.data.effective.decision.backends) || []
    const key = chain.join('|')
    if (seeded.current === key) return
    seeded.current = key
    setDraft([...chain])
  }, [config.data])

  const list = (packs.data && packs.data.packs) || []
  const names = list.map((p) => p.name)
  const free = names.filter((name) => !draft.includes(name))
  const freeKey = free.join('|')

  useEffect(() => {
    if (!pick || !free.includes(pick)) setPick(free[0] || '')
  }, [pick, freeKey])

  const move = (index, delta) => {
    const target = index + delta
    if (target < 0 || target >= draft.length) return
    const next = [...draft]
    const swap = next[index]
    next[index] = next[target]
    next[target] = swap
    setDraft(next)
  }

  const saveChain = async () => {
    setBusy(true)
    try {
      await ctx.rest('/api/config', { method: 'POST', body: { decision: { backends: draft } } })
      notify(t('链路已保存（下一次 pass 生效）'), 'ok')
      seeded.current = ''
      config.reload()
    } catch (error) {
      notify(msg(error), 'err')
    } finally {
      setBusy(false)
    }
  }

  const create = async () => {
    const name = newName.trim()
    if (!name) return notify(t('先起个名字'), 'err')
    setBusy(true)
    try {
      await ctx.rest('/api/packs', { method: 'POST', body: { name, text: newText } })
      notify(fmt('已创建 {0}', name), 'ok')
      setNewName('')
      packs.reload()
    } catch (error) {
      notify(msg(error), 'err')
    } finally {
      setBusy(false)
    }
  }

  const problems = (packs.data && packs.data.problems) || []

  return jsxs('div', {
    children: [
      jsx(Card, {
        title: t('链路顺序'),
        children: jsxs('div', {
          children: [
            draft.length
              ? jsx('div', {
                  className: 'emg-chain',
                  children: draft.map((name, index) =>
                    jsx('span', {
                      className: 'emg-chip',
                      children: [
                        jsx('b', { children: `${index + 1}. ${name}` }),
                        jsx('button', { title: t('上移'), onClick: () => move(index, -1), children: '↑' }),
                        jsx('button', { title: t('下移'), onClick: () => move(index, 1), children: '↓' }),
                        jsx('button', { title: t('移除'), onClick: () => setDraft(draft.filter((item, i) => i !== index)), children: '✕' })
                      ]
                    }, name)
                  )
                })
              : jsx('div', { className: 'emg-sub', children: t('空（用 decision.url 单端点）') }),
            jsxs('div', {
              className: 'emg-row',
              style: { marginTop: 10, flexWrap: 'wrap' },
              children: [
                jsx(Select, {
                  value: pick,
                  disabled: !free.length,
                  onChange: setPick,
                  options: free.length
                    ? free.map((name) => ({ value: name, label: name }))
                    : [{ value: '', label: t('（没有可加的）') }]
                }),
                jsx(Btn, { disabled: !pick, onClick: () => pick && setDraft([...draft, pick]), children: t('加到末尾') }),
                jsx(Btn, { primary: true, disabled: busy, onClick: saveChain, children: t('保存链路') })
              ]
            }),
            jsx('div', { className: 'emg-sub', style: { marginTop: 6 }, children: t('顺序即回退顺序：一轮开始时逐项探活，第一个活着的负责整轮。空链路 = 只用 decision.url。') })
          ]
        })
      }),

      jsx(Card, {
        title: t('新建后端'),
        children: jsxs('div', {
          children: [
            jsxs('div', {
              className: 'emg-row',
              style: { flexWrap: 'wrap' },
              children: [
                jsx(Select, {
                  value: template,
                  onChange: (next) => {
                    setTemplate(next)
                    setNewText(templates()[next] || '')
                  },
                  options: [
                    { value: 'cloud', label: t('云端 API（System One 协议）') },
                    { value: 'lmstudio', label: t('LM Studio / 已装好的本地服务') },
                    { value: 'local', label: t('本地 GGUF（自己托管，daemon 负责启动）') }
                  ]
                }),
                jsx('input', {
                  className: 'emg-in',
                  style: { width: 220 },
                  placeholder: t('名字，例如 typesafe / lev'),
                  value: newName,
                  onChange: (event) => setNewName(event.target.value)
                })
              ]
            }),
            jsx('div', {
              style: { marginTop: 8 },
              children: [
                jsx('span', { className: 'emg-label', children: t('config.yaml（可以先改再保存）') }),
                jsx('textarea', { className: 'emg-ta', spellCheck: false, rows: 8, value: newText, onChange: (event) => setNewText(event.target.value) })
              ]
            }),
            jsxs('div', {
              className: 'emg-row',
              style: { marginTop: 8 },
              children: [
                jsx(Btn, { primary: true, disabled: busy, onClick: create, children: t('创建') }),
                jsx('span', { className: 'emg-sub', children: t('创建后还要把它加进链路，再重启 daemon 才会自动拉起本地进程。') })
              ]
            })
          ]
        })
      }),

      problems.length ? jsx(Note, { text: fmt('有 pack 加载失败：\n{0}', problems.join('\n')), kind: 'err' }) : null,
      packs.error ? jsx(Note, { text: packs.error, kind: 'err' }) : null,

      jsx('div', {
        style: { marginTop: 12 },
        children: list.length
          ? list.map((pack) =>
              jsx(PackCard, { pack, ctx, notify, onChanged: () => { packs.reload(); config.reload() } }, pack.name)
            )
          : jsx('div', { className: 'emg-sub', children: fmt('{0} 里还没有后端', (packs.data && packs.data.dir) || '') })
      })
    ]
  })
}

/* ------------------------------------------------------------------ 模型 */

function Models({ ctx, notify }) {
  const models = useFetch(ctx, '/api/models')
  const job = useFetch(ctx, '/api/job', 1200)
  const [name, setName] = useState('lev')
  const [repo, setRepo] = useState('ggml-org/lev-GGUF')
  const [quant, setQuant] = useState('Q4_K_M')
  const [skipLlama, setSkipLlama] = useState(false)
  const [autoChain, setAutoChain] = useState(false)
  const [busy, setBusy] = useState(false)
  const lastState = useRef('')

  const current = (job.data && job.data.job) || null
  const running = Boolean(current && current.state === 'running')

  useEffect(() => {
    if (!current) return
    if (running) {
      lastState.current = 'running'
      return
    }
    if (lastState.current === 'running') {
      lastState.current = current.state
      models.reload()
      notify(current.state === 'done' ? t('安装完成') : fmt('安装失败：{0}', current.error || ''), current.state === 'done' ? 'ok' : 'err')
    }
  }, [current, running])

  const install = async (body, label) => {
    setBusy(true)
    try {
      await ctx.rest('/api/install', { method: 'POST', body })
      notify(label, 'ok')
      job.reload()
    } catch (error) {
      notify(msg(error), 'err')
    } finally {
      setBusy(false)
    }
  }

  const installModel = async () => {
    const target = name.trim() || 'lev'
    await install({ name: target, repo: repo.trim(), quant: quant.trim(), skip_llama: skipLlama, force: false }, t('安装任务已开始（首次要下 2 GB 左右）'))
    if (!autoChain) return
    try {
      const config = await ctx.rest('/api/config')
      const chain = [...((((config || {}).effective || {}).decision || {}).backends || [])]
      if (!chain.includes(target)) {
        chain.push(target)
        await ctx.rest('/api/config', { method: 'POST', body: { decision: { backends: chain } } })
        notify(fmt('装完会自动把 {0} 加进链路', target), 'ok')
      }
    } catch (error) {
      notify(msg(error), 'err')
    }
  }

  const llama = (models.data && models.data.llama) || {}
  const pct = current && current.total
    ? Math.min(100, Math.round((current.done / current.total) * 100))
    : current && current.state === 'done'
      ? 100
      : 0

  return jsxs('div', {
    children: [
      jsxs('div', {
        className: 'emg-grid c2',
        children: [
          jsx(Card, {
            title: t('运行时'),
            children: jsxs('div', {
              children: [
                llama.build
                  ? jsxs('div', {
                      children: [
                        jsxs('div', { children: ['llama.cpp ', jsx('b', { children: llama.build }), llama.asset ? ` · ${llama.asset}` : ''] }),
                        jsx('div', { className: 'emg-sub emg-mono emg-trunc', children: llama.binary || '' })
                      ]
                    })
                  : jsx('div', { className: 'emg-sub', children: t('还没装 llama.cpp（按硬件自动选包）') }),
                jsx('div', {
                  className: 'emg-row',
                  style: { marginTop: 10 },
                  children: jsx(Btn, { disabled: busy || running, onClick: () => install({ runtime_only: true }, t('开始下载 llama.cpp')), children: t('下载 / 更新 llama.cpp') })
                })
              ]
            })
          }),
          jsx(Card, {
            title: t('已装模型'),
            children: (models.data && models.data.models || []).length
              ? models.data.models.map((item) =>
                  jsxs(
                    'div',
                    {
                      className: 'emg-line first',
                      children: [
                        jsx('span', { className: 'emg-grow emg-mono emg-trunc', children: `${item.repo}/${item.file}` }),
                        jsx('span', { className: 'emg-mono', children: sizeGb(item.size) })
                      ]
                    },
                    `${item.repo}/${item.file}`
                  )
                )
              : jsx('div', { className: 'emg-sub', children: t('没有 GGUF') })
          })
        ]
      }),

      jsx(Card, {
        title: t('安装 GGUF + 生成 pack'),
        children: jsxs('div', {
          children: [
            jsxs('div', {
              className: 'emg-row',
              style: { flexWrap: 'wrap', alignItems: 'flex-end' },
              children: [
                jsxs('div', {
                  className: 'emg-grow',
                  children: [
                    jsx('span', { className: 'emg-label', children: t('后端名') }),
                    jsx('input', { className: 'emg-in', style: { width: '100%' }, value: name, onChange: (event) => setName(event.target.value) })
                  ]
                }),
                jsxs('div', {
                  className: 'emg-grow',
                  children: [
                    jsx('span', { className: 'emg-label', children: 'HuggingFace repo' }),
                    jsx('input', { className: 'emg-in', style: { width: '100%' }, value: repo, onChange: (event) => setRepo(event.target.value) })
                  ]
                }),
                jsxs('div', {
                  style: { width: 120 },
                  children: [
                    jsx('span', { className: 'emg-label', children: t('量化') }),
                    jsx('input', { className: 'emg-in', style: { width: '100%' }, value: quant, onChange: (event) => setQuant(event.target.value) })
                  ]
                })
              ]
            }),
            jsxs('div', {
              className: 'emg-row',
              style: { marginTop: 10, flexWrap: 'wrap' },
              children: [
                jsxs('label', {
                  className: 'emg-row',
                  style: { gap: 6 },
                  children: [
                    jsx('input', { type: 'checkbox', checked: skipLlama, onChange: (event) => setSkipLlama(event.target.checked) }),
                    jsx('span', { className: 'emg-sub', children: t('跳过 llama.cpp（我已经有）') })
                  ]
                }),
                jsxs('label', {
                  className: 'emg-row',
                  style: { gap: 6 },
                  children: [
                    jsx('input', { type: 'checkbox', checked: autoChain, onChange: (event) => setAutoChain(event.target.checked) }),
                    jsx('span', { className: 'emg-sub', children: t('装完自动加入链路') })
                  ]
                }),
                jsx('span', { className: 'emg-grow' }),
                jsx(Btn, { primary: true, disabled: busy || running, onClick: installModel, children: running ? t('安装中…') : t('开始安装') })
              ]
            }),
            jsxs('div', {
              style: { marginTop: 12 },
              children: [
                jsxs('div', {
                  className: 'emg-row',
                  children: [
                    jsx('span', {
                      className: 'emg-sub emg-grow',
                      children: current ? `${current.state}${current.phase ? ` · ${current.phase}` : ''}${current.error ? ` · ${current.error}` : ''}` : t('空闲')
                    }),
                    jsx('span', {
                      className: 'emg-sub emg-mono',
                      children: current && current.total ? `${sizeMb(current.done)} / ${sizeMb(current.total)}` : current && current.elapsed ? `${current.elapsed}s` : ''
                    })
                  ]
                }),
                jsx('div', { className: 'emg-bar', style: { marginTop: 6 }, children: jsx('i', { style: { width: `${pct}%` } }) })
              ]
            })
          ]
        })
      })
    ]
  })
}

/* ------------------------------------------------------------------ 调度 */

const PRESETS = {
  light: { heads_vec_top_k: 5, session_heads_top_k: 3, unclosed_lessons: 1, decision_skeleton: 3, cross_top_k: 1 },
  normal: { heads_vec_top_k: 10, session_heads_top_k: 10, unclosed_lessons: 2, decision_skeleton: 5, cross_top_k: 3 },
  deep: { heads_vec_top_k: 20, session_heads_top_k: 15, unclosed_lessons: 3, decision_skeleton: 8, cross_top_k: 5 }
}

const FIELDS = [
  ['heads_vec_top_k', t('Tier1 · 向量近邻 top-k'), 1],
  ['heads_cos_min', t('Tier1 · 余弦下限'), 0.05],
  ['margin', t('Tier1 · 写边 margin'), 0.01],
  ['session_heads_top_k', t('Tier2 · 同会话最近 top-k'), 1],
  ['unclosed_lessons', t('Tier2 · 未闭环教训'), 1],
  ['decision_skeleton', t('Tier2 · 决策骨架'), 1],
  ['cross_top_k', t('Tier3 · 跨项目 top-k'), 1],
  ['cross_cos_min', t('Tier3 · 余弦下限'), 0.05],
  ['poll_seconds', t('轮询间隔（秒）'), 1]
]

const DEFAULTS = {
  heads_vec_top_k: 10,
  heads_cos_min: 0.35,
  margin: 0.15,
  session_heads_top_k: 10,
  unclosed_lessons: 2,
  decision_skeleton: 5,
  cross_top_k: 3,
  cross_cos_min: 0.7,
  poll_seconds: 60
}

function Schedule({ ctx, notify }) {
  const config = useFetch(ctx, '/api/config')
  const [form, setForm] = useState({ ...DEFAULTS })
  const [busy, setBusy] = useState(false)
  const seeded = useRef(false)

  useEffect(() => {
    if (!config.data || seeded.current) return
    const edges = ((config.data.effective || {}).edges) || {}
    const next = { ...DEFAULTS }
    for (const key of Object.keys(DEFAULTS)) if (edges[key] !== undefined && edges[key] !== null) next[key] = edges[key]
    setForm(next)
    seeded.current = true
  }, [config.data])

  const pairs = ['heads_vec_top_k', 'session_heads_top_k', 'unclosed_lessons', 'decision_skeleton', 'cross_top_k']
    .map((key) => Number(form[key]) || 0)
    .reduce((a, b) => a + b, 0)

  const edit = (key, value) => setForm((old) => ({ ...old, [key]: value }))

  const save = async () => {
    setBusy(true)
    const edges = {}
    for (const key of Object.keys(DEFAULTS)) edges[key] = Number(form[key])
    try {
      await ctx.rest('/api/config', { method: 'POST', body: { edges } })
      notify(t('已保存；下一轮 pass 用新值'), 'ok')
      seeded.current = false
      config.reload()
    } catch (error) {
      notify(msg(error), 'err')
    } finally {
      setBusy(false)
    }
  }

  const reset = async () => {
    setBusy(true)
    try {
      await ctx.rest('/api/config', { method: 'POST', body: { edges: null, decision: null, recall: null } })
      notify(t('已恢复默认（清掉 settings.yaml 里的覆盖）'), 'ok')
      seeded.current = false
      config.reload()
    } catch (error) {
      notify(msg(error), 'err')
    } finally {
      setBusy(false)
    }
  }

  return jsxs('div', {
    children: [
      jsx(Card, {
        title: t('预设（每轮问多少对）'),
        children: jsxs('div', {
          className: 'emg-row wrap',
          children: [
            jsx(Btn, { onClick: () => setForm((old) => ({ ...old, ...PRESETS.light })), children: t('轻') }, 'light'),
            jsx(Btn, { onClick: () => setForm((old) => ({ ...old, ...PRESETS.normal })), children: t('标准（默认）') }, 'normal'),
            jsx(Btn, { onClick: () => setForm((old) => ({ ...old, ...PRESETS.deep })), children: t('深') }, 'deep'),
            jsx('span', { className: 'emg-grow' }),
            jsx('span', { className: 'emg-sub', children: fmt('每事件 ≈ {0} 对 ≈ {1}（按 8 s/对估算）', pairs, dur(pairs * 8)) })
          ]
        })
      }),

      jsx(Card, {
        title: t('Tier 明细'),
        children: jsxs('div', {
          children: [
            jsx('div', {
              className: 'emg-grid c3',
              children: FIELDS.map(([key, label, step]) =>
                jsx('div', {
                  children: [
                    jsx('span', { className: 'emg-label', children: t(label) }),
                    jsx('input', {
                      className: 'emg-in',
                      style: { width: '100%' },
                      type: 'number',
                      step,
                      min: 0,
                      value: form[key],
                      onChange: (event) => edit(key, event.target.value)
                    })
                  ]
                }, key)
              )
            }),
            jsxs('div', {
              className: 'emg-row',
              style: { marginTop: 12, flexWrap: 'wrap' },
              children: [
                jsx(Btn, { primary: true, disabled: busy, onClick: save, children: t('保存') }),
                jsx(Btn, { disabled: busy, onClick: reset, children: t('全部恢复默认') }),
                jsx('span', { className: 'emg-sub emg-mono emg-trunc', children: (config.data && config.data.file) || t('（无配置文件）') })
              ]
            })
          ]
        })
      }),

      jsx(Card, {
        title: t('说明'),
        children: jsx('div', { className: 'emg-sub', children: t('Tier1 = 全局向量近邻；Tier2 = 同会话近期 + 未闭环教训 + 决策骨架；Tier3 = 跨项目。改的是 ~/.hermes/eventmem/settings.yaml，下一轮 pass 生效。') })
      })
    ]
  })
}

/* ------------------------------------------------------------------ 日志 */

function Logs({ ctx }) {
  const { data, error, reload } = useFetch(ctx, '/api/logs', 4000)
  const [follow, setFollow] = useState(true)
  const box = useRef(null)
  const lines = (data && data.logs) || []

  useEffect(() => {
    if (!follow || !box.current) return
    box.current.scrollTop = box.current.scrollHeight
  }, [lines.length, follow])

  return jsx(Card, {
    title: t('本进程日志（最近 400 条）'),
    right: jsxs('div', {
      className: 'emg-row',
      children: [
        jsxs('label', {
          className: 'emg-row',
          style: { gap: 5 },
          children: [
            jsx('input', { type: 'checkbox', checked: follow, onChange: (event) => setFollow(event.target.checked) }),
            jsx('span', { className: 'emg-sub', children: t('自动刷新') })
          ]
        }),
        jsx(Btn, { tiny: true, onClick: reload, children: t('刷新') })
      ]
    }),
    children: [
      error ? jsx(Note, { text: error, kind: 'err' }, 'err') : null,
      jsx('pre', {
        ref: box,
        className: 'emg-log',
        children: lines.length
          ? lines.map((line, index) =>
              jsx(
                'span',
                {
                  className:
                    line.level === 'WARNING' ? 'w' : line.level === 'ERROR' ? 'e' : line.level === 'DEBUG' ? 'd' : '',
                  children: `${when(line.ts)} ${String(line.level || '?')[0]} ${String(line.name || '').split('.').pop()}  ${line.message}\n`
                },
                String(index)
              )
            )
          : t('（还没有日志）')
      })
    ]
  })
}

/* ------------------------------------------------------------------ 图谱 */

function GraphTab({ notify }) {
  const shared = globalThis.__eventmemGraph
  if (!shared || !shared.GraphPage || !shared.ctx) {
    return jsx(Card, {
      title: t('图谱'),
      children: jsxs('div', {
        children: [
          jsx(Note, {
            text: t('没有找到 eventmem-graph 插件：它没装、没启用，或者加载顺序早于本页。\n图谱页现在只有这一个入口，插件要留着（它不再有自己的按钮和页面）。\n启用办法：hermes plugins enable eventmem-graph，然后在 app 里 Reload desktop plugins。'),
            kind: 'err'
          }),
          jsxs('div', {
            className: 'emg-row',
            style: { marginTop: 10 },
            children: [jsx(Btn, { onClick: () => { notify(t('重新加载中…')); host.navigate(PAGE) }, children: t('重新加载这一页') })]
          })
        ]
      })
    })
  }
  return jsx('div', { className: 'emg-graph', children: jsx(shared.GraphPage, { ctx: shared.ctx }) })
}

/* ------------------------------------------------------------------ page */

/* ------------------------------------------------------------------ live */

/**
 * Which backend is deciding right now, and how far along it is.
 *
 * The numbers come from the daemon's ``/api/status.live``, which the pass publishes through
 * ``on_progress`` after every answered pair — so the counter moves at pair granularity, the
 * same grain as the model call.  The two buttons are the reason this strip exists: a backend
 * that comes up mid-round (LM Studio finishing a load) would otherwise wait out a pass that
 * can run for half an hour on the slow one.
 */
function LiveBar({ ctx, notify }) {
  const status = useFetch(ctx, '/api/status', 1500)
  const live = (status.data && status.data.live) || null
  const busy = Boolean(live) || Boolean(status.data && status.data.busy)
  const stopping = Boolean(status.data && status.data.stopping)
  const pairs = live ? live.pairs : 0
  const edges = live ? live.edges : 0
  const previous = useRef(null)
  const [flash, setFlash] = useState(null)
  const pairNow = live ? live.pairs : null

  useEffect(() => {
    const before = previous.current
    previous.current = pairNow
    if (before === null || pairNow === null || pairNow <= before) return undefined
    setFlash({ n: pairNow - before, at: Date.now() })
    const timer = window.setTimeout(() => setFlash(null), 1000)
    return () => window.clearTimeout(timer)
  }, [pairNow])

  const packs = (status.data && status.data.backends) || []
  const active = packs.find((pack) => pack.name === ((live && live.backend) || '')) || null
  const runs = (status.data && status.data.runs) || []
  const last = runs[runs.length - 1] || null
  const elapsed = live ? Math.max(0, Date.now() / 1000 - live.since) : 0

  const post = async (path, message) => {
    try {
      await ctx.rest(path, { method: 'POST', body: {} })
      notify(t(message), 'ok')
      status.reload()
    } catch (error) {
      notify(msg(error), 'err')
    }
  }

  return jsxs('div', {
    className: `emg-live${busy ? ' on' : ''}`,
    children: [
      jsx('span', { className: `emg-dot${busy ? ' on' : ''}` }),
      jsx('span', { className: 'emg-sub', children: busy ? t('正在使用') : t('空闲') }),
      jsx('span', {
        className: 'emg-live-backend',
        children: (live && live.backend) || (last && last.backend) || '—'
      }),
      active ? jsx('span', { className: 'emg-tag', children: active.kind }) : null,
      jsx('span', { className: 'emg-grow' }),
      busy
        ? jsxs('div', {
            className: 'emg-row',
            children: [
              jsx('span', {
                className: 'emg-count',
                children: jsxs('span', { children: [jsx('b', { children: pairs }), ` ${t('对')}`] })
              }),
              jsx('span', {
                className: 'emg-count',
                children: jsxs('span', { children: [jsx('b', { children: edges }), ` ${t('边')}`] })
              }),
              flash ? jsx('span', { className: 'emg-plus', key: flash.at, children: `+${flash.n}` }) : null,
              jsx('span', { className: 'emg-sub', children: dur(elapsed) })
            ]
          })
        : jsx('span', {
            className: 'emg-sub',
            children: last
              ? fmt('上次 {0} · {1} · {2} 对 / {3} 边', last.backend || '—', when(last.ts), NI(last.pairs), NI(last.edges))
              : t('还没跑过')
          }),
      jsx(Btn, { danger: true, disabled: !busy, onClick: () => post('/api/stop', '已请求停止：本轮做完手上这一对就停'), children: stopping ? t('停止中…') : t('停止') }),
      jsx(Btn, { primary: true, onClick: () => post('/api/switch', '已请求切换：本轮停下后立刻重跑，重新探活链路'), children: t('切换并重跑') })
    ]
  })
}

function Console({ ctx }) {
  const [tab, setTab] = useState('overview')
  const [note, setNote] = useState({ text: '', kind: '' })
  const [busy, setBusy] = useState(false)
  const health = useFetch(ctx, '/api/health', 5000)
  const i18n = useFetch(ctx, '/api/i18n')
  const dark = useDarkTheme()
  const noteTimer = useRef(0)

  // Module-level on purpose (see the i18n block): every child render happens after this
  // line, and `t()` is also called from module-scope tables. One assignment is enough.
  if (i18n.data) {
    LANG = i18n.data.lang || 'zh'
    MAP = i18n.data.map || {}
  }

  const notify = useCallback((text, kind) => {
    setNote({ text: String(text || ''), kind: kind || '' })
    if (noteTimer.current) window.clearTimeout(noteTimer.current)
    noteTimer.current = window.setTimeout(() => setNote({ text: '', kind: '' }), kind === 'err' ? 9000 : 4000)
  }, [])

  useEffect(
    () => () => {
      if (noteTimer.current) window.clearTimeout(noteTimer.current)
    },
    []
  )

  const runOnce = async () => {
    setBusy(true)
    try {
      const result = await ctx.rest('/api/run-once', { method: 'POST', body: {} })
      notify(
        result.skipped
          ? fmt('跳过：{0}', result.skipped)
          : fmt('跑完：{0} 对，写 {1} 条边{2}', NI(result.pairs), NI(result.edges), result.backend ? fmt('（{0}）', result.backend) : ''),
        'ok'
      )
    } catch (error) {
      notify(msg(error), 'err')
    } finally {
      setBusy(false)
    }
  }

  const healthy = Boolean(health.data) && !health.error
  const bodyClass = tab === 'graph' ? 'emg-body still' : 'emg-body'

  if (!i18n.data && !i18n.error) {
    return jsxs('div', {
      className: `emg${dark ? ' dark' : ''}`,
      children: [
        jsx('div', { className: 'emg-head', children: jsx('div', { className: 'emg-title', children: 'eventmem' }) }),
        jsx('div', { className: 'emg-body', children: jsx('div', { className: 'emg-sub', children: t('载入中…') }) })
      ]
    })
  }

  return jsxs('div', {
    className: `emg${dark ? ' dark' : ''}`,
    children: [
      jsxs('div', {
        className: 'emg-head',
        children: [
          jsx('div', {
            className: 'emg-title',
            children: [t('记忆后台'), jsx('small', { children: (health.data && health.data.db) || '' })]
          }),
          jsx('nav', {
            className: 'emg-tabs',
            children: TABS.map(([key, label]) =>
              jsx(
                'button',
                {
                  type: 'button',
                  className: `emg-tab ${tab === key ? 'on' : ''}`,
                  onClick: () => setTab(key),
                  children: t(label)
                },
                key
              )
            )
          }),
          jsx('span', {
            className: `emg-pill ${healthy ? 'ok' : 'bad'}`,
            children: healthy ? `ok · schema ${NI(health.data.schema)} · pid ${NI(health.data.pid)}` : t('daemon 不可达')
          }),
          jsx(Btn, { primary: true, disabled: busy, onClick: runOnce, children: busy ? t('跑着…') : t('跑一轮') })
        ]
      }),
      jsx(LiveBar, { ctx, notify }),
      note.text ? jsx('div', { style: { padding: '8px 16px 0' }, children: jsx(Note, { text: note.text, kind: note.kind }) }) : null,
      jsx('div', {
        className: bodyClass,
        children:
          tab === 'overview'
            ? jsx(Overview, { ctx })
            : tab === 'backends'
              ? jsx(Backends, { ctx, notify })
              : tab === 'models'
                ? jsx(Models, { ctx, notify })
                : tab === 'schedule'
                  ? jsx(Schedule, { ctx, notify })
                  : tab === 'logs'
                    ? jsx(Logs, { ctx })
                    : jsx(GraphTab, { notify })
      })
    ]
  })
}

function Chip() {
  return jsx(Tip, {
    label: t('eventmem 记忆后台（概览 / 后端 / 模型 / 调度 / 日志 / 图谱）'),
    children: jsx('button', {
      type: 'button',
      className: 'emg-chipbar',
      onClick: () => {
        haptic('tap')
        host.navigate(PAGE)
      },
      children: jsxs('span', {
        children: [jsx('span', { className: 'emg-gear', children: '⚙' }), jsx('span', { children: t('记忆后台') })]
      })
    })
  })
}

export default {
  id: ID,
  name: 'eventmem gui',
  description: 'eventmem 的一体化后台：概览、后端链路与 config.yaml、模型安装、连边调度、日志、记忆图谱；远端 backend 也能用。',
  register(ctx) {
    // The status-bar chip renders before the page does, so warm the dictionary here too —
    // otherwise an English app shows a Chinese chip until the page is opened.
    ctx
      .rest('/api/i18n')
      .then((data) => {
        LANG = (data && data.lang) || 'zh'
        MAP = (data && data.map) || {}
      })
      .catch(() => {})

    const style = document.createElement('style')
    style.dataset.plugin = ID
    style.textContent = CSS
    document.head.append(style)
    ctx.onDispose(() => style.remove())

    ctx.register({
      id: 'eventmem-gui-page',
      area: ROUTES_AREA,
      title: '记忆后台',
      data: { path: PAGE },
      render: () => jsx(Console, { ctx })
    })
    ctx.register({
      id: 'eventmem-gui-chip',
      area: STATUSBAR_AREAS.right,
      order: 11,
      render: () => jsx(Chip, {})
    })
  }
}
