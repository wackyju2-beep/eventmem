# dsh-console-smoke

The DSH memory console (`adapters/dsh/client.js`) has no build step and no test runner in
the plugin itself, so this directory is where it gets checked: the real bundle is evaluated
in a jsdom window with the real React 18, the Host `ctx` is stubbed, and the daemon's JSON
is fed through a `fetch` stub. No browser, no tunnel, no daemon needed.

`proxy.mjs` covers the other half — the route allow-list in `adapters/dsh/lib/console.js`
that is the only door from a browser session to the daemon.

```sh
cd tools/dsh-console-smoke
npm install          # once: jsdom + react 18
npm run all          # → SMOKE PASS + PROXY PASS
```

What `run.mjs` asserts (each line prints `ok` / `FAIL`):

- 待判 counts down `pairs_planned − pairs`, and the tile shows a number rather than `—`;
- a poll that judged two pairs flashes `+2`, and the numbers flip digit by digit
  (`.emc-flip` cells) instead of snapping;
- the live strip is on the page, names the backend, and the header no longer duplicates
  the pause button;
- 调度 has the seven Hermes blocks (自动运行 / 预设 / 候选选择与成本 / 关系接受条件 /
  历史关系重校准 / 历史候选补算 / 说明) and no longer the old single 「Tier 明细」 card;
- 关系接受条件 has six thresholds and reports `已保存` / `有未保存的改动`;
- 预览历史 on both history cards POSTs the right route with only the fields that route
  takes (acceptance fields for recalibrate, candidate fields for backfill) and renders the
  returned preview;
- 暂停全部判断 POSTs `/api/pause`, 仅保存未来设置 POSTs `/api/config`, and saving never
  starts a history operation.

What `proxy.mjs` asserts: every route the console uses is proxied (and forwarded as
`/api/<route>`), every other route 404s before it reaches the daemon, and traversal or
percent-encoded paths are refused.

When you change `client.js` or `lib/console.js`, run `npm run all` before reloading the
plugin — a screenshot is slower and lies about flaky numbers.
