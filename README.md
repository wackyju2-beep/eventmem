# eventmem

[![tests](https://github.com/wackyju2-beep/hermes-eventmem/actions/workflows/tests.yml/badge.svg)](https://github.com/wackyju2-beep/hermes-eventmem/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

**English** · [中文](README.zh-CN.md)

Event-shaped memory for [Hermes Agent](https://github.com/NousResearch/hermes-agent). One SQLite
file, no API key, and a 4B model running on the CPU deciding how your memories relate to each other.

I wrote it for my own agent, on a mini PC, because chunk memory kept handing me paragraphs where
half the sentences had already been replaced by something newer. Nothing in the store said which
half. eventmem writes that down as data: this event changed that one, on this date, with this
probability.

![The memory graph](docs/img/graph-default.png)

## The unit is an event

An event is a timestamp, a kind, and one sentence.

```
[11-05] decision  the mini PC uses a 256G SSD as its system disk
[11-06] change    the mini PC switched to a 2T NVMe system disk    -> changes the line above
```

There are five kinds: `decision / attempt / result / change / lesson`. Events are append-only. You
never edit history, you append a correction to it. Commands, configs, logs and code go into
artifacts, stored once by content hash and referenced from the event, so the event sentence stays
one line.

When recall hits the old event it follows the edge and renders it as `[superseded by 11-06]`. The
model does not have to work out that the 256G number is stale. It is told.

## What is actually different

Most memory layers for agents do three things: split text, embed it, fetch top-k. eventmem keeps
that for retrieval and adds a layer on top that most of them skip: a typed, directed correction
between two events.

Four choices keep it cheap enough for one person's machine.

Relations are judged by a small model on the CPU. Nothing leaves the machine, and no GPU is
involved. What I measured is llama.cpp serving a 4B decision model, one forward pass per pair, about
6.7 s on six CPU threads and 16–18 s on two. It
answers two independent questions in that pass: what the later event does to the earlier one
(repeat / change / negate / neither), and whether it also explains it (because / refines / neither).

The candidate set stays bounded. A new event is only compared against active heads, meaning events
that nothing has changed, negated or repeated yet. Four sources feed the list: vector top-10, the
same session's recent heads, anything sharing the same artifact or the same file path, and
cross-project pairs when both ends are changes or decisions. A few context sets are added for the
second question. 42 events produce 113 pairs under this rule; the earlier "everything within seven
days" rule produced 861 pairs on the same corpus, and the gap widens as the library grows.

Judgements are kept even when they don't produce an edge. The model's raw distribution goes into `edges.probs`,
and pairs it judged unrelated go into `pair_evals`. Changing a threshold later is a SQL query, not
another night of inference. The graph draws judged-but-unlinked pairs as faint dashed lines, so you
can see what the model considered and rejected.

You can look at all of it. `mem_neighbors 62` returns what #62 replaced, what replaced it, and the
events around it in the same session. The desktop app draws the whole thing as a 3D graph: boxes are
events, curves are corrections, and the inspector shows the probability distribution behind each
line. If a relation looks wrong you can change it by hand; edited items get a rainbow rim.

None of this is heavy. One SQLite file holds everything. The vector arm is optional and silently
degrades to FTS5 when onnxruntime is missing. The judge is optional too: without it you get events
and recall, just no edges. There is one background process, and it only wakes up when there are new
events.

## Install

Put the files where Hermes can see them:

```bash
git clone https://github.com/wackyju2-beep/hermes-eventmem ~/hermes-eventmem
cd ~/hermes-eventmem && ./install.sh          # symlinks into ~/.hermes/{plugins,desktop-plugins}
```

Or copy the three directories yourself:

```
plugins/eventmem               -> ~/.hermes/plugins/eventmem
plugins/eventmem-graph         -> ~/.hermes/plugins/eventmem-graph                (graph backend, optional)
desktop-plugins/eventmem-graph -> ~/.hermes/desktop-plugins/eventmem-graph        (graph UI, optional)
```

Then switch it on:

```bash
hermes config set memory.provider eventmem    # only one external provider at a time
hermes plugins enable eventmem-graph          # only for the graph
```

It takes effect in a new session. The database lives at `~/.hermes/eventmem/events.db` and is
created on the first write (schema v7).

## Running the judge (optional)

Edges are built by a local decision model. `plugins/eventmem/worker.py` is a resident process that
checks for new events every 60 s; `plugins/eventmem/bin/nightly.sh --since-hours 24` does one pass
and exits.

The worker talks HTTP to any OpenAI-compatible `/v1/chat/completions` endpoint that can answer a
four-way choice with probabilities (`EVENTMEM_JEV_URL`, default `http://127.0.0.1:18090`). If the
service is down it runs `EVENTMEM_START_SCRIPT`, if you set one, otherwise it logs a warning. Any
local server will do; mine is llama.cpp with StartLux 4B Instant, quantised to Q8, CPU only.

The model weights are not in this repository. The one I measured is CC BY-NC 4.0.

## Vector arm (optional)

```bash
pip install onnxruntime tokenizers numpy
```

Drop a Qwen3-Embedding-0.6B ONNX into `~/localmem/models/` (512-dim, int8, named
`model_int8.onnx`, `model_quantized.onnx` or `model.onnx`; `model_dir` can be passed explicitly).
Missing anything just means FTS-only, silently.

## Using it

The model calls the tools; you normally don't. Manually:

```
mem_recall "system disk migration"     # hybrid semantic + literal search
mem_neighbors 88                       # what #88 replaced / what replaced it / same-session
expand 88                              # the full artifact text behind event #88
proj_list                              # projects
evt_add [{"kind":"变更","text":"…","project":"mini-pc"}]   # write one yourself
```

Kind and relation names are stored and accepted in Chinese: `决定/尝试/结果/变更/教训` for kinds,
`改变/否定/重复/因为/细化` for relations. The English words in this README are labels for reading.
The tools take the stored strings, and the decision model is prompted in Chinese.

Every turn the model also gets a line like
`[eventmem] projects (by relevance to this turn): eventmem / mini-pc / lab-notes`. The candidates are
scored against the centroid of each project's event vectors, so the model knows what already exists
before it picks a project or decides to create one.

The graph lives in the desktop app: the `记忆图谱` chip at the right of the status bar (left of the
context meter), or the `/eventmem-graph` route. The UI itself is Chinese for now.

| Action | Result |
|---|---|
| Left-drag on empty space / wheel | orbit / zoom |
| Middle-drag | pan |
| Click a box | the camera flies to it and the box is pulled forward; edit its text and kind on the right |
| Click an edge | both endpoints are pulled to the left and right of the screen; probabilities, relation edit and delete on the right |
| Left-drag a box | put it wherever you like (positions are saved and survive a refresh) |
| Click empty space | drops focus only, nothing snaps back |
| Filtering | non-matching atoms grey out and recede; either matches only, or matches plus their one-hop neighbours |

![One atom focused](docs/img/graph-atom.png)
![One edge focused](docs/img/graph-edge.png)

## How a turn is written

Two paths, both of them the model's job.

A tool-execution middleware appends one reminder line to every state-changing tool result, so the
model writes its event in the same turn instead of at the end of the conversation, when the reason
is already gone. At turn end, if nothing was written, `sync_turn` asks an auxiliary model to pull
0–3 events out of the exchange. Plain Q&A turns are recorded that way too; small talk is filtered
before the call.

Recall runs the other way round. A skeleton pass gives the shape of each project once per session
(oldest decision, newest decision, newest result, newest change, newest lesson). The hits pass does
vector + FTS5 with RRF, adds one hop along the correction edges, and adds same-session neighbours.
Anything already shown this session collapses into a pointer line, `#63 #60 see above`.

## Layout

| Path | Responsibility |
|---|---|
| `plugins/eventmem/schema.py` | DDL, migrations, two FTS5 trigram tables + sync triggers |
| `plugins/eventmem/store.py` | every SQL statement: events, projects, edges, artifacts, cursors, forget |
| `plugins/eventmem/recall.py` | vector + FTS -> RRF -> two-stage packing -> grouped rendering |
| `plugins/eventmem/embed.py` | Qwen3-Embedding-0.6B ONNX encoder (512-dim, int8) + degradation check |
| `plugins/eventmem/jev.py` | local decision-model client (two questions, asks for probabilities) |
| `plugins/eventmem/nightly.py` | pick candidates, ask the model, write edges by margin, advance the cursor |
| `plugins/eventmem/worker.py` | resident process: compute when something changed, sleep otherwise |
| `plugins/eventmem/i18n.py` | language resolution (`display.language`) + the zh/en string switch |
| `plugins/eventmem/__init__.py` | `MemoryProvider` + tool middleware + seven tools + end-of-turn extraction |
| `plugins/eventmem-graph/` | graph backend (FastAPI) reading and writing the same database |
| `desktop-plugins/eventmem-graph/` | graph UI: one `plugin.js`, the host injects React |
| `docs/architecture.md` | data model, write path, recall algorithm, edge-building details (Chinese) |

## Tests

Plain scripts, no pytest:

```bash
cd plugins/eventmem
for t in tests/test_*.py; do python3 "$t"; done
```

```
test_store.py    PASS 31
test_recall.py   PASS 33
test_plugin.py   PASS 43
test_nightly.py  PASS 93
test_worker.py   PASS 3
test_i18n.py     PASS 9
```

212 checks, each case on its own temporary database, nothing touches `~/.hermes`. Recall tests use
`FakeEncoder`, so they pass without numpy or onnx. `test_plugin.py` needs the host's
`agent.memory_provider`; on a machine without Hermes (CI, for instance) it swaps in the stub in
`tests/_host_stub.py`, which is why the suite is 212/212 on a bare interpreter too.

## Known limitations

- No migration. Chunks in another memory store do not become events. Accumulation starts when you
  switch it on.
- The model picks the project. Tools reject names that do not exist, but a wrong-but-real project is
  not caught. Fix it by eye in the graph.
- Relation quality is the model's quality. The 4B Instant model I use is weaker than its
  thinking-mode sibling on pair judgement (41.5 vs 59.9 on DecisionPivot), which is why the
  thresholds and the prompt wording matter more than the model choice.
- The thresholds (vector 0.35, margin 0.15, budgets 600/850) were set from a small corpus. Re-measure
  once there are a few thousand events.
- The graph UI and the stored kind/relation names are Chinese. The model-facing text is bilingual.

## License

MIT. See `LICENSE`. Use it, change it, ship it — including commercially; just keep the copyright
notice.

Third-party weights are not distributed here. The embedding and decision models carry their own
licenses; the decision model I measured is CC BY-NC 4.0.
