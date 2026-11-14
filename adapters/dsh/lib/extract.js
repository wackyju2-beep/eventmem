/**
 * eventmem / dsh — turn-end extraction: ask the small model what to remember.
 *
 * A pure Q&A turn never calls a tool, so `evt_add` never runs. After `turn/end` this
 * asks the session's own model (temperature 0, ≤500 tokens, 60 s) for 0–3 events and
 * writes them through the data plane. A failed call or unparsable answer produces **no
 * memory and no mechanical fallback event** — only one local warning.
 */

import { KINDS } from './text.js'

export const CALL_FAILED = 'call_failed'
export const UNPARSABLE = 'unparsable'
export const EMPTY = 'empty'

/** The auxiliary call failed or its output was not the JSON we asked for. */
export class ExtractError extends Error {
  constructor(reason, detail = '') {
    super(detail ? `${reason}: ${detail}` : reason)
    this.name = 'ExtractError'
    this.reason = String(reason || CALL_FAILED)
    this.detail = String(detail || '')
  }
}

/** Collapse whitespace (and optionally truncate) before handing a turn to the model. */
export function squash(text, limit = 0) {
  let out = String(text ?? '').replace(/\s+/g, ' ').trim()
  if (limit && out.length > limit) out = out.slice(0, Math.max(0, limit - 1)) + '…'
  return out
}

/** "Nothing worth remembering" gate: greetings, bare confirmations, slash commands. */
export function isTrivial(text) {
  const compact = String(text ?? '').replace(/\s+/g, '')
  if (!compact) return true
  if (compact.startsWith('/')) return true
  return compact.length <= 10
    && /^(好|好的|好哒|嗯+|哦+|噢+|谢谢|谢了|收到|在吗|你好|您好|嗨|hi|hello|hey|ok|okay|thanks|thankyou|yes|no|对|是|行|可以|没事)[。.!！?？~～…]*$/i.test(compact)
}

/**
 * Parse the extractor's JSON: `[]` for "nothing to record", `null` when unusable.
 * @param {string} raw @param {number} limit
 * @param {{error?: string, length?: number, tail?: string}} [diagnostics] filled on failure
 */
export function parseEvents(raw, limit = 3, diagnostics) {
  let text = String(raw ?? '').trim()
  if (!text) {
    if (diagnostics) diagnostics.error = 'empty output'
    return null
  }
  if (text.startsWith('```')) {
    text = text.replace(/^```[A-Za-z]*\s*/, '').replace(/\s*```$/, '')
  }
  const start = text.indexOf('{')
  const end = text.lastIndexOf('}')
  if (start < 0 || end <= start) {
    if (diagnostics) diagnostics.error = 'no JSON object in the answer'
    return null
  }
  let payload
  try {
    payload = JSON.parse(text.slice(start, end + 1))
  } catch (error) {
    if (diagnostics) {
      diagnostics.error = error && error.message ? error.message : String(error)
      diagnostics.length = text.length
      diagnostics.tail = text.slice(-120)
    }
    return null
  }
  const items = payload && typeof payload === 'object' ? payload.events : undefined
  if (!Array.isArray(items)) {
    if (diagnostics) diagnostics.error = 'the JSON has no events array'
    return null
  }
  const out = []
  for (const item of items) {
    if (!item || typeof item !== 'object') continue
    const kind = String(item.kind ?? '').trim()
    const body = String(item.text ?? '').trim()
    if (KINDS.includes(kind) && body) out.push({ kind, text: body })
    if (out.length >= limit) break
  }
  return out
}

/** Resolve the route for one auxiliary call: the session's own model, else configured. */
function resolveRoute(agent, cfg) {
  try {
    const selection = agent && agent.model && typeof agent.model.current === 'function'
      ? agent.model.current()
      : undefined
    if (selection && selection.provider && selection.model) {
      return { provider: String(selection.provider), model: String(selection.model) }
    }
  } catch {
    // A disposed agent loses its model; fall through to the configured route.
  }
  if (cfg.auxProvider && cfg.auxModel) return { provider: cfg.auxProvider, model: cfg.auxModel }
  return undefined
}

/**
 * One auxiliary completion over (user, assistant) and the parsed event list.
 * @param {{ctx: any, agent: any, cfg: any, t: any, question: string, answer: string}} input
 * @returns {Promise<Array<{kind: string, text: string}>>}
 * @throws {ExtractError} when the route, the call, or the JSON fails
 */
export async function extractEvents({ ctx, agent, cfg, t, question, answer }) {
  const route = resolveRoute(agent, cfg)
  if (!route) throw new ExtractError(CALL_FAILED, 'no provider/model route for the auxiliary call')
  if (!ctx.llm || typeof ctx.llm.stream !== 'function') {
    throw new ExtractError(CALL_FAILED, 'the llm service is not mounted')
  }

  /**
   * One auxiliary attempt. `text-delta` streams the answer while `block-end` repeats the
   * whole block, so keeping both would concatenate two JSON copies.
   * @returns {Promise<{text: string, finish: string}>}
   */
  async function callOnce(maxTokens) {
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(new Error('eventmem extraction timed out')), cfg.auxTimeoutMs)
    let deltas = ''
    let blocks = ''
    let finishKind = ''
    try {
      const stream = ctx.llm.stream({
        provider: route.provider,
        model: route.model,
        system: t.extractSystem,
        messages: [{ role: 'user', content: [{ type: 'text', text: t.extractUser(question, answer) }] }],
        temperature: 0,
        maxTokens,
        signal: controller.signal,
        purpose: 'eventmem-extract',
      })
      for await (const chunk of stream) {
        if (chunk && chunk.type === 'finish' && chunk.reason && chunk.reason.kind) {
          finishKind = String(chunk.reason.kind)
          if (finishKind === 'error' || finishKind === 'aborted') {
            const failure = chunk.reason.failure
            throw new ExtractError(CALL_FAILED, failure
              ? `${failure.code || 'LLM_FAILURE'}: ${failure.message || finishKind}`
              : finishKind)
          }
          continue
        }
        if (chunk && chunk.type === 'text-delta' && typeof chunk.text === 'string') deltas += chunk.text
        else if (chunk && chunk.type === 'block-end' && chunk.block && chunk.block.type === 'text') {
          blocks += typeof chunk.block.text === 'string' ? chunk.block.text : ''
        }
      }
    } catch (error) {
      if (error instanceof ExtractError) throw error
      throw new ExtractError(CALL_FAILED, `${error && error.name ? error.name : 'Error'}: ${error && error.message ? error.message : error}`)
    } finally {
      clearTimeout(timer)
    }
    return { text: deltas.length > 0 ? deltas : blocks, finish: finishKind }
  }

  let attempt = await callOnce(cfg.auxMaxTokens)
  // A reasoning model spends output tokens before it writes anything; hitting the cap
  // with no visible text is not "nothing to record", so retry once with a larger cap.
  if (!attempt.text.trim() && attempt.finish === 'max-tokens') {
    attempt = await callOnce(Math.min(8000, Math.max(cfg.auxMaxTokens * 2, cfg.auxMaxTokens + 500)))
  }
  if (!attempt.text.trim()) {
    throw new ExtractError(EMPTY, `the auxiliary model returned no text (finish=${attempt.finish || 'unknown'})`)
  }

  const diagnostics = {}
  const parsed = parseEvents(attempt.text, cfg.maxEventsPerCall, diagnostics)
  if (parsed === null) {
    const detail = [
      diagnostics.error || 'unparsable',
      `finish=${attempt.finish || 'unknown'}`,
      `chars=${diagnostics.length === undefined ? attempt.text.length : diagnostics.length}`,
      diagnostics.tail ? `tail=${squash(diagnostics.tail, 120)}` : '',
    ].filter(Boolean).join(' · ')
    throw new ExtractError(UNPARSABLE, detail)
  }
  return parsed
}
