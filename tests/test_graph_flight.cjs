const assert = require('node:assert/strict')
const fs = require('node:fs')
const vm = require('node:vm')
const path = require('node:path')
const source = fs.readFileSync(path.join(__dirname, '../adapters/hermes/desktop-plugins/eventmem-graph/plugin.js'), 'utf8')
let effects = [], handlers = {}, raf = null, clock = 0
const stage = { getBoundingClientRect: () => ({width: 1200, height: 800}), addEventListener: (k, fn) => { handlers[k] = fn }, removeEventListener: () => {} }
const elements = new Map()
const makeElement = () => ({style:{display:'',opacity:'',setProperty(k,v){this[k]=v}},dataset:{}})
const jsx = (tag,props) => {
  if (props?.className === 'em-stage') props.ref.current = stage
  if (props?.className === 'em-atom') { const el=makeElement(); elements.set(props['data-id'],el); props.ref(el) }
  return {tag,props}
}
const ctx = { console, performance:{now:()=>clock}, Math, Map, Set, Number, String, Boolean, Date, Object, Array, globalThis: null,
  useRef: value => ({current:value}), useMemo: fn => fn(), useCallback: fn => fn,
  useEffect: fn => effects.push(fn), useState: value => [value, () => {}], jsx, jsxs:jsx, Button: 'button',
  ResizeObserver: class {observe(){} disconnect(){}}, getComputedStyle: () => ({getPropertyValue: () => ''}),
  document: {documentElement:{}}, window: {devicePixelRatio: 1}, requestAnimationFrame: fn => {raf=fn; return 1}, cancelAnimationFrame: () => {},
  setTimeout, clearTimeout }
ctx.globalThis = ctx
const code = source.replace(/import\s*\{[\s\S]*?\}\s*from\s*'@hermes\/plugin-sdk'\s*/, '').replace(/import[^\n]*\n/g, '').replace('export default {', 'const pluginDefault = {') + '\nglobalThis.__test = {buildScene, Scene, cameraBasis, AtomInspector, placeEdgeLabels: typeof placeEdgeLabels === "undefined" ? null : placeEdgeLabels};'
vm.runInNewContext(code, ctx, {filename:'plugin.js'})
const {buildScene, Scene, cameraBasis} = ctx.__test
const step = (a,b) => Math.hypot(a.x-b.x,a.y-b.y,a.z-b.z)
const events = Array.from({length: 50}, (_, i) => ({id:i+1,ts:i+1,kind:'决定',text:'atom'}))
const scene = buildScene({events,edges:[],pairs:[]}, new Map())
const cam = {current:{yaw:.55,pitch:.26,dist:3.5,fx:0,fy:0,fz:0,panX:0,panY:0,fit:40,dirty:true,ready:false}}
Scene({scene,focusInfo:{active:false},sel:null,focusReq:null,camRef:cam,posRef:{current:new Map()},onMoved(){}})
for (const effect of effects) effect()
const eye = () => {const {ex,ey,ez} = cameraBasis(cam.current); return {x:ex,y:ey,z:ez}}
const newest = scene.byId.get(scene.newestId)
assert.ok(step(eye(),newest) < 0.1, 'opening eye must be at newest atom')
const p0 = eye()
handlers.wheel({deltaY:-12000,preventDefault(){}})
clock=1000; raf(clock)
const p1 = eye()
handlers.wheel({deltaY:-12000,preventDefault(){}})
clock=2000; raf(clock)
const p2 = eye()
assert.ok(step(p0,p1)>0.1 && step(p1,p2)>0.1, 'wheel must keep flying through focus, not clamp there')
console.log('PASS opening eye and uninterrupted wheel flight')
// Settle old momentum before testing a new wheel impulse.
clock=4000; raf(clock)
const coastStart=eye()
handlers.wheel({deltaY:-120,preventDefault(){}})
const queued=eye()
assert.ok(step(coastStart,queued)<1e-8,'wheel must not jump immediately')
clock=4016; raf(clock)
const coastFirst=eye()
clock=4032; raf(clock)
assert.ok(step(coastStart,coastFirst)>0 && step(coastFirst,eye())>0,'wheel should coast across frames')
console.log('PASS wheel coasts smoothly')

function flightDistance(depth, deltaY, deltaMode=0) {
  effects=[]; handlers={}; elements.clear(); clock=0
  const s=buildScene({events:events.slice(0,1),edges:[],pairs:[]},new Map([[1,{x:0,y:0,z:-depth}]]))
  const c={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:-3.5,panX:0,panY:0,fit:40,dirty:true,ready:true}}
  Scene({scene:s,focusInfo:{active:false},sel:null,focusReq:null,camRef:c,posRef:{current:new Map()},onMoved(){throw Error('drift cannot save')}})
  for(const effect of effects) effect()
  clock=16; raf(clock)
  const start=c.current.fz
  handlers.wheel({deltaY,deltaMode,preventDefault(){}})
  for(clock=32;clock<=1000;clock+=16) raf(clock)
  return Math.abs(c.current.fz-start)
}
const nearSpeed=flightDistance(1,-120), farSpeed=flightDistance(12,-120)
assert.ok(farSpeed>nearSpeed*2, `far flight must be faster: ${nearSpeed} vs ${farSpeed}`)
const lineSpeed=flightDistance(12,-3,1),pixelSpeed=flightDistance(12,-48,0)
assert.ok(Math.abs(lineSpeed-pixelSpeed)<1e-8,'wheel deltaMode normalizes line and pixel events')
console.log('PASS flight distance scaling and delta normalization')

// Interrupting before the scheduled pull must not persist untouched neighbors.
effects=[]; handlers={}; elements.clear(); clock=0
const idleScene=buildScene({events:events.slice(0,2),edges:[{src:1,dst:2,rel:'细化'}],pairs:[]},new Map([[1,{x:0,y:0,z:-2}],[2,{x:1,y:0,z:-2}]]))
const idleCam={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:-3.5,panX:0,panY:0,fit:40,dirty:true,ready:true}}
const idleWrites=[]
Scene({scene:idleScene,focusInfo:{active:false},sel:{type:'atom',id:1},focusReq:{mode:'atom',id:1,seq:1},camRef:idleCam,posRef:{current:new Map()},onMoved(ids){idleWrites.push(...ids)}})
for(const effect of effects) effect()
clock=300; raf(clock)
handlers.wheel({deltaY:-20,preventDefault(){}})
assert.deepEqual(idleWrites,[],'interrupting before pull cannot persist untouched positions')
console.log('PASS interrupted scheduled pull does not save')

effects=[]; handlers={}; clock=0
const positions = new Map([[1,{x:0,y:0,z:0}], [2,{x:1,y:.2,z:.1}], [3,{x:1.5,y:.4,z:.4}]])
const chain=buildScene({events:events.slice(0,3),edges:[{src:1,dst:2,rel:'细化'}, {src:2,dst:3,rel:'细化'}],pairs:[]},positions)
const camera={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:-3.5,panX:0,panY:0,fit:40,dirty:true,ready:true}}
const before = [...chain.atoms].map(a=>({id:a.id,x:a.x,y:a.y,z:a.z}))
const saved=[]
Scene({scene:chain,focusInfo:{active:false},sel:{type:'atom',id:1},focusReq:{mode:'atom',id:1,seq:1},camRef:camera,posRef:{current:positions},onMoved(ids){saved.push(...ids)}})
for(const effect of effects) effect()
function frame(t) {clock=t; raf(t)}
frame(300)
const at300=chain.byId.get(2); const early1=step(at300,before.find(a=>a.id===2))
const early2=step(chain.byId.get(3),before.find(a=>a.id===3))
assert.ok(early1 < 1e-8 && early2 < 1e-8, 'highlight and camera retreat precede node expansion')
frame(1500)
const mid1=step(chain.byId.get(2),before.find(a=>a.id===2))
frame(2400)
const late2=step(chain.byId.get(3),before.find(a=>a.id===3))
assert.ok(camera.current.dist > 3.5, 'focus must retreat out from the cluster')
assert.ok(mid1 > 0.01, 'one-hop layer should move after camera retreat')
assert.ok(late2 > 0.01 && late2 > early2, 'two-hop layer should expand later in 3D')
console.log('PASS focused camera retreat and two-hop expansion')

// A rejected pair is editable evidence, not a graph link for expansion.
effects=[]; elements.clear(); clock=0
const mixed=buildScene({events:events.slice(0,4),edges:[{src:1,dst:2,rel:'细化'}, {src:2,dst:3,rel:'因为'}],pairs:[{src:1,dst:4}]},new Map([[1,{x:0,y:0,z:0}],[2,{x:1,y:0,z:0}],[3,{x:2,y:1,z:0}],[4,{x:0,y:1,z:0}]]))
const mixedCam={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:-3.5,panX:0,panY:0,fit:40,dirty:true,ready:true}}
const rejectedBefore={...mixed.byId.get(4)}, savedMixed=[]
Scene({scene:mixed,focusInfo:{active:false},sel:{type:'atom',id:1},focusReq:{mode:'atom',id:1,seq:1},camRef:mixedCam,posRef:{current:new Map()},onMoved(ids){savedMixed.push(...ids)}})
for(const effect of effects) effect()
frame(1500)
assert.equal(step(rejectedBefore,mixed.byId.get(4)),0,'none pair must not cause neighbor motion')
assert.ok(!savedMixed.includes(4),'none pair must not be persisted as expanded')
assert.ok(mixed.edges.some(e=>e.kind==='none'),'none pair remains available for display and editing')
console.log('PASS rejected pair does not expand')

// Repeated focus must settle rather than multiply world distances and camera retreat.
function expandAgain(scene, center, now) {
  effects=[]; elements.clear(); clock=now
  const camera={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:-3.5,panX:0,panY:0,fit:40,dirty:true,ready:true}}
  Scene({scene,focusInfo:{active:false},sel:{type:'atom',id:center},focusReq:{mode:'atom',id:center,seq:1},camRef:camera,posRef:{current:new Map()},onMoved(){}})
  for(const effect of effects) effect()
  frame(now+2600)
  return camera.current.dist
}
const stable=buildScene({events:events.slice(0,3),edges:[{src:1,dst:2,rel:'细化'},{src:2,dst:3,rel:'因为'}],pairs:[]},new Map([[1,{x:0,y:0,z:0}],[2,{x:1,y:0,z:0}],[3,{x:2,y:1,z:0}]]))
expandAgain(stable,1,0)
const once=stable.atoms.map(a=>({...a}))
const dist2=expandAgain(stable,1,3000)
assert.ok(stable.atoms.every(a=>step(a,once.find(b=>b.id===a.id))<1e-7),'repeating the same expansion must not keep moving the atoms')
const dist3=expandAgain(stable,1,6000)
assert.ok(Math.abs(dist3-dist2)<1e-7,'repeated expansion must not increase camera distance')
console.log('PASS repeat expansion settles')

// Dense neighborhoods are bounded on stage, but the scene keeps every relation for the inspector.
const denseEvents=events.slice(0,50)
const denseEdges=denseEvents.slice(1).map(e=>({src:1,dst:e.id,rel:'细化'}))
const dense=buildScene({events:denseEvents,edges:denseEdges,pairs:[]},new Map())
effects=[]; elements.clear(); clock=0
const denseSaved=[]
const denseCam={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:-3.5,panX:0,panY:0,fit:40,dirty:true,ready:true}}
Scene({scene:dense,focusInfo:{active:false},sel:{type:'atom',id:1},focusReq:{mode:'atom',id:1,seq:1},camRef:denseCam,posRef:{current:new Map()},onMoved(ids){denseSaved.push(...ids)}})
for(const effect of effects) effect()
frame(2600)
assert.ok(denseSaved.length<=33,`dense focus should move at most 32 neighbors, got ${denseSaved.length}`)
assert.equal(dense.edges.length,49,'all relations remain available for inspection')
const shell=dense.atoms.filter(a=>denseSaved.includes(a.id))
let minPair=Infinity
for(let i=0;i<shell.length;i++) for(let j=i+1;j<shell.length;j++) minPair=Math.min(minPair,step(shell[i],shell[j]))
assert.ok(minPair>0.9,`dense shell must separate neighboring nodes, minimum distance ${minPair}`)
console.log('PASS dense focus is bounded without deleting relations')
// Dense expansion is explicitly disclosed; the inspector must retain all entries.
effects=[]
const inspector=ctx.__test.AtomInspector({atom:dense.byId.get(1),scene:dense,ctx:{},onSaved(){},onSelectEdge(){},onClose(){}})
const inspectorText=JSON.stringify(inspector)
assert.ok(inspectorText.includes('每层最多 16'), 'inspector should explain the stage limit')
assert.ok(inspectorText.includes('连着的边（49）'),'inspector shows the complete relationship count')
console.log('PASS stage limit is disclosed in inspector')
assert.ok(ctx.__test.placeEdgeLabels, 'dense edge labels need collision-aware placement')
const kept=ctx.__test.placeEdgeLabels([{x:100,y:100,size:10,text:'细化'}, {x:105,y:101,size:10,text:'因为'}, {x:105,y:101,size:10,text:'改变',selected:true}, {x:250,y:200,size:10,text:'重复'}])
assert.equal(kept.length,2,'overlapping labels are omitted')
assert.ok(kept.some(x=>x.selected),'selected label wins collisions')
console.log('PASS selected edge labels take precedence over collisions')

// A remote unselected atom must not force the visible shell into a tiny cluster.
const outlier=buildScene({events:events.slice(0,4),edges:[{src:1,dst:2,rel:'细化'},{src:2,dst:3,rel:'因为'}],pairs:[]},new Map([[1,{x:0,y:0,z:0}],[2,{x:1,y:0,z:0}],[3,{x:2,y:1,z:0}],[4,{x:20,y:0,z:0}]]))
const outlierDistance=expandAgain(outlier,1,0)
assert.ok(outlierDistance>3.5,'camera retreats to reveal the linked neighborhood')
assert.ok(outlierDistance<40,'unselected outlier must not force the entire cloud to fit in view')
console.log('PASS distant outlier does not shrink focused shell')

effects=[]; elements.clear(); clock=0
const behind=buildScene({events:events.slice(0,1),edges:[],pairs:[]},new Map([[1,{x:0,y:0,z:1}]]))
const view={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:-3.5,panX:0,panY:0,fit:40,dirty:true,ready:true}}
Scene({scene:behind,focusInfo:{active:false},sel:null,focusReq:null,camRef:view,posRef:{current:new Map()},onMoved(){}})
for(const effect of effects) effect()
frame(20)
assert.ok(elements.get(1).style.display === 'none' || elements.get(1).style.opacity === '0', 'atom behind the camera must not appear in front')
console.log('PASS behind-camera atom is hidden')

// A swarm behind the eye must never consume the visible card budget.
effects=[]; elements.clear(); clock=0
const crowdEvents=Array.from({length:100},(_,i)=>({id:i+1,ts:i+1,kind:'决定',text:'atom'}))
const crowdPos=new Map(crowdEvents.map(e=>[e.id,e.id===1?{x:0,y:0,z:-2}:{x:0,y:0,z:1}]))
const crowd=buildScene({events:crowdEvents,edges:[],pairs:[]},crowdPos)
const crowdCam={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:-3.5,panX:0,panY:0,fit:40,dirty:true,ready:true}}
Scene({scene:crowd,focusInfo:{active:false},sel:null,focusReq:null,camRef:crowdCam,posRef:{current:new Map()},onMoved(){}})
for(const effect of effects) effect()
frame(20)
assert.ok(Number(elements.get(1).style['--m']) > .9, 'visible near atom must morph into a card even with many atoms behind camera')
console.log('PASS behind-camera atoms do not consume card budget')

// A manual drag must take ownership of the atom, rather than fighting a stale animation.
effects=[]; elements.clear(); clock=0
const moving=buildScene({events:events.slice(0,2),edges:[{src:1,dst:2,rel:'细化'}],pairs:[]},new Map([[1,{x:0,y:0,z:0}],[2,{x:1,y:0,z:0}]]))
const view2={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:-3.5,panX:0,panY:0,fit:40,dirty:true,ready:true}}
const mounted=Scene({scene:moving,focusInfo:{active:false},sel:{type:'atom',id:1},focusReq:{mode:'atom',id:1,seq:1},camRef:view2,posRef:{current:new Map()},onMoved(){}})
for(const effect of effects) effect()
frame(300)
const interruptedAt={...moving.byId.get(1)}
handlers.wheel({deltaY:-20,preventDefault(){}})
frame(400)
assert.ok(step(interruptedAt,moving.byId.get(1))<1e-8, 'manual flight must stop the expansion at its current position')
console.log('PASS wheel interrupts expansion consistently')
const target={closest:()=>({dataset:{id:'2'}})}
mounted.props.onPointerDown({button:0,preventDefault(){},target,pointerId:1,clientX:0,clientY:0})
mounted.props.onPointerMove({clientX:200,clientY:0,currentTarget:{setPointerCapture(){}}})
const dragged={...moving.byId.get(2)}
frame(900)
assert.ok(step(dragged,moving.byId.get(2)) < 1e-8, 'manual drag must cancel that atom animation')
console.log('PASS manual drag interrupts atom animation')

// Middle-drag must translate the physical camera and change distances to nodes.
effects=[]; elements.clear(); clock=0
const panScene=buildScene({events:events.slice(0,1),edges:[],pairs:[]},new Map([[1,{x:1,y:0,z:-3}]]))
const panCam={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:-3.5,panX:0,panY:0,fit:40,dirty:true,ready:true}}
const panView=Scene({scene:panScene,focusInfo:{active:false},sel:null,focusReq:null,camRef:panCam,posRef:{current:new Map()},onMoved(){throw Error('pan cannot save')}})
for(const effect of effects) effect()
const panBefore=cameraBasis(panCam.current), panAtom=panScene.byId.get(1)
panView.props.onPointerDown({button:1,preventDefault(){},clientX:0,clientY:0,pointerId:1})
panView.props.onPointerMove({clientX:180,clientY:60,currentTarget:{setPointerCapture(){}}})
const panAfter=cameraBasis(panCam.current)
assert.ok(Math.hypot(panAfter.ex-panBefore.ex,panAfter.ey-panBefore.ey,panAfter.ez-panBefore.ez)>.1,'middle drag must move the 3D eye')
assert.ok(Math.abs(Math.hypot(panAfter.ex-panAtom.x,panAfter.ey-panAtom.y,panAfter.ez-panAtom.z)-Math.hypot(panBefore.ex-panAtom.x,panBefore.ey-panAtom.y,panBefore.ez-panAtom.z))>.01,'middle drag changes actual node distance')
console.log('PASS middle drag translates eye and changes distance')

const speed12=flightDistance(12,120), speed48=flightDistance(48,120), speed120=flightDistance(120,120)
assert.ok(speed48>speed12*2 && speed120>speed48*1.5,`remote retreat should keep accelerating: ${speed12}, ${speed48}, ${speed120}`)
console.log(`PASS remote retreat scales at multiple distances: ${speed12.toFixed(3)}, ${speed48.toFixed(3)}, ${speed120.toFixed(3)}`)

assert.ok(flightDistance(-120,120)>flightDistance(-12,120)*4,'retreat must accelerate when the entire cloud is behind the eye')
console.log('PASS retreat speed follows distance even if cloud is behind')

// Equal depth is not equal eye distance when nodes lie off axis.
effects=[]; elements.clear(); clock=0
const depthScene=buildScene({events:events.slice(0,2),edges:[],pairs:[]},new Map([[1,{x:0,y:0,z:-4}],[2,{x:1.4,y:0,z:-4}]]))
const depthCam={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:-3.5,panX:0,panY:0,fit:40,dirty:true,ready:true}}
Scene({scene:depthScene,focusInfo:{active:false},sel:null,focusReq:null,camRef:depthCam,posRef:{current:new Map()},onMoved(){}})
for(const effect of effects) effect(); frame(20)
const nearMorph=Number(elements.get(1).style['--m']), offMorph=Number(elements.get(2).style['--m'])
assert.ok(nearMorph>offMorph+.01,`morph must depend on 3D distance: ${nearMorph} vs ${offMorph}`)
console.log('PASS morph uses actual eye distance')

// Two near-card centers projecting to the same screen spot must not render as stacked cards.
effects=[]; elements.clear(); clock=0
const overlapScene=buildScene({events:events.slice(0,2),edges:[],pairs:[]},new Map([[1,{x:0,y:0,z:-3}],[2,{x:0,y:0,z:-4}]]))
const overlapCam={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:-3.5,panX:0,panY:0,fit:40,dirty:true,ready:true}}
Scene({scene:overlapScene,focusInfo:{active:false},sel:null,focusReq:null,camRef:overlapCam,posRef:{current:new Map()},onMoved(){}})
for(const effect of effects) effect(); frame(20)
assert.ok(Number(elements.get(2).style['--m'])<.25,'farther overlapping card should smoothly recede to a ball')
console.log('PASS projected overlap prevents stacked cards')

assert.ok(!/background:var\(--ui-bg-input\)/.test(source),'inspector fields must not use the host white input background')
assert.ok(!/jsx\('select'/.test(source),'native select menus ignore desktop dark colors')
console.log('PASS themed inspector and dropdown')

// Motion of a blocker through an intermediate morph must not cause an abrupt jump.
function collisionMorph(blockerDepth) {
 effects=[]; elements.clear(); clock=0
 const s=buildScene({events:events.slice(0,3),edges:[],pairs:[]},new Map([[1,{x:0,y:0,z:-blockerDepth}],[2,{x:0.03,y:0,z:-5.5}],[3,{x:0,y:3,z:-5}]]))
 const c={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:-3.5,panX:0,panY:0,fit:40,dirty:true,ready:true}}
 Scene({scene:s,focusInfo:{active:false},sel:null,focusReq:null,camRef:c,posRef:{current:new Map()},onMoved(){}})
 for(const effect of effects)effect();frame(20);return Number(elements.get(2).style['--m'])
}
let morphs=Array.from({length:241},(_,i)=>collisionMorph(3+i*.01))
let maxJump=Math.max(...morphs.slice(1).map((v,i)=>Math.abs(v-morphs[i])))
assert.ok(maxJump<.12,`blocker morph transitions continuously; max jump ${maxJump}`)
console.log('PASS overlap handling stays continuous')

// Similar-depth neighbors swapping depth must not pop abruptly.
function crossing(depth) {
 effects=[]; elements.clear(); clock=0
 const s=buildScene({events:events.slice(0,2),edges:[],pairs:[]},new Map([[1,{x:0,y:0,z:-4}],[2,{x:.02,y:0,z:-depth}]]))
 const c={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:-3.5,panX:0,panY:0,fit:40,dirty:true,ready:true}}
 Scene({scene:s,focusInfo:{active:false},sel:null,focusReq:null,camRef:c,posRef:{current:new Map()},onMoved(){}})
 for(const effect of effects)effect();frame(20);return [1,2].map(id=>Number(elements.get(id).style['--m']))
}
let cross=Array.from({length:81},(_,i)=>crossing(3.6+i*.01))
let jump=Math.max(...cross.slice(1).map((v,i)=>Math.max(...v.map((x,j)=>Math.abs(x-cross[i][j])))))
assert.ok(jump<.2,`crossing neighbors must morph continuously, jump ${jump}`)
console.log('PASS crossing neighbors do not pop')

// Ball labels are plain digits, while the expanded card retains its # prefix.
{
  effects=[]; elements.clear(); clock=0
  const s=buildScene({events:[{id:123,ts:1,kind:'决定',text:'atom'}],edges:[],pairs:[]},new Map())
  const c={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:-3.5,panX:0,panY:0,fit:40,dirty:true,ready:true}}
  const tree=Scene({scene:s,focusInfo:{active:false},sel:null,focusReq:null,camRef:c,posRef:{current:new Map()},onMoved(){}})
  const collect=(v, out=[])=>{if (!v || typeof v !== 'object') return out; if(v.props?.className==='em-atom-num') out.push(v.props.children); for(const x of Object.values(v)) if(typeof x==='object') collect(x,out); return out}
  assert.deepEqual(collect(tree),['123'],'ball shows the complete number without #')
  console.log('PASS ball number is plain digits')
}

// Display-only drift must not alter the morph, including collision interactions.
{
 effects=[]; elements.clear(); clock=0
 const s=buildScene({events:events.slice(0,3),edges:[],pairs:[]},new Map([[1,{x:0,y:0,z:-4}],[2,{x:.07,y:0,z:-5.5}],[3,{x:1.2,y:0,z:-5}]]))
 const c={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:-3.5,panX:0,panY:0,fit:40,dirty:true,ready:true}}
 Scene({scene:s,focusInfo:{active:false},sel:null,focusReq:null,camRef:c,posRef:{current:new Map()},onMoved(){throw Error('display drift cannot save')}})
 for(const effect of effects)effect()
 frame(20); const first=[1,2,3].map(id=>elements.get(id).style['--m'])
 for(let t=100;t<=5000;t+=100){frame(t);assert.deepEqual([1,2,3].map(id=>elements.get(id).style['--m']),first,`morph must be stable at ${t}ms`)}
 s.byId.get(1).x+=2; frame(5100)
 assert.notDeepEqual([1,2,3].map(id=>elements.get(id).style['--m']),first,'real movement must update morphology')
 console.log('PASS floating never changes morph but real movement does')
}

// Near-plane visibility must also be stable when only display drift changes.
{
 effects=[]; elements.clear(); clock=0
 const s=buildScene({events:events.slice(0,1),edges:[],pairs:[]},new Map([[1,{x:0,y:0,z:-.4}]]))
 const c={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:-3.5,panX:0,panY:0,fit:40,dirty:true,ready:true}}
 Scene({scene:s,focusInfo:{active:false},sel:null,focusReq:null,camRef:c,posRef:{current:new Map()},onMoved(){}})
 for(const effect of effects)effect();frame(20);const initial=elements.get(1).style.opacity
 for(let t=100;t<=5000;t+=100){frame(t);assert.equal(elements.get(1).style.opacity,initial,'drift must not change near-plane visibility')}
 console.log('PASS floating does not change near-plane visibility')
}

// Close full cards drift at most one third of the former per-axis amplitude;
// distant balls retain more motion. Same phase makes the comparison exact.
{
 const samples = depth => {
   effects=[]; elements.clear(); clock=0
   const s=buildScene({events:events.slice(0,1),edges:[],pairs:[]},new Map([[1,{x:0,y:0,z:-depth}]]))
   const c={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:-3.5,panX:0,panY:0,fit:40,dirty:true,ready:true}}
   Scene({scene:s,focusInfo:{active:false},sel:null,focusReq:null,camRef:c,posRef:{current:new Map()},onMoved(){}})
   for(const effect of effects)effect()
   const ys=[]; for(let t=0;t<=16000;t+=100){frame(t);ys.push(Number(/px, ([-\d.]+)px/.exec(elements.get(1).style.transform)[1]))}
   return {range:Math.max(...ys)-Math.min(...ys),m:Number(elements.get(1).style['--m'])}
 }
 const near=samples(2), far=samples(12)
 // Convert screen displacement back to world units (perspective depth is fixed to first order).
 const nearWorld=near.range*2, farWorld=far.range*12
 assert.equal(near.m,1,'near target is a fully expanded card')
 assert.ok(nearWorld<farWorld*.38,`near drift must be at most one third: ${nearWorld} vs ${farWorld}`)
 console.log('PASS near card drift shrinks relative to distant ball')
}
