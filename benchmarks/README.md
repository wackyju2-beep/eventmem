# Reproducible eventmem evaluation

Two tiny, frozen fixtures that make a change's effect visible without a model, a network,
or a GPU:

```bash
python benchmarks/run.py            # human-readable summary
python benchmarks/run.py --json     # machine-readable report
python benchmarks/run.py --out report.json
```

## What is measured

- **Recall** (`benchmarks/data/recall.json`: 16 events, 5 queries, one project) writes the
  fixture into a temporary SQLite database with the encoder disabled, calls `EventMem.recall`
  exactly as the daemon does (FTS5 + optional vector arm), parses the rendered `#id` lines,
  and reports hit@k, full@k and MRR.
- **Relations** (`benchmarks/data/relations.json`: 13 pairs, 6 labels) runs a deliberately
  simple keyword + character-bigram classifier and reports accuracy, macro-F1 and per-label
  precision/recall/F1. It exists so a change to the fixtures or the label schema shows up as a
  diff, and so the label distribution stays visible.

## What this does not measure

- **Not the relation model.** The relation half never calls a backend. It shares only the label
  vocabulary with the shipped judge, which is a two-axis `choice` request (change/negate/repeat
  axis plus because/refine detail, with `重复` suppressing the detail axis), candidate
  pre-selection and per-label confidence thresholds. The reported accuracy (0.846) and macro-F1
  (0.743) belong to the lexical baseline and must not be quoted as eventmem's relation accuracy.
- **Not semantic recall.** The encoder is disabled, so only the literal/FTS arm is scored.
  Paraphrase, synonym and cross-language recall — the reason the ONNX arm exists — is untested.
- **Not scale.** 16 events and 5 queries: one fixture row moves hit@k by 20 points, and with
  k=5 out of 16 events a hit is cheap. A production database with hundreds of events is not
  represented.
- **Not the whole loop.** Only the ranking of the rendered block is scored — not latency, cost,
  the agent/daemon path, or how later edge building changes what recall returns.

The fixtures are small on purpose: they are the fixed ruler, not a quality claim. A change that
moves these numbers should be explained, and a larger human-labelled set is still needed for
real accuracy claims.
