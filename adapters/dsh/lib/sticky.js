/**
 * eventmem / dsh — "one session, one project", persisted in the daemon.
 *
 * The per-session project is the adapter's job (the data plane deliberately does not
 * classify events), but the *answer* must outlive this process: a resumed session, a
 * reloaded plugin or a restarted Harness must not drop the same conversation's later
 * turns into `unfiled` or let the model re-roll the project every turn.
 *
 * So the authority is the daemon's `session_project` table — the same one Hermes writes —
 * reached through `POST /api/mem/session/project`. `SessionBook`'s copy is a cache:
 * loaded once per session per process, written back whenever the session's project
 * changes. Every failure degrades to "no inherited project", never to a failed turn.
 */

/**
 * @param {{net: any, cfg: any, book: any, tag: (raw: string) => string, warn: (message: string) => void}} deps
 */
export function makeSticky({ net, cfg, book, tag, warn }) {
  /** One line of context for a swallowed error. */
  const reason = (error) => (error && error.message ? error.message : String(error))

  return {
    /**
     * The session's project, reading the daemon's copy the first time this process asks.
     * Never throws: a dead tunnel means this turn simply inherits nothing.
     * @param {string} raw session id (without the agent prefix)
     * @returns {Promise<string>}
     */
    async ensure(raw) {
      if (!raw) return ''
      const state = book.get(raw)
      // A write that failed earlier is pushed again before anything else, so a transient
      // tunnel outage cannot roll the session back to its old project.
      if (state.projectDirty) await this.save(raw, state.project)
      if (state.projectLoaded) return state.project
      state.projectLoaded = true
      try {
        const data = await net.post('/api/mem/session/project',
          { session: tag(raw) }, { timeoutMs: cfg.timeoutMs, retry: 1 })
        const name = String((data && data.project) || '').trim()
        if (name) state.project = name
        return state.project
      } catch (error) {
        // Retry next time: a read is cheap, and this process may still be starting up.
        state.projectLoaded = false
        warn(`session project read failed (${reason(error)})`)
        return state.project
      }
    },

    /**
     * Record the session's project (empty string = no project). The in-memory value
     * changes even when the write fails, and the failed write is retried by `ensure`.
     * @param {string} raw @param {string} name
     * @returns {Promise<string>}
     */
    async save(raw, name) {
      if (!raw) return ''
      const state = book.get(raw)
      const value = String(name || '').trim()
      state.project = value
      state.projectLoaded = true
      try {
        await net.post('/api/mem/session/project',
          { session: tag(raw), project: value || null }, { timeoutMs: cfg.timeoutMs })
        state.projectDirty = false
      } catch (error) {
        state.projectDirty = true
        warn(`session project write failed (${reason(error)})`)
      }
      return value
    },
  }
}
