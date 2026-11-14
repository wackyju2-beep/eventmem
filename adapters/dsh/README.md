# dsh-eventmem — the DSH adapter for eventmem

DSH runs on Windows; the event memory database runs wherever the daemon runs — on the same
machine, or on another one reachable over SSH. This bundle is a
**pure HTTP client** for the daemon's `/api/mem/*` data plane: it does not import
`eventmem_core`, does not touch `events.db`, and does not re-implement recall or edge
judgement. Every memory decision stays in the daemon; this side only translates host
events into data-plane calls.

```
Windows (DSH)                                  Mint
┌──────────────────────────┐   ssh -L 9123   ┌───────────────────────────┐
│ dsh-eventmem             │ ──────────────▶ │ eventmem daemon :9123     │
│  prefetch / tools /      │   127.0.0.1     │  /api/mem/* → EventMem    │
│  turn-end extraction     │                 │              → events.db  │
└──────────────────────────┘                 └───────────────────────────┘
```

## What it does

| Hook | Behaviour |
|---|---|
| `agent/pre-step` (step 1 only) | one recall for the turn; `block` injected verbatim as a user-role message, `hint` appended after it |
| `session/event` `turn/start` | clears the "this turn already wrote" flag |
| `session/event` `turn/end` | if the turn wrote nothing, one auxiliary extraction (temperature 0, ≤500 tokens, 60 s) writes 0–3 events |
| `session/event` `compaction/end` | next recall sends `reset: true` |
| `tools/post-execute` | after a state-changing tool, appends one reminder line to the result content |
| seven tools | `evt_add`, `mem_recall`, `expand`, `mem_neighbors`, `proj_add`, `proj_list`, `evt_forget` |

Three rules the code never bends:

1. every write carries `"agent": "dsh"` (the configurable `agent` name);
2. every session id is `dsh:<session id>` — the daemon's per-session dedupe and the
   project stickiness table are keyed by it;
3. `reset: true` only at real boundaries (first recall of an agent, after compaction),
   never on an ordinary turn — otherwise the `#id 同上文` pointers never appear.

### One session, one project

A session's project is decided once and then belongs to that session: the first event that
names one (an `evt_add` with `project`, or `proj_add`) sets the session's default, and every
later turn-end extraction inherits it. The answer is **not** kept in memory only — it lives
in the daemon's `session_project` table (the same one Hermes writes), through
`POST /api/mem/session/project`:

- read once per session per process, before the session's first write or extraction;
- written back whenever the session's project changes;
- a failed write is retried on the next turn instead of silently rolling the session back.

Without this, a plugin reload or a Harness restart would drop the sticky project and the
second half of a conversation would land in `unfiled` (or, worse, in whatever project the
model happened to pick that turn). New sessions still start with no project; `proj_add` /
`evt_add` is how one is born, exactly as in Hermes.

And one hard requirement: **no memory failure reaches the user**. If the tunnel is down,
the daemon stopped, or the auxiliary call fails, prefetch returns no injection, the
turn ends normally, and the only trace is one line in the log.

## The memory console

The sidebar entry **记忆后台** sits directly under 插件 and opens the daemon's operator
console as a native DSH page: 概览 / 后端 / 模型 / 调度 / 日志 / 图谱. It is deliberately not an
iframe — the browser calls `/api/eventmem?path=…` on the DSH origin, and `lib/console.js`
proxies only the console's own routes to the daemon over the tunnel this adapter already
owns, so the daemon's loopback stays on the Mint box and the page follows the host theme,
type scale and locale. The sidebar glyph is the host's own `database-outline` icon
(16 px, 1 px stroke), so the row reads as part of the built-in navigation.

Two things it does the way Hermes does them, because they are the parts an operator
actually watches:

- **A live strip on every tab.** It names the backend the pass is using, counts the pairs
  and edges of *this* pass, and flashes `+N` after a poll in which N pairs were judged
  (a 1.5 s poll during a pass, 5 s when idle). The 待判 tile counts down
  `planned − judged`, and every number on the page flips digit by digit instead of
  snapping — unchanged digits stay put.
- **A schedule page, not a settings form.** 自动运行 (mode, interval/time, pause — the only
  pause switch), 预设, 候选选择与成本, 关系接受条件 (margin + the five per-relation minimum
  confidences, with its own save), 历史关系重校准, 历史候选补算, 说明. A history action never
  writes future settings, and saving future settings never starts a history job — the
  preview's token is bound to the exact parameters, so changing a box invalidates it.

### 图谱 (the 3D graph)

The last tab is the Hermes memory graph, rendered natively: atoms live on a 3D lattice
(newest at the centre), drag orbits/pans, a drag on an atom moves it, a long right-press
lassoes a selection, wheel flies the camera, and clicking an atom opens its inspector.
Edges are background texture until an atom is selected: selecting one lights up its two
neighbourhood layers plus the edges incident to it, and those can then be clicked directly
to walk the graph (an edge click opens the relation inspector). A click anywhere else only
drops the focus. The filter panel floats above the canvas, so the first click outside it
only closes the panel — the next click does the selection. Filters are facets (project /
session / date / kind / relation / ids) plus an optional embedding search, and every human
correction — text, kind, project, relations — is written back through `/api/mem/graph/*`,
so the daemon re-embeds the vector, re-marks `edges.cross`, and leaves a reseat marker for
the next link pass. The kind/relation palette is data encoding and stays fixed; everything
around the canvas (bar, filter panel, inspectors, legend) is DSH markup and `--dsw-*` tokens.

## The tunnel follows the Harness

`tunnel: true` (the default) makes this plugin own the SSH forwarding:

- on activation it probes `baseUrl`; if something already answers, that tunnel is
  **adopted** (autossh, a manual `ssh -N`, or a leftover from a hard kill) and nothing is
  duplicated;
- otherwise it spawns `ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o
  ServerAliveInterval=30 -o ServerAliveCountMax=3 -L <local>:127.0.0.1:<remote> <host>`,
  re-probes to confirm, and respawns with bounded backoff if ssh exits;
- on plugin unload (disable, uninstall, reload) the ssh **it started** is terminated. An
  adopted tunnel is left alone, because it is not ours.

Cleanup also survives a hard kill: the ssh is spawned with its stdin as an open pipe, so
when the Harness process disappears the pipe closes and ssh exits on EOF (verified: a
child ssh with a dead parent is gone within seconds). A tunnel the plugin did **not**
start — autossh, a systemd unit, a manual `ssh -N` — is adopted and left alone; use
`tunnel: false` in that setup so the plugin never touches it.

## Install

The bundle is this directory (`D:\eventmem\adapters\dsh`). Install it into the running
profile with **Plugins → Add plugin** (pick the directory), or:

```
plugin_manager action=install_bundle target=D:\eventmem\adapters\dsh
```

The bundle's `cordis.patch.yml` mounts one row named `eventmem`; config lives there.

**One machine (no SSH).** Run the repository's `install.ps1` first: it installs the daemon and
serves it on `127.0.0.1:9123`. The default config then works unchanged — activation probes
`baseUrl`, and a daemon that already answers is adopted, so no `ssh` is spawned. Set
`tunnel: false` when the plugin should never touch ssh at all.

## Config (row `config`)

| Field | Default | Meaning |
|---|---|---|
| `baseUrl` | `http://127.0.0.1:9123` | local end of the SSH tunnel; never bind the daemon to `0.0.0.0` |
| `agent` | `dsh` | the `agent` written on every event, and the session prefix |
| `lang` | `zh` | tool descriptions / extractor prompt language (`zh` or `en`) |
| `timeoutMs` | `8000` | per-request write/read timeout |
| `recallTimeoutMs` | `10000` | prefetch budget for one turn |
| `auxTimeoutMs` | `60000` | turn-end extraction call budget |
| `auxMaxTokens` | `800` | extraction output cap (reasoning tokens count toward it; one automatic retry at double the cap if the first attempt is capped with no text) |
| `budgetTokens` | `600` | recall injection budget |
| `maxEventsPerCall` | `3` | `evt_add` batch cap and extraction cap |
| `prefetch` / `extract` / `reminder` / `projectHint` | `true` | switch each piece off independently |
| `auxProvider` / `auxModel` | — | only needed when the session has no model selection |
| `logFile` | `%TEMP%\dsh-eventmem.log` | local notice log (rotation at 256 KiB); `""` disables the file and keeps the Harness logger |
| `tunnel` | `true` | own the SSH forwarding (start on activation, stop on unload); `false` leaves it to autossh/systemd |
| `tunnelHost` | `mint` | ssh destination (host alias from `~/.ssh/config`) |
| `tunnelLocalPort` / `tunnelRemotePort` | `9123` / `9123` | must match `baseUrl`'s port on this side |
| `tunnelSsh` | `ssh` | executable, if `ssh` is not on PATH |
| `tunnelArgs` | `[]` | extra `ssh` arguments appended after the defaults |
| `tunnelIntervalMs` | `30000` | supervision interval and backoff ceiling |

## Notes on the dedupe state

`reset: true` is sent on the first recall this process performs for a session and after
`compaction/end`. Resuming the same session later — a subagent woken for another turn, a
session reopened on an existing agent — is **not** a boundary: the session bookkeeping
survives agent disposal (bounded to 256 sessions), so the second turn still gets the
`#id 同上文` pointer form. A restarted Harness process starts with an empty book and
therefore re-injects full text once, which is the safe direction.

## Known gaps (by design, not bugs)

- **No edge writes.** The data plane mirrors `add_event` only; there is no link endpoint.
  `evt_add` still validates `links` (direction: only earlier events; index inside the
  batch) and reports them as skipped — the nightly worker links by session/artifact.
- **Recall blocks carry no source label.** `agent` is visible through `expand(#id)`; the
  rendered block format is the user's contract and is not decorated here.
- **No second worker.** This adapter never starts a daemon and never calls
  `POST /api/run-once`; one database keeps exactly one nightly worker
  (`eventmem-worker` on the Mint side).
- **Delegated child sessions are sessions too.** Each subagent gets its own
  `dsh:<id>`, its own recall, and its own turn-end extraction.

See `NOTES-DSH-RUNTIME.md` for the four environment facts this adapter was built against
(process model, turn boundaries, auxiliary model, tool callbacks) and where each was
verified in the installed Harness.
