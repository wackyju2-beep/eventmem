/**
 * eventmem / dsh — the setup/status surface.
 *
 * Installing the bundle is not installing eventmem: the adapter is one process, the
 * daemon (database, recall, edge worker) is another. This module answers "what is
 * actually working?" and writes the few connection fields through the profile's config
 * editor, so a user never has to edit `cordis.patch.yml` to point at their own machine.
 */

import { Net } from './net.js'
import { ADAPTER_VERSION, normalizeDraft, publicConfig } from './config.js'

function check(id, label, state, detail) {
  return { id, label, state, detail: String(detail || '') }
}

function isLoopbackHost(hostname) {
  const host = String(hostname || '').toLowerCase()
  return host === 'localhost' || host === '::1' || host === '[::1]' || /^127\./.test(host)
}

function endpointOf(baseUrl) {
  try {
    return new URL(baseUrl)
  } catch {
    return null
  }
}

/**
 * One snapshot for the setup page. `health` is optional: when it has not been probed yet
 * this uses the Net instance's last result, so the page can render immediately.
 */
export function setupSnapshot({ cfg, net, health }) {
  const h = health || net.lastHealth
  const config = publicConfig(cfg)
  const endpoint = endpointOf(cfg.baseUrl)
  const checks = [
    check('adapter', '适配器已加载', 'ok', `dsh-eventmem ${ADAPTER_VERSION}`),
  ]

  if (h) {
    checks.push(check('daemon', '记忆服务', 'ok',
      `${h.schema !== undefined ? 'schema ' + h.schema : 'reachable'}${h.instance_id ? ' · ' + String(h.instance_id).slice(0, 8) : ''}`))
  } else {
    checks.push(check('daemon', '记忆服务', 'bad',
      net.identityError || net.lastHealthError || `不可达：${cfg.baseUrl}`))
  }

  if (h) {
    const db = h.db_id ? `db ${h.db_id}` : ''
    const expected = cfg.expectDbId ? (h.db_id === cfg.expectDbId ? 'matches expectDbId' : 'does not match expectDbId') : ''
    checks.push(check('database', '数据库身份', expected.includes('does not') ? 'bad' : 'ok',
      [db, expected].filter(Boolean).join(' · ') || 'daemon did not report an id'))
  }

  if (h && h.auth === 'token' && !net.token) {
    checks.push(check('auth', 'API token', 'bad', 'daemon requires a token; set tokenFile'))
  } else if (net.tokenError) {
    checks.push(check('auth', 'API token', 'bad', net.tokenError))
  } else {
    checks.push(check('auth', 'API token', 'ok', h && h.auth === 'token' ? 'configured' : 'not required on this endpoint'))
  }

  if (h) {
    const caps = h.capabilities || {}
    checks.push(check('embedding', '向量检索', caps.embedding ? 'ok' : 'warn',
      caps.embedding ? 'encoder available' : 'optional · not configured, recall uses FTS5'))
    checks.push(check('decision', '关系后端', caps.decision ? 'ok' : 'warn',
      caps.decision ? 'configured' : 'optional · no decision backend, no new edges'))
  }

  if (cfg.mode === 'remote' && !cfg.tunnelHost) {
    checks.push(check('tunnel', '远程模式', 'bad', 'remote mode needs tunnelHost'))
  } else if (cfg.mode === 'remote') {
    checks.push(check('tunnel', '远程模式', 'ok', `ssh ${cfg.tunnelHost}`))
  } else if (endpoint && !isLoopbackHost(endpoint.hostname)) {
    checks.push(check('tunnel', '本机模式', 'warn', `baseUrl points at ${endpoint.hostname}, not loopback`))
  } else {
    checks.push(check('tunnel', '本机模式', 'ok', 'no SSH tunnel'))
  }

  return {
    adapter: { loaded: true, version: ADAPTER_VERSION },
    config,
    daemon: h ? {
      ok: true,
      schema: h.schema,
      db: h.db,
      dbId: h.db_id,
      instanceId: h.instance_id,
      api: h.api,
      auth: h.auth,
    } : null,
    error: h ? '' : (net.identityError || net.lastHealthError || ''),
    checks,
  }
}

/** Probe a draft without saving it. Always returns a 200-shaped result. */
export async function testDraft(draft) {
  const normalized = normalizeDraft(draft)
  if (!normalized.ok) return { ok: false, error: normalized.error, checks: [], health: null }
  const probe = new Net({
    baseUrl: normalized.patch.baseUrl,
    tokenFile: normalized.patch.tokenFile,
    expectDbId: normalized.patch.expectDbId,
    timeoutMs: 5000,
  })
  const health = await probe.health({ timeoutMs: 5000 })
  if (!health) {
    return {
      ok: false,
      error: probe.identityError || probe.tokenError || probe.lastHealthError || 'daemon did not answer',
      checks: setupSnapshot({ cfg: { ...normalized.patch }, net: probe }).checks,
      health: null,
    }
  }
  let authError = ''
  if (health.auth === 'token' && !probe.token) {
    authError = 'daemon requires a token; set tokenFile'
  } else {
    try {
      await probe.get('/api/status', { timeoutMs: 5000 })
    } catch (error) {
      authError = error && error.message ? error.message : 'status request failed'
    }
  }
  return {
    ok: !authError,
    error: authError,
    checks: setupSnapshot({ cfg: { ...normalized.patch }, net: probe, health }).checks,
    health: { schema: health.schema, db: health.db, dbId: health.db_id, instanceId: health.instance_id },
  }
}

/**
 * Persist the form through the profile's config editor. Never rewrites the profile file by
 * hand: the editor owns the surrounding rows/comments and stale-write checks.
 */
export async function saveSetup(ctx, draft) {
  const normalized = normalizeDraft(draft)
  if (!normalized.ok) return { ok: false, status: 400, error: normalized.error }
  let entry
  try {
    entry = ctx && ctx.fiber ? ctx.fiber.entry : undefined
  } catch {
    entry = undefined
  }
  let editor
  try {
    editor = ctx && typeof ctx.get === 'function' ? ctx.get('configEditor') : undefined
  } catch {
    editor = undefined
  }
  if (!entry || !editor || typeof editor.edit !== 'function') {
    return {
      ok: false,
      status: 409,
      error: 'configuration cannot be saved in this profile; override the eventmem row in cordis.patch.yml',
    }
  }
  try {
    await editor.edit(entry, (current) => ({ ...(current || {}), ...normalized.patch }))
  } catch (error) {
    return { ok: false, status: 409, error: error && error.message ? error.message : String(error) }
  }
  return { ok: true, patch: normalized.patch, restart: true }
}
