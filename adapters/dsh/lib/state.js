/**
 * eventmem / dsh — per-session bookkeeping.
 *
 * One Harness process runs many sessions (and subagents) concurrently, so every piece
 * of mutable state here is keyed by the session id and never by "the current session".
 */

/** @typedef {{project: string, projectLoaded: boolean, projectDirty: boolean, wrote: boolean, resetPrefetch: boolean, extracting: boolean}} SessionState */

export class SessionBook {
  constructor() {
    /** @type {Map<string, SessionState>} */
    this.bySession = new Map()
  }

  /** @param {string} id @returns {SessionState} */
  ensure(id) {
    let state = this.bySession.get(id)
    if (state === undefined) {
      state = {
        project: '',
        // The daemon's `session_project` table is the authority; these two flags say
        // whether this process has read it yet, and whether a write still needs retrying.
        projectLoaded: false,
        projectDirty: false,
        wrote: false,
        resetPrefetch: true,
        extracting: false,
      }
      this.bySession.set(id, state)
    }
    return state
  }

  /** @param {string} id @returns {SessionState} */
  get(id) {
    return this.ensure(id)
  }

  /**
   * Bound the map without losing a live session's dedupe state: eviction order is
   * insertion order, so the oldest untouched session goes first.
   * @param {number} max
   */
  prune(max) {
    while (this.bySession.size > max) {
      const oldest = this.bySession.keys().next()
      if (oldest.done) return
      this.bySession.delete(oldest.value)
    }
  }
}
