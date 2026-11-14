/**
 * eventmem / dsh — the local notice channel.
 *
 * "A failed extraction records no memory, only one local notice." That notice has to be
 * visible when the Harness console is not: this appends one line per event to a small
 * rotating file (the Harness logger is always written too, when present).
 */

import { appendFileSync, renameSync, statSync } from 'node:fs'

/**
 * @param {{file?: string, logger?: any, maxBytes?: number}} options
 * @returns {(level: 'debug'|'info'|'warn', message: string) => void}
 */
export function createNotifier(options) {
  const file = typeof options.file === 'string' ? options.file : ''
  const logger = options.logger
  const maxBytes = Number(options.maxBytes) > 0 ? Number(options.maxBytes) : 256 * 1024

  return function notify(level, message) {
    const line = `${new Date().toISOString()} ${level} eventmem: ${message}`
    try {
      const method = logger && typeof logger[level] === 'function' ? logger[level] : undefined
      if (method) method.call(logger, `eventmem: ${message}`)
    } catch {
      // Logging must never fail a turn.
    }
    if (!file) return
    try {
      let size = 0
      try { size = statSync(file).size } catch { /* first write */ }
      if (size > maxBytes) {
        try { renameSync(file, `${file}.1`) } catch { /* keep appending */ }
      }
      appendFileSync(file, line + '\n', 'utf8')
    } catch {
      // A read-only temp directory is not a reason to lose a turn.
    }
  }
}
