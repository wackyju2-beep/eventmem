# Historical recalibration safety

`POST /api/recalibrate/preview` and `/api/recalibrate/apply` return the existing
`token`, `added`, `removed`, and `skipped` fields plus:

- `uncovered_jev_edges` (integer): existing `decided_by='jev'` edges whose ordered
  `(src,dst)` has no `pair_evals` row. This counts edges, not pairs, includes edges
  with stored probabilities, and excludes non-Jev edges. They are not recalibrated.
- `manual_deletion_pairs` (integer): archived pairs skipped due to explicit manual
  deletion evidence; included in `skipped`. Unarchived pairs are not counted here.
- `legacy_deletions_unknown` (boolean, always `true`): prior manual deletions
  cannot be reconstructed reliably. An absent tombstone does not establish that
  an old pair was never manually edited/deleted. Apply is not a guarantee that
  unrecorded historical deletions will be preserved.

Schema v9 adds `edge_deletions(src,dst,rel,ts)` and an insert guard. Store and
HTTP graph edge deletions and replacements record evidence in the same transaction
as deletion. Once any edge in an ordered pair is deleted manually, future Jev
inserts for that pair are ignored and recalibration skips the whole archived pair;
manual edge writes remain possible. There is no automatic evidence reset or
legacy backfill. Recalibration fingerprints evidence so an intervening deletion
invalidates preview. Existing store opens and graph adapter requests perform their
normal idempotent migrations; deploy only after backing up and authorizing the
production migration. Already-running old code can bypass these protections until
reloaded, and uninstrumented direct SQL deletion does not create evidence.
