# Console harness (real Chromium, fake ctx)

The smoke runner renders each tab once with `react-dom/server`; that proves a tab does not
crash, but nothing about effects, intervals or CSS geometry. This harness mounts the *real*
console in Chromium against a fake `ctx.rest`, so the things that only exist at runtime are
observable:

- **the poll while a pass runs** — `?live=1` makes `/api/stats` count up on every call and
  `/api/status` report a live pass; the tiles must move at the strip's tempo, not every 5 s;
- **the block rhythm** — `__harness.gaps()` returns the vertical gap between consecutive
  top-level blocks of a tab (the fused borders were a 0);
- **what a button actually posts** — `__harness.posts()` records every `POST /api/config`,
  so "does 保存接受条件 send only the rule fields?" is a measurement, not a reading of code.

```
./tools/plugin-smoke/harness/build.sh          # patches a copy of plugin.js, esbuilds it
cd tools/plugin-smoke/harness && python3 -m http.server 8899 --bind 127.0.0.1
# open http://127.0.0.1:8899/index.html?page=overview&live=1
```

React comes from `~/.hermes/hermes-agent/node_modules` (the version the app itself ships) and
esbuild from the same install, so no network is involved. `HERMES_NODE_MODULES` overrides.

Query flags: `page=overview|backends|models|schedule|logs`, `live=1`, `slim=1` (empty library).

Helpers on `window.__harness`: `tab('调度')`, `blocks()`, `gaps()`, `text()`, `posts()`,
`click('保存接受条件')`, `type('margin', 0.25)`, `calls()`.

Port 8899 is the same one `../bench` uses (the graph fps bench) — one at a time.

Known blind spots: it is not the app. IPC, the host's real theme, and `ctx.rest` over SSH are
not covered; after a plugin change the app still needs Ctrl+K → Reload desktop plugins.
