// 框选的命中语义 + 远景收缩曲线。跑的是真 plugin.js（vm 里剥掉 import / 导出）。
const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const path = require('node:path')
const source = fs.readFileSync(
  path.join(__dirname, '../adapters/hermes/desktop-plugins/eventmem-graph/plugin.js'),
  'utf8'
)
const ctx = {
  console, performance: { now: () => 0 }, Math, Map, Set, Number, String, Boolean, Date, Object, Array,
  JSON, RegExp, Error, isFinite, isNaN,
  useRef: value => ({ current: value }), useMemo: fn => fn(), useCallback: fn => fn(),
  useEffect: () => {}, useState: value => [value, () => {}],
  jsx: () => null, jsxs: () => null, Button: 'button',
  document: { documentElement: {} }, window: {}, setTimeout, clearTimeout
}
ctx.globalThis = ctx
const code = source
  .replace(/import\s*\{[\s\S]*?\}\s*from\s*'@hermes\/plugin-sdk'\s*/, '')
  .replace(/import[^\n]*\n/g, '')
  .replace('export default {', 'const pluginDefault = {') +
  '\nglobalThis.__test = { lassoPick, farFactor };'
vm.runInNewContext(code, ctx, { filename: 'plugin.js' })
const { lassoPick, farFactor } = ctx.__test

// --- lasso hit test --------------------------------------------------------
const el = {}
const pos = (x, y, extra = {}) => ({ x, y, el, visibility: 1, depth: 1, ...extra })
let positions = new Map([
  [1, pos(10, 10)],
  [2, pos(100, 10)],
  [3, pos(10, 100)],
  [4, pos(10, 10, { visibility: 0.2 })],  // 被筛选压暗 → 不能选
  [5, pos(10, 10, { depth: 0.2 })],       // 在相机背后 → 不能选
  [6, null],                              // 没有 DOM 的占位
  [7, { x: 10, y: 10, visibility: 1, depth: 1 }] // el 为空 → 跳过
])
let picked = [...lassoPick(positions, { x0: 0, y0: 0, x1: 50, y1: 50 })]
assert.deepEqual(picked, [1], 'only the visible atom inside the box, sorted by id')

// 边界值算「框内」（用户把框拉到卡片边缘时不该漏掉）
picked = [...lassoPick(positions, { x0: 10, y0: 10, x1: 100, y1: 100 })]
assert.deepEqual(picked, [1, 2, 3])

// 反向拖出来的框由调用方先归一化；空框/极小框不选中任何东西
assert.deepEqual([...lassoPick(positions, { x0: 5, y0: 5, x1: 5, y1: 5 })], [])

// 大框能同时拿多个原子
picked = [...lassoPick(positions, { x0: 0, y0: 0, x1: 200, y1: 200 })]
assert.deepEqual(picked, [1, 2, 3])

// 空表不炸
assert.deepEqual([...lassoPick(new Map(), { x0: 0, y0: 0, x1: 10, y1: 10 })], [])

// --- far shrink curve ------------------------------------------------------
assert.equal(farFactor(0.05), 1, 'very far (rel→0) is a plain dot')
assert.equal(farFactor(0.12), 1, 'still a dot up to the knee')
assert.equal(farFactor(0.28), 0, 'from the card knee on it is a ball again')
assert.equal(farFactor(0.6), 0)
// 球↔卡用了 smoothstep(0.3, 0.72, rel)：能变成卡的距离上，远景收缩必须是 0，否则两者打架
assert.equal(farFactor(0.3), 0, 'no overlap with the ball→card morph')
// 单调：越远（rel 越小）收缩越强
let previous = -1
for (const rel of [0.30, 0.26, 0.22, 0.18, 0.12]) {
  const f = farFactor(rel)
  assert.ok(f >= previous, `farFactor must not decrease as rel grows: rel=${rel} f=${f}`)
  previous = f
}
assert.ok(farFactor(0.2) > 0 && farFactor(0.2) < 1, 'the transition band is continuous, not a switch')

console.log('PASS lasso hit test only picks visible atoms with their projected centre inside the box')
console.log('PASS far shrink is a continuous, monotonic function that never overlaps the card morph')
