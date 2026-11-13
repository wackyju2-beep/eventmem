// Run the actual Scene through the existing deterministic rAF harness.
const fs = require('node:fs')
const vm = require('node:vm')
const path = require('node:path')
const base = fs.readFileSync(path.join(__dirname, 'test_graph_flight.cjs'), 'utf8')
const test = `
// A dense one-hop neighborhood must all survive the initial retreat, including
// those beyond the eventual expansion budget. A grey pair is not a relationship.
effects=[]; elements.clear(); clock=0
const ev=Array.from({length:24},(_,i)=>({id:i+1,ts:i+1,kind:'决定',text:'atom'}))
const pos=new Map(ev.map(e=>[e.id,{x:(e.id-1)*0.12,y:0,z:0}]))
pos.set(23,{x:8,y:2,z:4}); pos.set(24,{x:0,y:1,z:0})
const s=buildScene({events:ev,edges:ev.slice(1,23).map(e=>({src:1,dst:e.id,rel:'细化'})),pairs:[{src:1,dst:24}]},pos)
const c={current:{yaw:0,pitch:0,dist:3.5,fx:0,fy:0,fz:0,panX:0,panY:0,fit:40,dirty:true,ready:true}}
const writes=[]
Scene({scene:s,focusInfo:{active:false},sel:{type:'atom',id:1},focusReq:{mode:'atom',id:1,seq:1},camRef:c,posRef:{current:new Map()},onMoved(ids){writes.push(...ids)}})
for(const effect of effects) effect()
frame(300)
assert.ok(Number(elements.get(20).style.opacity)>.9,'all true direct endpoints, not only the first 16, must stay bright during retreat')
assert.ok(Number(elements.get(24).style.opacity)>.9,'unrelated atoms must not fade or avoid before the retreat finishes')
const parseXY = id => {const m=/translate3d\\(([-\\d.]+)px, ([-\\d.]+)px/.exec(elements.get(id).style.transform);return [Number(m[1]),Number(m[2])]}
const projectFor = (a,c,t) => {const b=cameraBasis(c),phase=a.id*2.399963,seconds=t/1000,d=Math.hypot(a.x-b.ex,a.y-b.ey,a.z-b.ez),u=Math.max(0,Math.min(1,(d-2.4/.72)/(12-2.4/.72))),amp=.025*(1/3+2/3*u*u*(3-2*u)),x=a.x+amp*Math.sin(seconds*.47+phase),y=a.y+amp*Math.sin(seconds*.39+phase*1.7),z=a.z+amp*Math.cos(seconds*.43+phase),dx=x-b.ex,dy=y-b.ey,dz=z-b.ez,depth=dx*b.fx+dy*b.fy+dz*b.fz,scale=(400/Math.tan(26*Math.PI/360))/Math.max(depth,.35);return [600+(dx*b.rx+dy*b.ry+dz*b.rz)*scale+(c.panX||0),400-(dx*b.ux+dy*b.uy+dz*b.uz)*scale+(c.panY||0)]}
const earlyXY=parseXY(24), earlyExpected=projectFor(s.byId.get(24),c.current,300)
assert.ok(Math.hypot(earlyXY[0]-earlyExpected[0],earlyXY[1]-earlyExpected[1])<.2,'unrelated display position is not displaced during retreat')
frame(1050)
const basis=cameraBasis(c.current), focal=800/2/Math.tan(26*Math.PI/360)
for(const id of Array.from({length:23},(_,i)=>i+1)) {
  const a=s.byId.get(id), dx=a.x-basis.ex, dy=a.y-basis.ey, dz=a.z-basis.ez
  const depth=dx*basis.fx+dy*basis.fy+dz*basis.fz
  const x=398+focal*(dx*basis.rx+dy*basis.ry+dz*basis.rz)/depth
  const y=400-focal*(dx*basis.ux+dy*basis.uy+dz*basis.uz)/depth
  assert.ok(depth>.65 && x>=10 && x<=786 && y>=10 && y<=790, 'retreat should fit direct atom '+id+' at its actual depth: '+[x,y,depth])
}
frame(1300)
const midXY=parseXY(24), midExpected=projectFor(s.byId.get(24),c.current,1300)
const avoidance=Math.hypot(midXY[0]-midExpected[0],midXY[1]-midExpected[1])
assert.ok(avoidance>.1,'unrelated atom progressively moves aside only after retreat')
assert.ok(Number(elements.get(24).style.opacity)<.99,'unrelated opacity ramps after retreat')
assert.ok(!writes.includes(24),'display-only avoidance must never write unrelated atoms')
console.log('PASS complete one-hop visibility, delayed avoidance and depth-aware retreat')
`
new Function('require', '__dirname', base + '\n' + test)(require, __dirname)
