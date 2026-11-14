/**
 * eventmem / dsh — the seven tools, their schemas and their dispatch.
 *
 * Every handler is a thin translation layer over one `/api/mem/*` endpoint: validate what
 * the model sent, then let the daemon decide. Batch limits, project stickiness, the
 * closed project vocabulary and redaction live here because the data plane deliberately
 * does not do them. Links are validated but not written: the cross-machine data plane
 * mirrors `add_event` only and has no edge endpoint.
 */

import { KINDS, RELS, UNFILED } from './text.js'

/** Raw JSON-Schema tool definitions: no `defineTool` import is possible from a linked bundle. */
function define(name, description, parameters, run) {
  return {
    name,
    description,
    parameters,
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: String(value) }],
    },
    async execute(args, exec) {
      const input = args && typeof args === 'object' ? args : {}
      try {
        return JSON.stringify(await run(input, exec))
      } catch (error) {
        const name0 = error && error.name ? error.name : 'Error'
        const message = error && error.message ? error.message : String(error)
        return JSON.stringify({ error: `${name0}: ${message}` })
      }
    },
  }
}

function asInt(value) {
  if (value === null || value === undefined || value === '') return null
  const n = Number(value)
  return Number.isInteger(n) ? n : null
}

/** A short-lived project vocabulary cache: one HTTP round trip per batch, not per entry. */
export function makeProjectCache(net, cfg) {
  let rows = []
  let at = 0
  return {
    async get(force = false, timeoutMs = 0) {
      if (!force && at !== 0 && Date.now() - at < 20000) return rows
      const data = await net.get('/api/mem/projects', { timeoutMs: timeoutMs || cfg.timeoutMs })
      rows = Array.isArray(data && data.projects) ? data.projects : []
      at = Date.now()
      return rows
    },
    peek() { return rows },
    invalidate() { at = 0 },
  }
}

/**
 * Register all seven tools on the plugin context.
 * @param {any} ctx
 * @param {{net: any, cfg: any, book: any, t: any, redact: Function, tag: Function, sessionIdOf: Function, projects: any, sticky: any}} deps
 */
export function registerTools(ctx, deps) {
  const { net, cfg, book, t, redact, tag, sessionIdOf, projects, sticky } = deps
  const limit = cfg.maxEventsPerCall

  const evtAdd = define('evt_add', t.evtAdd(limit), {
    type: 'object',
    properties: {
      events: {
        type: 'array',
        minItems: 0,
        maxItems: limit,
        description: t.pEvents,
        items: {
          type: 'object',
          properties: {
            kind: { type: 'string', enum: [...KINDS], description: t.pKind },
            text: { type: 'string', description: t.pText },
            project: { type: 'string', description: t.pProject },
            artifact: { type: 'string', description: t.pArtifact },
            artifact_kind: { type: 'string', description: t.pArtifactKind },
            path: { type: 'string', description: t.pPath },
            extra_projects: {
              type: 'array', items: { type: 'string' }, description: t.pExtraProjects,
            },
            links: {
              type: 'array',
              description: t.pLinks,
              items: {
                type: 'object',
                properties: {
                  rel: { type: 'string', enum: [...RELS], description: t.pRel },
                  index: { type: 'integer', minimum: 1, description: t.pIndex },
                  event_id: { type: 'integer', description: t.pEventId },
                  conf: { type: 'number', minimum: 0, maximum: 1, description: t.pConf },
                },
                required: ['rel'],
              },
            },
          },
          required: ['kind', 'text'],
        },
      },
    },
    required: ['events'],
  }, async (args, exec) => {
    const items = args.events
    if (items === undefined || items === null) return { ok: true, count: 0, note: t.noteNothing }
    if (!Array.isArray(items)) return { error: t.errEventsArray }
    if (items.length > limit) return { error: t.errTooMany(limit, items.length) }
    if (items.length === 0) return { ok: true, count: 0, note: t.noteNothing }

    const rawSession = sessionIdOf(exec.agent)
    // One session, one project: the sticky default lives in the daemon's table, so this
    // is where a resumed session gets its project back (one read per session per process).
    if (rawSession) await sticky.ensure(rawSession)
    const st = rawSession ? book.get(rawSession) : { project: '' }
    let rows
    try {
      rows = await projects.get(true)
    } catch (error) {
      return { error: t.unavailable(error && error.message ? error.message : String(error)) }
    }
    const known = new Map(rows.map((row) => [String(row.name), row]))

    /** @type {Array<any>} */
    const prepared = []
    for (let pos = 1; pos <= items.length; pos += 1) {
      const item = items[pos - 1]
      if (!item || typeof item !== 'object' || Array.isArray(item)) return { error: t.errEntryObject(pos) }
      const kind = String(item.kind ?? '').trim()
      const text = String(item.text ?? '').trim()
      if (!KINDS.includes(kind)) return { error: t.errKind(pos, kind), kinds: [...KINDS] }
      if (!text) return { error: t.errText(pos) }

      const resolved = resolveProject(item, known, st.project, pos, t)
      if (resolved.error) return resolved.error
      const extras = resolveExtras(item, known, pos, t)
      if (extras.error) return extras.error

      const artifact = String(item.artifact ?? '')
      prepared.push({
        kind,
        text: redact(text),
        project: resolved.project,
        extras: extras.names,
        artifact: artifact ? redact(artifact) : '',
        artifactKind: String(item.artifact_kind ?? '其他').trim() || '其他',
        path: String(item.path ?? '').trim() || null,
        links: Array.isArray(item.links) ? item.links : [],
      })
    }

    const sessionTag = rawSession ? tag(rawSession) : undefined
    const written = []
    for (const item of prepared) {
      const body = { kind: item.kind, text: item.text, agent: cfg.agent }
      if (sessionTag) body.session = sessionTag
      if (item.project) body.project = item.project
      if (item.extras.length) body.extra_projects = item.extras
      if (item.artifact) {
        body.artifact = item.artifact
        body.artifact_kind = item.artifactKind
      }
      if (item.path) body.path = item.path
      try {
        const response = await net.post('/api/mem/events', body, { timeoutMs: cfg.timeoutMs })
        written.push({
          id: response && response.id,
          kind: item.kind,
          text: item.text,
          project: item.project || UNFILED,
          artifact: Boolean(item.artifact),
        })
      } catch (error) {
        return {
          error: `write failed: ${error && error.message ? error.message : String(error)}`,
          written,
          known_projects: [...known.keys()],
        }
      }
    }

    const sessionProject = written.filter((row) => row.project !== UNFILED).map((row) => row.project).pop()
    if (sessionProject) await sticky.save(rawSession, sessionProject)
    if (written.length > 0) st.wrote = true

    // Links: validated against the batch, never written (no edge endpoint cross-machine).
    let skipped = 0
    const edgeErrors = []
    for (let pos = 0; pos < prepared.length; pos += 1) {
      for (const link of prepared[pos].links) {
        const check = validateLink(link, pos, prepared.length, written[pos] && written[pos].id)
        if (check.error) edgeErrors.push(`#${written[pos] && written[pos].id}: ${check.error}`)
        else skipped += 1
      }
    }

    const result = { ok: true, count: written.length, events: written }
    if (sessionProject) result.session_project = sessionProject
    if (skipped > 0) result.notes = [t.noteLinksSkipped(skipped)]
    if (edgeErrors.length) result.edge_errors = edgeErrors
    return result
  })

  const expand = define('expand', t.expand, {
    type: 'object',
    properties: { event_id: { type: 'integer', description: t.pEventId } },
    required: ['event_id'],
  }, async (args) => {
    const eventId = asInt(args.event_id ?? args.id)
    if (eventId === null) return { error: t.errEventId }
    try {
      return await net.get(`/api/mem/events/${eventId}`, { timeoutMs: cfg.timeoutMs })
    } catch (error) {
      if (error && error.kind === 'http' && error.status === 404) return { error: t.errNoEvent(eventId) }
      return { error: t.unavailable(error && error.message ? error.message : String(error)) }
    }
  })

  const neighbors = define('mem_neighbors', t.neighbors, {
    type: 'object',
    properties: {
      event_id: { type: 'integer', description: t.pEventId },
      rels: { type: 'array', items: { type: 'string', enum: [...RELS] }, description: t.pRels },
      depth: { type: 'integer', minimum: 1, maximum: 3, description: t.pDepth },
    },
    required: ['event_id'],
  }, async (args) => {
    const eventId = asInt(args.event_id ?? args.id)
    if (eventId === null) return { error: t.errEventId }
    let rels
    if (typeof args.rels === 'string') {
      rels = args.rels.split(/[,/\s]+/).filter(Boolean)
    } else if (Array.isArray(args.rels)) {
      rels = args.rels.map((rel) => String(rel)).filter(Boolean)
    }
    if (rels && rels.length) {
      const bad = rels.find((rel) => !RELS.includes(rel))
      if (bad) return { error: `rel=${JSON.stringify(bad)} is not one of ${RELS.join(' / ')}` }
    } else {
      rels = undefined
    }
    const depth = Math.min(3, Math.max(1, asInt(args.depth) ?? 1))
    const body = { event_id: eventId, depth }
    if (rels) body.rels = rels
    try {
      return await net.post('/api/mem/neighbors', body, { timeoutMs: cfg.timeoutMs })
    } catch (error) {
      if (error && error.kind === 'http' && error.status === 404) return { error: t.errNoEvent(eventId) }
      return { error: t.unavailable(error && error.message ? error.message : String(error)) }
    }
  })

  const recall = define('mem_recall', t.recall, {
    type: 'object',
    properties: {
      query: { type: 'string', description: t.pQuery },
      budget_tokens: { type: 'integer', minimum: 100, maximum: 4000, description: t.pBudget },
    },
    required: ['query'],
  }, async (args) => {
    const query = String(args.query ?? '').trim()
    if (!query) return { error: t.errQueryEmpty }
    const budget = asInt(args.budget_tokens)
    const body = { query }
    if (budget !== null) body.budget_tokens = Math.max(100, Math.min(budget, 4000))
    try {
      // No session: an explicit tool call wants the full block, not dedupe pointers.
      const data = await net.post('/api/mem/recall', body, { timeoutMs: cfg.recallTimeoutMs })
      return {
        query,
        context: String((data && data.block) || ''),
        found: Boolean(String((data && data.block) || '').trim()),
        count: Number((data && data.count) || 0),
      }
    } catch (error) {
      return { error: t.unavailable(error && error.message ? error.message : String(error)) }
    }
  })

  const projAdd = define('proj_add', t.projAdd, {
    type: 'object',
    properties: { name: { type: 'string', description: t.pName } },
    required: ['name'],
  }, async (args, exec) => {
    const name = String(args.name ?? '').replace(/\s+/g, ' ').trim()
    if (!name) return { error: t.errNameEmpty }
    if (name === UNFILED) return { error: t.errNameReserved }
    if (name.length > 60) return { error: t.errNameLong }
    try {
      const data = await net.post('/api/mem/projects', { name }, { timeoutMs: cfg.timeoutMs })
      const project = (data && data.project) || {}
      const archived = project.status === 'archived' || project.archived === true
      projects.invalidate()
      const rawSession = sessionIdOf(exec.agent)
      if (!archived && rawSession) await sticky.save(rawSession, name)
      const out = {
        ok: true, id: project.id, name: project.name || name,
        existed: Boolean(project.existed), session_project: archived ? undefined : name,
      }
      if (archived) out.note = t.noteArchived
      return out
    } catch (error) {
      return { error: t.unavailable(error && error.message ? error.message : String(error)) }
    }
  })

  const projList = define('proj_list', t.projList, { type: 'object', properties: {} }, async () => {
    try {
      const rows = await projects.get(true)
      return {
        projects: rows.map((row) => ({
          id: row.id,
          name: row.name,
          status: row.status || 'active',
          event_count: row.event_count,
        })),
        count: rows.length,
        note: t.noteProjectList,
      }
    } catch (error) {
      return { error: t.unavailable(error && error.message ? error.message : String(error)) }
    }
  })

  const forget = define('evt_forget', t.forget, {
    type: 'object',
    properties: { event_id: { type: 'integer', description: t.pEventId } },
    required: ['event_id'],
  }, async (args) => {
    const eventId = asInt(args.event_id ?? args.id)
    if (eventId === null) return { error: t.errEventId }
    // The data plane reports `removed: true` even for a missing id (the core's forget is
    // idempotent), so check existence first or the model is told a lie.
    try {
      await net.get(`/api/mem/events/${eventId}`, { timeoutMs: cfg.timeoutMs })
    } catch (error) {
      if (error && error.kind === 'http' && error.status === 404) return { error: t.errNoEvent(eventId) }
      return { error: t.unavailable(error && error.message ? error.message : String(error)) }
    }
    try {
      const data = await net.post(`/api/mem/events/${eventId}/forget`, {}, { timeoutMs: cfg.timeoutMs })
      if (data && data.removed === false) return { error: t.errNoEvent(eventId) }
      return { ok: true, forgotten: eventId }
    } catch (error) {
      if (error && error.kind === 'http' && error.status === 404) return { error: t.errNoEvent(eventId) }
      return { error: t.unavailable(error && error.message ? error.message : String(error)) }
    }
  })

  const disposers = [
    ctx.tools.register(evtAdd),
    ctx.tools.register(expand),
    ctx.tools.register(neighbors),
    ctx.tools.register(recall),
    ctx.tools.register(projAdd),
    ctx.tools.register(projList),
    ctx.tools.register(forget),
  ]
  return () => {
    for (const dispose of disposers) {
      try { dispose() } catch { /* already disposed with the fiber */ }
    }
  }
}

/** Resolve one entry's project: sticky default, explicit existing name, or unfiled. */
function resolveProject(item, known, inherited, pos, t) {
  if (!('project' in item) || item.project === null || item.project === undefined) {
    const sticky = String(inherited || '')
    const row = sticky ? known.get(sticky) : undefined
    if (row && row.status !== 'archived') return { project: String(row.name) }
    return { project: '' }
  }
  const raw = String(item.project).trim()
  if (!raw || raw === UNFILED) return { project: '' }
  const row = known.get(raw)
  if (!row) {
    return {
      error: {
        error: t.errUnknownProject(pos, raw),
        known_projects: [...known.keys()],
        hint: t.hintProject,
      },
    }
  }
  if (row.status === 'archived') return { error: { error: t.errArchivedProject(pos, raw), known_projects: [...known.keys()] } }
  return { project: String(row.name) }
}

/** Resolve `extra_projects` against the same closed vocabulary. */
function resolveExtras(item, known, pos, t) {
  const raw = item.extra_projects
  if (!Array.isArray(raw)) return { names: [] }
  const names = []
  for (const value of raw) {
    const name = String(value ?? '').trim()
    if (!name || name === UNFILED || names.includes(name)) continue
    const row = known.get(name)
    if (!row) return { error: { error: t.errUnknownExtra(pos, name), known_projects: [...known.keys()] } }
    if (row.status === 'archived') return { error: { error: t.errArchivedExtra(pos, name), known_projects: [...known.keys()] } }
    names.push(String(row.name))
  }
  return { names }
}

/**
 * Validate one batch-internal link the way the data plane's DAG rule would: only
 * backwards in time. The edge itself is not written (no endpoint cross-machine).
 */
function validateLink(link, pos, total, newId) {
  if (!link || typeof link !== 'object' || Array.isArray(link)) return { error: 'link is not an object' }
  const rel = String(link.rel ?? '').trim()
  if (!RELS.includes(rel)) return { error: `invalid rel=${JSON.stringify(rel)}` }
  if (link.index !== undefined && link.index !== null) {
    const index = asInt(link.index)
    if (index === null || index < 1 || index > total) return { error: `index=${String(link.index)} is out of range` }
    if (index - 1 >= pos) return { error: `index=${index} must point to an earlier event in this batch` }
    return { rel }
  }
  if (link.event_id !== undefined && link.event_id !== null) {
    const target = asInt(link.event_id)
    if (target === null) return { error: 'event_id is not an integer' }
    if (newId !== undefined && newId !== null && target >= newId) {
      return { error: `event_id=${target} is not older than this event; links only go back` }
    }
    return { rel }
  }
  return { error: 'link needs index or event_id' }
}
