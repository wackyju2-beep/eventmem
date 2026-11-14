/**
 * eventmem / dsh — the adapter's configuration contract.
 *
 * Kept apart from `index.js` so tests can exercise the defaults without importing the
 * Cordis plugin. The defaults describe a **single-machine** install: a daemon on
 * `127.0.0.1:9123`, no SSH, no host name from anyone's private network. Remote sharing is
 * opt-in (`mode: remote` or `tunnel: true` plus a real `tunnelHost`).
 */

import { readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

/** Package version, read once so `setup` can show it without a JSON import assertion. */
export const ADAPTER_VERSION = (() => {
  try {
    const url = new URL('../package.json', import.meta.url)
    const pkg = JSON.parse(readFileSync(fileURLToPath(url), 'utf8'))
    return String(pkg.version || '0.0.0')
  } catch {
    return '0.0.0'
  }
})()

/** Everything unset means "local daemon, no tunnel". */
export const DEFAULTS = {
  baseUrl: 'http://127.0.0.1:9123',
  agent: 'dsh',
  lang: 'zh',
  timeoutMs: 8000,
  recallTimeoutMs: 10000,
  auxTimeoutMs: 60000,
  auxMaxTokens: 800,
  budgetTokens: 600,
  maxEventsPerCall: 3,
  prefetch: true,
  extract: true,
  reminder: true,
  projectHint: true,
  auxProvider: '',
  auxModel: '',
  logFile: join(tmpdir(), 'dsh-eventmem.log'),
  token: '',
  tokenFile: '',
  expectDbId: '',
  mode: 'local',
  tunnel: false,
  tunnelHost: '',
  tunnelLocalPort: 9123,
  tunnelRemotePort: 9123,
  tunnelSsh: 'ssh',
  tunnelArgs: [],
  tunnelIntervalMs: 30000,
}

function positive(value, fallback) {
  const n = Number(value)
  return Number.isFinite(n) && n > 0 ? n : fallback
}

function text(value, fallback) {
  return typeof value === 'string' ? value.trim() : fallback
}

/**
 * Resolve one row's `config` by hand. The bundle intentionally has no `Config` export:
 * a linked package cannot import schemastery, and the console's setup page writes the
 * same fields through the profile's config editor.
 */
export function resolveConfig(raw) {
  const input = raw && typeof raw === 'object' ? raw : {}
  // `mode` is the simple switch the setup page writes. Older rows used `tunnel` alone;
  // keep honouring those so an existing profile does not change meaning.
  const mode = input.mode === 'remote' ? 'remote'
    : input.mode === 'local' ? 'local'
      : input.tunnel === true ? 'remote' : 'local'
  const tunnel = mode === 'remote'
  return {
    baseUrl: text(input.baseUrl, DEFAULTS.baseUrl) || DEFAULTS.baseUrl,
    agent: text(input.agent, DEFAULTS.agent) || DEFAULTS.agent,
    lang: typeof input.lang === 'string' ? input.lang : DEFAULTS.lang,
    timeoutMs: positive(input.timeoutMs, DEFAULTS.timeoutMs),
    recallTimeoutMs: positive(input.recallTimeoutMs, DEFAULTS.recallTimeoutMs),
    auxTimeoutMs: positive(input.auxTimeoutMs, DEFAULTS.auxTimeoutMs),
    auxMaxTokens: Math.floor(positive(input.auxMaxTokens, DEFAULTS.auxMaxTokens)),
    budgetTokens: Math.floor(positive(input.budgetTokens, DEFAULTS.budgetTokens)),
    maxEventsPerCall: Math.min(10, Math.max(1, Math.floor(positive(input.maxEventsPerCall, DEFAULTS.maxEventsPerCall)))),
    prefetch: input.prefetch !== false,
    extract: input.extract !== false,
    reminder: input.reminder !== false,
    projectHint: input.projectHint !== false,
    auxProvider: typeof input.auxProvider === 'string' ? input.auxProvider : '',
    auxModel: typeof input.auxModel === 'string' ? input.auxModel : '',
    logFile: typeof input.logFile === 'string' ? input.logFile : DEFAULTS.logFile,
    token: text(input.token, DEFAULTS.token),
    tokenFile: text(input.tokenFile, DEFAULTS.tokenFile),
    expectDbId: text(input.expectDbId, DEFAULTS.expectDbId),
    mode,
    tunnel,
    tunnelHost: text(input.tunnelHost, DEFAULTS.tunnelHost),
    tunnelLocalPort: Math.floor(positive(input.tunnelLocalPort, DEFAULTS.tunnelLocalPort)),
    tunnelRemotePort: Math.floor(positive(input.tunnelRemotePort, DEFAULTS.tunnelRemotePort)),
    tunnelSsh: text(input.tunnelSsh, DEFAULTS.tunnelSsh) || DEFAULTS.tunnelSsh,
    tunnelArgs: Array.isArray(input.tunnelArgs) ? input.tunnelArgs.map((value) => String(value)) : [],
    tunnelIntervalMs: positive(input.tunnelIntervalMs, DEFAULTS.tunnelIntervalMs),
  }
}

/** A config summary safe for the console: never returns the token itself. */
export function publicConfig(cfg) {
  return {
    mode: cfg.mode,
    baseUrl: cfg.baseUrl,
    tunnel: cfg.tunnel,
    tunnelHost: cfg.tunnelHost,
    expectDbId: cfg.expectDbId,
    tokenFile: cfg.tokenFile,
    hasToken: Boolean(cfg.token),
  }
}

/** Validate the setup form; returns `{ ok, error, patch }` (patch only when ok). */
export function normalizeDraft(draft, current = DEFAULTS) {
  const input = draft && typeof draft === 'object' ? draft : {}
  const mode = input.mode === 'remote' ? 'remote' : 'local'
  const baseUrl = text(input.baseUrl, current.baseUrl) || current.baseUrl
  let parsed
  try {
    parsed = new URL(baseUrl)
  } catch {
    return { ok: false, error: 'baseUrl must be an absolute http(s) URL' }
  }
  if (!['http:', 'https:'].includes(parsed.protocol)) {
    return { ok: false, error: 'baseUrl must start with http:// or https://' }
  }
  if (parsed.username || parsed.password) {
    return { ok: false, error: 'credentials in baseUrl are not supported' }
  }
  if (parsed.search || parsed.hash) {
    return { ok: false, error: 'baseUrl must not carry a query or fragment' }
  }
  const tunnelHost = text(input.tunnelHost, '')
  if (mode === 'remote' && !tunnelHost) {
    return { ok: false, error: 'remote mode needs an SSH host alias (tunnelHost)' }
  }
  const expectDbId = text(input.expectDbId, '')
  if (expectDbId && !/^[0-9a-f]{8,64}$/i.test(expectDbId)) {
    return { ok: false, error: 'expectDbId must be a hexadecimal database id' }
  }
  const urlPort = Number(parsed.port) || 0
  return {
    ok: true,
    patch: {
      mode,
      baseUrl: baseUrl.replace(/\/+$/, ''),
      tunnel: mode === 'remote',
      tunnelHost: mode === 'remote' ? tunnelHost : '',
      tunnelLocalPort: mode === 'remote'
        ? (urlPort || current.tunnelLocalPort || 9123)
        : (current.tunnelLocalPort || 9123),
      tunnelRemotePort: current.tunnelRemotePort || 9123,
      expectDbId,
      tokenFile: text(input.tokenFile, ''),
    },
  }
}
