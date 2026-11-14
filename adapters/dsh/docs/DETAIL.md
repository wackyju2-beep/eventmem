# eventmem memory for DeepSeek Harness

This plugin is a **client only**: it does not install or start the separate eventmem
daemon, database, or models. The daemon owns the database and exposes `/api/mem/*`; it may
run on the same machine as DSH or on another machine reached over SSH. Both agents must
use the same database to share memory.

## Get started on Windows

1. Install DSH, Git, and Python 3.11+ (SQLite needs FTS5 and its trigram tokenizer). In
   PowerShell clone the public repository and install the daemon:

   ```powershell
   git clone https://github.com/wackyju2-beep/eventmem.git D:\eventmem
   cd D:\eventmem
   .\install.cmd
   ```

   The installer creates a virtual environment, installs core plus the optional vector
   runtime, starts a loopback daemon on port 9123, configures login autostart, and runs a
   self-check. It may use `winget` to install Python 3.12 if Python is missing. Data defaults
   to `%USERPROFILE%\.hermes\eventmem` or `%HERMES_HOME%\eventmem`. A newly generated or
   updated launcher explicitly uses `--host 127.0.0.1`; an already running daemon may still
   use an old launcher, so check its actual listener. If the installer warns `daemon.cmd`
   needs an update, first check affected processes and autostart entries: `-Uninstall`
   forcibly stops matching `python.exe` processes whose command lines contain `run.py`,
   `daemon`, and the configured database path, including other services using that database.
   It also deletes the `eventmem-daemon` scheduled task and Startup shortcut with that name;
   the data is kept. After confirming it is safe, run `.\install.cmd -Uninstall` in
   PowerShell from the repository. Task deletion and process-stop errors are silently ignored;
   do not rely on the success message. Check that the target PIDs, fixed-name scheduled task
   and Startup shortcut are actually gone. Then rerun `.\install.cmd` to regenerate the
   launcher and check its actual listening address. Options include `-NoPythonInstall`,
   `-NoVector`, and `-NoAutostart`. Newly generated launch commands
   explicitly bind to loopback; check an already-running daemon's actual listener separately.
2. In DSH open **Plugins → Add plugin** and select `D:\eventmem\adapters\dsh` from the
   clone. The default is a single-machine setup: `mode: local`,
   `baseUrl: http://127.0.0.1:9123`, no SSH, no private host name. Open
   **记忆后台 → 连接** to see the dependency status. The page separates the adapter, the
   daemon, the optional vector encoder, and the optional relation backend; choose
   **本机记忆** or **远程记忆**, fill the address (and SSH alias for remote), click
   **测试连接**, then **保存并重新加载**. It writes through DSH's profile config editor, so
   no YAML editing is required. If that editor is unavailable, a higher-level profile patch
   still works; see the example below.
3. Check `Invoke-RestMethod http://127.0.0.1:9123/api/health` in PowerShell. The adapter
   verifies `service: eventmem-daemon`, `api: 1`, a numeric `schema`, and (if configured)
   the expected `db_id`; a different service on the port is rejected before any write. The
   installer also runs a self-check. Open **记忆后台** in DSH to inspect the daemon.

Basic storage and literal recall need no relation judge. Turn-end extraction needs a
working DSH host model; `auxProvider` and `auxModel` can be set if the session has no
model selection. Semantic recall additionally requires the vector runtime and the
Qwen3-Embedding-0.6B ONNX `tokenizer.json` and `model_int8.onnx` in
`<HERMES_HOME>\eventmem\models` (default `%USERPROFILE%\.hermes\eventmem\models`).
Automatic relations require a running daemon and a configured decision backend; use the
memory console to configure a backend or download a model. See
[backend setup](https://github.com/wackyju2-beep/eventmem/blob/main/docs/backends.md).

### Profile configuration example

Prefer **记忆后台 → 连接**. If you edit the profile by hand, back up the active profile's
`cordis.patch.yml` first. Its path is `<DSH_HOME>/profiles/<profile>/cordis.patch.yml`; on
Windows `DSH_HOME` defaults to `%USERPROFILE%\.dsh` unless the environment variable is set.
`desktop` is an example profile, not necessarily the active one: consult your DSH profile
documentation. Preserve existing rows and comments. If the file contains only `[]`,
replace the empty list with the sequence below; if an `eventmem` override already exists,
update it instead of duplicating its ID. For a local daemon append this top-level item:

```yaml
- id: eventmem
  name: dsh-eventmem
  config:
    mode: local
    baseUrl: http://127.0.0.1:9123
    agent: dsh
    lang: zh
```

`config` is replaced as a whole: preserve these fields and any custom fields you need.
For remote use `mode: remote`, set `tunnelHost` to the remote passwordless SSH alias, and
adjust `tunnelLocalPort`/`baseUrl` to an unused local port if 9123 is taken. `tunnelHost`
is required only in remote mode. Higher-priority layers may override this patch. Same-ID
replacement without a deep merge was verified with `dump-config` in an isolated profile;
the 连接 page's save path is covered by a config-editor stub test, not an active GUI
session. Confirm the effective configuration and connection in your host.

### Linux or a remote shared database

The [repository quick start](https://github.com/wackyju2-beep/eventmem/blob/main/README.md) covers
Linux/Hermes installation and starting its daemon; `install.sh` only links adapters and does
not start a service. On the remote machine run one daemon for the same database Hermes
uses. Remote use requires `mode: remote` and `tunnelHost` set to a working passwordless
SSH alias for that machine; prefer the 连接 page, or ask someone familiar with profile
patches to make the override as described above.

**A local daemon on 9123 can take precedence over SSH.** The probe validates the service id,
API version, and (when configured) the database id, so a different service is rejected — but
a different *eventmem database* on the expected endpoint needs `expectDbId`. The 连接 page
shows the connected `db_id`; paste it into **预期数据库 ID** to pin it. Either stop the local
daemon and remove its autostart after checking the effects of `.\install.cmd -Uninstall` as
described above (it can stop other matching services; data is retained), or use an unused
local port, e.g. `baseUrl: http://127.0.0.1:9124` and `tunnelLocalPort: 9124`, with
`tunnelRemotePort: 9123` when the remote daemon uses 9123. Do not place SQLite/WAL on a
network share. Run only one daemon per database: Windows lacks the daemon's `flock`
single-instance guard, so it cannot be relied on to prevent a second daemon from opening
the same database. The [DSH adapter guide](https://github.com/wackyju2-beep/eventmem/blob/main/adapters/dsh/README.md)
has the configuration fields.

**Security:** the daemon binds loopback by default. It can require a bearer token
(`--token` or `--token-file`); when set, every route except the minimal health probe needs
it, and a non-loopback bind without a token is refused at startup. Loopback without a token
is still a local trust boundary: any local process that can reach the port can read and edit
the database. Enable a token on shared machines, keep the daemon on loopback, and use SSH
for remote access; do not expose it to an untrusted network. `EVENTMEM_DAEMON_HOST` changes
the daemon's default bind address unless an explicit `--host` is supplied; the token guard
still applies. SSH `-L` listening also depends on the client's `GatewayPorts` setting, so
verify the actual forwarded listener is loopback-only. See the [API guide](https://github.com/wackyju2-beep/eventmem/blob/main/docs/api.md).

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

`mode: remote` (or the older `tunnel: true`) makes the plugin responsible for the SSH
forwarding rather than relying on an external supervisor. It is not the default. On
activation it probes `/api/health`: only a payload that names itself `eventmem-daemon`,
speaks API `1`, and reports a valid schema is treated as an existing endpoint and adopted.
Otherwise it starts `ssh -N -L …` itself and probes again a few seconds later. If that ssh
dies, it is respawned with bounded backoff; on plugin unload the ssh it started is
terminated. A tunnel it did not start is never killed. The spawned ssh keeps its stdin as
an open pipe, so a hard-killed Harness makes it exit on EOF instead of leaving an orphan
(verified).

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

The mounted row is named `eventmem` and lives in the bundle's `cordis.patch.yml`. Prefer
**记忆后台 → 连接**, which tests and saves `mode`, `baseUrl`, `tunnelHost`, `expectDbId`,
and `tokenFile` through the profile's config editor. `agent` labels writes; `tokenFile`
points at a bearer token for a protected daemon and keeps the secret out of configuration.
The defaults are `mode: local`, `baseUrl: http://127.0.0.1:9123`, no SSH.

## Privacy

Turn-end extraction asks the DSH host's configured model to turn one excerpt of the turn
into 0–3 events. That model may be remote, so the excerpt leaves this machine and may incur
cost. `lib/redact.js` masks obvious secret shapes before the call and the output is redacted
again before a write, but it is pattern-based best-effort, not a secret detector. Treat the
extraction model as a recipient you chose.
