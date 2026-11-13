# Decision backends

The edge builder needs one thing from a model: for a pair of events, a **distribution over five
relations plus "none"**, with the runner-up close enough that a margin means something. That is a
different contract from "generate text", so eventmem has its own notion of a backend — not a
provider list inside the plugin, but **a folder with a `config.yaml` in it**.

```
~/.hermes/eventmem/backends/
  startlux/config.yaml      # my 4B, already running as a service
  lev/config.yaml          # weights I let eventmem launch
  typesafe/config.yaml     # the hosted judge
```

The folder name is the backend's name. Dropping a folder in is the whole install step; there is no
registry, no `add`, nothing to unregister. A folder without `config.yaml` is not a backend and is
ignored.

## The three shapes

Which keys are present decides what the folder is:

| keys | kind | who runs it |
|---|---|---|
| `weights:` | `self` | **eventmem** — the daemon launches `llama-server` when a pass has work |
| `base_url:` + `external: true` | `external` | someone else — LM Studio, a systemd unit, `start.sh` |
| `base_url:` | `remote` | a hosted endpoint (TypeSafe, a gateway, a box over the LAN) |

`weights` and `base_url` together is an error: a folder is one backend or the other, never both.

## Every key

| key | default | meaning |
|---|---|---|
| `protocol` | — (required) | `systemone` (`POST /v1/systemone`, distributions) or `openai-decisions` (`POST /v1/decisions`) |
| `model` | stem of `weights` for `self`, else required | the model id the server expects |
| `weights` | — | `self` only: a GGUF path, absolute or relative to the pack folder |
| `base_url` | — | `external` / `remote`: the server root, e.g. `http://127.0.0.1:1234` |
| `external` | `false` | marks a `base_url` as already running (we never start or stop it) |
| `launch` | `auto` for `self` | the command to start the server; `auto` uses the `llama.cpp` this repo installed. **A `self` pack may skip `weights` entirely and give only `launch`** — that is how a backend whose weights live on another machine is driven (a wrapper script that starts the remote server, gates on free VRAM, and runs the local engine) |
| `args` | — | extra server args; `{port}`, `{weights}`, `{dir}` are substituted |
| `port` | `0` | `self` only: `0` = pick a free port and record it in `.runtime.json` |
| `release_after_pass` | `false` | `self` only: stop this pack again when the pass that started it ends (frees VRAM). Only packs the daemon started itself are stopped |
| `api_key_env` | — | read the key from this environment variable (never stored in the file) |
| `api_key` | — | the key itself, for local endpoints; `api_key_env` wins when both are set |
| `margin` | `null` | 解析并显示，但 nightly 目前只用 `edges.margin`（按后端分阈值还没接进写边路径） |
| `license` | — | shown in the GUI; nothing is inferred or assumed |
| `note` | — | free text, shown in the GUI |

A `remote` pack with `base_url` but no key is **not** an error: the probe returns `needs_key` and the
chain moves on. That is what makes "hosted first, local as the fallback" safe to configure before you
have a key.

## The chain

`decision.backends` is an ordered list of pack names:

```yaml
decision:
  backends:
    - typesafe        # hosted, might be down / unkeyed
    - lev             # self-managed fallback
```

Rules, all of them about **not mixing models inside one pass**:

- The chain is probed **once per pass**; the first pack that answers runs the whole pass.
- A mid-pass failure **aborts** the pass. The cursor does not advance, and the next pass starts over
  with a fresh probe. Two models in one night would put two distribution shapes on the same
  threshold, and `edges.backend` records which one wrote each edge so you can recalibrate later.
- `401`/`403` mark the pack `needs_key` for that pass instead of retrying it.
- An **empty** `decision.backends` means the v2.1 behaviour: the single endpoint at `decision.url`.
  That mode still works and is what a deployment with one `start.sh` uses. Filling the chain retires
  it — `decision.url` is then no longer consulted, so list every backend you want, including the
  local one.

## Installing one

GUI: `http://127.0.0.1:9123/` → 后端 → 新建后端 (templates for cloud / LM Studio / local GGUF), then
add it to the chain.

CLI, for the self-managed path:

```bash
PY=~/.hermes/hermes-agent/.venv/bin/python
$PY ~/eventmem/adapters/hermes/run.py models install lev      # llama.cpp + GGUF + pack
$PY ~/eventmem/adapters/hermes/run.py models list
```

`models install` picks the `llama.cpp` release asset for this machine (OS, arch, accelerator), fetches
the GGUF you asked for by quant, verifies the sha256 HuggingFace publishes, and writes the pack. It
never downloads weights unless you ask: the default recommendation `ggml-org/lev-GGUF` (Apache-2.0,
Q4_K_M, ~2.4 GB) is a convenience, not a dependency.

Then restart the daemon so it sees the new folder:

```bash
systemctl --user restart eventmem-worker     # the unit kept its v2 name on purpose
```

## Probing

`POST /api/backends/<name>/probe` costs one HTTP GET and no tokens. `self` packs report
`not-running` until the daemon starts them; `remote` packs report `needs_key` when no key is set.
The GUI's 探活 button is that route.

A probe asks **"does the port answer"**, never "is a model loaded" — with LM Studio's JIT the
request itself loads the model, so requiring a loaded model would refuse a perfectly usable
backend.

### `check_model: true`

Opt-in, for a backend that names a model id (LM Studio is the case): the probe also reads
`/v1/models` and requires `model:` to be in the listing. With JIT that listing holds every
model the server *could* load, so this is cheap and it catches a renamed weight **before** the
round starts — otherwise the id fails at decide time, mid-round, and every later round fails
the same way, so the chain never falls back to the second entry. A body the loader cannot parse
(a shim, an older server) is treated as liveness-only, never as a failure. `self` packs ignore
the flag with a warning: they derive the id from the weights filename.

```yaml
protocol: systemone
model: StartLux-Decision-v1.1-4B-AutoThink
external: true
base_url: http://10.42.0.51:1234
check_model: true
```

## When it does not work

| symptom | what it means |
|---|---|
| `protocol=... 不认识` | only `systemone` and `openai-decisions` exist; check the spelling |
| `至少写 weights（本地）或 base_url` | the folder has neither; it is not a backend |
| `weights 与 base_url 只能写一个` | pick a shape, not both |
| probe says `needs_key` | `api_key_env` points at an unset variable, or the endpoint wants a key you have not set |
| probe says `not-running` | `self` pack, and the daemon has not started it (it only starts on a pass with work) |
| edges stop appearing after a chain change | the chain's first pack answers but returns junk; check `edges.backend` and the daemon log |
