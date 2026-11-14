# eventmem for Hermes

An event-shaped local memory provider for Hermes. It stores decisions, results,
changes, attempts and lessons in SQLite, and can recall literal matches with
FTS5 or combine them with optional local ONNX embeddings. Stored kinds and
relation names are Chinese. Memory is selective and recall is not guaranteed.

## Scope and setup

This catalog package contains the **memory provider only**. It does not install
or start the separately distributed eventmem daemon, relation worker, graph,
management console or desktop/dashboard plugins. To use those components, see
the [full project installation guide](https://github.com/wackyju2-beep/eventmem#readme).
The provider itself does not need a daemon for local writes and recall.

After installing this package, select it with Hermes' memory setup or set the
active profile's `config.yaml`:

```yaml
memory:
  provider: eventmem
```

Requires Hermes 0.23.1 or later and Python 3.11+ with SQLite FTS5 and its
trigram tokenizer. Start a new session (and restart Hermes if its dependency
environment changed). The default database is `<HERMES_HOME>/eventmem/events.db`. Keep backups; the
provider can create, modify and permanently delete events and artifacts there.
The provider reads the profile's `config.yaml` and its own
`eventmem/settings.yaml` for configuration. It may also read a legacy
`localmem/models` directory for an existing embedding model. Do not store secrets
in eventmem settings.

The seven provider tools are `evt_add`, `expand`, `mem_neighbors`, `mem_recall`,
`proj_add`, `proj_list`, and `evt_forget`. Unlike the full repository's symlink
installation, this catalog edition does not register tool-result middleware:
Hermes currently runs provider registration during availability discovery and
can otherwise leave duplicate reminders active even when a provider is not selected.
It also does not register an auxiliary task in the model picker. Advanced users
can set `auxiliary.eventmem_extract` manually in `config.yaml`; the extractor
passes a 60-second timeout explicitly. Relations are stored and recalled if present, but automatic relation building
requires the separately installed worker and configured decision backend.

For semantic recall, install the declared vector dependencies and provide the
compatible Qwen3-Embedding-0.6B ONNX model files separately; this package does
not download model weights. Without an available encoder, recall falls back to
literal search. See the [model configuration guide](https://github.com/wackyju2-beep/eventmem/blob/main/docs/backends.md).

## Privacy and security

- Event and artifact text is stored locally. The redactor is best effort, not
  a guarantee; avoid storing secrets and inspect important records. Manual tool
  writes may store text unchanged if the Hermes redactor is unavailable.
- If a substantive turn finishes without an event, eventmem asks Hermes' chosen
  auxiliary model to extract memories from excerpts of the user and assistant
  messages. This catalog edition applies Hermes' forced redactor to inputs and
  output and skips automatic extraction when that redactor fails; it cannot
  guarantee detection of every secret; redaction may retain partial token prefixes
  or suffixes. A remote model receives the redacted excerpts, which can still
  contain sensitive information and incur costs.
  Extraction can fail without writing a memory. No separate telemetry is sent.
- The optional encoder loads files from local model directories. The core includes
  an HTTP decision-model client used by relation-building code; this catalog
  package does not start a worker, daemon or server automatically.
- Separately installed daemon and console have no built-in API authentication or
  TLS. Keep them on trusted loopback or behind a properly secured tunnel; do not
  expose them directly to an untrusted network. Use one daemon per database.
- Display language is read from the active profile at render time. In one process
  serving several profiles, without an ambient profile home, Hermes' own home
  getter falls back to the process home; only display strings are affected.
- The provider currently uses Hermes' auxiliary-client and language interfaces
  which are not all listed as stable public plugin APIs. A future Hermes release
  could require an adapter update.

This is community software, not an official Nous Research plugin. See the
[repository](https://github.com/wackyju2-beep/eventmem) for source, limitations,
settings and the MIT license.
