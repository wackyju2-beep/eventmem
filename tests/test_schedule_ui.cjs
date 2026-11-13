// Schedule contract test: exercise the real web schedule handlers with an isolated DOM/API.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const root = __dirname + '/..';
const web = fs.readFileSync(root + '/core/eventmem_core/ui/app.js', 'utf8');
const desktop = fs.readFileSync(root + '/adapters/hermes/desktop-plugins/eventmem-gui/plugin.js', 'utf8');
for (const source of [web, desktop]) {
  for (const path of ['/api/recalibrate/preview', '/api/recalibrate/apply', '/api/backfill/preview', '/api/backfill/start']) assert.ok(source.includes(path), `missing ${path}`);
  for (const key of ['min_conf_change', 'min_conf_negate', 'min_conf_repeat', 'min_conf_because', 'min_conf_refine']) assert.ok(source.includes(key), `missing ${key}`);
  assert.ok(source.includes('acceptanceEdges'), 'recalibration must send acceptance fields only');
  assert.ok(!source.includes('edges: null, decision: null, recall: null'), 'reset must not delete unrelated settings');
}
console.log('schedule contracts present');

// Run the actual browser handlers in an isolated DOM; all API responses are fixtures.
const { JSDOM } = require(process.env.HERMES_NODE_MODULES ? process.env.HERMES_NODE_MODULES + '/jsdom' : process.env.HOME + '/.hermes/hermes-agent/node_modules/jsdom');
const html = fs.readFileSync(root + '/core/eventmem_core/ui/index.html', 'utf8');
const dom = new JSDOM(html, { url: 'http://localhost/#schedule', runScripts: 'outside-only' });
const calls = [];
const replies = {
  'api/config': { effective: { edges: {} }, overrides: { edges: { margin: 0.2, unrelated: 9 }, recall: { x: 1 } } },
  'api/i18n': { map: {} }, 'api/logs': { logs: [] },
  'api/recalibrate/preview': { token: 'r', added: { change: 2 }, removed: { because: 1 }, skipped: 3, confidence_updates: 2 },
  'api/backfill/preview': { token: 'b', estimated_pairs: 30, uncertain: true },
  'api/backfill/start': { state: 'running', done: 1, total: 30 },
};
dom.window.fetch = async (url, opts = {}) => {
  calls.push({ path: url, body: opts.body && JSON.parse(opts.body) });
  return { ok: true, json: async () => replies[url] || {} };
};
dom.window.confirm = () => true;
dom.window.eval(web);
const tick = () => new Promise((resolve) => setTimeout(resolve, 10));
const el = (id) => dom.window.document.getElementById(id);
(async () => {
  await tick();
  assert.equal(el('f-min_conf_change').value, '0');
  el('recal-preview').click(); await tick();
  let p = calls.filter(c => c.path === 'api/recalibrate/preview').at(-1);
  assert.equal(p.body.historical_scale, null);
  assert.deepEqual(Object.keys(p.body.edges).sort(), ['margin', 'min_conf_because', 'min_conf_change', 'min_conf_negate', 'min_conf_refine', 'min_conf_repeat'].sort());
  assert.match(el('recal-result').textContent, /跳过的事件对：3/);
  assert.match(el('recal-result').textContent, /置信度更新的边：2/);
  assert.equal(el('recal-apply').disabled, false);
  el('f-min_conf_change').value = '0.4'; el('f-min_conf_change').dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  assert.equal(el('recal-apply').disabled, true);
  el('recal-preview').click(); await tick(); el('recal-apply').click(); await tick();
  p = calls.filter(c => c.path === 'api/recalibrate/apply').at(-1);
  assert.equal(p.body.token, 'r'); assert.equal(p.body.edges.min_conf_change, 0.4);
  assert.equal(p.body.historical_scale, null);
  const scale = el('recal-conditional');
  assert.equal(scale.checked, false);
  scale.checked = true; scale.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  el('recal-preview').click(); await tick();
  assert.equal(calls.filter(c => c.path === 'api/recalibrate/preview').at(-1).body.historical_scale, 'conditional');
  scale.checked = false; scale.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  assert.equal(el('recal-apply').disabled, true);
  scale.checked = true; scale.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  el('recal-preview').click(); await tick(); el('recal-apply').click(); await tick();
  assert.equal(calls.filter(c => c.path === 'api/recalibrate/apply').at(-1).body.historical_scale, 'conditional');
  el('backfill-preview').click(); await tick();
  assert.match(el('backfill-result').textContent, /当前图观察到/);
  assert.equal(calls.filter(c => c.path === 'api/backfill/start').length, 0);
  el('backfill-start').click(); await tick();
  p = calls.filter(c => c.path === 'api/backfill/start').at(-1);
  assert.equal(p.body.token, 'b'); assert.match(el('backfill-progress').textContent, /补算状态/);
  el('sched-reset').click(); await tick();
  p = calls.filter(c => c.path === 'api/config' && c.body).at(-1);
  assert.deepEqual(Object.keys(p.body), ['edges']); assert.equal(p.body.edges.margin, null); assert.ok(!('unrelated' in p.body.edges));
  // The acceptance card has its own save: only the six rule fields go out, nothing else.
  el('f-margin').value = '0.4';
  el('f-margin').dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  el('accept-save').click(); await tick();
  const accept = calls.filter(c => c.path === 'api/config' && c.body).at(-1);
  assert.deepEqual(Object.keys(accept.body.edges).sort(),
    ['margin', 'min_conf_because', 'min_conf_change', 'min_conf_negate', 'min_conf_refine', 'min_conf_repeat'].sort());
  assert.equal(accept.body.edges.margin, 0.4);
  dom.window.close();
  console.log('schedule web interactions pass');
})().catch((error) => { console.error(error); dom.window.close(); process.exitCode = 1; });
