/**
 * Allow-list check for adapters/dsh/lib/console.js — the console's Host-side proxy.
 *
 * The route table is the only door from a browser session to the daemon, so it is worth
 * asserting directly: the console's own routes must pass, everything else must 404, and
 * traversal/encoding tricks must not slip through.
 */
const { registerConsole } = await import(new URL('../../adapters/dsh/lib/console.js', import.meta.url))

let handler = null
const ctx = {
  inject(_names, fn) { fn({ effect: (f) => f(), connection: { fetch: { register(entry) { handler = entry.fetch; return () => {} } } } }) },
}
const calls = []
const net = {
  request: async (method, path, body) => { calls.push({ method, path, body }); return { ok: true } },
}
registerConsole(ctx, { baseUrl: 'http://127.0.0.1:9123', timeoutMs: 8000 }, net)

const failures = []
const check = (cond, message) => {
  if (!cond) failures.push(message)
  console.log(`${cond ? 'ok  ' : 'FAIL'} ${message}`)
}

async function call(method, route, body) {
  const url = 'http://127.0.0.1:12467/api/eventmem?path=' + encodeURIComponent(route)
  // The Host hands the plugin one Request-like object: url + method + json().
  const request = { url, method, json: async () => (body === undefined ? {} : body) }
  const response = await handler(request)
  return { status: response.status, body: await response.json() }
}

const allowed = [
  ['GET', 'health'], ['GET', 'status'], ['GET', 'stats'], ['GET', 'config'],
  ['GET', 'logs'], ['GET', 'packs'], ['GET', 'models'], ['GET', 'job'],
  ['GET', 'mem/graph'],
  ['POST', 'pause'], ['POST', 'stop'], ['POST', 'switch'], ['POST', 'restart'],
  ['POST', 'config'], ['POST', 'notices/clear'], ['POST', 'edges/clear'],
  ['POST', 'packs'], ['POST', 'packs/lev/config'], ['POST', 'packs/lev/delete'],
  ['POST', 'backends/lev/probe'], ['POST', 'backends/lev/start'], ['POST', 'backends/lev/stop'],
  ['POST', 'install'],
  ['POST', 'recalibrate/preview'], ['POST', 'recalibrate/apply'],
  ['POST', 'backfill/preview'], ['POST', 'backfill/start'],
  ['POST', 'mem/graph/semantic'], ['POST', 'mem/graph/event'], ['POST', 'mem/graph/events'],
  ['POST', 'mem/graph/edge'], ['POST', 'mem/graph/edge/delete'],
  ['POST', 'mem/graph/positions'], ['POST', 'mem/graph/positions/clear'],
]
for (const [method, route] of allowed) {
  calls.length = 0
  const response = await call(method, route, method === 'POST' ? {} : undefined)
  check(response.status === 200, `${method} ${route} is proxied`)
  check(calls.length === 1 && calls[0].path === '/api/' + route, `${route} forwards to the daemon as /api/${route}`)
}

const denied = [
  ['GET', 'mem/projects'], ['GET', 'i18n'], ['GET', 'backends'], ['GET', 'runs'],
  ['POST', 'run-once'], ['POST', 'edges'], ['POST', 'packs/../config'],
  ['POST', 'packs/a/config/../../x'], ['POST', 'recalibrate/again'], ['POST', 'config/../config'],
  ['POST', 'mem/session/project'], ['GET', 'mem/graph?x=1'],
]
for (const [method, route] of denied) {
  calls.length = 0
  const response = await call(method, route, method === 'POST' ? {} : undefined)
  check(response.status === 404, `${method} ${route} is refused`)
  check(calls.length === 0, `${method} ${route} never reaches the daemon`)
}

const traversal = ['..%2Fconfig', 'packs%2F..%2Fconfig', 'config%00']
for (const route of traversal) {
  calls.length = 0
  const response = await call('POST', route, {})
  check(response.status === 404, `encoded traversal ${route} is refused`)
  check(calls.length === 0, `encoded traversal ${route} never reaches the daemon`)
}

const empty = await call('GET', '', undefined)
check(empty.status === 404, 'an empty route is refused')

console.log(`\n${failures.length ? `${failures.length} FAILED` : 'PROXY PASS'} (${allowed.length} allowed, ${denied.length + traversal.length + 1} refused)`)
for (const failure of failures) console.log(' - ' + failure)
process.exit(failures.length ? 1 : 0)
