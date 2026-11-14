// Real adapter/host integration: mount index.js with a fake Cordis context, drive the
// hooks, and let the adapter talk HTTP to a stub daemon. No DSH installation required.
// Run: node tests/test_dsh_host.mjs
import assert from 'node:assert/strict'
import { createServer } from 'node:http'

import { apply } from '../adapters/dsh/index.js'
import { OUR_SOURCE_KIND } from '../adapters/dsh/lib/text.js'

const KIND_RESULT = '\u7ed3\u679c' // the stored kind "result"
const PROJECT = 'demo'
const calls = []
const answers = {
  health: {
    status: 'ok', service: 'eventmem-daemon', api: 1, schema: 12,
    instance_id: 'b'.repeat(32), db_id: '0123456789abcdef', auth: 'none',
    capabilities: { embedding: false, decision: false }, pid: 7, db: 'D:/tmp/events.db',
    backends: [], paused: false,
  },
}

function json(res, status, body) {
  const raw = JSON.stringify(body)
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(raw) })
  res.end(raw)
}

const server = createServer(async (req, res) => {
  const url = new URL(req.url, 'http://127.0.0.1')
  let raw = ''
  for await (const chunk of req) raw += chunk
  const body = raw ? JSON.parse(raw) : null
  calls.push({ method: req.method, path: url.pathname, body, auth: req.headers.authorization || '' })
  if (url.pathname === '/api/health') return json(res, 200, answers.health)
  if (url.pathname === '/api/mem/recall') {
    return json(res, 200, {
      block: '[eventmem] use expand(#id)\n[#7] result old decision',
      hint: '', count: 1, stateless: false,
    })
  }
  if (url.pathname === '/api/mem/projects') {
    return json(res, 200, { projects: [{ id: 1, name: PROJECT, status: 'active', event_count: 3 }] })
  }
  if (url.pathname === '/api/mem/session/project') {
    return json(res, 200, { session: body.session, project: '' })
  }
  if (url.pathname === '/api/mem/events') return json(res, 200, { ok: true, id: 99, agent: 'dsh' })
  if (url.pathname === '/api/mem/events/7') {
    return json(res, 200, { id: 7, date: '2026-11-15', kind: KIND_RESULT, text: 'old decision', project: PROJECT })
  }
  return json(res, 404, { error: 'not found' })
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const baseUrl = 'http://127.0.0.1:' + server.address().port

const handlers = new Map()
const tools = new Map()
function on(name, fn) {
  const list = handlers.get(name) || []
  list.push(fn)
  handlers.set(name, list)
  return () => {
    const current = handlers.get(name) || []
    handlers.set(name, current.filter((item) => item !== fn))
  }
}
const sub = {
  effect: (fn) => { const dispose = fn(); return typeof dispose === 'function' ? dispose : () => {} },
  connection: { fetch: { register: () => () => {} } },
}
const agent = { session: { id: 's1', seq: 0 } }
const ctx = {
  logger: { warn() {}, debug() {}, info() {} },
  on,
  effect: (fn) => { const dispose = fn(); return typeof dispose === 'function' ? dispose : () => {} },
  get: (name) => name === 'agents' ? { list: () => [agent] } : undefined,
  inject: (names, fn) => { if (names.includes('connection')) fn(sub) },
  tools: { register: (tool) => { tools.set(tool.name, tool); return () => tools.delete(tool.name) } },
  fiber: { entry: { id: 'eventmem' } },
}

const dispose = apply(ctx, {
  mode: 'local', baseUrl, prefetch: true, extract: false, reminder: false,
  projectHint: true, timeoutMs: 2000, logFile: '',
})
await new Promise((resolve) => setTimeout(resolve, 120))

let failures = 0
function check(cond, message) {
  if (cond) console.log('PASS ' + message)
  else { failures += 1; console.error('FAIL ' + message) }
}

try {
  check(tools.size >= 7, `seven tools registered (got ${tools.size})`)

  for (const fn of handlers.get('agent/created') || []) fn({ agent, source: 'test' })

  const preStep = (handlers.get('agent/pre-step') || [])[0]
  assert.ok(preStep, 'pre-step handler is registered')
  const decision = await preStep({ agent, step: 1, signal: null }, async () => ({
    kind: 'enter',
    messages: [{ role: 'user', content: [{ type: 'text', text: 'how did we choose the disk last time?' }] }],
  }))
  const injected = decision.messages[decision.messages.length - 1]
  check(decision.messages.length === 2, 'recall injects one extra user-role message')
  check(injected.source && injected.source.kind === OUR_SOURCE_KIND, 'injected message is marked as adapter recall')
  check(injected.content[0].text.includes('old decision'), 'injected text is the daemon block')
  const recall = calls.find((call) => call.path === '/api/mem/recall')
  check(recall && recall.body.session === 'dsh:s1', 'recall carries dsh:<session>')
  check(recall && recall.body.reset === true, 'first recall of a process resets the dedupe state')

  const evtAdd = tools.get('evt_add')
  assert.ok(evtAdd, 'evt_add exists')
  const result = JSON.parse(await evtAdd.execute({
    events: [{ kind: KIND_RESULT, text: 'new conclusion', project: PROJECT }],
  }, { agent }))
  check(result.ok === true && result.events[0].id === 99, 'evt_add writes through the data plane')
  const write = calls.find((call) => call.path === '/api/mem/events' && call.method === 'POST')
  check(write && write.body.agent === 'dsh' && write.body.session === 'dsh:s1', 'write carries agent and session')
  check(write && write.body.project === PROJECT, 'write carries the project')

  const expand = tools.get('expand')
  const expanded = JSON.parse(await expand.execute({ id: 7 }, { agent }))
  check(expanded.id === 7 && expanded.text === 'old decision', 'expand reads one event')
} finally {
  dispose()
  server.close()
}

console.log(`\n${failures ? 'FAIL' : 'PASS'} host integration`)
process.exitCode = failures ? 1 : 0
