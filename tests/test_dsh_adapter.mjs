// DSH adapter contract tests: defaults, package metadata, daemon identity, token, setup.
// Run: node tests/test_dsh_adapter.mjs   (no dependencies; Node 20+)
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  ADAPTER_VERSION, DEFAULTS, normalizeDraft, publicConfig, resolveConfig,
} from '../adapters/dsh/lib/config.js'
import { Net } from '../adapters/dsh/lib/net.js'
import { saveSetup, setupSnapshot, testDraft } from '../adapters/dsh/lib/setup.js'

const HERE = fileURLToPath(new URL('.', import.meta.url))
const PKG = JSON.parse(readFileSync(join(HERE, '..', 'adapters', 'dsh', 'package.json'), 'utf8'))
const PATCH = readFileSync(join(HERE, '..', 'adapters', 'dsh', 'cordis.patch.yml'), 'utf8')

let passed = 0
let failed = 0
async function test(name, fn) {
  try {
    await fn()
    passed += 1
    console.log('PASS ' + name)
  } catch (error) {
    failed += 1
    console.error('FAIL ' + name)
    console.error(error && error.stack ? error.stack : error)
  }
}

// ------------------------------------------------------------------ defaults

await test('defaults are local: no SSH host from anyone machine', () => {
  const cfg = resolveConfig({})
  assert.equal(cfg.tunnel, false)
  assert.equal(cfg.tunnelHost, '')
  assert.equal(cfg.mode, 'local')
  assert.equal(cfg.baseUrl, 'http://127.0.0.1:9123')
  assert.equal(DEFAULTS.tunnel, false)
  assert.equal(DEFAULTS.tunnelHost, '')
})

await test('old tunnel:true rows still mean remote', () => {
  const cfg = resolveConfig({ tunnel: true, tunnelHost: 'box' })
  assert.equal(cfg.tunnel, true)
  assert.equal(cfg.mode, 'remote')
})

await test('mode wins over a stale tunnel flag', () => {
  assert.equal(resolveConfig({ mode: 'local', tunnel: true, tunnelHost: 'box' }).tunnel, false)
  assert.equal(resolveConfig({ mode: 'remote', tunnel: false, tunnelHost: 'box' }).tunnel, true)
  assert.equal(resolveConfig({ tunnel: false }).tunnel, false)
})

await test('bundled patch is local and opens no tunnel', () => {
  // Comments in the patch discuss remote mode; only the shipped config lines count.
  const shipped = PATCH.split('\n').filter((line) => !line.trim().startsWith('#'))
  assert.ok(shipped.some((line) => /^\s*mode:\s*local\s*$/.test(line)), 'mode: local')
  assert.ok(!shipped.some((line) => /^\s*tunnelHost:/.test(line)), 'no host is shipped')
  assert.ok(!shipped.some((line) => /^\s*tunnel:\s*true/.test(line)), 'no tunnel is shipped')
  assert.ok(
    shipped.some((line) => /^\s*baseUrl:\s*http:\/\/127\.0\.0\.1:\d+\s*$/.test(line)),
    'baseUrl is loopback',
  )
})

// ------------------------------------------------------------------ metadata

await test('npm metadata is publishable and pins a DSH range', () => {
  assert.equal(PKG.private, undefined, 'private:true blocks npm and the official index')
  assert.equal(PKG.version, ADAPTER_VERSION)
  assert.match(PKG.engines.dsh, />=0\.2\.2-rc\.3/)
  assert.match(PKG.engines.dsh, /<0\.3\.0/)
  assert.equal(PKG.publishConfig.access, 'public')
  assert.match(PKG.repository.url, /eventmem/)
  assert.ok(PKG.files.includes('lib'))
  assert.ok(PKG.files.includes('client.js'))
})

await test('draft validation rejects non-http and secret-bearing URLs', () => {
  assert.equal(normalizeDraft({ mode: 'local', baseUrl: 'ftp://x' }).ok, false)
  assert.equal(normalizeDraft({ mode: 'local', baseUrl: 'http://u:p@127.0.0.1:9123' }).ok, false)
  assert.equal(normalizeDraft({ mode: 'remote', baseUrl: 'http://127.0.0.1:9123', tunnelHost: '' }).ok, false)
  assert.equal(normalizeDraft({ mode: 'remote', baseUrl: 'http://127.0.0.1:9123', tunnelHost: 'box' }).ok, true)
  assert.equal(normalizeDraft({ mode: 'local', baseUrl: 'http://127.0.0.1:9123/' }).patch.baseUrl,
    'http://127.0.0.1:9123')
})

await test('public config never exposes a token', () => {
  const shown = publicConfig({ mode: 'local', baseUrl: 'http://x', tunnel: false, tunnelHost: '',
    expectDbId: '', tokenFile: 'C:/t', token: 'secret' })
  assert.equal(JSON.stringify(shown).includes('secret'), false)
  assert.equal(shown.hasToken, true)
})

await test('every literal UI string has an English entry', () => {
  const client = readFileSync(join(HERE, '..', 'adapters', 'dsh', 'client.js'), 'utf8')
  const start = client.indexOf('const EN = {')
  const end = client.indexOf('const ZH =')
  assert.ok(start > 0 && end > start, 'EN dictionary is present')
  const dictionary = new Set()
  for (const match of client.slice(start, end).matchAll(/^\s*'((?:[^'\\]|\\.)*)':/gm)) {
    dictionary.add(match[1].replace(/\\'/g, "'"))
  }
  const used = new Set()
  for (const match of client.matchAll(/\bt\('((?:[^'\\]|\\.)*)'\)/g)) {
    used.add(match[1].replace(/\\'/g, "'"))
  }
  const missing = [...used].filter((key) => !dictionary.has(key))
  assert.deepEqual(missing, [], 'add these keys to EN: ' + missing.join(' | '))
})

// ------------------------------------------------------------------ stub daemon

function healthFor({ service = 'eventmem-daemon', api = 1, dbId = '0123456789abcdef',
  auth = 'none', schema = 12 } = {}) {
  return {
    status: 'ok', service, api, schema, instance_id: 'a'.repeat(32), db_id: dbId,
    auth, capabilities: { embedding: false, decision: false },
  }
}

async function withServer(handler, fn) {
  const server = createServer(handler)
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const baseUrl = 'http://127.0.0.1:' + server.address().port
  try {
    return await fn(baseUrl)
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
}

function json(res, status, body) {
  const raw = JSON.stringify(body)
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(raw) })
  res.end(raw)
}

await test('Net accepts only the expected daemon identity', async () => {
  let hits = 0
  await withServer((req, res) => {
    hits += 1
    json(res, 200, healthFor())
  }, async (baseUrl) => {
    const net = new Net({ baseUrl })
    const health = await net.health()
    assert.ok(health)
    assert.equal(net.identityError, '')
    assert.equal(hits, 1)
  })
})

await test('a different service on the port is rejected and blocks writes', async () => {
  let hits = 0
  await withServer((req, res) => {
    hits += 1
    json(res, 200, healthFor({ service: 'some-other-service' }))
  }, async (baseUrl) => {
    const net = new Net({ baseUrl })
    assert.equal(await net.health(), null)
    assert.match(net.identityError, /not eventmem-daemon/)
    const before = hits
    await assert.rejects(() => net.get('/api/mem/projects'), (error) => error.kind === 'identity')
    assert.equal(hits, before, 'no write request may leave after an identity failure')
  })
})

await test('API version and expected database id are enforced', async () => {
  await withServer((req, res) => json(res, 200, healthFor({ api: 2 })), async (baseUrl) => {
    const net = new Net({ baseUrl })
    assert.equal(await net.health(), null)
    assert.match(net.identityError, /API 2/)
  })
  await withServer((req, res) => json(res, 200, healthFor({ dbId: 'deadbeefdeadbeef' })), async (baseUrl) => {
    const net = new Net({ baseUrl, expectDbId: '0123456789abcdef' })
    assert.equal(await net.health(), null)
    assert.match(net.identityError, /does not match expectDbId/)
  })
})

await test('token is sent as Bearer and a 401 names tokenFile', async () => {
  let seen = ''
  await withServer((req, res) => {
    seen = req.headers.authorization || ''
    if (req.url === '/api/health') return json(res, 200, healthFor({ auth: 'token' }))
    if (seen !== 'Bearer topsecret') return json(res, 401, { error: 'missing or invalid API token' })
    return json(res, 200, { projects: [] })
  }, async (baseUrl) => {
    const net = new Net({ baseUrl, token: 'topsecret' })
    assert.ok(await net.health())
    assert.equal(net.lastAuthRequired, true)
    await net.get('/api/mem/projects')
    assert.equal(seen, 'Bearer topsecret')

    const bare = new Net({ baseUrl })
    assert.ok(await bare.health(), 'health is public on a token-protected daemon')
    await assert.rejects(() => bare.get('/api/mem/projects'), (error) => {
      assert.equal(error.status, 401)
      assert.match(error.message, /tokenFile/)
      return true
    })
  })
})

await test('tokenFile is read without putting the secret in config', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'eventmem-token-'))
  const file = join(dir, 'daemon.token')
  writeFileSync(file, 'file-secret\n', 'utf8')
  let seen = ''
  await withServer((req, res) => {
    seen = req.headers.authorization || ''
    json(res, 200, healthFor({ auth: 'token' }))
  }, async (baseUrl) => {
    const net = new Net({ baseUrl, tokenFile: file })
    assert.ok(await net.health())
    assert.equal(seen, 'Bearer file-secret')
  })
})

// ------------------------------------------------------------------ setup

const fakeCfg = {
  ...resolveConfig({ mode: 'local', baseUrl: 'http://127.0.0.1:9123' }),
  token: '',
  tokenFile: '',
}

await test('setup snapshot marks basic memory usable and optional arms off', () => {
  const net = new Net({ baseUrl: fakeCfg.baseUrl })
  const health = healthFor({ auth: 'none' })
  const snap = setupSnapshot({ cfg: fakeCfg, net, health })
  const byId = Object.fromEntries(snap.checks.map((check) => [check.id, check]))
  assert.equal(snap.adapter.loaded, true)
  assert.equal(byId.adapter.state, 'ok')
  assert.equal(byId.daemon.state, 'ok')
  assert.equal(byId.database.state, 'ok')
  assert.equal(byId.embedding.state, 'warn')
  assert.equal(byId.decision.state, 'warn')
  assert.equal(byId.auth.state, 'ok')
})

await test('setup snapshot flags a missing token and a wrong database', () => {
  const net = new Net({ baseUrl: fakeCfg.baseUrl, expectDbId: 'aaaaaaaaaaaaaaaa' })
  const health = healthFor({ auth: 'token', dbId: 'bbbbbbbbbbbbbbbb' })
  const snap = setupSnapshot({ cfg: { ...fakeCfg, expectDbId: 'aaaaaaaaaaaaaaaa' }, net, health })
  const byId = Object.fromEntries(snap.checks.map((check) => [check.id, check]))
  assert.equal(byId.auth.state, 'bad')
  assert.equal(byId.database.state, 'bad')
})

await test('setup/test probes a draft end to end', async () => {
  await withServer((req, res) => {
    if (req.url === '/api/health') return json(res, 200, healthFor())
    return json(res, 200, { status: 'ok', cursor: {}, runs: [] })
  }, async (baseUrl) => {
    const result = await testDraft({ mode: 'local', baseUrl })
    assert.equal(result.ok, true, result.error)
    assert.ok(result.health.schema === 12)
  })
  const bad = await testDraft({ mode: 'local', baseUrl: 'ftp://nope' })
  assert.equal(bad.ok, false)
})

await test('setup/save writes through the config editor, never the profile file', async () => {
  let saved = null
  const ctx = {
    fiber: { entry: { id: 'eventmem' } },
    get: (name) => name === 'configEditor' ? {
      edit: async (_entry, mutate) => { saved = mutate({ baseUrl: 'http://old:9123', lang: 'zh' }) },
    } : undefined,
  }
  const local = await saveSetup(ctx, { mode: 'local', baseUrl: 'http://127.0.0.1:9124', tunnelHost: 'box' })
  assert.equal(local.ok, true)
  assert.equal(saved.mode, 'local')
  assert.equal(saved.tunnel, false)
  assert.equal(saved.tunnelHost, '')
  assert.equal(saved.baseUrl, 'http://127.0.0.1:9124')
  assert.equal(saved.lang, 'zh', 'unknown existing fields survive')

  const remote = await saveSetup(ctx, { mode: 'remote', baseUrl: 'http://127.0.0.1:9125', tunnelHost: 'box' })
  assert.equal(remote.ok, true)
  assert.equal(saved.tunnel, true)
  assert.equal(saved.tunnelHost, 'box')
  assert.equal(saved.tunnelLocalPort, 9125, 'local tunnel port follows the baseUrl port')

  const invalid = await saveSetup(ctx, { mode: 'local', baseUrl: 'not a url' })
  assert.equal(invalid.ok, false)
  assert.equal(invalid.status, 400)
})

await test('setup/save explains when the profile cannot be edited', async () => {
  const ctx = { fiber: {}, get: () => undefined }
  const result = await saveSetup(ctx, { mode: 'local', baseUrl: 'http://127.0.0.1:9123' })
  assert.equal(result.ok, false)
  assert.equal(result.status, 409)
  assert.match(result.error, /cordis\.patch\.yml/)
})

console.log(`\n${passed} passed, ${failed} failed`)
process.exitCode = failed ? 1 : 0
