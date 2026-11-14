/**
 * Best-effort secret scrub before anything leaves this machine for events.db.
 *
 * The host has no generic redactor to borrow (Hermes did), so this is deliberately
 * conservative: only shapes that are secrets by construction. It must never fail a
 * write — a false negative stores what the model sent, a false positive loses text.
 */

const RULES = [
  // Private key blocks: keep the header, drop the body.
  [/(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]*?(-----END [A-Z ]*PRIVATE KEY-----)/g, '$1 [redacted] $2'],
  // Provider key prefixes.
  [/\b(sk|rk)-[A-Za-z0-9_-]{12,}\b/g, '$1-***'],
  [/\bghp_[A-Za-z0-9]{20,}\b/g, 'ghp_***'],
  [/\bgho_[A-Za-z0-9]{20,}\b/g, 'gho_***'],
  [/\bgithub_pat_[A-Za-z0-9_]{20,}\b/g, 'github_pat_***'],
  [/\bAKIA[0-9A-Z]{16}\b/g, 'AKIA***'],
  [/\bxox[baprs]-[A-Za-z0-9-]{10,}\b/g, 'xox***'],
  // "name: value" / "name=value" for credential-shaped names.
  [
    /\b(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key|authorization)\b(\s*[:=]\s*)(["']?)[^\s"',;]{6,}\3/gi,
    '$1$2$3***$3',
  ],
  // Bearer credentials.
  [/\b(Bearer)\s+[A-Za-z0-9._~+/=-]{10,}/gi, '$1 ***'],
  // URL credentials: scheme://user:pass@host
  [/\b(https?:\/\/)[^\s/:@]+:[^\s/@]+@/gi, '$1***@'],
]

/** @param {unknown} text @returns {string} the same text with obvious secrets masked */
export function redact(text) {
  let out = typeof text === 'string' ? text : String(text ?? '')
  if (!out) return out
  for (const [pattern, replacement] of RULES) {
    try {
      out = out.replace(pattern, replacement)
    } catch {
      // A pathological regex state must not break a write.
    }
  }
  return out
}
