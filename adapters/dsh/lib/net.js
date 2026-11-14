/**
 * eventmem / dsh — the whole network surface, in one file.
 *
 * The daemon lives on another machine behind an SSH tunnel; that tunnel is a single
 * point of failure. Everything here therefore has two shapes: a typed failure the
 * caller can render (tool calls), and a failure the caller must swallow (prefetch /
 * turn-end extraction). Nothing in this file throws into the harness loop.
 */

/** One data-plane failure, classified so callers can decide whether to degrade or report. */
export class MemError extends Error {
  /**
   * @param {string} message operator-facing detail.
   * @param {{kind?: 'unavailable'|'http'|'aborted', status?: number, body?: unknown, raw?: string, cause?: unknown}} [info]
   */
  constructor(message, info = {}) {
    super(message, info.cause === undefined ? undefined : { cause: info.cause })
    this.name = 'MemError'
    this.kind = info.kind || 'unavailable'
    this.status = info.status || 0
    this.body = info.body
    this.raw = info.raw || ''
  }
}

function describe(cause) {
  const name = cause && cause.name ? String(cause.name) : 'Error'
  const text = cause && cause.message ? String(cause.message) : String(cause || '')
  return text ? `${name}: ${text}` : name
}

/** A thin JSON client for `/api/mem/*`: base URL, timeout, bounded retry. */
export class Net {
  /**
   * @param {{baseUrl: string, timeoutMs?: number, logger?: {warn?: Function, debug?: Function}}} options
   */
  constructor(options) {
    this.baseUrl = String(options.baseUrl || 'http://127.0.0.1:9123').replace(/\/+$/, '')
    this.timeoutMs = Number(options.timeoutMs) > 0 ? Number(options.timeoutMs) : 8000
    this.logger = options.logger
  }

  url(path) {
    return this.baseUrl + (String(path).startsWith('/') ? path : '/' + path)
  }

  /**
   * One request. HTTP 4xx/5xx is final (no retry — the daemon never 500s on bad values
   * and a write must not be replayed). Transport failures retry only when asked.
   * @param {'GET'|'POST'} method
   * @param {string} path
   * @param {unknown} [body]
   * @param {{timeoutMs?: number, retry?: number, signal?: AbortSignal}} [options]
   */
  async request(method, path, body, options = {}) {
    const timeoutMs = Number(options.timeoutMs) > 0 ? Number(options.timeoutMs) : this.timeoutMs
    const retries = Number(options.retry) > 0 ? Math.floor(Number(options.retry)) : 0
    const external = options.signal
    let last = null
    for (let attempt = 0; attempt <= retries; attempt += 1) {
      if (external && external.aborted) throw new MemError('aborted', { kind: 'aborted' })
      const timeout = AbortSignal.timeout(timeoutMs)
      const signal = external ? AbortSignal.any([external, timeout]) : timeout
      try {
        const response = await fetch(this.url(path), {
          method,
          headers: body === undefined
            ? { accept: 'application/json' }
            : { accept: 'application/json', 'content-type': 'application/json; charset=utf-8' },
          body: body === undefined ? undefined : JSON.stringify(body),
          signal,
        })
        const raw = await response.text()
        let data = null
        if (raw) {
          try { data = JSON.parse(raw) } catch { data = null }
        }
        if (!response.ok) {
          const detail = data && typeof data === 'object' && data.error ? String(data.error) : (raw || '')
          throw new MemError(`HTTP ${response.status}${detail ? ': ' + detail : ''}`, {
            kind: 'http', status: response.status, body: data, raw,
          })
        }
        return data
      } catch (error) {
        if (error instanceof MemError) throw error
        if (external && external.aborted) throw new MemError('aborted', { kind: 'aborted', cause: error })
        last = error
        // Transport or timeout: the next attempt (if any) starts with a fresh deadline.
      }
    }
    throw new MemError(describe(last), { kind: 'unavailable', cause: last })
  }

  /** GET an endpoint; transport errors are retried once because a GET is idempotent. */
  get(path, options = {}) {
    return this.request('GET', path, undefined, { retry: 1, ...options })
  }

  /** POST JSON. Writes are never retried: a duplicated event is worse than a failed call. */
  post(path, body, options = {}) {
    return this.request('POST', path, body === undefined ? {} : body, { retry: 0, ...options })
  }

  /** Cheap liveness probe; never throws. */
  async health(options = {}) {
    try {
      return await this.get('/api/health', { timeoutMs: 3000, ...options })
    } catch {
      return null
    }
  }
}
