# eventmem memory for DeepSeek Harness

This plugin gives the Harness a shared, long-term event memory without giving it the
database. The library normally lives on the same machine; when the Harness runs somewhere
else it lives there instead, behind an SSH tunnel. In both cases the daemon exposes a thin
REST mirror of the core (`/api/mem/*`), and this plugin is a client of that mirror.

## What you get

- **Recall before each turn.** On the first step of a turn the plugin recalls against
  the user's message and injects the rendered block as one extra user-role message. The
  header and the `#id` line format are passed through untouched; the project hint line
  follows the block.
- **Seven tools.** `evt_add` (0–3 events per call, closed project vocabulary), `expand`,
  `mem_recall`, `mem_neighbors`, `proj_add`, `proj_list`, `evt_forget`.
- **Turn-end extraction.** A turn that never called `evt_add` gets one small auxiliary
  completion over (user message, reply); 0–3 parsed events are written. A failed call or
  unparsable answer writes nothing at all — only a local warning.
- **One session, one project.** A session's project is set once (the first event that
  names one) and then inherited, including by turn-end extraction. It is stored in the
  daemon's `session_project` table through `POST /api/mem/session/project`, so a plugin
  reload or Harness restart does not scatter the same conversation across `unfiled`.
- **A write reminder.** After a state-changing tool (file mutation, or a shell command
  that mutates) the tool result gets one line asking the model to record the change, so
  pure narration turns stop being the ones that vanish.
- **The memory console.** 记忆后台 sits in the sidebar directly under 插件: the eventmem
  daemon's own operator console (概览 / 后端 / 模型 / 调度 / 日志 / 图谱), rendered as a native
  Harness page instead of an iframe. It reads the daemon through the Host route
  `/api/eventmem`, which proxies only the console's own routes over the same tunnel. The
  page uses the host's theme tokens, so light/dark, type scale and locale follow the rest
  of the app; the sidebar entry uses a 16px stroke glyph in the same language as the
  built-in panel icons. A live strip sits above every tab (backend, this pass's pairs and
  edges, a `+N` flash per poll), the tiles count with digit flips, and 调度 is the same page
  Hermes has: 自动运行 (with the only pause switch), presets, candidate selection, 关系接受条件,
  历史关系重校准, 历史候选补算 — history previews are token-bound to their parameters.
- **The 3D graph, ported.** The 图谱 tab is the Hermes memory graph (lattice layout, orbit/pan,
  atom drag, right-press lasso, focus pull, filter facets, inspector) running in the same
  client module. It reads `GET /api/mem/graph` and writes through the other
  `/api/mem/graph/*` routes, so editing an atom re-embeds it, re-marks `edges.cross`, and
  marks a candidate reseat in the daemon — no second data path, no direct SQL from the
  browser.

## How the identity is carried

Every event carries `agent: "dsh"`, so a shared database can still answer "who recorded
this". Session ids are written as `dsh:<session id>`; the daemon keys both the per-turn
dedupe state and the project stickiness by that string, so two hosts with a same-named
session never steal each other's state.

`reset: true` is sent only when "what this session already showed" is no longer true:
the first recall of a live agent (which also covers resume and a restarted process) and
the recall right after a compaction. Ordinary turns do **not** reset, which is what makes
the second turn of a session show `#id 同上文` pointers instead of repeating the text.

## The tunnel it owns

`tunnel: true` makes the plugin responsible for the SSH forwarding rather than relying on
an external supervisor. On activation it probes the data plane: if something already
answers, that tunnel is adopted; otherwise it starts `ssh -N -L …` itself and confirms the
forwarding a few seconds later. If that ssh dies, it is respawned with bounded backoff; on
plugin unload the ssh it started is terminated. A tunnel it did not start is never killed.
The spawned ssh keeps its stdin as an open pipe, so a hard-killed Harness makes it exit on
EOF instead of leaving an orphan (verified).

## Failure behaviour

The tunnel is a single point of failure, so every path is built to lose memory rather
than a turn:

- prefetch: any failure returns the unmodified step decision;
- turn-end extraction: any failure logs one warning and writes nothing;
- tools: return a structured `{"error": ...}` the model can read (an explicit tool call
  is allowed to report that memory is unreachable).

## Boundaries it respects

- The plugin never imports the core, never opens `events.db`, never starts a worker and
  never calls `POST /api/run-once`; one database keeps one nightly worker.
- Recall ranking, edge judgement and skeleton budgeting stay in the daemon.
- The cross-machine data plane has no link endpoint, so batch-internal `links` are
  validated (backwards-only) and reported as skipped rather than written.

## Configuration

The mounted row is named `eventmem` and lives in the bundle's `cordis.patch.yml`. The
only field most setups touch is `baseUrl` (the local end of the SSH tunnel) and `agent`.
