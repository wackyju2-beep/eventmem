/**
 * eventmem / dsh — the Host half of the adapter.
 *
 * Cross-machine shape: this file is the only place that knows DSH exists; `lib/net.js`
 * is the only place that knows the daemon exists. Nothing here imports the core, touches
 * `events.db`, or re-implements recall / edge judgement — those stay in the daemon.
 *
 * The hard rules:
 *  - every write carries `agent: "dsh"` and `session: "dsh:<session id>"`;
 *  - `reset: true` only at real boundaries (a session's first recall in this process,
 *    after compaction); an ordinary turn keeps the daemon's `#id 同上文` dedupe;
 *  - prefetch and turn-end extraction degrade silently: a dead tunnel ends the turn, it
 *    never blocks it and never surfaces an exception.
 */

import { randomUUID } from 'node:crypto'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

import { Net } from './lib/net.js'
import { strings, OUR_SOURCE_KIND } from './lib/text.js'
import { SessionBook } from './lib/state.js'
import { makeSticky } from './lib/sticky.js'
import { registerTools, makeProjectCache } from './lib/tools.js'
import { registerMiddleware } from './lib/middleware.js'
import { ExtractError, extractEvents, isTrivial, squash } from './lib/extract.js'
import { redact } from './lib/redact.js'
import { createNotifier } from './lib/notify.js'
import { TunnelSupervisor } from './lib/tunnel.js'
import { registerConsole } from './lib/console.js'

/** Cordis plugin name (loader diagnostics and `agent/created` filtering). */
export const name = 'eventmem'

/** Services this plugin cannot work without; a profile lacking one keeps it inactive. */
export const inject = ['tools', 'llm']

const DEFAULTS = {
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
  tunnel: true,
  tunnelHost: 'mint',
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

/** Validate the row's `config` by hand: the bundle cannot import schemastery. */
function resolveConfig(raw) {
  const input = raw && typeof raw === 'object' ? raw : {}
  const cfg = {
    baseUrl: typeof input.baseUrl === 'string' && input.baseUrl.trim() ? input.baseUrl.trim() : DEFAULTS.baseUrl,
    agent: typeof input.agent === 'string' && input.agent.trim() ? input.agent.trim() : DEFAULTS.agent,
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
    tunnel: input.tunnel !== false,
    tunnelHost: typeof input.tunnelHost === 'string' ? input.tunnelHost.trim() : DEFAULTS.tunnelHost,
    tunnelLocalPort: Math.floor(positive(input.tunnelLocalPort, DEFAULTS.tunnelLocalPort)),
    tunnelRemotePort: Math.floor(positive(input.tunnelRemotePort, DEFAULTS.tunnelRemotePort)),
    tunnelSsh: typeof input.tunnelSsh === 'string' && input.tunnelSsh.trim() ? input.tunnelSsh.trim() : DEFAULTS.tunnelSsh,
    tunnelArgs: Array.isArray(input.tunnelArgs) ? input.tunnelArgs.map((value) => String(value)) : [],
    tunnelIntervalMs: positive(input.tunnelIntervalMs, DEFAULTS.tunnelIntervalMs),
  }
  return cfg
}

/** `dsh:<session id>` — the prefix belongs to exactly one place. */
function tagSession(cfg, raw) {
  const value = String(raw || '').trim()
  if (!value) return ''
  const prefix = `${cfg.agent}:`
  return value.startsWith(prefix) ? value : prefix + value
}

function sessionIdOf(agent) {
  try {
    const id = agent && agent.session ? agent.session.id : undefined
    return id === undefined || id === null ? '' : String(id)
  } catch {
    return ''
  }
}

/** Model-facing text of one message, ignoring non-text blocks. */
function messageText(message) {
  // Session events wrap the message (`{turn, step, message}`); live payloads do not.
  const body = message && message.message && message.message.content ? message.message : message
  if (!body || !Array.isArray(body.content)) return ''
  return body.content
    .filter((block) => block && block.type === 'text' && typeof block.text === 'string')
    .map((block) => block.text)
    .join('\n')
    .trim()
}

/** One durable recall injection: a user-role message, source declared, never mutated. */
function recallMessage(text) {
  return {
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: OUR_SOURCE_KIND, form: 'recall' },
  }
}

/**
 * Harness-generated context messages that ride in the same user role as the prompt.
 * They are not what the user asked, so neither recall nor the extractor should treat
 * them as the question.
 */
const CONTEXT_SOURCE_KINDS = new Set([
  'runtime-context',
  'skill-catalog',
  'time-context',
  'user-approval',
  'subagent-settled',
  OUR_SOURCE_KIND,
])

function isPromptMessage(message) {
  const kind = message && message.source && message.source.kind
  return !kind || !CONTEXT_SOURCE_KINDS.has(kind)
}

/** The user text that should drive recall for this step. */
function queryText(messages) {
  if (!Array.isArray(messages)) return ''
  const prompt = []
  const fallback = []
  for (const message of messages) {
    if (!message || message.role !== 'user') continue
    const text = messageText(message)
    if (!text) continue
    if (isPromptMessage(message)) prompt.push(text)
    else if (message.source && message.source.kind !== OUR_SOURCE_KIND) fallback.push(text)
  }
  const parts = prompt.length > 0 ? prompt : fallback.slice(-1)
  return squash(parts.join('\n'), 2000)
}

/** Everything the model saw in one turn, read back from the session log. */
function turnExcerpt(agent, turn) {
  const session = agent && agent.session
  if (!session || typeof session.eventAt !== 'function') return { question: '', answer: '' }
  const end = Number(session.seq) || 0
  const events = []
  for (let seq = end - 1; seq >= 0; seq -= 1) {
    const event = session.eventAt(seq)
    if (!event) continue
    if (event.type === 'turn/start' && Number(event.data && event.data.turn) === turn) break
    events.push(event)
  }
  events.reverse()
  const userParts = []
  const assistantParts = []
  for (const event of events) {
    if (event.type === 'user/message') {
      const message = event.data && event.data.message ? event.data.message : event.data
      if (!isPromptMessage(message)) continue
      const text = messageText(message)
      if (text) userParts.push(text)
    } else if (event.type === 'assistant/message') {
      const text = messageText(event.data)
      if (text) assistantParts.push(text)
    }
  }
  return {
    question: squash(userParts.join('\n'), 2000),
    answer: squash(assistantParts.join('\n'), 3000),
  }
}

/**
 * Mount the adapter.
 * @param {any} ctx Cordis context of the bundle row.
 * @param {{baseUrl?: string, agent?: string, lang?: string, prefetch?: boolean}} [rawConfig]
 */
export function apply(ctx, rawConfig) {
  const cfg = resolveConfig(rawConfig)
  const t = strings(cfg.lang)
  const net = new Net({ baseUrl: cfg.baseUrl, timeoutMs: cfg.timeoutMs, logger: ctx.logger })
  const book = new SessionBook()
  const projects = makeProjectCache(net, cfg)
  /** @type {Map<string, any>} */
  const agentsBySession = new Map()

  const notify = createNotifier({ file: cfg.logFile, logger: ctx.logger })
  const warn = (message) => notify('warn', message)
  const debug = (message) => notify('debug', message)
  const info = (message) => notify('info', message)

  // The session's project lives in the daemon's `session_project` table (the Hermes side
  // writes the same one); the book only caches it for as long as this process lives.
  const sticky = makeSticky({ net, cfg, book, tag: (raw) => tagSession(cfg, raw), warn })

  // ---- tunnel lifecycle --------------------------------------------------
  // Owned by this plugin activation: starts with the adapter, stops on unload. An
  // already-working tunnel is adopted instead of duplicated.

  let tunnel = null
  if (cfg.tunnel) {
    tunnel = new TunnelSupervisor({
      cfg,
      probe: () => net.health({ timeoutMs: 2500 }),
      notify,
    })
    ctx.effect(() => () => tunnel.stop())
    tunnel.start()
  }

  /** The live Agent for a session, including agents that predate a plugin reload. */
  function agentFor(raw) {
    const known = agentsBySession.get(raw)
    if (known) return known
    try {
      const registry = typeof ctx.get === 'function' ? ctx.get('agents') : undefined
      const live = registry && typeof registry.list === 'function' ? registry.list() : []
      for (const agent of live) {
        if (sessionIdOf(agent) === raw) {
          agentsBySession.set(raw, agent)
          return agent
        }
      }
    } catch {
      // A missing agents service is not fatal: extraction simply has no agent to ask.
    }
    return undefined
  }

  // ---- lifecycle ---------------------------------------------------------

  ctx.on('agent/created', ({ agent, source }) => {
    const raw = sessionIdOf(agent)
    if (!raw) return
    agentsBySession.set(raw, agent)
    // A brand-new entry starts with resetPrefetch=true (a fresh process cannot know what
    // the daemon's LRU still holds). A session this process already saw keeps its dedupe
    // state: resuming the same session is not a boundary, and resetting here would turn
    // every `#id 同上文` pointer back into a full re-injection.
    const state = book.get(raw)
    book.prune(256)
    debug(`agent ready for session ${raw} (${String(source || 'startup')}) reset=${state.resetPrefetch}`)
  })

  ctx.on('agent/disposed', ({ agent }) => {
    const raw = sessionIdOf(agent)
    if (!raw) return
    agentsBySession.delete(raw)
    // Keep the session's bookkeeping: a resumed agent continues the same daemon-side
    // recall state. `prune()` bounds the map instead of dropping it here.
  })

  // ---- turn boundaries ---------------------------------------------------

  if (cfg.prefetch || cfg.extract || cfg.reminder) {
    ctx.on('session/event', (session, event) => {
      try {
        const raw = session && session.id !== undefined ? String(session.id) : ''
        if (!raw || !event || typeof event.type !== 'string') return
        if (event.type === 'turn/start') {
          book.get(raw).wrote = false
          return
        }
        if (event.type === 'compaction/end') {
          book.get(raw).resetPrefetch = true
          debug(`compaction ended; recall state will reset for session ${raw}`)
          return
        }
        if (event.type === 'turn/end') {
          const turn = Number(event.data && event.data.turn) || 0
          scheduleExtraction(raw, turn, agentFor(raw))
        }
      } catch (error) {
        warn(`session/event handler failed (${error && error.message ? error.message : error})`)
      }
    })
  }

  // ---- prefetch (read-only, once per turn) --------------------------------

  if (cfg.prefetch) {
    ctx.on('agent/pre-step', async (payload, next) => {
      const decision = await next()
      try {
        const { agent, step, signal } = payload || {}
        if (!decision || decision.kind !== 'enter') return decision
        if (step !== 1 || (signal && signal.aborted)) return decision

        const raw = sessionIdOf(agent)
        if (!raw) return decision
        agentsBySession.set(raw, agent)
        const query = queryText(decision.messages)
        if (!query) return decision

        const state = book.get(raw)
        let data
        try {
          data = await net.post('/api/mem/recall', {
            query,
            session: tagSession(cfg, raw),
            reset: state.resetPrefetch === true,
            budget_tokens: cfg.budgetTokens,
          }, { timeoutMs: cfg.recallTimeoutMs, signal })
        } catch (error) {
          // Tunnel down / daemon down / turn cancelled: this turn simply has no memory.
          debug(`prefetch skipped (${error && error.kind ? error.kind : 'error'}): ${error && error.message ? error.message : error}`)
          return decision
        }

        const block = String((data && data.block) || '')
        const hint = cfg.projectHint ? String((data && data.hint) || '') : ''
        let text = block
        if (hint && block.trim()) text = block.replace(/\s+$/, '') + '\n\n' + hint
        else if (hint && !block.trim()) text = hint
        state.resetPrefetch = false
        if (!text.trim()) return decision

        return { ...decision, messages: [...decision.messages, recallMessage(text)] }
      } catch (error) {
        warn(`prefetch failed (${error && error.message ? error.message : error})`)
        return decision
      }
    })
  }

  // ---- turn-end extraction ----------------------------------------------

  async function runExtraction(raw, turn, agent) {
    const live = agent || agentsBySession.get(raw)
    if (!live) {
      debug(`turn-end skipped: no live agent for session ${raw}`)
      return
    }
    const state = book.get(raw)
    if (state.wrote) {
      debug(`turn-end skipped: the turn already wrote events (session ${raw})`)
      return
    }
    if (state.extracting) {
      debug(`turn-end skipped: an extraction is already running (session ${raw})`)
      return
    }
    const { question, answer } = turnExcerpt(live, turn)
    if (!question || !answer) {
      debug(`turn-end skipped: empty turn excerpt (session ${raw} turn ${turn} q=${question.length} a=${answer.length})`)
      return
    }
    if (isTrivial(question)) {
      debug(`turn-end skipped: trivial prompt (session ${raw})`)
      return
    }
    debug(`turn excerpt (session ${raw}): ${squash(question, 140)}`)

    state.extracting = true
    try {
      // A turn that wrote nothing still belongs to the session's project: read it from the
      // daemon (one call per session per process) instead of writing `unfiled`.
      await sticky.ensure(raw)
      const events = await extractEvents({ ctx, agent: live, cfg, t, question, answer })
      if (!events.length) {
        info(`extractor found nothing worth recording (session ${raw} turn ${turn})`)
        return
      }
      const sessionTag = tagSession(cfg, raw)
      for (const item of events.slice(0, cfg.maxEventsPerCall)) {
        const body = { kind: item.kind, text: redact(item.text), session: sessionTag, agent: cfg.agent }
        if (state.project) body.project = state.project
        try {
          const written = await net.post('/api/mem/events', body, { timeoutMs: cfg.timeoutMs })
          info(`turn-end recorded #${written && written.id} ${item.kind} (session ${raw})`)
        } catch (error) {
          warn(`turn-end write failed (${error && error.message ? error.message : error})`)
          return
        }
      }
    } catch (error) {
      // One local notice, no fallback event: a turn without a conclusion stays unrecorded.
      if (error instanceof ExtractError) {
        warn(`turn-end extraction failed (${error.reason})${error.detail ? ': ' + squash(error.detail, 200) : ''}`)
      } else {
        warn(`turn-end extraction failed (${error && error.message ? error.message : error})`)
      }
    } finally {
      state.extracting = false
    }
  }

  function scheduleExtraction(raw, turn, agent) {
    if (!cfg.extract) return
    info(`turn-end reached session=${raw} turn=${turn} agent=${agent ? 'live' : 'missing'}`)
    // Run off the append stack: `turn/end` is emitted synchronously while the turn closes.
    setTimeout(() => {
      runExtraction(raw, turn, agent).catch((error) => {
        warn(`turn-end extraction crashed (${error && error.message ? error.message : error})`)
      })
    }, 0)
  }

  // ---- memory console (the Hermes backstage, over the same daemon) -------

  registerConsole(ctx, cfg, net)

  // ---- tools and middleware ---------------------------------------------

  const disposeTools = registerTools(ctx, {
    net,
    cfg,
    book,
    t,
    redact,
    tag: (raw) => tagSession(cfg, raw),
    sessionIdOf,
    projects,
    sticky,
  })
  const disposeMiddleware = registerMiddleware(ctx, { cfg, book, t, sessionIdOf })

  // ---- warm-up (never blocks activation) --------------------------------

  net.health().then((health) => {
    if (!health) {
      if (cfg.tunnel) debug(`data plane not up yet at ${cfg.baseUrl}; the tunnel supervisor owns bringing it up`)
      else warn(`data plane unreachable at ${cfg.baseUrl} (tunnel down or daemon stopped); memory degrades silently`)
      return
    }
    if (Number(health.schema) !== 12) {
      warn(`daemon reports schema ${String(health.schema)}, expected 12 (old daemon code?)`)
    }
    projects.get().catch((error) => {
      debug(`project vocabulary warm-up failed (${error && error.message ? error.message : error})`)
    })
  }).catch(() => {})

  return () => {
    try { if (tunnel) tunnel.stop() } catch { /* fiber teardown */ }
    try { disposeMiddleware() } catch { /* fiber teardown */ }
    try { disposeTools() } catch { /* fiber teardown */ }
  }
}
