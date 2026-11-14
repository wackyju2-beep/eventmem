/**
 * eventmem / dsh — the "you just changed state, record it" reminder.
 *
 * Hermes' tool-name table is meaningless here, so this is rewritten against DSH's own
 * tools: file-mutation tools always count; shell tools count when the command actually
 * mutates something. The line is appended to the tool result's model-facing content via
 * `tools/post-execute` (the only waterfall that may rewrite a result), never as a new
 * session event and never for eventmem's own tools.
 */

/** Tools whose successful call always means the environment changed. */
const MUTATION_TOOLS = new Set([
  'write', 'edit', 'multi_edit', 'apply_patch', 'notebook_edit',
  'create_file', 'delete_file', 'move_file', 'str_replace_editor',
])

/** Shell tools: only a mutating command counts. */
const SHELL_TOOLS = new Set(['bash', 'pwsh', 'shell', 'powershell'])

const MUTATING_COMMAND = new RegExp([
  // filesystem
  '\\b(rm|rmdir|del|erase|mv|move|cp|copy|mkdir|touch|chmod|chown|ln|tee|truncate|dd)\\b',
  '\\bsed\\s+-i', '\\bperl\\s+-pi',
  // redirects / in-place writes
  '(^|[^>])>{1,2}\\s*[^&\\s]', '\\|\\s*tee\\b',
  // version control and package managers
  '\\bgit\\s+(commit|add|push|reset|checkout|switch|merge|rebase|stash|tag|clean)\\b',
  '\\b(npm|pnpm|yarn)\\s+(i|install|add|remove|uninstall|update|up)\\b',
  '\\bpip3?\\s+install\\b', '\\bapt(-get)?\\s+(install|remove|purge)\\b',
  // services and containers
  '\\bsystemctl\\s+(start|stop|restart|reload|enable|disable)\\b',
  '\\bdocker\\s+(run|build|rm|stop|start|restart|push)\\b',
  '\\bkubectl\\s+(apply|delete|create|patch|scale)\\b',
  // Windows
  '\\b(Remove-Item|New-Item|Set-Content|Add-Content|Out-File|Set-ItemProperty|Start-Process|Stop-Process|Move-Item|Copy-Item|Rename-Item)\\b',
  '\\bsc(\\.exe)?\\s+(create|delete|config|start|stop)\\b', '\\breg(\\.exe)?\\s+add\\b',
  '\\b(shutdown|reboot)\\b',
].join('|'), 'i')

/** @returns {string} the command text carried by a shell tool call */
function commandOf(args) {
  if (!args || typeof args !== 'object') return ''
  for (const key of ['command', 'cmd', 'script', 'code']) {
    if (typeof args[key] === 'string') return args[key]
  }
  return ''
}

/**
 * @param {string} name tool name
 * @param {unknown} args model arguments
 * @returns {boolean} whether this successful call likely changed durable state
 */
export function isStateChanging(name, args) {
  if (MUTATION_TOOLS.has(name)) return true
  if (SHELL_TOOLS.has(name)) {
    const command = commandOf(args)
    return Boolean(command) && MUTATING_COMMAND.test(command)
  }
  return false
}

/**
 * Attach the reminder to state-changing tool results.
 * @param {any} ctx
 * @param {{cfg: any, book: any, t: any, sessionIdOf: Function}} deps
 */
export function registerMiddleware(ctx, deps) {
  const { cfg, book, t, sessionIdOf } = deps
  if (!cfg.reminder) return () => {}

  const dispose = ctx.on('tools/post-execute', async (exec, result, next) => {
    const decision = await next()
    try {
      if (!decision || decision.kind !== 'accept') return decision
      if (result && result.isError) return decision
      if (decision.value !== undefined) return decision
      if (!isStateChanging(String(exec && exec.name ? exec.name : ''), exec && exec.arguments)) return decision

      const agent = exec && exec.agent
      const rawSession = agent ? sessionIdOf(agent) : ''
      if (!rawSession) return decision
      if (book.get(rawSession).wrote) return decision

      const content = Array.isArray(decision.content)
        ? decision.content
        : (result && Array.isArray(result.content) ? result.content : null)
      if (!content) return decision
      return { ...decision, content: [...content, { type: 'text', text: t.reminder }] }
    } catch {
      // A reminder must never fail a tool call.
      return decision
    }
  })
  return dispose
}
