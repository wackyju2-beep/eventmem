# Reproducible eventmem evaluation

Two tiny, frozen fixtures that make a change's effect visible without a model, a network,
or a GPU:

```bash
python benchmarks/run.py            # human-readable summary
python benchmarks/run.py --json     # machine-readable report
python benchmarks/run.py --out report.json
```

- **Recall** writes `benchmarks/data/recall.json` into a temporary SQLite database with the
  encoder disabled, calls `EventMem.recall` exactly as the daemon does (FTS5 + optional
  vector arm), parses the rendered `#id` lines, and reports hit@k, full@k and MRR.
- **Relations** runs a deliberately simple lexical baseline over
  `benchmarks/data/relations.json` and reports micro precision/recall/F1 plus per-label
  counts. It is a **regression baseline**, not a claim about the real decision model: swap
  in a recorder (`benchmarks/run.py --pairs ...`) to compare a real backend later.

The fixtures are small on purpose. They are the fixed ruler, not a quality claim: a change
that moves these numbers should be explained, and a larger human-labelled set is still
needed for real accuracy claims.
