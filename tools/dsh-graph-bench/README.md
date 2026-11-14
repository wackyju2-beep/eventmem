# DSH graph bench

Measures the memory graph that ships in `adapters/dsh/client.js` (the console's 图谱 tab)
in a real Chromium, outside the app. It loads the real client bundle, mounts the real
`Console` with a stub Host `ctx`, serves a real `/api/mem/graph` dump through a fetch stub,
and opens the tab by clicking 图谱.

    # 1. dump a real scene (daemon reachable through the tunnel on :9123)
    curl -s http://127.0.0.1:9123/api/mem/graph -o tools/dsh-graph-bench/graph.json

    # 2. run it (headless Chrome, CDP; no extra packages — Node 22+ WebSocket)
    node tools/dsh-graph-bench/run.mjs --tag=before
    node tools/dsh-graph-bench/run.mjs --tag=after  --width=2560 --height=1440 --dpr=1.5

Output goes to stdout and `--tag-report.json`; screenshots are `<tag>-rest|mid|close|zoom|selected.png`.

## Interaction probes

`screens.selected` comes after `__select()`; the three click rules are covered by real
pointer/mouse sequences and printed as `ringPolicy` / `edgePolicy` / `panelPolicy`:

- `__ringPolicy()` — select an atom, then click a highlighted second-layer neighbour
  **once**: the inspector must switch to that neighbour (no deselect-then-click).
- `__edgePolicy()` — with nothing selected the stage hit table must be empty; after
  selecting an atom it holds only that atom's incident edges; clicking one opens the
  relation inspector, hovering it gives a pointer cursor; with the selection gone the same
  pixel must not select an edge again.
- `__panelPolicy()` — open the filter panel and click an atom outside it: the first click
  only closes the panel (no inspector, no selection), the second click selects the atom.

`__emEdgeHits()` and `__emRingLayers()` expose the highlighted edges / the two neighbourhood
layers; they are only installed when `window.__EM_GRAPH_DEBUG` is on.

## What the numbers mean

`fps` is the app's own draw rate, counted from back-canvas clears — **not** rAF. Headless
Chromium does not vsync, so rAF runs at 100–240 Hz whatever the page does; the draw rate and
`ms/frame` (main-thread task time divided by draws) are the honest numbers. On a 60 Hz
display the frame budget is 16.7 ms, so `ms/frame` is what decides whether it hitches.

`--ab=1` (default) measures a set of ablation patches in the same session, each preceded by
a warm-up and a camera reset. Pick arms with `--arms=edges,morph,text,legacy`.

Two traps this harness had to fix, in case it is ever rewritten:

- **rAF fps is meaningless here.** Early runs reported 160 "fps" while idle. Use the draw
  counter.
- **The camera must be reset before every measurement.** Without it, later runs started at
  whatever zoom the previous run ended on, which made a warm-up ramp look like a causal
  effect of the patch. `window.__reset()` clicks 重置视角.
- **One clear per frame, not two.** The front canvas is lazy now; counting both clears and
  dividing by two halves the reported rate. The meter counts `.emc-g-back` only.
- **`__filter` / `__deselect` are the visibility regression tests.** The per-atom style
  cache once skipped `display` for atoms hidden through the early-continue paths, so they
  never came back: the bench showed `matchedVisible 8/63` and `visible 7/524`. Any change
  to the style cache must keep those at 63/63 and 524/524. `window.__emGraphState()` /
  `__emAtomState(ids)` (enabled by `window.__EM_GRAPH_DEBUG`) expose the fade and the
  screen map for exactly this.
- Long sessions on this laptop (Intel UHD) thermally throttle; absolute numbers drift by up
  to 2x over an hour. Compare arms inside one session, and let the machine cool between runs.
