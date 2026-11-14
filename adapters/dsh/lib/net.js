/**
 * eventmem / dsh — the whole network surface, in one file.
 *
 * The daemon may live on another machine behind an SSH tunnel; that tunnel is a single
 * point of failure. Everything here therefore has two shapes: a typed failure the
 * caller can render (tool calls), and a failure the caller must swallow (prefetch /
 * turn-end extraction). Nothing in this file throws into the harness loop.
 *
 * Identity is checked before anything is written. A successful HTTP response is not
 * enough: the endpoint must name itself `eventmem-daemon`, speak protocol `1`, and match
 * the expected database id when one is configured. That is what stops a stale daemon on
 * the same port — or a different local service — from becoming the target of a write.
 */

import { readFileSync } from 'node:fs'

/** One data-plane failure, classified so callers can decide whether to degrade or report. */
export class MemError extends Error {
  /**
   * @param {string} message operator-facing detail.
   * @param {{kind?: 'unavailable'|'http'|'aborted'|'identity', status?: number, body?: unknown, raw?: string, cause?: unknown}} [info]
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

/** Protocol version this adapter understands. Bump only with a breaking data-plane change. */
export const API_VERSION = 1

/** Service id the daemon writes into `/api/health`. */
export const SERVICE_ID = 'eventmem-daemon'

function describe(cause) {
  const name = cause && cause.name ? String(cause.name) : 'Error'
  const text = cause && cause.message ? String(cause.message) : String(cause || '')
  return text ? `${name}: ${text}` : name
}

/** Resolve a token from the config value or a file, without ever logging the value. */
function resolveToken(options) {
  const direct = typeof options.token === 'string' ? options.token.trim() : ''
  if (direct) return { token: direct, tokenError: '' }
  const file = typeof options.tokenFile === 'string' ? options.tokenFile.trim() : ''
  if (!file) return { token: '', tokenError: '' }
  try {
    return { token: readFileSync(file, 'utf8').trim(), tokenError: '' }
  } catch (error) {
    return { token: '', tokenError: `cannot read tokenFile ${file}: ${error && error.message ? error.message : error}` }
  }
}

/** A base URL is an origin (+ optional path prefix); credentials/query/fragment are refused. */
function normalizeBaseUrl(raw) {
  const value = String(raw || '').trim()
  try {
    const parsed = new URL(value || 'http://127.0.0.1:9123')
    if (!['http:', 'https:'].includes(parsed.protocol)) return { baseUrl: value, error: 'baseUrl must be http(s)' }
    if (parsed.username || parsed.password) return { baseUrl: value, error: 'baseUrl must not carry credentials' }
    if (parsed.search || parsed.hash) return { baseUrl: value, error: 'baseUrl must not carry a query or fragment' }
    return { baseUrl: value.replace(/\/+$/, ''), error: '' }
  } catch {
    return { baseUrl: value, error: 'baseUrl is not an absolute URL' }
  }
}

/** A thin JSON client for `/api/*`: base URL, timeout, token, bounded retry. */
export class Net {
  /**
   * @param {{baseUrl: string, timeoutMs?: number, token?: string, tokenFile?: string,
   *          expectDbId?: string, logger?: {warn?: Function, debug?: Function}}} options
   */
  constructor(options) {
    const base = normalizeBaseUrl(options.baseUrl)
    this.baseUrl = base.baseUrl || 'http://127.0.0.1:9123'
    this.baseUrlError = base.error
    this.timeoutMs = Number(options.timeoutMs) > 0 ? Number(options.timeoutMs) : 8000
    this.logger = options.logger
    const resolved = resolveToken(options)
    this.token = resolved.token
    this.tokenError = resolved.tokenError
    this.expectDbId = String(options.expectDbId || '').trim().toLowerCase()
    /** Identity failure: block every non-health request until a later probe succeeds. */
    this.identityError = ''
    /** Last health failure for the console/setup view. */
    this.lastHealthError = ''
    /** The daemon says its API requires a token but this adapter has none. */
    this.lastAuthRequired = false
    this.lastHealth = null
  }

  url(path) {
    if (this.baseUrlError) throw new MemError(this.baseUrlError, { kind: 'identity' })
    return this.baseUrl + (String(path).startsWith('/') ? path : '/' + path)
  }

  headers(body) {
    const headers = body === undefined
      ? { accept: 'application/json' }
      : { accept: 'application/json', 'content-type': 'application/json; charset=utf-8' }
    if (this.token) headers.authorization = `Bearer ${this.token}`
    return headers
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
    // A probe that proved this endpoint is not the intended eventmem blocks writes. Health
    // itself stays callable so a later probe can recover without reloading the plugin.
    if (this.identityError && path !== '/api/health') {
      throw new MemError(this.identityError, { kind: 'identity' })
    }
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
          headers: this.headers(body),
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
          const hint = response.status === 401 && !this.token
            ? ' (the daemon requires a token; configure tokenFile)'
            : ''
          throw new MemError(`HTTP ${response.status}${detail ? ': ' + detail : ''}${hint}`, {
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

  /**
   * Validate one health payload. Returns the payload when it is the expected daemon;
   * records why it is not and returns null otherwise. Never throws.
   */
  verifyHealth(data) {
    if (!data || typeof data !== 'object') {
      this.identityError = 'health response is not a JSON object'
      return null
    }
    if (data.status !== 'ok') {
      this.identityError = `daemon status is ${String(data.status || 'missing')}, expected ok`
      return null
    }
    if (data.service !== SERVICE_ID) {
      this.identityError = `endpoint is not ${SERVICE_ID} (service=${String(data.service || 'missing')}); another service may own this port`
      return null
    }
    if (Number(data.api) !== API_VERSION) {
      this.identityError = `daemon speaks API ${String(data.api || 'missing')}, this adapter expects ${API_VERSION}`
      return null
    }
    if (!Number.isFinite(Number(data.schema))) {
      this.identityError = 'daemon health does not report a schema version'
      return null
    }
    const dbId = String(data.db_id || '').toLowerCase()
    if (this.expectDbId && dbId !== this.expectDbId) {
      this.identityError = `connected database id ${dbId || 'missing'} does not match expectDbId ${this.expectDbId}`
      return null
    }
    this.identityError = ''
    return data
  }

  /**
   * Liveness + identity probe; never throws. Returns null when unreachable, unauthenticated
   * in a way that hides health, or not the expected daemon.
   */
  async health(options = {}) {
    try {
      const data = await this.get('/api/health', { timeoutMs: 3000, ...options })
      const verified = this.verifyHealth(data)
      if (!verified) {
        this.lastHealthError = this.identityError
        this.lastHealth = null
        return null
      }
      this.lastHealth = verified
      this.lastHealthError = ''
      this.lastAuthRequired = verified.auth === 'token'
      return verified
    } catch (error) {
      this.lastHealthError = error && error.message ? error.message : describe(error)
      this.lastHealth = null
      return null
    }
  }
}
