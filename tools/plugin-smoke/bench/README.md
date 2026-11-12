# Graph bench

Measures the memory graph under load, in a real Chromium, outside the app:

    # 1. dump the real scene (347 atoms / 2231 edges / 2883 pairs)
    python3 - <<'PY' > tools/plugin-smoke/bench/graph.json
    import json, sys; sys.path.insert(0, 'adapters/hermes/eventmem-graph/dashboard')
    import plugin_api; print(json.dumps(plugin_api._read_graph(), ensure_ascii=False))
    PY

    # 2. serve and open http://127.0.0.1:8899/index.html   (add ?v=old for a baseline copy)
    cd tools/plugin-smoke/bench && python3 -m http.server 8899 --bind 127.0.0.1

    # 3. in the page console / CDP:  await __bench(3000)

Needs network: React is imported from esm.sh so nothing has to be bundled. The number to
watch is `fps` / `p95` while the wheel zoom keeps the camera moving — that is the frame
path that made the graph unusable when every edge was an SVG path.
