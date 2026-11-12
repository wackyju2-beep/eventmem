/**
 * eventmem-graph — the 3D memory graph, as a component.
 *
 * Headless: it registers no route and no status-bar chip. The graph is the 图谱 tab inside
 * `eventmem-gui`; this module keeps the rendering (orbit/zoom/focus/edit) and the data path
 * (`/api/plugins/eventmem-graph`) and hands its component to the console through
 * `globalThis.__eventmemGraph`.
 *
 * Drag = orbit, wheel = zoom, click an atom/edge = focus + inspect; edits get a rainbow rim.
 *
 * Plain ESM, loaded uncompiled — jsx() calls, not JSX syntax.
 */

import {
  Button,
  EmptyState,
  GlyphSpinner,
  haptic,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'eventmem-graph'

const KIND_COLORS = {
  决定: '#6aa9ff',
  尝试: '#3ad6c4',
  结果: '#48c979',
  变更: '#f2a93b',
  教训: '#ff6f8e'
}
const REL_COLORS = {
  改变: '#f2a93b',
  否定: '#ff5d5d',
  重复: '#3ec7a4',
  因为: '#a877ff',
  细化: '#54a8ff'
}
const NONE_COLOR = '#8b93a3'
// 人工改过的边原来吃 SVG 的 linearGradient；canvas 里一个纯色更省，也更清楚。
const EDITED_COLOR = '#c58bff'
const KINDS = ['决定', '尝试', '结果', '变更', '教训']
const RELS = ['改变', '否定', '重复', '因为', '细化']
const AXIS_LABELS = ['重复', '改变', '否定', '都不是']
const DETAIL_LABELS = ['因为', '细化', '都不是']

// Disk plugins are not scanned by Tailwind: every class below is plain CSS, colored from the
// app's own theme vars so the graph follows the active skin.
const CSS = `
.em-root{position:relative;height:100%;min-height:360px;overflow:hidden;color:var(--ui-text-primary);font-size:12px}
.em-root.em-center{display:grid;place-items:center;align-content:center;gap:8px;padding:28px;text-align:center}
.em-stage{position:absolute;inset:0;overflow:hidden;cursor:grab;touch-action:none;
  background:radial-gradient(circle at 50% 42%,color-mix(in srgb,var(--ui-accent) 8%,transparent),transparent 64%),
             radial-gradient(circle at 50% 50%,transparent 38%,color-mix(in srgb,var(--ui-bg-primary) 62%,transparent) 100%)}
.em-stage:active{cursor:grabbing}
/* Edges live on two canvases: a back one under the atom cards, a front one for the
   selected edge and the labels. Drawing 2k+ edges as <path> elements meant ~13k
   attribute writes per frame and dropped the whole window to single-digit fps. */
.em-edges{position:absolute;inset:0;pointer-events:none;width:100%;height:100%}
.em-edge-back{z-index:1}
.em-edges-front{z-index:25000}
.em-atom-layer{position:absolute;inset:0;pointer-events:none}
.em-atom{position:absolute;left:0;top:0;width:116px;box-sizing:border-box;padding:4px 6px 5px;border-radius:6px;
  pointer-events:auto;cursor:pointer;user-select:none;overflow:hidden;
  background:color-mix(in srgb,var(--ui-bg-card) 86%,var(--ui-bg-primary));
  border:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 74%,transparent);
  border-left:3px solid var(--ui-stroke-secondary);
  box-shadow:0 7px 20px color-mix(in srgb,var(--ui-base) 34%,transparent);
  transition:opacity .18s ease,filter .18s ease;will-change:transform,opacity}
.em-atom[data-kind="决定"]{border-left-color:#6aa9ff}
.em-atom[data-kind="尝试"]{border-left-color:#3ad6c4}
.em-atom[data-kind="结果"]{border-left-color:#48c979}
.em-atom[data-kind="变更"]{border-left-color:#f2a93b}
.em-atom[data-kind="教训"]{border-left-color:#ff6f8e}
.em-atom:hover{z-index:30000!important;box-shadow:0 10px 28px color-mix(in srgb,var(--ui-base) 46%,transparent)}
.em-atom[data-state="promo"]{filter:grayscale(.9)}
.em-atom[data-state="promo"] .em-atom-text{color:var(--ui-text-tertiary)}
.em-atom[data-state="out"]{filter:grayscale(1)}
.em-atom[data-state="out"] .em-atom-text{color:var(--ui-text-quaternary)}
.em-atom[data-selected="1"]{box-shadow:0 0 0 2px var(--ui-accent),0 12px 30px color-mix(in srgb,var(--ui-base) 50%,transparent)}
.em-atom[data-edited="1"]::after{content:'';position:absolute;inset:-1px;border-radius:8px;padding:2px;pointer-events:none;
  background:conic-gradient(from 0deg,#ff5f6d,#ffc371,#a8e063,#56ccf2,#9b73ff,#ff5f6d);
  -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);
  -webkit-mask-composite:xor;mask-composite:exclude;animation:em-hue 7s linear infinite}
.em-atom-head{display:flex;align-items:center;gap:4px;font-size:8.5px;line-height:1.25}
.em-atom-id{font-family:var(--font-mono,ui-monospace,monospace);font-weight:700;color:var(--ui-text-primary)}
.em-atom-kind{font-weight:600}
.em-atom-time{margin-left:auto;color:var(--ui-text-quaternary);font-variant-numeric:tabular-nums;white-space:nowrap}
.em-atom-text{margin-top:2px;font-size:8.5px;line-height:1.3;color:var(--ui-text-secondary);word-break:break-word;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.em-atom-edited{position:absolute;right:4px;bottom:2px;font-size:9px;color:var(--ui-accent);opacity:.9}
.em-atom[data-clickable="0"]{cursor:default}
.em-atom[data-compact="1"]{width:88px;padding:2px 5px;border-radius:5px}
.em-atom[data-compact="1"] .em-atom-text,.em-atom[data-compact="1"] .em-atom-time{display:none}
.em-atom[data-compact="1"] .em-atom-head{font-size:8px}
.em-atom[data-focus="1"]{box-shadow:0 0 0 1.5px var(--ui-accent),0 24px 60px color-mix(in srgb,var(--ui-base) 62%,transparent);background:color-mix(in srgb,var(--ui-bg-card) 96%,var(--ui-bg-primary))}
.em-atom[data-focus="1"] .em-atom-text{-webkit-line-clamp:8;color:var(--ui-text-primary);font-size:9px}
.em-atom[data-focus="1"] .em-atom-time{color:var(--ui-text-tertiary)}
@keyframes em-hue{to{filter:hue-rotate(360deg)}}
.em-bar{position:absolute;left:10px;right:10px;top:10px;z-index:40000;display:flex;flex-wrap:wrap;align-items:center;gap:6px;
  padding:6px 8px;border-radius:9px;pointer-events:auto;
  background:color-mix(in srgb,var(--ui-bg-elevated) 82%,transparent);border:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 70%,transparent);
  backdrop-filter:blur(12px)}
.em-brand{font-weight:650;letter-spacing:.02em;margin-right:2px}
.em-seg{display:inline-flex;border:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 70%,transparent);border-radius:6px;overflow:hidden}
.em-seg-btn{font-size:11px;padding:3px 8px;line-height:1.35;color:var(--ui-text-tertiary);background:transparent;border:0;cursor:pointer}
.em-seg-btn:hover{background:var(--chrome-action-hover);color:var(--ui-text-primary)}
.em-seg-btn[data-on="1"]{background:color-mix(in srgb,var(--ui-accent) 20%,transparent);color:var(--ui-text-primary)}
.em-select{height:24px;font-size:11px;padding:0 6px;border-radius:5px;background:var(--ui-bg-tertiary);color:var(--ui-text-primary);
  border:1px solid var(--ui-stroke-secondary);outline:none;max-width:260px;
  appearance:none;-webkit-appearance:none}
  /* --ui-bg-input is #fcfcfc in both themes (host styles.css:314), so a native select
     stays white in dark mode; appearance:none is what stops the UA painting it. */
.em-value{min-width:150px}
.em-check{display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--ui-text-tertiary);cursor:pointer;user-select:none}
.em-check input{accent-color:var(--ui-accent);margin:0}
.em-counts{font-size:10.5px;color:var(--ui-text-quaternary);font-variant-numeric:tabular-nums}
.em-spacer{flex:1}
.em-legend{position:absolute;left:12px;bottom:10px;z-index:40000;display:flex;flex-wrap:wrap;align-items:center;gap:8px;
  padding:5px 8px;border-radius:8px;font-size:10px;color:var(--ui-text-tertiary);pointer-events:none;
  background:color-mix(in srgb,var(--ui-bg-primary) 62%,transparent);border:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 45%,transparent)}
.em-legend-item{display:inline-flex;align-items:center;gap:4px}
.em-legend-dot{width:7px;height:7px;border-radius:2px;display:inline-block}
.em-legend-line{width:14px;height:2px;border-radius:1px;display:inline-block}
.em-legend-dash{height:0;border-top:1.5px dashed;background:none!important}
.em-legend-sep{width:1px;height:10px;background:var(--ui-stroke-secondary)}
.em-insp{position:absolute;top:10px;right:10px;bottom:10px;width:384px;max-width:calc(100% - 20px);z-index:41000;overflow:auto;
  display:flex;flex-direction:column;gap:10px;padding:12px 14px 16px;border-radius:10px;pointer-events:auto;
  background:color-mix(in srgb,var(--ui-bg-elevated) 96%,transparent);border:1px solid var(--ui-stroke-secondary);
  box-shadow:0 18px 40px color-mix(in srgb,var(--ui-base) 44%,transparent)}
.em-insp-head{display:flex;align-items:center;gap:8px}
.em-insp-title{font-size:15px;font-weight:650;font-variant-numeric:tabular-nums}
.em-insp-sub{font-size:11px;color:var(--ui-text-tertiary)}
.em-x{margin-left:auto;border:0;background:transparent;color:var(--ui-text-tertiary);cursor:pointer;font-size:12px;padding:2px 4px;border-radius:4px}
.em-x:hover{background:var(--chrome-action-hover);color:var(--ui-text-primary)}
.em-meta{display:flex;flex-direction:column;gap:2px;font-size:10.5px;color:var(--ui-text-tertiary);font-variant-numeric:tabular-nums}
.em-edited-note{color:var(--ui-accent)}
.em-field{display:flex;flex-direction:column;gap:4px}
.em-label{font-size:10.5px;color:var(--ui-text-tertiary)}
.em-textarea{width:100%;box-sizing:border-box;font-size:12px;line-height:1.5;padding:7px 8px;border-radius:6px;resize:vertical;
  background:var(--ui-bg-input);color:var(--ui-text-primary);border:1px solid var(--ui-stroke-secondary);outline:none;font-family:inherit}
.em-textarea:focus{border-color:var(--ui-accent)}
.em-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.em-hint{font-size:10px;color:var(--ui-text-quaternary);line-height:1.4}
.em-error{font-size:11px;color:var(--ui-red,#ff5d5d)}
.em-error-title{font-size:14px;font-weight:650}
.em-error-body{font-size:11px;color:var(--ui-text-tertiary);max-width:560px;word-break:break-word}
.em-section{display:flex;flex-direction:column;gap:5px;margin-top:2px}
.em-section-title{font-size:10.5px;color:var(--ui-text-tertiary);text-transform:uppercase;letter-spacing:.06em}
.em-edge-list{display:flex;flex-direction:column;gap:3px;max-height:180px;overflow:auto}
.em-edge-row{display:block;width:100%;text-align:left;border:0;background:transparent;color:var(--ui-text-secondary);
  font-size:11px;padding:3px 5px;border-radius:4px;cursor:pointer}
.em-edge-row:hover{background:var(--chrome-action-hover);color:var(--ui-text-primary)}
.em-edge-row>span{display:flex;align-items:center;gap:6px}
.em-edge-dot{width:6px;height:6px;border-radius:50%;flex:none}
.em-edge-row-rel{min-width:26px}
.em-edge-row-id{font-family:var(--font-mono,ui-monospace,monospace);color:var(--ui-text-quaternary)}
.em-edge-row-flag{margin-left:auto;color:var(--ui-accent)}
.em-endpoints{display:flex;flex-direction:column;gap:5px}
.em-endpoint{display:block;width:100%;text-align:left;border:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 60%,transparent);
  background:color-mix(in srgb,var(--ui-bg-card) 60%,transparent);border-radius:6px;padding:6px 8px;cursor:pointer;color:inherit}
.em-endpoint:hover{border-color:var(--ui-accent)}
.em-endpoint-head{display:flex;align-items:center;gap:6px;font-size:10.5px}
.em-endpoint-id{font-family:var(--font-mono,ui-monospace,monospace);font-weight:700}
.em-endpoint-kind{font-weight:600}
.em-endpoint-role{margin-left:auto;color:var(--ui-text-quaternary)}
.em-endpoint-text{display:block;margin-top:3px;font-size:10.5px;line-height:1.4;color:var(--ui-text-secondary);
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.em-prob{display:flex;flex-direction:column;gap:3px;padding:7px 8px;border-radius:6px;
  background:color-mix(in srgb,var(--ui-bg-card) 55%,transparent);border:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 42%,transparent)}
.em-prob-title{font-size:10.5px;color:var(--ui-text-tertiary);margin-bottom:2px}
.em-prob-row{display:flex;align-items:center;gap:7px;font-size:10.5px}
.em-prob-row[data-hi="1"] .em-prob-label{color:var(--ui-text-primary);font-weight:650}
.em-prob-label{width:38px;flex:none;color:var(--ui-text-tertiary)}
.em-prob-track{flex:1;height:6px;border-radius:3px;background:color-mix(in srgb,var(--ui-stroke-secondary) 30%,transparent);overflow:hidden}
.em-prob-fill{display:block;height:100%;border-radius:3px;opacity:.85}
.em-prob-num{width:44px;text-align:right;font-family:var(--font-mono,ui-monospace,monospace);color:var(--ui-text-tertiary);font-variant-numeric:tabular-nums}
.em-prob-empty{font-size:10px;color:var(--ui-text-quaternary);padding:4px 0}
.em-rel-select{width:100%;height:26px}
.em-chip{display:inline-flex;align-items:center;height:100%;padding:0 7px;border:0;background:transparent;
  color:var(--ui-text-tertiary);cursor:pointer;font-size:11px;white-space:nowrap}
.em-chip:hover{background:var(--chrome-action-hover);color:var(--ui-text-primary)}
.em-chip-inner{display:inline-flex;align-items:center;gap:4px}
.em-chip-glyph{color:var(--ui-accent);font-size:12px;line-height:1}
`

// --------------------------------------------------------------------------- helpers

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v))

// 世界单位只由相机距离定义：START_DIST 是默认视距，FOCUS_DIST 是"拉到眼前"后的视距。
const START_DIST = 3.5
const FOCUS_DIST = 2.4
const PULL_IN = 0.7
const easeInOut = t => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2)

function pad2(n) {
  return String(n).padStart(2, '0')
}
function fmtTime(ts) {
  const d = new Date(ts * 1000)
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}
function fmtFull(ts) {
  const d = new Date(ts * 1000)
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
}
function dayKey(ts) {
  const d = new Date(ts * 1000)
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}
function shortSession(sid) {
  if (!sid) return '无会话'
  const m = /^(\d{8})_(\d{6})/.exec(sid)
  if (!m) return sid
  return `${m[1].slice(4, 6)}-${m[1].slice(6, 8)} ${m[2].slice(0, 2)}:${m[2].slice(2, 4)}:${m[2].slice(4, 6)}`
}

// --------------------------------------------------------------------------- scene layout

/**
 * Atoms live in per-project helixes on a horizontal ring: each project is a chronologically
 * wound column (old at the bottom), so time is legible and the cloud is genuinely 3D.
 */
/**
 * Atoms live on a 3D lattice: the newest memory sits at the centre and older ones fill
 * outward shells, so the cloud "grows" from the middle and looks the same from any angle.
 * Cell spacing is 1 world unit — the camera distance is what decides how much of it you see.
 * `pos` (id → {x,y,z}) carries positions the user has dragged/pulled and that we saved.
 */
function buildScene(data, pos) {
  const events = data.events || []
  const N = Math.max(1, events.length)

  const side = Math.max(1, Math.ceil(Math.cbrt(N)))
  const nx = side
  const nz = side
  const ny = Math.max(1, Math.ceil(N / (nx * nz)))
  const cells = []
  for (let i = 0; i < nx; i += 1) {
    for (let j = 0; j < ny; j += 1) {
      for (let k = 0; k < nz; k += 1) {
        const x = i - (nx - 1) / 2
        const y = j - (ny - 1) / 2
        const z = k - (nz - 1) / 2
        cells.push({
          x,
          y,
          z,
          d: x * x + y * y * 1.2 + z * z,
          tie: ((i * 73 + j * 131 + k * 197) % 977) / 977
        })
      }
    }
  }
  // 从中心向外一层层铺，最新的原子占最中间的格子。
  cells.sort((a, b) => a.d - b.d || a.tie - b.tie)

  const order = [...events].sort((a, b) => b.ts - a.ts || b.id - a.id)
  const atoms = []
  order.forEach((ev, idx) => {
    const c = cells[idx % cells.length]
    const j1 = (((ev.id * 2654435761) % 997) / 997 - 0.5) * 0.1
    const j2 = (((ev.id * 40503 + 17) % 977) / 977 - 0.5) * 0.1
    const j3 = (((ev.id * 7919 + 41) % 983) / 983 - 0.5) * 0.1
    const home = { x: c.x + j1, y: c.y + j2, z: c.z + j3 }
    const saved = pos && pos.get(ev.id)
    atoms.push({
      ...ev,
      project: ev.project || '未归类',
      home,
      x: saved ? saved.x : home.x,
      y: saved ? saved.y : home.y,
      z: saved ? saved.z : home.z
    })
  })

  const byId = new Map(atoms.map(a => [a.id, a]))
  const pairKeys = new Set((data.edges || []).map(e => `${e.src}-${e.dst}`))
  const edges = []
  for (const e of data.edges || []) {
    if (!byId.has(e.src) || !byId.has(e.dst)) continue
    edges.push({
      key: `r|${e.src}|${e.dst}|${e.rel}`,
      kind: 'rel',
      src: e.src,
      dst: e.dst,
      rel: e.rel,
      conf: e.conf,
      cross: Boolean(e.cross),
      probs: e.probs || null,
      edited: Boolean(e.edited_ts),
      manual: e.decided_by === 'human',
      decidedBy: e.decided_by
    })
  }
  for (const p of data.pairs || []) {
    if (!byId.has(p.src) || !byId.has(p.dst)) continue
    if (pairKeys.has(`${p.src}-${p.dst}`)) continue
    edges.push({
      key: `p|${p.src}|${p.dst}`,
      kind: 'none',
      src: p.src,
      dst: p.dst,
      rel: null,
      conf: 0,
      cross: false,
      probs: null,
      pair: p,
      edited: Boolean(p.edited_ts),
      manual: false
    })
  }

  // 每条边都给一段可见的弧（同一对之间的多条边左右错开），不要画成直线。
  const byPair = new Map()
  for (const e of edges) {
    const key = `${Math.min(e.src, e.dst)}-${Math.max(e.src, e.dst)}`
    if (!byPair.has(key)) byPair.set(key, [])
    byPair.get(key).push(e)
  }
  for (const list of byPair.values()) {
    list.forEach((e, i) => {
      let h = 0
      for (let j = 0; j < e.key.length; j += 1) h = (h * 31 + e.key.charCodeAt(j)) % 997
      const amp = 0.14 + ((h % 9) / 9) * 0.14
      const sign = i % 2 === 0 ? 1 : -1
      e.bow = clamp(sign * (amp + Math.floor(i / 2) * 0.16), -0.62, 0.62)
    })
  }

  let radius = 1.4
  for (const a of atoms) radius = Math.max(radius, Math.hypot(a.x, a.y, a.z))

  const seed = order[0]
  return { atoms, byId, edges, radius, newestId: seed ? seed.id : null, projects: [...new Set(atoms.map(a => a.project))].sort() }
}

function fitDistance(radius, aspect, fovDeg = FOV_DEG) {
  const half = ((fovDeg / 2) * Math.PI) / 180
  const horizontal = Math.atan(Math.tan(half) * Math.max(0.45, aspect))
  return (radius * 1.12) / Math.sin(Math.min(half, horizontal))
}

function animateCamera(cam, patch, ms = 520) {
  const to = {
    yaw: cam.yaw,
    pitch: cam.pitch,
    dist: cam.dist,
    fx: cam.fx,
    fy: cam.fy,
    fz: cam.fz,
    panX: cam.panX || 0,
    panY: cam.panY || 0,
    ...patch
  }
  cam.anim = {
    t0: performance.now(),
    ms,
    from: {
      yaw: cam.yaw,
      pitch: cam.pitch,
      dist: cam.dist,
      fx: cam.fx,
      fy: cam.fy,
      fz: cam.fz,
      panX: cam.panX || 0,
      panY: cam.panY || 0
    },
    to
  }
  cam.dirty = true
}

// --------------------------------------------------------------------------- scene component

const FOV_DEG = 26
const FOV = (FOV_DEG * Math.PI) / 180

/** Eye position + orthonormal camera basis (forward / right / up) for a camera state. */
function cameraBasis(cam) {
  const cp = Math.cos(cam.pitch)
  const sp = Math.sin(cam.pitch)
  const cy = Math.cos(cam.yaw)
  const sy = Math.sin(cam.yaw)
  const ex = cam.fx + cam.dist * cp * sy
  const ey = cam.fy + cam.dist * sp
  const ez = cam.fz + cam.dist * cp * cy
  let fx = cam.fx - ex
  let fy = cam.fy - ey
  let fz = cam.fz - ez
  const fl = Math.hypot(fx, fy, fz) || 1
  fx /= fl
  fy /= fl
  fz /= fl
  let rx = -fz
  let rz = fx
  const rl = Math.hypot(rx, rz) || 1
  rx /= rl
  rz /= rl
  // up = cross(right, forward)，right 的 y 恒为 0
  const ux = -rz * fy
  const uy = rz * fx - rx * fz
  const uz = rx * fy
  return { ex, ey, ez, fx, fy, fz, rx, ry: 0, rz, ux, uy, uz }
}

function Scene({
  scene,
  focusInfo,
  sel,
  focusReq,
  showLabels,
  camRef,
  posRef,
  onSelectAtom,
  onSelectEdge,
  onBackground,
  onMoved
}) {
  const stageRef = useRef(null)
  const sizeRef = useRef({ w: 0, h: 0 })
  const atomEls = useRef(new Map())
  const backRef = useRef(null)
  const frontRef = useRef(null)
  const haloRef = useRef('#0f1115')
  const hitRef = useRef([])
  const dragRef = useRef(null)
  const movedRef = useRef(0)
  const atomAnims = useRef(new Map())
  const lastReqRef = useRef(0)
  const sceneRef = useRef(scene)
  sceneRef.current = scene
  const handlersRef = useRef({})
  handlersRef.current = { onSelectAtom, onSelectEdge, onBackground, onMoved }
  const focusRef = useRef(focusInfo)
  focusRef.current = focusInfo
  const selRef = useRef(sel)
  selRef.current = sel
  const labelsRef = useRef(showLabels)
  labelsRef.current = showLabels

  useEffect(() => {
    camRef.current.dirty = true
  }, [focusInfo, sel, camRef])

  const setAtomEl = useCallback((id, el) => {
    if (el) atomEls.current.set(id, el)
    else atomEls.current.delete(id)
  }, [])

  useEffect(() => {
    const live = new Set(scene.atoms.map(a => a.id))
    for (const id of [...atomEls.current.keys()]) if (!live.has(id)) atomEls.current.delete(id)
    camRef.current.dirty = true
  }, [scene, camRef])

  // Size tracking + opening camera: sit a few cells away from the newest atom, not at the far wall.
  useEffect(() => {
    const el = stageRef.current
    if (!el) return undefined
    const measure = () => {
      const rect = el.getBoundingClientRect()
      const w = Math.max(1, Math.round(rect.width))
      const h = Math.max(1, Math.round(rect.height))
      if (w === sizeRef.current.w && h === sizeRef.current.h) return
      sizeRef.current = { w, h }
      const cam = camRef.current
      cam.fit = fitDistance(scene.radius, w / h)
      cam.min = Math.max(0.8, cam.fit * 0.16)
      cam.max = cam.fit * 2.8
      if (!cam.ready) {
        cam.ready = true
        cam.startDist = START_DIST
        cam.dist = START_DIST
        const seed = scene.byId.get(scene.newestId)
        cam.fx = seed ? seed.x : 0
        cam.fy = seed ? seed.y : 0
        cam.fz = seed ? seed.z : 0
      }
      const cs = getComputedStyle(document.documentElement)
      haloRef.current = cs.getPropertyValue('--ui-bg-primary').trim() || '#0f1115'
      cam.dirty = true
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [scene, camRef])

  // One rAF loop; React never re-renders for camera/atom motion.
  useEffect(() => {
    let raf = 0
    const tick = now => {
      raf = requestAnimationFrame(tick)
      const cam = camRef.current
      if (cam.anim) {
        const { t0, ms, from, to } = cam.anim
        const t = clamp((now - t0) / ms, 0, 1)
        const e = easeInOut(t)
        for (const k of ['yaw', 'pitch', 'dist', 'fx', 'fy', 'fz', 'panX', 'panY']) {
          if (to[k] !== undefined && from[k] !== undefined) cam[k] = from[k] + (to[k] - from[k]) * e
        }
        if (t >= 1) cam.anim = null
        cam.dirty = true
      }

      const anims = atomAnims.current
      if (anims.size) {
        const t = performance.now()
        const done = []
        for (const [id, a] of anims) {
          const atom = scene.byId.get(id)
          if (!atom) {
            anims.delete(id)
            continue
          }
          const p = clamp((t - a.t0) / a.ms, 0, 1)
          const e = easeInOut(p)
          atom.x = a.from.x + (a.to.x - a.from.x) * e
          atom.y = a.from.y + (a.to.y - a.from.y) * e
          atom.z = a.from.z + (a.to.z - a.from.z) * e
          posRef.current.set(id, { x: atom.x, y: atom.y, z: atom.z })
          if (p >= 1) {
            done.push(id)
            anims.delete(id)
          }
        }
        cam.dirty = true
        if (done.length) handlersRef.current.onMoved(done)
      }

      if (!cam.dirty) return
      cam.dirty = false
      draw(now, cam)
    }

    const draw = (now, cam) => {
      const { w, h } = sizeRef.current
      if (!w || !h) return
      const b = cameraBasis(cam)
      const focal = h / 2 / Math.tan(FOV / 2)
      const startDist = cam.startDist || START_DIST
      const fi = focusRef.current
      const active = fi.active
      const push = startDist * 0.55
      const selNow = selRef.current
      const panX = cam.panX || 0
      const panY = cam.panY || 0

      let focusIds = []
      if (selNow) {
        if (selNow.type === 'atom') focusIds = [selNow.id]
        else {
          const e = scene.edges.find(x => x.key === selNow.key)
          if (e) focusIds = [e.src, e.dst]
        }
      }

      const project = (atom, pushed) => {
        const vx = atom.x - b.ex
        const vy = atom.y - b.ey
        const vz = atom.z - b.ez
        const zc = vx * b.fx + vy * b.fy + vz * b.fz + (pushed ? push : 0)
        if (zc <= 0.05) return null
        const xc = vx * b.rx + vz * b.rz
        const yc = vx * b.ux + vy * b.uy + vz * b.uz
        const unit = focal / zc
        return {
          x: w / 2 + xc * unit + panX,
          y: h / 2 - yc * unit + panY,
          z: zc,
          unit,
          k: clamp(startDist / zc, 0.3, 1.55)
        }
      }

      const positions = new Map()
      for (const atom of scene.atoms) {
        const state = !active ? 'in' : fi.match.has(atom.id) ? 'in' : fi.focus.has(atom.id) ? 'promo' : 'out'
        const p = project(atom, state === 'out')
        if (!p) {
          positions.set(atom.id, null)
          const el0 = atomEls.current.get(atom.id)
          if (el0) el0.style.display = 'none'
          continue
        }
        positions.set(atom.id, { ...p, state })
        const el = atomEls.current.get(atom.id)
        if (!el) continue
        el.style.display = ''
        const isSel = focusIds.includes(atom.id)
        el.style.transform = `translate3d(${p.x.toFixed(1)}px, ${p.y.toFixed(1)}px, 0) translate(-50%, -50%) scale(${p.k.toFixed(3)})`
        el.dataset.compact = p.k < 0.55 ? '1' : '0'
        el.dataset.focus = isSel ? '1' : '0'
        el.dataset.state = state
        el.dataset.selected = isSel ? '1' : '0'
        // 筛选时被排除在外的原子不响应点击（点它只等于点空白）
        el.dataset.clickable = !active || fi.focus.has(atom.id) ? '1' : '0'
        el.style.opacity =
          state === 'out'
            ? '0.2'
            : state === 'promo'
              ? '0.5'
              : String(clamp(1.25 - (p.z / (startDist * 2.2)) * 0.5, 0.45, 1))
        // 选中层压在所有边/标签之上（SVG 是 25000）。
        el.style.zIndex = isSel ? '34000' : String(Math.round(20000 - p.z * 100))
      }

      // ---- edges, on canvas -------------------------------------------------
      // One Path2D per (color, opacity, width) bucket: thousands of edges collapse into
      // a few dozen stroke() calls, and nothing in this loop touches the DOM.
      const dpr = Math.min(2, window.devicePixelRatio || 1)
      const prep = el => {
        if (!el) return null
        const pw = Math.round(w * dpr)
        const ph = Math.round(h * dpr)
        if (el.width !== pw || el.height !== ph) {
          el.width = pw
          el.height = ph
        }
        const c = el.getContext('2d')
        c.setTransform(dpr, 0, 0, dpr, 0, 0)
        c.clearRect(0, 0, w, h)
        return c
      }
      const back = prep(backRef.current)
      const front = prep(frontRef.current)

      const screenEdges = []
      const selKey = selNow && selNow.type === 'edge' ? selNow.key : null
      const selAtom = selNow && selNow.type === 'atom' ? selNow.id : null
      const labelsOn = labelsRef.current
      // LOD by zoom (cam.dist relative to the distance that fits the whole graph), not by
      // raw depth: when the camera pulls back the edges would stack into a sub-pixel haze,
      // so draw a stable subset instead — half, then a quarter — with a higher alpha floor
      // and a 1px minimum so the shape of the graph still reads.
      const zoomOut = cam.dist / (cam.fit || cam.dist)
      const coarse = zoomOut > 1.9
      const midCoarse = !coarse && zoomOut > 1.15
      const sampleEdge = (e, n) => (e.key.length + e.key.charCodeAt(0) + e.key.charCodeAt(e.key.length - 1)) % n === 0
      const buckets = new Map()
      const labels = []
      let selEdge = null

      for (const edge of scene.edges) {
        const a = positions.get(edge.src)
        const c = positions.get(edge.dst)
        if (!a || !c) continue
        const isSel = selKey === edge.key
        const offP = p => p.x < -80 || p.x > w + 80 || p.y < -80 || p.y > h + 80
        if (!isSel && offP(a) && offP(c)) continue
        if (!isSel && coarse && !sampleEdge(edge, 4)) continue
        if (!isSel && midCoarse && !sampleEdge(edge, 2)) continue
        const inA = a.state !== 'out'
        const inB = c.state !== 'out'
        const cls = !active ? 'in' : inA && inB ? 'in' : inA || inB ? 'cross' : 'out'
        const color = edge.edited ? EDITED_COLOR : edge.kind === 'none' ? NONE_COLOR : REL_COLORS[edge.rel] || NONE_COLOR
        const crossFade = edge.cross ? 0.45 : 1
        const depthFade = clamp(1.45 - Math.max(a.z, c.z) / (startDist * 1.85), 0.16, 1)
        const far = coarse || midCoarse
        const baseOpacity = far
          ? Math.max((cls === 'out' ? 0.05 : cls === 'cross' ? 0.13 : 0.2) * crossFade, coarse ? 0.14 : 0.1)
          : (cls === 'out' ? 0.04 : cls === 'cross' ? 0.12 : 0.22) * crossFade * depthFade
        const k = clamp(startDist / Math.min(a.z, c.z), 0.45, 1.65)
        const width = far
          ? Math.max(0.9, 1.15 * (edge.cross ? 0.8 : 1) * k)
          : 1.15 * (edge.cross ? 0.8 : 1) * k

        const dx = c.x - a.x
        const dy = c.y - a.y
        const len = Math.hypot(dx, dy) || 1
        const bow = isSel ? 0.1 : edge.bow || 0.16
        const cx = (a.x + c.x) / 2 + (-dy / len) * bow * len
        const cy = (a.y + c.y) / 2 + (dx / len) * bow * len
        // 筛选时两头都被排除的边不响应点击
        if (!active || fi.focus.has(edge.src) || fi.focus.has(edge.dst)) {
          screenEdges.push({ key: edge.key, edge, x1: a.x, y1: a.y, x2: c.x, y2: c.y, cx, cy })
        }

        if (isSel) {
          selEdge = { color, x1: a.x, y1: a.y, cx, cy, x2: c.x, y2: c.y, width }
        } else if (back) {
          const op = Math.round(baseOpacity * 100) / 100
          const wd = Math.round(width * 2) / 2
          const bucketKey = `${color}|${op}|${wd}`
          let bucket = buckets.get(bucketKey)
          if (!bucket) {
            bucket = { color, op, wd, path: new Path2D() }
            buckets.set(bucketKey, bucket)
          }
          bucket.path.moveTo(a.x, a.y)
          bucket.path.quadraticCurveTo(cx, cy, c.x, c.y)
        }

        const incident = selAtom != null && (edge.src === selAtom || edge.dst === selAtom)
        const visible = isSel || (incident && len < 420) || (labelsOn && cls === 'in' && len > 120)
        if (!visible) continue
        labels.push({
          text: edge.kind === 'none' ? '无' : edge.rel,
          color,
          x: 0.25 * a.x + 0.5 * cx + 0.25 * c.x,
          y: 0.25 * a.y + 0.5 * cy + 0.25 * c.y,
          size: 11 * k,
          alpha: isSel ? 1 : cls === 'cross' ? 0.55 : 0.85,
          selected: isSel
        })
      }

      if (back) {
        back.lineCap = 'round'
        for (const b of buckets.values()) {
          back.globalAlpha = b.op
          back.strokeStyle = b.color
          back.lineWidth = b.wd
          back.stroke(b.path)
        }
        back.globalAlpha = 1
      }

      if (front && selEdge) {
        const path = new Path2D()
        path.moveTo(selEdge.x1, selEdge.y1)
        path.quadraticCurveTo(selEdge.cx, selEdge.cy, selEdge.x2, selEdge.y2)
        front.lineCap = 'round'
        front.strokeStyle = selEdge.color
        front.globalAlpha = 0.34
        front.lineWidth = selEdge.width * 4.4
        front.stroke(path)
        front.globalAlpha = 1
        front.lineWidth = selEdge.width + 2
        front.stroke(path)
      }

      if (front && labels.length) {
        const halo = haloRef.current
        front.textAlign = 'center'
        front.textBaseline = 'alphabetic'
        front.lineJoin = 'round'
        for (const lb of labels) {
          front.font = `600 ${lb.size.toFixed(1)}px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`
          front.globalAlpha = lb.selected ? 1 : 0.9
          front.lineWidth = 3
          front.strokeStyle = halo
          front.strokeText(lb.text, lb.x, lb.y - 6)
          front.fillStyle = lb.color
          front.globalAlpha = lb.alpha
          front.fillText(lb.text, lb.x, lb.y - 6)
        }
        front.globalAlpha = 1
      }

      hitRef.current = screenEdges
    }

    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [scene, camRef, posRef])

  // 点原子/点边 = 相机飞过去 + 把原子往镜头前拉。拉完就留在那儿，取消选中不回弹。
  useEffect(() => {
    const req = focusReq
    if (!req || !scene) return
    if (req.seq === lastReqRef.current) return
    const { w, h } = sizeRef.current
    if (!w || !h) return
    lastReqRef.current = req.seq
    const cam = camRef.current
    const b = cameraBasis(cam)
    const focal = h / 2 / Math.tan(FOV / 2)
    const usableW = Math.max(320, w - 404)
    // 检查器占右边 404px：把投影中心左移，焦点才落在「真正可用」的画面中间。
    const panX = usableW / 2 - w / 2
    const D = FOCUS_DIST
    const startAnim = (atom, to) =>
      atomAnims.current.set(atom.id, {
        from: { x: atom.x, y: atom.y, z: atom.z },
        to,
        t0: performance.now(),
        ms: 480
      })

    if (req.mode === 'atom') {
      const a = scene.byId.get(req.id)
      if (!a) return
      const to = { x: a.x + b.fx * PULL_IN, y: a.y + b.fy * PULL_IN, z: a.z + b.fz * PULL_IN }
      startAnim(a, to)
      animateCamera(cam, { fx: to.x, fy: to.y, fz: to.z, dist: D, panX, panY: 0 }, 820)
    } else {
      const a = scene.byId.get(req.src)
      const c = scene.byId.get(req.dst)
      if (!a || !c) return
      const mid = { x: (a.x + c.x) / 2, y: (a.y + c.y) / 2, z: (a.z + c.z) / 2 }
      const focus = { x: mid.x + b.fx * PULL_IN, y: mid.y + b.fy * PULL_IN, z: mid.z + b.fz * PULL_IN }
      const half = ((usableW * 0.25) * D) / focal
      startAnim(a, { x: focus.x - b.rx * half, y: focus.y - b.ry * half, z: focus.z - b.rz * half })
      startAnim(c, { x: focus.x + b.rx * half, y: focus.y + b.ry * half, z: focus.z + b.rz * half })
      animateCamera(cam, { fx: focus.x, fy: focus.y, fz: focus.z, dist: D, panX, panY: 0 }, 820)
    }
    cam.dirty = true
  }, [focusReq, scene, camRef])

  const onPointerDown = useCallback(
    e => {
      const pan = e.button === 1
      if (e.button !== 0 && !pan) return
      e.preventDefault()
      const cam = camRef.current
      cam.anim = null
      movedRef.current = 0
      const card = e.button === 0 && e.target && e.target.closest ? e.target.closest('.em-atom') : null
      if (card && card.dataset.id) {
        const id = Number(card.dataset.id)
        const atom = sceneRef.current.byId.get(id)
        if (atom) {
          const b = cameraBasis(cam)
          const h = sizeRef.current.h || 1
          const focal = h / 2 / Math.tan(FOV / 2)
          const zc = Math.max(
            0.15,
            (atom.x - b.ex) * b.fx + (atom.y - b.ey) * b.fy + (atom.z - b.ez) * b.fz
          )
          // 直接拖着走：把鼠标位移换算成该深度上的世界位移（屏幕平面内）。
          dragRef.current = {
            mode: 'atom',
            id,
            pointerId: e.pointerId,
            x: e.clientX,
            y: e.clientY,
            base: { x: atom.x, y: atom.y, z: atom.z },
            b,
            scale: zc / focal,
            captured: false
          }
          return
        }
      }
      dragRef.current = {
        mode: pan ? 'pan' : 'rotate',
        x: e.clientX,
        y: e.clientY,
        yaw: cam.yaw,
        pitch: cam.pitch,
        panX: cam.panX || 0,
        panY: cam.panY || 0
      }
      // Capture retargets the following `click` at the stage, which would swallow every card
      // click. Background drags capture; a drag that starts on a card keeps the card's click.
      if (!pan) {
        try {
          e.currentTarget.setPointerCapture(e.pointerId)
        } catch {
          /* ignore */
        }
      }
    },
    [camRef]
  )

  const onPointerMove = useCallback(
    e => {
      const drag = dragRef.current
      if (!drag) return
      const dx = e.clientX - drag.x
      const dy = e.clientY - drag.y
      movedRef.current = Math.max(movedRef.current, Math.hypot(dx, dy))
      const cam = camRef.current
      if (drag.mode === 'atom') {
        if (!drag.captured && movedRef.current > 4) {
          drag.captured = true
          try {
            e.currentTarget.setPointerCapture(drag.pointerId)
          } catch {
            /* ignore */
          }
        }
        const atom = sceneRef.current.byId.get(drag.id)
        if (atom) {
          atom.x = drag.base.x + (drag.b.rx * dx - drag.b.ux * dy) * drag.scale
          atom.y = drag.base.y + (drag.b.ry * dx - drag.b.uy * dy) * drag.scale
          atom.z = drag.base.z + (drag.b.rz * dx - drag.b.uz * dy) * drag.scale
          posRef.current.set(drag.id, { x: atom.x, y: atom.y, z: atom.z })
          cam.dirty = true
        }
      } else if (drag.mode === 'pan') {
        cam.panX = drag.panX + dx
        cam.panY = drag.panY + dy
        cam.dirty = true
      } else {
        cam.yaw = drag.yaw - dx * 0.006
        cam.pitch = clamp(drag.pitch + dy * 0.006, -1.36, 1.36)
        cam.dirty = true
      }
    },
    [camRef, posRef]
  )

  const onPointerUp = useCallback(e => {
    const drag = dragRef.current
    dragRef.current = null
    if (drag && drag.mode === 'atom' && movedRef.current > 4) handlersRef.current.onMoved([drag.id])
    try {
      e.currentTarget.releasePointerCapture(e.pointerId)
    } catch {
      /* ignore */
    }
  }, [])

  const onWheel = useCallback(
    e => {
      e.preventDefault()
      const cam = camRef.current
      cam.anim = null
      cam.dist = clamp(cam.dist * Math.exp(e.deltaY * 0.0011), cam.min, cam.max)
      cam.dirty = true
    },
    [camRef]
  )

  useEffect(() => {
    const el = stageRef.current
    if (!el) return undefined
    const handler = e => onWheel(e)
    el.addEventListener('wheel', handler, { passive: false })
    return () => el.removeEventListener('wheel', handler)
  }, [onWheel])

  const atomsTree = useMemo(
    () =>
      scene.atoms.map(a =>
        jsxs(
          'div',
          {
            ref: el => setAtomEl(a.id, el),
            className: 'em-atom',
            'data-id': a.id,
            'data-kind': a.kind,
            'data-state': 'in',
            'data-selected': '0',
            'data-compact': '0',
            'data-focus': '0',
            'data-edited': a.edited_ts ? '1' : '0',
            title: `${a.text}\n\n${fmtFull(a.ts)} · ${a.project || '未归类'}`,
            onClick: e => {
              e.stopPropagation()
              if (movedRef.current > 5) return
              if (e.currentTarget.dataset.clickable === '0') {
                handlersRef.current.onBackground()
                return
              }
              handlersRef.current.onSelectAtom(a.id)
            },
            children: [
              jsxs('div', {
                className: 'em-atom-head',
                children: [
                  jsx('span', { className: 'em-atom-id', children: `#${a.id}` }),
                  jsx('span', {
                    className: 'em-atom-kind',
                    style: { color: KIND_COLORS[a.kind] || 'var(--ui-text-secondary)' },
                    children: a.kind
                  }),
                  jsx('span', { className: 'em-atom-time', children: fmtTime(a.ts) })
                ]
              }),
              jsx('div', { className: 'em-atom-text', children: a.text }),
              a.edited_ts
                ? jsx('span', { className: 'em-atom-edited', title: '人工改过', children: '✎' })
                : null
            ]
          },
          a.id
        )
      ),
    [scene, setAtomEl]
  )

  return jsxs('div', {
    ref: stageRef,
    className: 'em-stage',
    onPointerDown,
    onPointerMove,
    onPointerUp,
    onPointerCancel: onPointerUp,
    onMouseDown: e => {
      if (e.button === 1) e.preventDefault()
    },
    onAuxClick: e => e.preventDefault(),
    onClick: e => {
      if (movedRef.current > 5) return
      if (e.target !== e.currentTarget) return
      const rect = stageRef.current.getBoundingClientRect()
      const x = e.clientX - rect.left
      const y = e.clientY - rect.top
      let best = null
      let bestDist = 10
      for (const seg of hitRef.current) {
        let px = seg.x1
        let py = seg.y1
        for (let i = 1; i <= 12; i += 1) {
          const t = i / 12
          const mt = 1 - t
          const qx = mt * mt * seg.x1 + 2 * mt * t * seg.cx + t * t * seg.x2
          const qy = mt * mt * seg.y1 + 2 * mt * t * seg.cy + t * t * seg.y2
          const dx = qx - px
          const dy = qy - py
          const len2 = dx * dx + dy * dy || 1
          const tt = clamp(((x - px) * dx + (y - py) * dy) / len2, 0, 1)
          const d = Math.hypot(x - (px + dx * tt), y - (py + dy * tt))
          if (d < bestDist) {
            bestDist = d
            best = seg
          }
          px = qx
          py = qy
        }
      }
      if (best) handlersRef.current.onSelectEdge(best.edge)
      else handlersRef.current.onBackground()
    },
    children: [
      jsx('div', { className: 'em-atom-layer', children: atomsTree }),
      jsx('canvas', { ref: backRef, className: 'em-edges em-edge-back' }),
      jsx('canvas', { ref: frontRef, className: 'em-edges em-edges-front' })
    ]
  })
}

// --------------------------------------------------------------------------- inspector bits

function ProbBlock({ title, probs, chosen }) {
  if (!probs || !Object.keys(probs).length) {
    return jsx('div', { className: 'em-prob-empty', children: '（这一问没有留档）' })
  }
  const entries = Object.entries(probs).sort((a, b) => b[1] - a[1])
  const max = Math.max(...entries.map(e => e[1]), 0.0001)
  return jsxs('div', {
    className: 'em-prob',
    children: [
      jsx('div', { className: 'em-prob-title', children: title }),
      ...entries.map(([label, p]) =>
        jsxs(
          'div',
          {
            className: 'em-prob-row',
            'data-hi': label === chosen ? '1' : '0',
            children: [
              jsx('span', { className: 'em-prob-label', children: label }),
              jsx('span', {
                className: 'em-prob-track',
                children: jsx('span', {
                  className: 'em-prob-fill',
                  style: {
                    width: `${Math.max(1, Math.round((p / max) * 100))}%`,
                    background: REL_COLORS[label] || (label === '都不是' ? NONE_COLOR : 'var(--ui-text-quaternary)')
                  }
                })
              }),
              jsx('span', { className: 'em-prob-num', children: p.toFixed(3) })
            ]
          },
          label
        )
      )
    ]
  })
}

function EndpointCard({ atom, onSelect, role }) {
  if (!atom) return null
  return jsx('button', {
    type: 'button',
    className: 'em-endpoint',
    onClick: () => onSelect(atom.id),
    children: jsxs('span', {
      children: [
        jsxs('span', {
          className: 'em-endpoint-head',
          children: [
            jsx('span', { className: 'em-endpoint-id', children: `#${atom.id}` }),
            jsx('span', {
              className: 'em-endpoint-kind',
              style: { color: KIND_COLORS[atom.kind] || 'var(--ui-text-secondary)' },
              children: atom.kind
            }),
            jsx('span', { className: 'em-endpoint-role', children: role })
          ]
        }),
        jsx('span', { className: 'em-endpoint-text', children: atom.text })
      ]
    })
  })
}

function RelationPicker({ value, onChange, allowEmpty }) {
  return jsx('select', {
    className: 'em-select em-rel-select',
    value: value || '',
    onChange: e => onChange(e.target.value),
    children: [
      allowEmpty ? jsx('option', { value: '', children: '— 选一个关系 —' }, '') : null,
      ...RELS.map(rel => jsx('option', { value: rel, children: rel }, rel))
    ]
  })
}

function AtomInspector({ atom, scene, ctx, onSaved, onSelectEdge, onClose }) {
  const [text, setText] = useState(atom.text)
  const [kind, setKind] = useState(atom.kind)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  useEffect(() => {
    setText(atom.text)
    setKind(atom.kind)
    setErr('')
  }, [atom.id, atom.text, atom.kind])

  const dirty = text.trim() !== atom.text || kind !== atom.kind
  const mine = scene.edges.filter(e => e.src === atom.id || e.dst === atom.id)

  const save = async () => {
    setBusy(true)
    setErr('')
    try {
      await ctx.rest(`/events/${atom.id}`, { method: 'PATCH', body: { text, kind } })
      await onSaved()
    } catch (e) {
      setErr(String(e?.message || e))
    } finally {
      setBusy(false)
    }
  }

  return jsxs('div', {
    className: 'em-insp',
    children: [
      jsxs('div', {
        className: 'em-insp-head',
        children: [
          jsx('span', { className: 'em-insp-title', children: `#${atom.id}` }),
          jsx('span', { className: 'em-insp-sub', children: atom.kind }),
          jsx('button', { type: 'button', className: 'em-x', onClick: onClose, title: '关闭', children: '✕' })
        ]
      }),
      jsxs('div', {
        className: 'em-meta',
        children: [
          jsx('div', { children: fmtFull(atom.ts) }),
          jsx('div', { children: `项目：${atom.project || '未归类'}` }),
          jsx('div', { children: `会话：${shortSession(atom.session)}` }),
          atom.edited_ts
            ? jsx('div', {
                className: 'em-edited-note',
                children: `✎ 人工修改于 ${fmtTime(atom.edited_ts)}（彩虹描边）`
              })
            : null
        ]
      }),
      jsxs('label', {
        className: 'em-field',
        children: [
          jsx('span', { className: 'em-label', children: '类型' }),
          jsx('select', {
            className: 'em-select',
            value: kind,
            onChange: e => setKind(e.target.value),
            children: KINDS.map(k => jsx('option', { value: k, children: k }, k))
          })
        ]
      }),
      jsxs('label', {
        className: 'em-field',
        children: [
          jsx('span', { className: 'em-label', children: '正文' }),
          jsx('textarea', {
            className: 'em-textarea',
            rows: 5,
            value: text,
            onChange: e => setText(e.target.value)
          })
        ]
      }),
      err ? jsx('div', { className: 'em-error', children: err }) : null,
      jsxs('div', {
        className: 'em-actions',
        children: [
          jsx(Button, {
            size: 'sm',
            variant: 'default',
            disabled: busy || !dirty || !text.trim(),
            onClick: save,
            children: busy ? '保存中…' : '保存'
          }),
          dirty
            ? jsx('span', { className: 'em-hint', children: '保存后这条原子会蒙上彩虹描边' })
            : jsx('span', { className: 'em-hint', children: '未改动' })
        ]
      }),
      jsxs('div', {
        className: 'em-section',
        children: [
          jsx('div', { className: 'em-section-title', children: `连着的边（${mine.length}）` }),
          mine.length === 0
            ? jsx('div', { className: 'em-hint', children: '没有边' })
            : jsx('div', {
                className: 'em-edge-list',
                children: mine.map(e => {
                  const other = e.src === atom.id ? e.dst : e.src
                  const dir = e.src === atom.id ? '←' : '→'
                  return jsx(
                    'button',
                    {
                      type: 'button',
                      className: 'em-edge-row',
                      onClick: () => onSelectEdge(e),
                      children: jsxs('span', {
                        children: [
                          jsx('span', {
                            className: 'em-edge-dot',
                            style: { background: e.kind === 'none' ? NONE_COLOR : REL_COLORS[e.rel] || NONE_COLOR }
                          }),
                          jsx('span', { className: 'em-edge-row-rel', children: e.kind === 'none' ? '无' : e.rel }),
                          jsx('span', { className: 'em-edge-row-id', children: `${dir} #${other}` }),
                          e.edited ? jsx('span', { className: 'em-edge-row-flag', children: '✎' }) : null
                        ]
                      })
                    },
                    e.key
                  )
                })
              })
        ]
      })
    ]
  })
}

function EdgeInspector({ edge, scene, data, ctx, onSaved, onSelectAtom, onClose }) {
  const [rel, setRel] = useState(edge.rel || '')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  useEffect(() => {
    setRel(edge.rel || '')
    setErr('')
  }, [edge.key, edge.rel])

  const src = scene.byId.get(edge.src)
  const dst = scene.byId.get(edge.dst)
  const pair = useMemo(() => {
    if (edge.pair) return edge.pair
    return (data.pairs || []).find(p => p.src === edge.src && p.dst === edge.dst) || null
  }, [data, edge])

  const ownAxis = edge.probs && Object.keys(edge.probs).length >= 4 ? edge.probs : null
  const ownDetail = edge.probs && Object.keys(edge.probs).length === 3 ? edge.probs : null
  const axisProbs = pair?.axis_probs || ownAxis
  const detailProbs = pair?.detail_probs || ownDetail

  const save = async () => {
    if (!rel) return
    setBusy(true)
    setErr('')
    try {
      await ctx.rest('/edges', {
        method: 'PATCH',
        body: { src: edge.src, dst: edge.dst, rel, old_rel: edge.kind === 'rel' ? edge.rel : null, conf: edge.conf }
      })
      await onSaved()
    } catch (e) {
      setErr(String(e?.message || e))
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    if (edge.kind !== 'rel') return
    setBusy(true)
    setErr('')
    try {
      await ctx.rest(
        `/edges?src=${edge.src}&dst=${edge.dst}&rel=${encodeURIComponent(edge.rel)}`,
        { method: 'DELETE' }
      )
      await onSaved()
    } catch (e) {
      setErr(String(e?.message || e))
    } finally {
      setBusy(false)
    }
  }

  return jsxs('div', {
    className: 'em-insp',
    children: [
      jsxs('div', {
        className: 'em-insp-head',
        children: [
          jsx('span', {
            className: 'em-insp-title',
            style: { color: edge.kind === 'none' ? 'var(--ui-text-secondary)' : REL_COLORS[edge.rel] },
            children: edge.kind === 'none' ? '判定：无关系' : edge.rel
          }),
          jsx('span', { className: 'em-insp-sub', children: `${edge.src} → ${edge.dst}` }),
          jsx('button', { type: 'button', className: 'em-x', onClick: onClose, title: '关闭', children: '✕' })
        ]
      }),
      jsxs('div', {
        className: 'em-meta',
        children: [
          jsx('div', { children: `#${edge.src}（后） → #${edge.dst}（前）` }),
          edge.kind === 'rel'
            ? jsx('div', {
                children: `置信 ${edge.conf.toFixed(3)} · ${edge.decidedBy === 'human' ? '人工' : edge.decidedBy === 'jev' ? '模型判定' : edge.decidedBy}`
              })
            : jsx('div', { children: '这一对问过模型，但没有过 margin —— 没有连边' }),
          edge.edited ? jsx('div', { className: 'em-edited-note', children: '✎ 人工改过（彩虹描边）' }) : null
        ]
      }),
      jsxs('div', {
        className: 'em-endpoints',
        children: [
          jsx(EndpointCard, { atom: src, role: '后', onSelect: onSelectAtom }),
          jsx(EndpointCard, { atom: dst, role: '前', onSelect: onSelectAtom })
        ]
      }),
      jsx(ProbBlock, { title: '问题一 · 后一条相对前一条', probs: axisProbs, chosen: pair?.axis_label || edge.rel }),
      jsx(ProbBlock, {
        title: '问题二 · 补充关系',
        probs: detailProbs,
        chosen: pair?.detail_label || (detailProbs === edge.probs ? edge.rel : null)
      }),
      jsxs('div', {
        className: 'em-field',
        children: [
          jsx('span', { className: 'em-label', children: edge.kind === 'rel' ? '改这条边的关系' : '人工连一条边' }),
          jsx(RelationPicker, { value: rel, onChange: setRel, allowEmpty: edge.kind !== 'rel' })
        ]
      }),
      err ? jsx('div', { className: 'em-error', children: err }) : null,
      jsxs('div', {
        className: 'em-actions',
        children: [
          jsx(Button, {
            size: 'sm',
            variant: 'default',
            disabled: busy || !rel || rel === edge.rel,
            onClick: save,
            children: busy ? '保存中…' : edge.kind === 'rel' ? '保存关系' : '连上'
          }),
          edge.kind === 'rel'
            ? jsx(Button, { size: 'sm', variant: 'ghost', disabled: busy, onClick: remove, children: '断开' })
            : null,
          jsx('span', { className: 'em-hint', children: '人工改过的边会蒙上彩虹光' })
        ]
      })
    ]
  })
}

// --------------------------------------------------------------------------- page

function GraphPage({ ctx }) {
  const qc = useQueryClient()
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ['eventmem-graph', 'graph'],
    queryFn: () => ctx.rest('/graph'),
    staleTime: 8000,
    refetchOnWindowFocus: false
  })

  const [dim, setDim] = useState('all')
  const [value, setValue] = useState('')
  const [mode, setMode] = useState('only')
  const [showRejected, setShowRejected] = useState(true)
  const [showLabels, setShowLabels] = useState(false)
  const [pulled, setPulled] = useState(() => new Set())
  const [sel, setSel] = useState(null)
  const [focusReq, setFocusReq] = useState(null)
  const [layoutEpoch, setLayoutEpoch] = useState(0)
  const seqRef = useRef(0)
  // 用户拖过/拉过的位置：本地权威，落库前也不回弹。
  const posRef = useRef(new Map())
  const seededRef = useRef(false)
  const pendingRef = useRef(new Set())
  const saveTimer = useRef(0)
  const camRef = useRef({
    yaw: 0.55,
    pitch: 0.26,
    dist: START_DIST,
    fx: 0,
    fy: 0,
    fz: 0,
    panX: 0,
    panY: 0,
    fit: 40,
    min: 6,
    max: 300,
    anim: null,
    dirty: true,
    ready: false
  })

  const rawScene = useMemo(() => {
    if (!data) return null
    if (!seededRef.current) {
      seededRef.current = true
      for (const p of data.positions || []) posRef.current.set(p.id, { x: p.x, y: p.y, z: p.z })
    }
    return buildScene(data, posRef.current)
  }, [data, layoutEpoch])
  const scene = useMemo(() => {
    if (!rawScene) return null
    if (showRejected) return rawScene
    return { ...rawScene, edges: rawScene.edges.filter(e => e.kind !== 'none') }
  }, [rawScene, showRejected])

  const options = useMemo(() => {
    if (!data) return []
    if (dim === 'project') {
      const counts = new Map()
      for (const ev of data.events) {
        const name = ev.project || '未归类'
        counts.set(name, (counts.get(name) || 0) + 1)
      }
      return [...counts.entries()]
        .sort((a, b) => a[0].localeCompare(b[0], 'zh'))
        .map(([name, n]) => ({ value: name, label: `${name} · ${n} 条` }))
    }
    if (dim === 'session') {
      const byId = new Map()
      for (const ev of data.events) {
        if (!ev.session) continue
        const row = byId.get(ev.session) || { id: ev.session, count: 0, last: 0 }
        row.count += 1
        row.last = Math.max(row.last, ev.ts)
        byId.set(ev.session, row)
      }
      return [...byId.values()]
        .sort((a, b) => b.last - a.last)
        .map(s => ({ value: s.id, label: `${shortSession(s.id)} · ${s.count} 条` }))
    }
    if (dim === 'date') {
      const counts = new Map()
      for (const ev of data.events) {
        const k = dayKey(ev.ts)
        counts.set(k, (counts.get(k) || 0) + 1)
      }
      return [...counts.entries()]
        .sort((a, b) => (a[0] < b[0] ? 1 : -1))
        .map(([k, n]) => ({ value: k, label: `${k} · ${n} 条` }))
    }
    return []
  }, [data, dim])

  useEffect(() => {
    if (dim === 'all') {
      setValue('')
      return
    }
    if (!options.some(o => o.value === value)) setValue(options[0]?.value ?? '')
  }, [dim, options, value])

  useEffect(() => {
    setPulled(new Set())
  }, [dim, value, mode])

  const focusInfo = useMemo(() => {
    const events = data?.events || []
    const edges = scene?.edges || []
    const active = dim !== 'all' && Boolean(value)
    if (!active) return { active: false, match: new Set(), focus: new Set(), promo: new Set(), count: 0 }
    const match = new Set()
    for (const ev of events) {
      const key = dim === 'project' ? ev.project || '未归类' : dim === 'session' ? ev.session : dayKey(ev.ts)
      if (key === value) match.add(ev.id)
    }
    const focus = new Set(match)
    if (mode === 'neighbors') {
      for (const e of edges) {
        if (match.has(e.src) || match.has(e.dst)) {
          focus.add(e.src)
          focus.add(e.dst)
        }
      }
    }
    for (const id of pulled) focus.add(id)
    const promo = new Set([...focus].filter(id => !match.has(id)))
    return { active, match, focus, promo, count: match.size }
  }, [data, scene, dim, value, mode, pulled])

  const sceneRef = useRef(scene)
  sceneRef.current = scene

  const selectAtom = useCallback(
    (id, force) => {
      // 已经有焦点时，点别的地方只负责退出；再点一次才选中新的。
      if (sel && !force) {
        setSel(null)
        haptic('tap')
        return
      }
      setSel({ type: 'atom', id })
      haptic('tap')
      seqRef.current += 1
      setFocusReq({ seq: seqRef.current, mode: 'atom', id })
    },
    [sel]
  )

  const selectEdge = useCallback(
    (edge, force) => {
      if (sel && !force) {
        setSel(null)
        haptic('tap')
        return
      }
      // 筛选时把这根边的两头都算进焦点：拉过之后它们才点得动。
      if (focusInfo.active) {
        setPulled(prev => {
          const next = new Set(prev)
          next.add(edge.src)
          next.add(edge.dst)
          return next
        })
      }
      setSel({ type: 'edge', key: edge.key })
      haptic('tap')
      seqRef.current += 1
      setFocusReq({ seq: seqRef.current, mode: 'edge', src: edge.src, dst: edge.dst })
    },
    [sel, focusInfo]
  )

  const resetView = useCallback(() => {
    const cam = camRef.current
    const seed = rawScene?.newestId != null ? rawScene.byId.get(rawScene.newestId) : null
    animateCamera(cam, {
      yaw: 0.55,
      pitch: 0.26,
      dist: START_DIST,
      fx: seed ? seed.x : 0,
      fy: seed ? seed.y : 0,
      fz: seed ? seed.z : 0,
      panX: 0,
      panY: 0
    })
  }, [rawScene])

  const onSaved = useCallback(async () => {
    await qc.invalidateQueries({ queryKey: ['eventmem-graph', 'graph'] })
  }, [qc])

  const flushPositions = useCallback(() => {
    const ids = [...pendingRef.current]
    pendingRef.current.clear()
    if (!ids.length) return
    const positions = ids
      .map(id => {
        const p = posRef.current.get(id)
        return p ? { id, x: p.x, y: p.y, z: p.z } : null
      })
      .filter(Boolean)
    if (!positions.length) return
    ctx.rest('/positions', { method: 'PUT', body: { positions } }).catch(() => {})
  }, [ctx])

  // 位置落库是防抖的：连续拖动/连点只在停下来 450ms 后写一次。
  const onMoved = useCallback(
    ids => {
      for (const id of ids) pendingRef.current.add(id)
      if (saveTimer.current) clearTimeout(saveTimer.current)
      saveTimer.current = setTimeout(flushPositions, 450)
    },
    [flushPositions]
  )

  const resetLayout = useCallback(async () => {
    try {
      await ctx.rest('/positions', { method: 'DELETE' })
    } catch {
      /* 后端不在也照样回到默认网格 */
    }
    posRef.current = new Map()
    pendingRef.current.clear()
    setSel(null)
    setLayoutEpoch(e => e + 1)
    resetView()
  }, [ctx, resetView])

  const selectedAtom = sel?.type === 'atom' ? scene?.byId.get(sel.id) : null
  const selectedEdge =
    sel?.type === 'edge' ? scene?.edges.find(e => e.key === sel.key) || rawScene?.edges.find(e => e.key === sel.key) : null

  if (isLoading) {
    return jsx('div', {
      className: 'em-root em-center',
      children: jsx(GlyphSpinner, { ariaLabel: '载入记忆图谱' })
    })
  }
  if (error) {
    return jsxs('div', {
      className: 'em-root em-center',
      children: [
        jsx('div', { className: 'em-error-title', children: '读不到 eventmem 图谱' }),
        jsx('div', { className: 'em-error-body', children: String(error?.message || error) }),
        jsx('div', {
          className: 'em-hint',
          children: '后端未挂载时先确认插件 eventmem-graph 已启用（hermes plugins enable eventmem-graph）。'
        }),
        jsx(Button, { size: 'sm', onClick: () => refetch(), children: '重试' })
      ]
    })
  }
  if (!data || !scene || scene.atoms.length === 0) {
    return jsx('div', {
      className: 'em-root em-center',
      children: jsx(EmptyState, { title: '还没有记忆原子', description: 'eventmem 里写过事件之后这里才有东西。' })
    })
  }

  const counts = {
    atoms: scene.atoms.length,
    edges: scene.edges.filter(e => e.kind === 'rel').length,
    rejected: scene.edges.filter(e => e.kind === 'none').length
  }

  return jsxs('div', {
    className: 'em-root',
    children: [
      jsx(Scene, {
        scene,
        focusInfo,
        sel,
        focusReq,
        showLabels,
        camRef,
        posRef,
        onMoved,
        onSelectAtom: selectAtom,
        onSelectEdge: selectEdge,
        onBackground: () => setSel(null)
      }),

      jsxs('div', {
        className: 'em-bar',
        children: [
          jsx('span', { className: 'em-brand', children: '记忆图谱' }),
          jsxs('div', {
            className: 'em-seg',
            children: [
              ['all', '全部'],
              ['project', '项目'],
              ['session', '会话'],
              ['date', '日期']
            ].map(([k, label]) =>
              jsx(
                'button',
                {
                  type: 'button',
                  className: 'em-seg-btn',
                  'data-on': dim === k ? '1' : '0',
                  onClick: () => {
                    setDim(k)
                    setSel(null)
                  },
                  children: label
                },
                k
              )
            )
          }),
          dim !== 'all'
            ? jsx('select', {
                className: 'em-select em-value',
                value,
                onChange: e => {
                  setValue(e.target.value)
                  setSel(null)
                },
                children: options.map(o => jsx('option', { value: o.value, children: o.label }, o.value))
              })
            : null,
          dim !== 'all'
            ? jsxs('div', {
                className: 'em-seg',
                title: '焦点范围',
                children: [
                  ['only', '仅命中'],
                  ['neighbors', '含一跳邻居']
                ].map(([k, label]) =>
                  jsx(
                    'button',
                    {
                      type: 'button',
                      className: 'em-seg-btn',
                      'data-on': mode === k ? '1' : '0',
                      onClick: () => setMode(k),
                      children: label
                    },
                    k
                  )
                )
              })
            : null,
          jsx('label', {
            className: 'em-check',
            children: [
              jsx('input', {
                type: 'checkbox',
                checked: showRejected,
                onChange: e => setShowRejected(e.target.checked)
              }),
              '灰边（判定为无）'
            ]
          }),
          jsx('label', {
            className: 'em-check',
            children: [
              jsx('input', {
                type: 'checkbox',
                checked: showLabels,
                onChange: e => setShowLabels(e.target.checked)
              }),
              '关系名'
            ]
          }),
          jsxs('span', {
            className: 'em-counts',
            children: [
              `${counts.atoms} 原子 · ${counts.edges} 边`,
              counts.rejected ? ` · ${counts.rejected} 灰边` : '',
              focusInfo.active ? ` · 命中 ${focusInfo.count}` : ''
            ]
          }),
          jsx('span', { className: 'em-spacer' }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: resetView, children: '重置视角' }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: resetLayout, children: '复原布局' }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => refetch(), children: '刷新' })
        ]
      }),

      jsxs('div', {
        className: 'em-legend',
        children: [
          ...KINDS.map(k =>
            jsxs(
              'span',
              { className: 'em-legend-item', children: [
                jsx('span', { className: 'em-legend-dot', style: { background: KIND_COLORS[k] } }),
                k
              ] },
              k
            )
          ),
          jsx('span', { className: 'em-legend-sep' }, 'sep'),
          ...RELS.map(r =>
            jsxs(
              'span',
              { className: 'em-legend-item', children: [
                jsx('span', { className: 'em-legend-line', style: { background: REL_COLORS[r] } }),
                r
              ] },
              r
            )
          ),
          jsx(
            'span',
            {
              className: 'em-legend-item',
              children: [
                jsx('span', { className: 'em-legend-line em-legend-dash', style: { borderColor: NONE_COLOR } }),
                '无'
              ]
            },
            'none'
          )
        ]
      }),

      selectedAtom
        ? jsx(AtomInspector, {
            atom: selectedAtom,
            scene,
            ctx,
            onSaved,
            onSelectEdge: e => selectEdge(e, true),
            onClose: () => setSel(null)
          })
        : null,
      selectedEdge
        ? jsx(EdgeInspector, {
            edge: selectedEdge,
            scene,
            data,
            ctx,
            onSaved,
            onSelectAtom: id => selectAtom(id, true),
            onClose: () => setSel(null)
          })
        : null
    ]
  })
}

// --------------------------------------------------------------------------- register

export default {
  id: ID,
  name: 'eventmem graph',
  description: 'eventmem 的三维记忆图谱（作为「记忆后台 → 图谱」页渲染）：原子按类型着色、关系边着色，可筛选、聚焦与人工校正。',
  register(ctx) {
    // Headless view provider: no route, no status-bar chip — the graph is a tab inside
    // `eventmem-gui` now. This plugin only hands its page over and keeps the data path
    // (/api/plugins/eventmem-graph/*) alive.
    //
    // Runtime plugins are evaluated from blob: URLs, so a relative import is impossible;
    // a global is the only way to hand a component over.
    globalThis.__eventmemGraph = { GraphPage, ctx, version: 1 }
    ctx.onDispose(() => {
      if (globalThis.__eventmemGraph && globalThis.__eventmemGraph.ctx === ctx) {
        delete globalThis.__eventmemGraph
      }
    })

    const style = document.createElement('style')
    style.dataset.plugin = ID
    style.textContent = CSS
    document.head.append(style)
    ctx.onDispose(() => style.remove())
  }
}
