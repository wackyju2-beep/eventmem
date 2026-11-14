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

## Start here: Windows and DSH on one machine

**Installing this bundle installs only the DSH client, not the eventmem daemon, database,
models, or worker.** First install and start the separate daemon, then add the client. The
diagram above illustrates the optional remote SSH setup, not a prerequisite for
single-machine use. Open **记忆后台 → 连接** after installing: it shows which parts are
working and which are optional, and it can test and save the connection settings.

1. Install DSH, Git, and Python 3.11+ on Windows. SQLite must include FTS5 and its trigram
   tokenizer. In PowerShell, clone the public repository and run its Windows installer:

   ```powershell
   git clone https://github.com/wackyju2-beep/eventmem.git D:\eventmem
   cd D:\eventmem
   .\install.cmd
   ```

   If Python is missing, the installer may install Python 3.12 with `winget`. It creates
   a virtual environment, installs core and the optional vector runtime, starts the daemon
   at `127.0.0.1:9123`, sets up login autostart, and runs a self-check. The default data
   directory is `%USERPROFILE%\.hermes\eventmem`, or `%HERMES_HOME%\eventmem` when set.
   Newly generated or updated launch commands explicitly bind to loopback even if
   `EVENTMEM_DAEMON_HOST` is set; an already-running daemon may use an older command, so
   verify its listener. If the installer warns that `daemon.cmd` needs an update, first
   check which processes and autostart entries will be affected: `-Uninstall` forcibly
   stops matching `python.exe` processes whose command lines contain `run.py`, `daemon`,
   and the configured database path, including other services using that database. It also
   deletes the `eventmem-daemon` scheduled task and Startup shortcut with that name;
   the data is kept. After confirming it is safe, run `.\install.cmd -Uninstall` in
   PowerShell from the repository. The script silently ignores task deletion and process-stop
   errors, so do not rely on its success message: check that the target PIDs, fixed-name
   scheduled task and Startup shortcut are actually gone. Then rerun `.\install.cmd` to
   regenerate the launcher and check its actual listening address.
   Pass `-NoPythonInstall`, `-NoVector`, or `-NoAutostart` to opt out; see the
   [repository quick start](https://github.com/wackyju2-beep/eventmem/blob/main/README.md).
2. In DSH choose **Plugins → Add plugin**, then select
   `D:\eventmem\adapters\dsh` from the clone. The mounted configuration row is `eventmem`.
   Installing the row is enough for the default single-machine setup: `mode: local`,
   `baseUrl: http://127.0.0.1:9123`, no SSH. The defaults contain no private host name.

   Open **记忆后台 → 连接** to see the dependency status and change the connection:
   choose **本机记忆** or **远程记忆**, fill the daemon address (and an SSH alias for remote),
   click **测试连接**, then **保存并重新加载**. The page writes through DSH's profile config
   editor, so you do not edit YAML by hand. It also shows the optional arms separately:
   vector retrieval needs the ONNX model, and relation edges need a decision backend.

   If the profile cannot be edited (the editor service is absent), a higher-level profile
   patch is still available; see the example below. `config` is replaced as a whole, not
   merged, so preserve the other fields from the bundle's `cordis.patch.yml` when writing
   the replacement.
3. Confirm the daemon and intended database in PowerShell:

   ```powershell
   Invoke-RestMethod http://127.0.0.1:9123/api/health | Format-List
   ~\.hermes\eventmem\venv\Scripts\python.exe tools\selfcheck.py
   ```

   The adapter verifies more than "HTTP 200 with JSON": `/api/health` must report
   `service: eventmem-daemon`, `api: 1`, a numeric `schema`, and (when configured) the
   expected `db_id`. A different service on the same port is rejected before any write.
   The 连接 page shows the same checks. If the daemon was started with a token, set the
   adapter's `tokenFile` to the generated `daemon.token`; the page reads the file and never
   writes the token into configuration. Then use **记忆后台** to inspect the service.

Basic storage and literal recall need no relation judge. Turn-end extraction needs a
working DSH host model; `auxProvider` and `auxModel` can be set when the session has no
model selection. Semantic recall additionally needs the vector runtime **and** the
Qwen3-Embedding-0.6B ONNX `tokenizer.json` and `model_int8.onnx` under
`<HERMES_HOME>\eventmem\models` (default `%USERPROFILE%\.hermes\eventmem\models`).
Automatic relations need a running daemon and a configured decision backend; use the
memory console to configure one or download a model. See
[backend setup](https://github.com/wackyju2-beep/eventmem/blob/main/docs/backends.md).

### Profile configuration example

Prefer **记忆后台 → 连接**. If you must edit the profile by hand, back up the **active
profile's** `cordis.patch.yml` first. DSH stores it under
`<DSH_HOME>/profiles/<profile>/cordis.patch.yml`; on Windows `DSH_HOME` defaults to
`%USERPROFILE%\.dsh` unless the `DSH_HOME` environment variable is set. `desktop` is an
example profile name, not necessarily your active profile; consult your DSH profile
configuration documentation to identify it. Preserve all existing rows and comments; if
the file is just `[]`, replace that empty list with the sequence below. If it already has
an `eventmem` override, update that row instead of adding a duplicate ID.

For a local daemon, append this top-level sequence item to the profile patch:

```yaml
- id: eventmem
  name: dsh-eventmem
  config:
    mode: local
    baseUrl: http://127.0.0.1:9123
    agent: dsh
    lang: zh
```

The override replaces the entire `config`, so retain the fields above and any other custom
fields you need. For a remote daemon use:

```yaml
- id: eventmem
  name: dsh-eventmem
  config:
    mode: remote
    baseUrl: http://127.0.0.1:9124
    tunnelHost: my-server
    tunnelLocalPort: 9124
    tunnelRemotePort: 9123
    agent: dsh
    lang: zh
```

`tunnelHost` must be a working passwordless SSH alias. If local port 9123 is occupied,
change **both** `baseUrl`'s port and `tunnelLocalPort` (e.g. 9124); keep `tunnelRemotePort`
aligned with the remote daemon. A higher-priority layer can override these values. The
same-ID replacement and non-merge behaviour were verified with `dump-config` in an
isolated profile; the 连接 page's save path is tested against a config-editor stub, not an
active GUI session; confirm the effective configuration and connection in your own host.

### Linux daemon or shared remote database

For Linux/Hermes installation and daemon startup, follow the
[repository Linux quick start](https://github.com/wackyju2-beep/eventmem/blob/main/README.md);
`install.sh` links the Hermes adapter but does **not** start the daemon. Run the remote
daemon against the same database used by Hermes, with one daemon per database. Remote use
requires adapter configuration `mode: remote` plus `tunnelHost` set to a working passwordless
SSH alias for that host; defaults are `baseUrl: http://127.0.0.1:9123` and local/remote
ports `9123`. Prefer the 连接 page, or ask someone familiar with profile patches to make the
override as noted above.

**Avoid silently selecting the wrong database:** a local daemon answering on port 9123 can
still be adopted when the remote mode points at the same local port. The probe now validates
the service id, API version, and (when configured) `db_id`, so a different service is
rejected, but a *different eventmem database* on the expected endpoint needs `expectDbId`.
The 连接 page shows the connected `db_id`; paste it into **预期数据库 ID** to pin it. Either
stop the local daemon and remove its autostart after checking the effects of
`.\install.cmd -Uninstall` as described above (it can stop other matching services; the
database is kept), or use an unused local port, for example
`baseUrl: http://127.0.0.1:9124` and `tunnelLocalPort: 9124`, keeping `tunnelRemotePort:
9123` if that is the remote daemon's port. Do not place SQLite/WAL on a network share. Use
only one daemon per database: Windows lacks the daemon's `flock` single-instance guard, so
do not rely on it to prevent another daemon from opening the same database.

**Security:** the daemon binds loopback by default. Its management API can require a bearer
token (`--token` or `--token-file`); when a token is set, every route except the minimal
health probe requires it, and a non-loopback bind without a token is refused at startup.
Loopback without a token is still a trust boundary: any local process or user that can reach
the port can read and edit the database. Enable a token on shared machines, keep the daemon
on loopback, and use SSH for remote access; do not expose it to an untrusted network.
`EVENTMEM_DAEMON_HOST` can change the daemon's default bind address when no explicit
`--host` is supplied, but the token guard still applies. SSH `-L` listening also depends on
the client's `GatewayPorts` setting, so verify the actual forwarded listener is
loopback-only. See the [data-plane API](https://github.com/wackyju2-beep/eventmem/blob/main/docs/api.md).

## What it does

| Hook | Behaviour |
|---|---|
| `agent/pre-step` (step 1 only) | one recall for the turn; `block` injected verbatim as a user-role message, `hint` appended after it |
| `session/event` `turn/start` | clears the "this turn already wrote" flag |
| `session/event` `turn/end` | if the turn wrote nothing, an auxiliary extraction (temperature 0, default 800-token cap, 60 s) writes 0–3 events; a capped empty response is retried once at double the cap |
| `session/event` `compaction/end` | next recall sends `reset: true` |
| `tools/post-execute` | after a state-changing tool, appends one reminder line to the result content |
| seven tools | `evt_add`, `mem_recall`, `expand`, `mem_neighbors`, `proj_add`, `proj_list`, `evt_forget` |

### Privacy: what leaves the machine

- Recall and tool calls send the query/tool arguments to the **daemon** (loopback or your
  own SSH tunnel). The daemon owns the database; the adapter never reads `events.db`.
- Turn-end extraction asks the **DSH host's configured model** to turn one excerpt of the
  conversation into 0–3 events. That model may be remote, so the excerpt leaves this
  machine and may incur cost. `auxProvider`/`auxModel` only choose which host model is used.
- Before a turn excerpt goes to the extractor, `lib/redact.js` masks shapes that are secrets
  by construction (private-key blocks, common provider key prefixes, `password: …`,
  `Bearer …`, URL credentials). It is **pattern-based best-effort**, not a secret detector:
  it can miss secrets and can over-redact ordinary text. Treat the extraction model as a
  recipient you chose, and avoid putting secrets in a turn you expect to be remembered.
- Events are written after a second redaction pass on the extractor's output. The daemon
  stores whatever was sent; there is no encryption at rest beyond the filesystem.

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

`mode: remote` (or the older `tunnel: true`) makes this plugin own the SSH forwarding. It is
**not** the default; a machine that runs its daemon locally never spawns `ssh`.

- on activation it probes `baseUrl` at `/api/health`; only a payload that identifies itself
  as `eventmem-daemon` with API `1` and a valid schema is **adopted**. A local daemon or an
  existing forward (autossh, manual `ssh -N`, or a leftover) is accepted; a different
  service that happens to answer on the port is not;
- otherwise it spawns `ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o
  ServerAliveInterval=30 -o ServerAliveCountMax=3 -L <local>:127.0.0.1:<remote> <host>`,
  re-probes with the same identity check, and respawns with bounded backoff if ssh exits;
- on plugin unload (disable, uninstall, reload) the ssh **it started** is terminated. An
  adopted tunnel is left alone, because it is not ours.

Cleanup also survives a hard kill: the ssh is spawned with its stdin as an open pipe, so
when the Harness process disappears the pipe closes and ssh exits on EOF (verified: a
child ssh with a dead parent is gone within seconds). A tunnel the plugin did **not**
start — autossh, a systemd unit, a manual `ssh -N` — is adopted and left alone; use
`mode: local` in that setup so the plugin never touches it.

## Install

The bundle is this directory (`D:\eventmem\adapters\dsh`). Install it into the running
profile with **Plugins → Add plugin** (pick the directory), or:

```
plugin_manager action=install_bundle target=D:\eventmem\adapters\dsh
```

The bundle's `cordis.patch.yml` mounts one row named `eventmem`; config lives there.

**One machine (no SSH).** Run the repository's `install.cmd` first: it invokes
`install.ps1` to install and start the daemon on `127.0.0.1:9123`. Activation probes
`baseUrl`, and an endpoint that passes the identity check is adopted, so no `ssh` is
spawned. The default `mode: local` prevents SSH attempts if that daemon stops. If the
installer was run with `-RequireToken`, open the 连接 page and set `tokenFile` to
`%HERMES_HOME%\eventmem\daemon.token` (the installer prints the exact path).

## Config (row `config`)

| Field | Default | Meaning |
|---|---|---|
| `mode` | `local` | `local` = no SSH; `remote` = own the SSH forwarding (older rows may use `tunnel` alone) |
| `baseUrl` | `http://127.0.0.1:9123` | local daemon endpoint, or the local end of the SSH tunnel; never bind the daemon to `0.0.0.0` |
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
| `token` / `tokenFile` | — | bearer token for a token-protected daemon; `tokenFile` keeps the secret out of configuration |
| `expectDbId` | — | optional `/api/health` `db_id`; a mismatch blocks every write |
| `tunnel` | `false` | legacy switch; `mode: remote` is the readable form |
| `tunnelHost` | — | ssh destination (host alias from `~/.ssh/config`); required only in remote mode |
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
