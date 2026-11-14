**eventmem**  

**English** · [中文](README.zh-CN.md "README.zh-CN.md")  

***Memory that remembers what changed.***  
A persistent, change-aware memory layer shared across AI agents. With the included [DeepSeek Harness (DSH) and Hermes adapters, one agent can record a project's decisions and results for another to recall—without sharing their conversation histories. Both must be configured to use the same eventmem database.](adapters/dsh/README.md "adapters/dsh/README.md")  
- **Share work across agents:** record a result in DSH and retrieve it in Hermes, or the other way around.  
- **Track changing facts:** keep earlier events and record when a newer event changes or negates them.  
- **Inspect and correct:** explore events and relations in a graph, inspect decisions and make manual corrections.  
**Why eventmem?**  
**A fact changes.** Instead of leaving two competing chunks for an agent to interpret, eventmem can link a newer event to an older one:  
```  
[11-05] decision  The mini PC uses a 256 GB SSD as its system disk.  
 [11-06] change    The mini PC switched to a 2 TB NVMe system disk. → changes the decision above  
   
```  
When recall finds the old event and the relation exists, it can mark that event as superseded and surface the newer one. Relations may be missed or wrong; see [Limitations.](#anchor-1 "#anchor-1")  
**Work moves between agents.** For example, DSH records a deployment result in the shared project; later, a Hermes session searching for that project can retrieve it. This is shared *memory*, not shared conversation context. Recall depends on what was actually recorded and on the query; it does not guarantee that every relevant event will be returned.  
The examples above illustrate intended behavior, not a recorded end-to-end demo. The screenshot below is an **older** graph view, not evidence of current edge density.  
![](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAABmJLR0QA/wD/AP+gvaeTAAAACXBIWXMAAA7EAAAOxAGVKw4bAAAAxElEQVR4nO1W0Q6AIAiE/v+bpaeaYyYc6NLVvbRm484DL1lEKAIRIWaG1zQ4IgAhsHCMJkc31BXQKmbtvF73iOkKQIvV8LbpFqAJ9Dvac2Z2iQ4N4UiYQ2gJ9G7g6TsupQw7UpHjefTCBCEmwueEaPAMhBzQBVDC+tkit2pOOQWXEx5H0gIy/ScK/Atq0os4k5ihHOilHJyYyyfhTIjIJg48idQ5EME6DrwlZB0HthaQcRG+FbeQuaZ/ewb2ScJfQBSe9p77EZ7a26s/yQAAAABJRU5ErkJggg==)  
**Quick start**  
**What is required?**  
  
|  |  |  
| - | - |  
| **Capability** |  **Requirements** |  
| Event storage and literal recall |  Python 3.11+, SQLite with FTS5 and its trigram tokenizer, and an adapter; no relation judge required |  
| Semantic recall |  Optional vector dependencies and an embedding model |  
| Automatic relations |  A running daemon and a configured decision backend; a local CPU backend is possible |  
| Shared memory and graph |  Adapters configured for the same database, plus the daemon/console; cross-machine access uses its HTTP API over SSH |  
  
   
The host model may be local or remote and is used by the adapter for turn-end extraction. A remote decision backend may also need credentials. Neither GPU nor a judge is required for basic storage and literal recall.  
**Windows + DSH**  
```  
git clone https://github.com/wackyju2-beep/eventmem.git D:\eventmem  
 cd D:\eventmem  
 .\install.cmd  
   
```  
The installer creates a virtual environment, installs core and the optional vector runtime, starts a local daemon and sets up login autostart. If Python is missing it may install Python 3.12 using winget. Use `-NoPythonInstall`, `-NoVector`, or `-NoAutostart` with `install.cmd` to opt out of those steps. Data defaults to `%USERPROFILE%\.hermes\eventmem` (or `%HERMES_HOME%\eventmem`).  
In DSH, open **Plugins → Add plugin** and select `D:\eventmem\adapters\dsh`. The adapter can use the local daemon at `http://127.0.0.1:9123`; its [guide covers remote SSH and configuration. Open the memory console to configure an optional relation backend or download a model. For semantic recall, put the Qwen3-Embedding-0.6B ONNX files `tokenizer.json` and `model_int8.onnx` in `<HERMES_HOME>\eventmem\models\` (default `%USERPROFILE%\.hermes\eventmem\models\`); installing the vector runtime alone is not enough.](adapters/dsh/README.md "adapters/dsh/README.md")  
Self-check from the repository directory:  
```  
~\.hermes\eventmem\venv\Scripts\python.exe tools\selfcheck.py  
   
```  
Adjust the path if `HERMES_HOME` is set.  
**Linux + Hermes**  
With Hermes installed, clone the repository and link the adapter:  
```  
git clone https://github.com/wackyju2-beep/eventmem.git ~/eventmem  
 cd ~/eventmem && bash ./install.sh  
   
```  
In Hermes' `~/.hermes/config.yaml`, select the provider:  
```  
memory:  
   provider: 'eventmem'  
 plugins:  
   entries:  
     eventmem:  
       settings: {}  
   
```  
Restart the relevant Hermes host processes and start a new session. `install.sh` only creates symlinks; it does not start a daemon. To run the daemon in the foreground for the default database, using the Python environment in which Hermes is installed:  
```  
~/.hermes/hermes-agent/.venv/bin/python ~/eventmem/adapters/hermes/run.py daemon --db "$HOME/.hermes/eventmem/events.db"  
   
```  
Adjust the Python and database paths for your installation and `HERMES_HOME`. In another terminal check `curl http://127.0.0.1:9123/api/health`. For the Hermes graph/console, enable **both** `eventmem-graph` and `eventmem-gui` under `plugins.enabled`; `install.sh` prints further setup steps. Run the daemon under a service manager if you need it to survive logout. The optional judge and embedding setup are described in [backends and](docs/backends.md "docs/backends.md") [settings.](docs/settings.md "docs/settings.md")  
To share with DSH on another machine, start the daemon on the Hermes machine with **the same database Hermes uses**. In DSH's plugin settings set `tunnelHost` to a working passwordless SSH alias for that machine. If the Windows installer already started a local daemon on port 9123, the default DSH settings will adopt it instead of opening a tunnel. Either stop it and remove its autostart with `install.cmd -Uninstall` (the local database is kept), or choose an unused local port and set both `baseUrl` (for example `http://127.0.0.1:9124`) and `tunnelLocalPort` (`9124`), leaving `tunnelRemotePort` at the remote daemon's port. See the [DSH guide for the tunnel settings. Do not put SQLite/WAL on a network share.](adapters/dsh/README.md "adapters/dsh/README.md")  
**Try the handoff:** ask DSH to record a distinctive result under a project, then ask a new Hermes session to recall that result. Use `expand` on the returned event to check its source and project. Try the reverse direction too. These are suggested verification steps, not a claim that this walkthrough was recorded or tested in your setup; recall depends on what was written and the query.  
**How it works**  
1. **Record:** the agent writes an event, or the adapter asks the host's auxiliary model to extract events at turn end. Commands, logs and other long material can be stored as artifacts.  
2. **Relate:** an optional background worker selects candidate pairs and asks a configured decision backend; accepted relations are stored for later inspection.  
3. **Recall:** literal search, and semantic search if configured, find relevant events. Stored correction relations and nearby context can be included in the result.  
Events have five kinds: decision, attempt, result, change and lesson. Relations describe **change, negate, repeat, because** and **refine**. Stored kind and relation names are Chinese. Projects group memories; `mem_recall`, `mem_neighbors` and `expand` help an agent retrieve events, follow their history and inspect artifacts. Events and relations can also be corrected in the graph.  
**Architecture**  
```  
 Hermes adapter ──────┐  
                       ├── eventmem Core ── Shared SQLite memory  
  DSH adapter ─ daemon ┘    storage · recall   events · projects · relations  
                            relation judging  
   
```  
The Hermes adapter calls core locally; the DSH adapter talks to the daemon's HTTP data plane. The adapters integrate with their hosts and call the host model for extraction—core is not a general-purpose extraction model. Agent/session labels preserve the source of new writes. Other hosts need their own adapter or API integration; they are not supported automatically.  
Use **one daemon per database**. The API and management console have no built-in authentication and default to loopback; do not expose them to an untrusted network. The daemon can be configured to bind elsewhere, so keep that setting private. See [architecture and](docs/architecture.md "docs/architecture.md") [data-plane API.](docs/api.md "docs/api.md")  
**Documentation**  
- [Architecture and data flow ·](docs/architecture.md "docs/architecture.md") [Relations and edge building](docs/relations.md "docs/relations.md")  
- [Decision backends and model setup ·](docs/backends.md "docs/backends.md") [Settings ·](docs/settings.md "docs/settings.md") [API](docs/api.md "docs/api.md")  
- [DSH adapter, console and SSH ·](adapters/dsh/README.md "adapters/dsh/README.md") [Backend examples](examples/backends/ "examples/backends/")  
**Limitations and design choices**  
- eventmem focuses on changing facts, project history and explicit event relationships. For a static document collection, ordinary RAG may be simpler.  
- Automatic judgments are fallible. The conservative policy prefers omitting an uncertain edge to permanently recording an unsupported relation; fewer edges alone do **not** prove higher accuracy. Human-labelled evaluation is needed to measure both false positives and missed relations. Ordinary search can still find events without an edge, but may not identify their relationship.  
- Existing chunks from another memory system are not automatically migrated. The agent can choose the wrong existing project; inspect and correct important entries.  
- DSH's `evt_add` validates supplied links but currently skips writing them; automatic linking happens separately and may not recreate them. See the [adapter guide.](adapters/dsh/README.md "adapters/dsh/README.md")  
- This is a personal project, not a hosted service. Schema and interfaces may change; graph labels and stored event/relation names are Chinese.  
**Motivation**  
I built eventmem for my own agent on a mini PC without a GPU. Chunked recall kept returning paragraphs in which some facts had already been replaced, without saying which ones. The agent had to guess, and sometimes repeated an old number as current. I wanted the change itself to be data: a relation that could be traced and corrected, rather than rediscovered on every recall.  
**Development and license**  
The CI workflow runs the Python scripts under `tests/test_*.py` with Python 3.11 and 3.12; see [the workflow for dependencies and the current command. Test results and model timings depend on the revision and environment.](.github/workflows/tests.yml ".github/workflows/tests.yml")  
MIT; see [LICENSE. Third-party model weights are not distributed here and have their own licenses.](LICENSE "LICENSE")  
