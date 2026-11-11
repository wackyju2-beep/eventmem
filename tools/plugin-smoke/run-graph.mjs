// Smoke-render the graph component on its own. It is headless now (no route, no chip), so
// nothing but this catches a broken import or a crash in its render path.
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'

globalThis.document = { head: { append: () => {} }, createElement: () => ({ dataset: {}, textContent: '', remove: () => {} }) }
globalThis.window = globalThis
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0)

const { GraphPage } = await import('./graph.js')
const ctx = { rest: () => Promise.resolve({}), register: () => {}, onDispose: () => {}, setInterval: () => () => {} }

const shapes = {
  empty: { events: [], edges: [], projects: [] },
  two: {
    events: [
      { id: 1, kind: '决定', text: '用 startlux', project: 'eventmem', ts: 1794419860, project_id: 1 },
      { id: 2, kind: '结果', text: '链路切好了', project: 'eventmem', ts: 1794420000, project_id: 1 }
    ],
    edges: [{ src: 2, dst: 1, relation: '细化', probs: [0.7, 0.2], backend: 'startlux' }],
    projects: [{ id: 1, name: 'eventmem' }]
  }
}

let bad = 0
for (const [name, data] of Object.entries(shapes)) {
  globalThis.__graphData = data
  try {
    const html = renderToStaticMarkup(React.createElement(GraphPage, { ctx }))
    const label = html.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim()
    console.log(`OK   ${name.padEnd(6)}${String(html.length).padStart(6)}B  ${label.slice(0, 90)}`)
  } catch (error) {
    bad += 1
    console.log(`FAIL ${name}: ${error && error.stack ? error.stack.split('\n').slice(0, 5).join(' | ') : error}`)
  }
}

console.log(bad === 0 ? 'GRAPH SMOKE PASS' : `GRAPH SMOKE FAIL (${bad})`)
process.exit(bad === 0 ? 0 : 1)
