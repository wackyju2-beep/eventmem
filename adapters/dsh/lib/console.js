/**
 * eventmem / dsh — the console's Host half.
 *
 * The memory backstage is the daemon's own web console (`/` on :9123). The dsh page is a
 * native rendering of the same JSON surface, so this file is only a **proxy**: the browser
 * calls `/api/eventmem?path=…` on the dsh origin, and every request is forwarded to the
 * daemon over the same tunnel the adapter already owns. No CORS, no second token, and the
 * daemon's loopback stays on the daemon machine.
 *
 * `setup` is the one route answered here instead of forwarded: it reports the adapter and
 * daemon state, tests a candidate endpoint, and writes the connection fields through the
 * profile's config editor. The allow-list below never contains it.
 */

import { saveSetup, setupSnapshot, testDraft } from './setup.js'

/** GET routes the console page actually calls. */
const GET_ROUTES = [
  /^health$/, /^status$/, /^stats$/, /^logs$/, /^config$/,
  /^packs$/, /^models$/, /^job$/,
  /^mem\/graph$/,
]

/** POST routes the console page actually calls. */
const POST_ROUTES = [
  /^pause$/, /^stop$/, /^switch$/, /^restart$/, /^notices\/clear$/, /^edges\/clear$/, /^config$/, /^packs$/,
  /^packs\/[A-Za-z0-9._-]+\/(config|delete)$/,
  /^backends\/[A-Za-z0-9._-]+\/(probe|start|stop)$/,
  /^install$/,
  /^recalibrate\/(preview|apply)$/,
  /^backfill\/(preview|start)$/,
  /^mem\/graph\/(semantic|event|events|edge|edge\/delete|positions|positions\/clear)$/,
]

/** Batch edits re-embed every event: the daemon needs minutes, not seconds. */
const GRAPH_TIMEOUT_MS = 180000

/** History passes walk the whole table (1.2 k edges / 500 events) — same budget as the graph. */
const HISTORY_TIMEOUT_MS = 180000
const HISTORY_ROUTE = /^(recalibrate|backfill)\//

const ROUTE_PATH = '/api/eventmem'

/**
 * A route may not encode a second path: no traversal, no separator, no escape.
 * Matching happens before the string becomes a URL, but `fetch` would normalise
 * `packs/../config`, so this gate is what keeps the allow-list exact.
 */
function cleanRoute(route) {
  if (route.length === 0 || route.length > 200) return false
  if (/[\\?#%]/.test(route)) return false
  if (route.includes('..') || route.includes('//')) return false
  return /^[A-Za-z0-9._\/-]+$/.test(route)
}

/** One console request, forwarded as-is. Never throws into the HTTP bridge. */
async function forward(net, method, route, request, timeoutMs) {
  let payload
  if (method === 'POST') {
    try {
      payload = await request.json()
    } catch {
      return json(400, { error: 'body is not JSON' })
    }
  }
  try {
    const data = await net.request(method, '/api/' + route, payload, { timeoutMs })
    return json(200, data === null ? {} : data)
  } catch (error) {
    const status = Number(error && error.status) || 502
    const detail = error && error.body && typeof error.body === 'object' && error.body.error
      ? String(error.body.error)
      : (error && error.message ? String(error.message) : 'daemon unreachable')
    return json(status, { error: detail })
  }
}

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json; charset=utf-8', 'cache-control': 'no-store' },
  })
}

/** Read one JSON body; `null` means the body was not valid JSON. */
async function readJson(request) {
  try {
    return await request.json()
  } catch {
    return null
  }
}

/** Whether a route may be proxied on the given method. */
function allowed(method, route) {
  if (!cleanRoute(route)) return false
  const list = method === 'GET' ? GET_ROUTES : POST_ROUTES
  return list.some((pattern) => pattern.test(route))
}

/** Per-route deadline: the graph page re-embeds a whole selection in one call. */
function timeoutFor(cfg, route) {
  const base = Math.max(cfg.timeoutMs || 0, 20000)
  if (route.startsWith('mem/graph')) return Math.max(base, GRAPH_TIMEOUT_MS)
  if (HISTORY_ROUTE.test(route)) return Math.max(base, HISTORY_TIMEOUT_MS)
  return base
}

/**
 * Register `/api/eventmem` on the dsh Connection's shared `/api` channel.
 * @param {any} ctx Cordis context of the bundle row.
 * @param {{baseUrl: string, timeoutMs?: number}} cfg resolved adapter config.
 * @param {{request: Function}} net the adapter's `Net` instance.
 */
export function registerConsole(ctx, cfg, net) {
  ctx.inject(['connection'], (sub) => {
    sub.effect(() => sub.connection.fetch.register({
      path: ROUTE_PATH,
      methods: ['GET', 'POST'],
      requestBody: 'buffered',
      fetch: async (request) => {
        const url = new URL(request.url)
        const route = String(url.searchParams.get('path') || '').replace(/^\/+/, '')
        // Local routes first: they are answered by the adapter and never forwarded.
        if (route === 'setup' && request.method === 'GET') {
          const health = await net.health({ timeoutMs: 6000 })
          return json(200, setupSnapshot({ cfg, net, health }))
        }
        if (route === 'setup/test' && request.method === 'POST') {
          const body = await readJson(request)
          if (body === null) return json(400, { error: 'body is not JSON' })
          return json(200, await testDraft(body))
        }
        if (route === 'setup/save' && request.method === 'POST') {
          const body = await readJson(request)
          if (body === null) return json(400, { error: 'body is not JSON' })
          const result = await saveSetup(ctx, body)
          return json(Number(result.status) || 200, result)
        }
        if (!route || !allowed(request.method, route)) {
          return json(404, { error: 'not found' })
        }
        return forward(net, request.method, route, request, timeoutFor(cfg, route))
      },
    }), 'eventmem: console route')
  })
}
