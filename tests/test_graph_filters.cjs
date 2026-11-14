// Pure filter semantics: id parsing + the AND-combination of atom/edge/semantic filters.
// Runs the shipped plugin.js in a vm with the same stubs the rAF harness uses.
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
  '\nglobalThis.__test = { parseIdSpec, computeFilterInfo, facetCounts };'
vm.runInNewContext(code, ctx, { filename: 'plugin.js' })
const { parseIdSpec, computeFilterInfo, facetCounts } = ctx.__test

// --- id spec ---------------------------------------------------------------
let spec = parseIdSpec('12, 30-33, #56')
assert.deepEqual([...spec.ids].sort((a, b) => a - b), [12, 30, 31, 32, 33, 56])
assert.equal(spec.bad.length, 0)

spec = parseIdSpec('9-7')
assert.deepEqual([...spec.ids].sort((a, b) => a - b), [7, 8, 9], 'a reversed range still means the same ids')

spec = parseIdSpec('12 x 40 1-999999')
assert.deepEqual([...spec.ids], [12, 40], 'junk and oversized ranges are rejected, valid tokens survive')
assert.deepEqual([...spec.bad], ['x', '1-999999'])

assert.equal(parseIdSpec('').ids.size, 0)
console.log('PASS id spec parsing, ranges and junk')

// --- fixtures --------------------------------------------------------------
const events = [
  { id: 1, ts: 1, kind: '决定', project: 'A', session: 's1' },
  { id: 2, ts: 2, kind: '结果', project: 'A', session: 's1' },
  { id: 3, ts: 3, kind: '教训', project: 'B', session: 's2' },
  { id: 4, ts: 4, kind: '变更', project: 'B', session: 's2' },
  { id: 5, ts: 5, kind: '教训', project: 'A', session: 's2' }
]
const edges = [
  { kind: 'rel', rel: '细化', src: 2, dst: 1 },
  { kind: 'rel', rel: '否定', src: 3, dst: 2 },
  { kind: 'rel', rel: '否定', src: 5, dst: 4 },
  { kind: 'none', rel: null, src: 4, dst: 1 }
]
const blank = { project: '', session: '', date: '', kinds: [], rels: [], ids: '' }
const run = (filters, extra = {}) =>
  computeFilterInfo({
    events,
    edges,
    filters: { ...blank, ...filters },
    idSet: extra.idSet || new Set(),
    semIds: extra.semIds || null,
    semActive: Boolean(extra.semActive),
    mode: extra.mode || 'only',
    pulled: extra.pulled || new Set()
  })
const ids = set => [...set].sort((a, b) => a - b)

// no filter at all
let info = run({})
assert.equal(info.active, false)
assert.equal(info.focus.size, 0, 'inactive filters leave the whole scene visible')

// project only
info = run({ project: 'A' })
assert.deepEqual(ids(info.match), [1, 2, 5])
assert.deepEqual(ids(info.focus), [1, 2, 5])
assert.equal(info.edgeCount, 1, 'only the 细化 edge has both ends inside project A')
assert.equal(info.atomActive, true)

// kind only
info = run({ kinds: ['教训'] })
assert.deepEqual(ids(info.match), [3, 5])

// relation only: the endpoints of that relation's edges
info = run({ rels: ['否定'] })
assert.deepEqual(ids(info.match), [2, 3, 4, 5])
assert.equal(info.edgeCount, 2)
assert.equal(info.relActive, true)

// grey pairs are addressable as relation 'none'
info = run({ rels: ['none'] })
assert.deepEqual(ids(info.match), [1, 4])
assert.equal(info.edgeCount, 1)

// id lookup
info = run({ ids: '3-4' }, { idSet: new Set([3, 4]) })
assert.deepEqual(ids(info.match), [3, 4])

// AND across dimensions, including relation (only edges between matched atoms survive)
info = run({ project: 'B', kinds: ['教训'], rels: ['否定'] })
assert.deepEqual(ids(info.match), [3], 'project B and kind 教训 and on a 否定 edge')
assert.equal(info.edgeCount, 0, 'the 3->2 edge is dropped because #2 is not matched')

// semantic ids intersect with the other atom filters
info = run({ kinds: ['教训'] }, { semIds: new Set([2, 5]), semActive: true })
assert.deepEqual(ids(info.match), [5])

// one-hop neighbours come through the kept edges only
info = run({ kinds: ['教训'], rels: ['否定'] }, { mode: 'neighbors' })
assert.deepEqual(ids(info.focus), [2, 3, 4, 5], 'the other ends of both negation edges join the focus set')
assert.equal(info.edgeCount, 2)

// a semantic search that returned nothing hides everything — it is a real filter, not a miss
info = run({}, { semIds: new Set(), semActive: true })
assert.equal(info.active, true)
assert.equal(info.focus.size, 0)

console.log('PASS AND-combination of project/kind/id/relation/semantic filters')


// --- 分面：下拉选项只来自「其它条件」筛出来的事件 ---------------------------
// 用户报的问题：选了项目（未归类 32 条）之后，会话下拉仍然是全库的几十个会话，
// 命中集里只有其中几个，等于在长列表里找不到。选项必须跟着其它条件收窄。
const facetEvents = [
  { id: 1, ts: 1, kind: '决定', project: null, session: 's1' },
  { id: 2, ts: 2, kind: '结果', project: null, session: 's1' },
  { id: 3, ts: 3, kind: '教训', project: null, session: 's9' },
  { id: 4, ts: 100000, kind: '变更', project: 'A', session: 's2' },
  { id: 5, ts: 200000, kind: '结果', project: null, session: 's1' }
]
const facetsOf = (filters, extra = {}) =>
  facetCounts(facetEvents, { ...blank, ...filters }, extra.idSet || new Set(),
    extra.semIds || null, Boolean(extra.semActive))

let facets = facetsOf({})
assert.deepEqual([...facets.project.keys()].sort(), ['A', '未归类'])
assert.equal(facets.project.get('未归类').n, 4)
assert.equal(facets.session.get('s1').n, 3)

// 选了项目之后：会话里只剩这个项目里出现过的会话，别的项目那些被去掉
facets = facetsOf({ project: '未归类' })
assert.deepEqual([...facets.session.keys()].sort(), ['s1', 's9'], 's2 only exists in project A')
assert.equal(facets.session.get('s1').n, 3)
// 日期同理：A 的那一天不该出现在未归类的日期列表里
assert.equal(facets.date.size, 2)

// 自己那一维要排除自己：选着会话 s1 时，会话下拉里仍然列着全部会话（否则换不了会话），
// 而且 s1 的计数是它自己的 3 条，不是被自己筛过之后的 0。
facets = facetsOf({ session: 's1' })
assert.deepEqual([...facets.session.keys()].sort(), ['s1', 's2', 's9'], 'the session facet ignores itself')
assert.equal(facets.session.get('s1').n, 3)
// ……而项目那一维受 s1 约束：只剩未归类（A 只有 s2 的事件）
assert.deepEqual([...facets.project.keys()], ['未归类'])

// 类型 / 序号 / 语义 一样收窄选项
facets = facetsOf({ kinds: ['教训'] })
assert.deepEqual([...facets.session.keys()], ['s9'])
facets = facetsOf({ ids: '1' }, { idSet: new Set([1]) })
assert.deepEqual([...facets.session.keys()], ['s1'])
assert.equal(facets.session.get('s1').n, 1)
facets = facetsOf({}, { semIds: new Set([4]), semActive: true })
assert.deepEqual([...facets.session.keys()], ['s2'])
assert.deepEqual([...facets.project.keys()], ['A'])

// 关系维度（边的端点池）也算一条约束
facets = facetCounts(facetEvents, blank, new Set(), null, false, new Set([3]))
assert.deepEqual([...facets.session.keys()], ['s9'], 'only the endpoints of the kept edges')

// 没有会话的事件不进会话选项，也不该把别的维度算坏
const noSession = facetCounts([{ id: 9, ts: 9, kind: '结果', project: 'B' }], blank, new Set(), null, false)
assert.equal(noSession.session.size, 0)
assert.equal(noSession.project.get('B').n, 1)

console.log('PASS facet options only list values that survive the other filters')
