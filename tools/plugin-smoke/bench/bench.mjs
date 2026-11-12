import React from 'react'
import { createRoot } from 'react-dom/client'
globalThis.__errors = []
window.addEventListener('error', e => globalThis.__errors.push(String(e.message || e.error)))
window.addEventListener('unhandledrejection', e => globalThis.__errors.push('rejection: ' + String(e.reason)))
globalThis.__React = React

// Point this at the plugin under test; the repo copy is the default so the bench always
// measures what ships. Copy an older revision in as plugin-old.js to A/B it.
const which = new URLSearchParams(location.search).get('v') === 'old' ? 'plugin-old.js' : '../../../../adapters/hermes/desktop-plugins/eventmem-graph/plugin.js'

// host theme vars the plugin CSS reads
const vars = {
  '--ui-bg-primary': '#0b0c0f', '--ui-bg-secondary': '#171a20', '--ui-bg-card': '#14161b',
  '--ui-stroke-secondary': '#2a2d34', '--ui-text-primary': '#c9cdd3', '--ui-text-secondary': '#9aa0a8',
  '--ui-text-tertiary': '#7c828b', '--ui-text-quaternary': '#5c626b', '--ui-accent': '#4f8cff',
  '--ui-base': '#000000', '--ui-red': '#ff6f8e', '--ui-green': '#48c979', '--ui-orange': '#f2a93b'
}
for (const [k, v] of Object.entries(vars)) document.documentElement.style.setProperty(k, v)

globalThis.__graphData = await (await fetch('./graph.json')).json()

const plugin = (await import(`./${which}`)).default
const ctx = {
  rest: async () => globalThis.__graphData,
  register: () => {}, onDispose: () => {},
  setInterval: () => () => {}, clearInterval: () => {}
}
plugin.register(ctx)
const GP = globalThis.__eventmemGraph.GraphPage
createRoot(document.getElementById('root')).render(React.createElement(GP, { ctx }))

document.getElementById('hud').textContent =
  `${which}: ${globalThis.__graphData.events.length} atoms / ${globalThis.__graphData.edges.length} edges / ${globalThis.__graphData.pairs.length} pairs`

// ---- measurement ------------------------------------------------------------
globalThis.__bench = async (ms = 3000, step = 40) => {
  const stage = document.querySelector('.em-stage')
  if (!stage) return { error: 'no .em-stage' }
  const deltas = []
  let last = performance.now()
  let stop = false
  const tick = t => { deltas.push(t - last); last = t; if (!stop) requestAnimationFrame(tick) }
  requestAnimationFrame(tick)
  const timer = setInterval(() => {
    stage.dispatchEvent(new WheelEvent('wheel', {
      deltaY: -step, clientX: innerWidth / 2, clientY: innerHeight / 2, bubbles: true, cancelable: true
    }))
  }, 16)
  await new Promise(r => setTimeout(r, ms))
  clearInterval(timer)
  stop = true
  await new Promise(r => setTimeout(r, 150))
  deltas.shift()
  const sorted = [...deltas].sort((a, b) => a - b)
  const q = f => sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * f))]
  const mean = deltas.reduce((a, b) => a + b, 0) / deltas.length
  return {
    which, frames: deltas.length, fps: +(1000 / mean).toFixed(1),
    median: +q(0.5).toFixed(1), p95: +q(0.95).toFixed(1), max: +sorted[sorted.length - 1].toFixed(1),
    svgEdgeGroups: document.querySelectorAll('.em-edge').length,
    canvases: document.querySelectorAll('canvas.em-edges').length,
    atoms: document.querySelectorAll('.em-atom').length
  }
}
