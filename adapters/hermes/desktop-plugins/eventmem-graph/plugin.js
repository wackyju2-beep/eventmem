/**
 * eventmem-graph — the 3D memory graph, as a component.
 *
 * Headless: it registers no route and no status-bar chip. The graph is the 图谱 tab inside
 * `eventmem-gui`; this module keeps the rendering (orbit/zoom/focus/edit) and the data path
 * (`/api/plugins/eventmem-graph`) and hands its component to the console through
 * `globalThis.__eventmemGraph`.
 *
 * Drag = orbit, wheel = zoom, click an atom/edge = focus + inspect; edits get a rainbow rim.
 *
 * Plain ESM, loaded uncompiled — jsx() calls, not JSX syntax.
 */

import {
  Button,
  EmptyState,
  GlyphSpinner,
  haptic,
  useQuery,
  useQueryClient
} from '@hermes/plugin-sdk'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'eventmem-graph'

const KIND_COLORS = {
  决定: '#7ea9e0',
  尝试: '#68c3ba',
  结果: '#79c08c',
  变更: '#d7a35f',
  教训: '#d97c92'
}
// 关系色是一族：同样的明度、都压掉一半饱和度，线才不会互相抢。
const REL_COLORS = {
  改变: '#d9a05b',
  否定: '#d0707f',
  重复: '#6fae9c',
  因为: '#a08ad8',
  细化: '#6f9fd8'
}
const NONE_COLOR = '#5f6672'
// 人工改过的边原来吃 SVG 的 linearGradient；canvas 里一个纯色更省，也更清楚。
const EDITED_COLOR = '#c58bff'
const KINDS = ['决定', '尝试', '结果', '变更', '教训']
const RELS = ['改变', '否定', '重复', '因为', '细化']
const AXIS_LABELS = ['重复', '改变', '否定', '都不是']
const DETAIL_LABELS = ['因为', '细化', '都不是']

// Disk plugins are not scanned by Tailwind: every class below is plain CSS, colored from the
// app's own theme vars so the graph follows the active skin.
const CSS = `
.em-root{position:relative;height:100%;min-height:360px;overflow:hidden;color:var(--ui-text-primary);font-size:12px}
.em-root.em-center{display:grid;place-items:center;align-content:center;gap:8px;padding:28px;text-align:center}
.em-stage{position:absolute;inset:0;overflow:hidden;cursor:grab;touch-action:none;
  background:radial-gradient(circle at 50% 42%,color-mix(in srgb,var(--ui-accent) 8%,transparent),transparent 64%),
             radial-gradient(circle at 50% 50%,transparent 38%,color-mix(in srgb,var(--ui-bg-primary) 62%,transparent) 100%)}
.em-stage:active{cursor:grabbing}
/* Edges live on two canvases: a back one under the atom cards, a front one for the
   selected edge and the labels. Drawing 2k+ edges as <path> elements meant ~13k
   attribute writes per frame and dropped the whole window to single-digit fps. */
.em-edges{position:absolute;inset:0;pointer-events:none;width:100%;height:100%}
.em-edge-back{z-index:1}
.em-edges-front{z-index:25000}
.em-atom-layer{position:absolute;inset:0;pointer-events:none}
/* An atom is ONE element whose geometry morphs with distance: a 20px ball at --m 0 grows
   into the 176x56 card at --m 1, and its border-radius goes 10px -> 9px, which is exactly a
   circle at 20px. Everything (size, radius, which label shows) is driven by the single
   --m custom property, so the shape transform is continuous instead of a cross-fade.
   The card body never gets transform-scaled, so its text stays crisp at every size. */
.em-atom{position:absolute;left:0;top:0;width:176px;height:56px;pointer-events:none;
  user-select:none;contain:layout style paint;will-change:transform}
/* The wrapper is 176x56 and the body shrinks inside it, so the body must be centred —
   anchored top-left it drifts away from the projected point and the edges then miss it. */
.em-atom-body{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);overflow:hidden;box-sizing:border-box;
  pointer-events:auto;cursor:pointer;
  /* 三段连续形态：--f 0→1 把 20px 小球收成 6px 实心点（远景只留颜色），--m 0→1 再把点长成卡片。 */
  width:calc((20px - 14px * var(--f,0)) + 156px * var(--m,0));
  height:calc((20px - 14px * var(--f,0)) + 36px * var(--m,0));
  border-radius:calc(10px - var(--m,0) * 1px);
  background:color-mix(in srgb,var(--em-kind-color,var(--ui-text-secondary)) calc(var(--f,0) * 100%),var(--ui-bg-card));
  border-style:solid;
  border-color:color-mix(in srgb,var(--em-kind-color,var(--ui-text-secondary)) calc((1 - var(--m,0)) * 100%),var(--ui-stroke-secondary));
  border-width:1px;border-left-width:calc(1px + 2px * var(--m,0));border-left-color:var(--em-kind-color,var(--ui-text-secondary));
  box-shadow:0 2px 8px color-mix(in srgb,var(--ui-base) 30%,transparent)}
.em-atom-num{position:absolute;left:0;right:0;top:50%;transform:translateY(-50%);text-align:center;
  font:700 8px/1 var(--font-mono,ui-monospace,monospace);color:var(--ui-text-secondary);
  opacity:calc((1 - var(--m,0)) * (1 - var(--f,0)));pointer-events:none}
.em-atom-card{position:absolute;left:0;top:0;width:176px;height:56px;box-sizing:border-box;
  padding:5px 9px;pointer-events:none;opacity:clamp(0,calc((var(--m,0) - 0.5) * 2.4),1)}
.em-atom[data-kind="决定"] .em-atom-body{--em-kind-color:#7ea9e0}
.em-atom[data-kind="尝试"] .em-atom-body{--em-kind-color:#68c3ba}
.em-atom[data-kind="结果"] .em-atom-body{--em-kind-color:#79c08c}
.em-atom[data-kind="变更"] .em-atom-body{--em-kind-color:#d7a35f}
.em-atom[data-kind="教训"] .em-atom-body{--em-kind-color:#d97c92}
.em-atom:hover .em-atom-body{box-shadow:0 6px 18px color-mix(in srgb,var(--ui-base) 42%,transparent)}
.em-atom[data-selected="1"] .em-atom-body{box-shadow:0 0 0 1.5px color-mix(in srgb,var(--ui-accent) 80%,transparent),0 10px 26px color-mix(in srgb,var(--ui-base) 50%,transparent)}
.em-atom[data-edited="1"] .em-atom-body::after{content:'';position:absolute;inset:-1px;border-radius:inherit;padding:2px;pointer-events:none;
  background:conic-gradient(from 0deg,#ff5f6d,#ffc371,#a8e063,#56ccf2,#9b73ff,#ff5f6d);
  -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);
  -webkit-mask-composite:xor;mask-composite:exclude;animation:em-hue 7s linear infinite}
.em-atom-head{display:flex;align-items:center;gap:5px;font-size:9px;line-height:1.25;letter-spacing:.01em}
.em-atom-id{font-family:var(--font-mono,ui-monospace,monospace);font-weight:700;color:var(--ui-text-primary)}
.em-atom-kind{font-weight:600}
.em-atom-time{margin-left:auto;color:var(--ui-text-quaternary);font-variant-numeric:tabular-nums;white-space:nowrap}
.em-atom-text{margin-top:3px;font-size:9.5px;line-height:1.4;color:var(--ui-text-secondary);word-break:break-word;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.em-atom-edited{position:absolute;right:5px;bottom:3px;font-size:9px;color:var(--ui-accent);opacity:.9}
@keyframes em-hue{to{filter:hue-rotate(360deg)}}
.em-bar{position:absolute;left:10px;right:10px;top:10px;z-index:40000;display:flex;flex-wrap:wrap;align-items:center;gap:6px;
  padding:6px 8px;border-radius:9px;pointer-events:auto;
  background:color-mix(in srgb,var(--ui-bg-elevated) 82%,transparent);border:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 70%,transparent);
  backdrop-filter:blur(12px)}
.em-brand{font-weight:650;letter-spacing:.02em;margin-right:2px}
.em-seg{display:inline-flex;border:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 70%,transparent);border-radius:6px;overflow:hidden}
.em-seg-btn{font-size:11px;padding:3px 8px;line-height:1.35;color:var(--ui-text-tertiary);background:transparent;border:0;cursor:pointer}
.em-seg-btn:hover{background:var(--chrome-action-hover);color:var(--ui-text-primary)}
.em-seg-btn[data-on="1"]{background:color-mix(in srgb,var(--ui-accent) 20%,transparent);color:var(--ui-text-primary)}
.em-select{height:24px;font-size:11px;padding:0 6px;border-radius:5px;background:var(--ui-bg-tertiary);color:var(--ui-text-primary);
  border:1px solid var(--ui-stroke-secondary);outline:none;max-width:260px;
  appearance:none;-webkit-appearance:none}
  /* --ui-bg-input is #fcfcfc in both themes (host styles.css:314), so a native select
     stays white in dark mode; appearance:none is what stops the UA painting it. */
.em-value{min-width:150px}
.em-dd{position:relative;min-width:0}
.em-dd-btn{width:100%;text-align:left;cursor:pointer;display:flex;justify-content:space-between;gap:12px;align-items:center}
.em-dd-list{position:absolute;top:100%;left:0;right:0;z-index:50000;max-height:240px;overflow:auto;background:var(--ui-bg-elevated,var(--ui-bg-editor,#202329));border:1px solid var(--ui-stroke-secondary);border-radius:5px;box-shadow:0 8px 20px #0004}
.em-dd-item{display:block;width:100%;text-align:left;padding:6px 8px;border:0;background:transparent;color:var(--ui-text-primary);font:inherit;cursor:pointer}
.em-dd-item:hover,.em-dd-item[aria-selected="true"]{background:color-mix(in srgb,var(--ui-accent) 20%,transparent)}
.em-check{display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--ui-text-tertiary);cursor:pointer;user-select:none}
.em-check input{accent-color:var(--ui-accent);margin:0}
.em-counts{font-size:10.5px;color:var(--ui-text-quaternary);font-variant-numeric:tabular-nums}
.em-spacer{flex:1}
.em-fbtn{display:inline-flex;align-items:center;gap:5px;height:24px;padding:0 9px;border-radius:6px;cursor:pointer;
  font-size:11px;line-height:1;background:transparent;color:var(--ui-text-tertiary);
  border:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 70%,transparent)}
.em-fbtn:hover{background:var(--chrome-action-hover);color:var(--ui-text-primary)}
.em-fbtn[data-on="1"]{background:color-mix(in srgb,var(--ui-accent) 20%,transparent);color:var(--ui-text-primary);
  border-color:color-mix(in srgb,var(--ui-accent) 55%,transparent)}
.em-fbtn[disabled]{opacity:.5;cursor:default}
.em-fbadge{font:700 9.5px/1 var(--font-mono,ui-monospace,monospace);padding:2px 4px;border-radius:999px;
  background:color-mix(in srgb,var(--ui-accent) 34%,transparent);color:var(--ui-text-primary)}
.em-panel{position:absolute;left:-1px;right:-1px;top:calc(100% + 6px);z-index:45000;padding:10px 12px;
  border-radius:10px;border:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 80%,transparent);
  background:color-mix(in srgb,var(--ui-bg-elevated,var(--ui-bg-secondary,#202329)) 96%,transparent);box-shadow:0 18px 40px #0005;
  display:grid;gap:8px}
.em-panel-head{display:flex;align-items:baseline;gap:8px;font-size:11.5px;font-weight:650;color:var(--ui-text-primary)}
.em-pgrid{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:10px 18px;align-items:start}
@media (max-width:760px){.em-pgrid{grid-template-columns:minmax(0,1fr)}}
.em-pcol{display:grid;gap:6px;min-width:0}
.em-ptitle{font-size:10px;font-weight:700;letter-spacing:.06em;color:var(--ui-text-quaternary)}
.em-prow{display:flex;flex-wrap:wrap;align-items:center;gap:6px;min-width:0}
.em-plabel{font-size:10.5px;color:var(--ui-text-quaternary);min-width:24px}
.em-pselect{flex:1 1 104px;min-width:96px}
.em-input{height:24px;font-size:11px;padding:0 7px;border-radius:5px;background:var(--ui-bg-tertiary,var(--ui-bg-secondary,#202329));
  color:var(--ui-text-primary);border:1px solid var(--ui-stroke-secondary);outline:none;min-width:0}
.em-input:focus{border-color:color-mix(in srgb,var(--ui-accent) 60%,transparent)}
.em-idinput{flex:1 1 120px;max-width:220px;font-family:var(--font-mono,ui-monospace,monospace)}
.em-sem{display:flex;align-items:center;gap:5px;flex:1 1 240px;min-width:190px;max-width:430px}
.em-seminput{flex:1}
.em-semtag{display:inline-flex;align-items:center;gap:4px;font-size:10.5px;color:var(--ui-text-secondary);
  padding:2px 4px 2px 7px;border-radius:999px;white-space:nowrap;
  border:1px solid color-mix(in srgb,var(--ui-accent) 45%,transparent)}
.em-fchip{display:inline-flex;align-items:center;gap:5px;font-size:11px;line-height:1;padding:4px 8px;border-radius:999px;
  border:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 80%,transparent);background:transparent;
  color:var(--ui-text-tertiary);cursor:pointer}
.em-fchip:hover{color:var(--ui-text-primary);background:var(--chrome-action-hover)}
.em-fchip[data-on="1"]{color:var(--ui-text-primary);background:color-mix(in srgb,var(--ui-accent) 18%,transparent);
  border-color:color-mix(in srgb,var(--ui-accent) 60%,transparent)}
.em-fdot{width:7px;height:7px;border-radius:2px;display:inline-block;flex:0 0 auto}
.em-fline{width:12px;height:2px;border-radius:1px;display:inline-block;flex:0 0 auto}
.em-fline-dash{height:0;border-top:1.5px dashed;background:none!important}
.em-hits{display:flex;flex-wrap:wrap;gap:5px;max-height:112px;overflow:auto}
.em-hit{font:600 10px/1 var(--font-mono,ui-monospace,monospace);padding:4px 6px;border-radius:5px;white-space:nowrap;
  border:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 80%,transparent);background:transparent;
  color:var(--ui-text-secondary);cursor:pointer}
.em-hit:hover{color:var(--ui-text-primary);border-color:color-mix(in srgb,var(--ui-accent) 55%,transparent)}
.em-hit b{font-weight:700;color:var(--ui-text-primary)}
.em-hintline{font-size:10.5px;color:var(--ui-text-quaternary);line-height:1.5}
.em-warn{font-size:10.5px;color:#d0707f;line-height:1.5}
.em-x-inline{padding:0;border:0;background:transparent;color:var(--ui-text-quaternary);cursor:pointer;font-size:10px;line-height:1}
.em-x-inline:hover{color:var(--ui-text-primary)}

.em-legend{position:absolute;left:12px;bottom:10px;z-index:40000;display:flex;flex-wrap:wrap;align-items:center;gap:8px;
  padding:5px 8px;border-radius:8px;font-size:10px;color:var(--ui-text-tertiary);pointer-events:none;
  background:color-mix(in srgb,var(--ui-bg-primary) 62%,transparent);border:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 45%,transparent)}
.em-legend-item{display:inline-flex;align-items:center;gap:4px}
.em-legend-dot{width:7px;height:7px;border-radius:2px;display:inline-block}
.em-legend-line{width:14px;height:2px;border-radius:1px;display:inline-block}
.em-legend-dash{height:0;border-top:1.5px dashed;background:none!important}
.em-legend-sep{width:1px;height:10px;background:var(--ui-stroke-secondary)}
.em-insp{position:absolute;top:10px;right:10px;bottom:10px;width:384px;max-width:calc(100% - 20px);z-index:41000;overflow:auto;
  display:flex;flex-direction:column;gap:10px;padding:12px 14px 16px;border-radius:10px;pointer-events:auto;
  background:color-mix(in srgb,var(--ui-bg-elevated) 96%,transparent);border:1px solid var(--ui-stroke-secondary);
  box-shadow:0 18px 40px color-mix(in srgb,var(--ui-base) 44%,transparent)}
.em-insp-head{display:flex;align-items:center;gap:8px}
.em-insp-title{font-size:15px;font-weight:650;font-variant-numeric:tabular-nums}
.em-insp-sub{font-size:11px;color:var(--ui-text-tertiary)}
.em-x{margin-left:auto;border:0;background:transparent;color:var(--ui-text-tertiary);cursor:pointer;font-size:12px;padding:2px 4px;border-radius:4px}
.em-x:hover{background:var(--chrome-action-hover);color:var(--ui-text-primary)}
.em-meta{display:flex;flex-direction:column;gap:2px;font-size:10.5px;color:var(--ui-text-tertiary);font-variant-numeric:tabular-nums}
.em-edited-note{color:var(--ui-accent)}
.em-field{display:flex;flex-direction:column;gap:4px}
.em-label{font-size:10.5px;color:var(--ui-text-tertiary)}
.em-textarea{width:100%;box-sizing:border-box;font-size:12px;line-height:1.5;padding:7px 8px;border-radius:6px;resize:vertical;
  background:var(--ui-bg-tertiary);color:var(--ui-text-primary);border:1px solid var(--ui-stroke-secondary);outline:none;font-family:inherit;appearance:none;-webkit-appearance:none}
.em-textarea:focus{border-color:var(--ui-accent)}
.em-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.em-hint{font-size:10px;color:var(--ui-text-quaternary);line-height:1.4}
.em-error{font-size:11px;color:var(--ui-red,#ff5d5d)}
.em-error-title{font-size:14px;font-weight:650}
.em-error-body{font-size:11px;color:var(--ui-text-tertiary);max-width:560px;word-break:break-word}
.em-section{display:flex;flex-direction:column;gap:5px;margin-top:2px}
.em-section-title{font-size:10.5px;color:var(--ui-text-tertiary);text-transform:uppercase;letter-spacing:.06em}
.em-edge-list{display:flex;flex-direction:column;gap:3px;max-height:180px;overflow:auto}
.em-edge-row{display:block;width:100%;text-align:left;border:0;background:transparent;color:var(--ui-text-secondary);
  font-size:11px;padding:3px 5px;border-radius:4px;cursor:pointer}
.em-edge-row:hover{background:var(--chrome-action-hover);color:var(--ui-text-primary)}
.em-edge-row>span{display:flex;align-items:center;gap:6px}
.em-edge-dot{width:6px;height:6px;border-radius:50%;flex:none}
.em-edge-row-rel{min-width:26px}
.em-edge-row-id{font-family:var(--font-mono,ui-monospace,monospace);color:var(--ui-text-quaternary)}
.em-edge-row-flag{margin-left:auto;color:var(--ui-accent)}
.em-endpoints{display:flex;flex-direction:column;gap:5px}
.em-endpoint{display:block;width:100%;text-align:left;border:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 60%,transparent);
  background:color-mix(in srgb,var(--ui-bg-card) 60%,transparent);border-radius:6px;padding:6px 8px;cursor:pointer;color:inherit}
.em-endpoint:hover{border-color:var(--ui-accent)}
.em-endpoint-head{display:flex;align-items:center;gap:6px;font-size:10.5px}
.em-endpoint-id{font-family:var(--font-mono,ui-monospace,monospace);font-weight:700}
.em-endpoint-kind{font-weight:600}
.em-endpoint-role{margin-left:auto;color:var(--ui-text-quaternary)}
.em-endpoint-text{display:block;margin-top:3px;font-size:10.5px;line-height:1.4;color:var(--ui-text-secondary);
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.em-prob{display:flex;flex-direction:column;gap:3px;padding:7px 8px;border-radius:6px;
  background:color-mix(in srgb,var(--ui-bg-card) 55%,transparent);border:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 42%,transparent)}
.em-prob-title{font-size:10.5px;color:var(--ui-text-tertiary);margin-bottom:2px}
.em-prob-row{display:flex;align-items:center;gap:7px;font-size:10.5px}
.em-prob-row[data-hi="1"] .em-prob-label{color:var(--ui-text-primary);font-weight:650}
.em-prob-label{width:38px;flex:none;color:var(--ui-text-tertiary)}
.em-prob-track{flex:1;height:6px;border-radius:3px;background:color-mix(in srgb,var(--ui-stroke-secondary) 30%,transparent);overflow:hidden}
.em-prob-fill{display:block;height:100%;border-radius:3px;opacity:.85}
.em-prob-num{width:44px;text-align:right;font-family:var(--font-mono,ui-monospace,monospace);color:var(--ui-text-tertiary);font-variant-numeric:tabular-nums}
.em-prob-empty{font-size:10px;color:var(--ui-text-quaternary);padding:4px 0}
.em-rel-select{width:100%;height:26px}
.em-chip{display:inline-flex;align-items:center;height:100%;padding:0 7px;border:0;background:transparent;
  color:var(--ui-text-tertiary);cursor:pointer;font-size:11px;white-space:nowrap}
.em-chip:hover{background:var(--chrome-action-hover);color:var(--ui-text-primary)}
.em-chip-inner{display:inline-flex;align-items:center;gap:4px}
.em-chip-glyph{color:var(--ui-accent);font-size:12px;line-height:1}
/* 框选（右键长按）：屏幕矩形的叠加层，只负责画，不接指针。 */
.em-lasso{position:absolute;left:0;top:0;display:none;pointer-events:none;z-index:33000;border-radius:2px;
  border:1px dashed color-mix(in srgb,var(--ui-accent) 78%,transparent);
  background:color-mix(in srgb,var(--ui-accent) 10%,transparent)}
/* 框选出来的多选：和「选中」分开一个视觉，否则看不出这是批量选择。 */
.em-atom[data-multi="1"] .em-atom-body{box-shadow:0 0 0 1.5px color-mix(in srgb,var(--ui-accent) 88%,transparent),
  0 0 0 4px color-mix(in srgb,var(--ui-accent) 16%,transparent),0 2px 8px color-mix(in srgb,var(--ui-base) 30%,transparent)}
.em-batch{position:absolute;left:10px;bottom:44px;width:318px;max-width:calc(100% - 20px);z-index:42000;
  padding:10px 12px;border-radius:10px;display:grid;gap:8px;
  background:color-mix(in srgb,var(--ui-bg-elevated,var(--ui-bg-secondary,#202329)) 96%,transparent);
  border:1px solid color-mix(in srgb,var(--ui-stroke-secondary) 80%,transparent);
  box-shadow:0 18px 40px #0005}
.em-batch-head{display:flex;align-items:center;gap:8px}
.em-batch-title{font-weight:650}
.em-batch-sub{font-size:10.5px;color:var(--ui-text-quaternary);font-variant-numeric:tabular-nums}
`

// --------------------------------------------------------------------------- helpers

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v))
const smoothstep = (e0, e1, x) => {
  const t = clamp((x - e0) / (e1 - e0), 0, 1)
  return t * t * (3 - 2 * t)
}

// 右键按住多久才进框选：短于一瞬的右键是误触（右键菜单已被屏蔽），拖出 4px 也会取消。
const LASSO_HOLD_MS = 320

// 远景收缩：eye 到原子的距离 / 参考视距。rel 小 = 很远。0 → 彩色点，1 → 正常小球。
// 和 baseMorph 用的是同一个 rel，所以「球→卡」与「点→球」两块区域天然不重叠。
const farFactor = rel => 1 - smoothstep(0.12, 0.28, rel)

// 屏幕矩形框选：只挑当前真的看得见的原子（相机前方、没被筛选压掉），判据是投影中心落在框内。
// 纯函数，测试里直接钉（tests/test_graph_lasso.cjs）。
const lassoPick = (positions, rect) => {
  const ids = []
  for (const [id, p] of positions) {
    if (!p || !p.el) continue
    if (!(p.visibility > 0.5) || !(p.depth > 0.35)) continue
    if (p.x < rect.x0 || p.x > rect.x1 || p.y < rect.y0 || p.y > rect.y1) continue
    ids.push(id)
  }
  return ids.sort((a, b) => a - b)
}

// 世界单位只由相机距离定义：START_DIST 是默认视距，FOCUS_DIST 是"拉到眼前"后的视距。
const START_DIST = 3.5
const FOCUS_DIST = 2.4
// 卡片“满尺寸”的距离：k/线宽/字号都以它归一，和开场视距解耦。
const SCALE_REF = 2.4
const PULL_IN = 0.7
const easeInOut = t => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2)

function pad2(n) {
  return String(n).padStart(2, '0')
}
function fmtTime(ts) {
  const d = new Date(ts * 1000)
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
}
function fmtFull(ts) {
  const d = new Date(ts * 1000)
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
}
function dayKey(ts) {
  const d = new Date(ts * 1000)
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
}
function shortSession(sid) {
  if (!sid) return '无会话'
  const m = /^(\d{8})_(\d{6})/.exec(sid)
  if (!m) return sid
  return `${m[1].slice(4, 6)}-${m[1].slice(6, 8)} ${m[2].slice(0, 2)}:${m[2].slice(2, 4)}:${m[2].slice(4, 6)}`
}

// --------------------------------------------------------------------------- filtering

//: 序号输入：12 / #12 / 30-42 / 12, 30-42, #56。范围上限 2000，手滑打 1-999999 不会卡死。
function parseIdSpec(text) {
  const ids = new Set()
  const bad = []
  for (const raw of String(text || '').split(/[\s,，、;；]+/)) {
    if (!raw) continue
    const token = raw.replace(/^#/, '')
    if (!token) {
      bad.push(raw)
      continue
    }
    const range = /^(\d+)\s*[-~–—]\s*(\d+)$/.exec(token)
    if (range) {
      let a = Number(range[1])
      let b = Number(range[2])
      if (a > b) {
        const t = a
        a = b
        b = t
      }
      if (b - a > 2000) {
        bad.push(raw)
        continue
      }
      for (let i = a; i <= b; i += 1) ids.add(i)
      continue
    }
    if (/^\d+$/.test(token)) {
      ids.add(Number(token))
      continue
    }
    bad.push(raw)
  }
  return { ids, bad }
}

function atomPasses(ev, filters, idSet, semIds, semActive) {
  if (filters.project && (ev.project || '未归类') !== filters.project) return false
  if (filters.session && ev.session !== filters.session) return false
  if (filters.date && dayKey(ev.ts) !== filters.date) return false
  if (filters.kinds.length && !filters.kinds.includes(ev.kind)) return false
  if (idSet && idSet.size && !idSet.has(ev.id)) return false
  if (semActive && !(semIds && semIds.has(ev.id))) return false
  return true
}

const edgeRelKey = e => (e.kind === 'none' ? 'none' : e.rel)

// 下拉选项的取值：项目的「未归类」是空 project，会话没有 id 时不可选。
const FACET_VALUE = {
  project: ev => ev.project || '未归类',
  session: ev => ev.session || '',
  date: ev => dayKey(ev.ts)
}

/**
 * 分面计数：每个下拉只列「其它条件都满足的事件里真的存在」的值。
 * 选了项目之后再打开会话下拉，里面剩的就只有这个项目里出现过的会话——
 * 否则列表是全库的（几十个会话），而命中集里往往只有其中几个，等于找不到。
 * 算某个维度时把它自己排除，否则选完会话，别的会话就全从列表里消失了。
 */
function facetCounts(events, filters, idSet, semIds, semActive, pool) {
  const out = { project: new Map(), session: new Map(), date: new Map() }
  for (const facet of ['project', 'session', 'date']) {
    const scoped = { ...filters, [facet]: '' }
    const pick = FACET_VALUE[facet]
    const counts = out[facet]
    for (const ev of events) {
      if (!atomPasses(ev, scoped, idSet, semIds, semActive)) continue
      if (pool && !pool.has(ev.id)) continue   // 关系维度也要算进来：选了「否定」就只看这类边的端点
      const value = pick(ev)
      if (!value) continue
      const row = counts.get(value) || { n: 0, last: 0 }
      row.n += 1
      if (ev.ts > row.last) row.last = ev.ts
      counts.set(value, row)
    }
  }
  return out
}

/**
 * 所有筛选条件同时生效（AND）：
 *   - 原子维度（项目/会话/日期/类型/序号/语义）决定哪些原子算「命中」；
 *   - 关系维度决定哪些边参与：选了关系之后，原子还必须落在这类边的端点上。
 * 返回的 focus 是最终留在舞台上的集合；mode=neighbors 时把命中集的一跳邻居也拉进来。
 */
function computeFilterInfo({ events, edges, filters, idSet, semIds, semActive, mode, pulled }) {
  const relSet = filters.rels.length ? new Set(filters.rels) : null
  const atomActive = Boolean(
    filters.project || filters.session || filters.date || filters.kinds.length ||
    (idSet && idSet.size) || semActive
  )
  if (!atomActive && !relSet) {
    return { active: false, match: new Set(), focus: new Set(), promo: new Set(),
      count: 0, edgeCount: 0, atomActive: false, relActive: false }
  }
  const pool = new Set()
  if (relSet) {
    for (const e of edges) {
      if (!relSet.has(edgeRelKey(e))) continue
      pool.add(e.src)
      pool.add(e.dst)
    }
  }
  const match = new Set()
  for (const ev of events) {
    if (!atomPasses(ev, filters, idSet, semIds, semActive)) continue
    if (relSet && !pool.has(ev.id)) continue
    match.add(ev.id)
  }
  const focus = new Set(match)
  if (mode === 'neighbors') {
    for (const e of edges) {
      if (relSet && !relSet.has(edgeRelKey(e))) continue
      if (match.has(e.src) || match.has(e.dst)) {
        focus.add(e.src)
        focus.add(e.dst)
      }
    }
  }
  if (pulled) for (const id of pulled) focus.add(id)
  let edgeCount = 0
  for (const e of edges) {
    if (relSet && !relSet.has(edgeRelKey(e))) continue
    if (focus.has(e.src) && focus.has(e.dst)) edgeCount += 1
  }
  return {
    active: true,
    match,
    focus,
    promo: new Set([...focus].filter(id => !match.has(id))),
    count: match.size,
    edgeCount,
    atomActive,
    relActive: Boolean(relSet)
  }
}

const toggleValue = (list, v) => (list.includes(v) ? list.filter(x => x !== v) : [...list, v])

function FilterChip({ on, onClick, dot, line, dash, title, children }) {
  return jsxs('button', {
    type: 'button',
    className: 'em-fchip',
    'data-on': on ? '1' : '0',
    title,
    onClick,
    children: [
      dot ? jsx('span', { className: 'em-fdot', style: { background: dot } }) : null,
      line
        ? jsx('span', {
            className: dash ? 'em-fline em-fline-dash' : 'em-fline',
            style: dash ? { borderColor: line } : { background: line }
          })
        : null,
      children
    ]
  })
}

function FilterPanel({
  filters,
  patch,
  projectOptions,
  sessionOptions,
  dateOptions,
  idSpec,
  idUnknown,
  onIdEnter,
  sem,
  semTop,
  setSemTop,
  onSelectHit,
  onClearSemantic,
  vectorCount,
  activeCount,
  onClearAll
}) {
  const hits = sem && !sem.error ? sem.hits : []
  return jsxs('div', {
    className: 'em-panel',
    children: [
      jsxs('div', {
        className: 'em-panel-head',
        children: [
          '筛选',
          jsx('span', { className: 'em-hint', children: '所有条件同时生效（AND）· 不选 = 不限' }),
          jsx('span', { className: 'em-spacer' }),
          activeCount ? jsx('button', { type: 'button', className: 'em-fbtn', onClick: onClearAll, children: '清除全部' }) : null
        ]
      }),
      jsxs('div', {
        className: 'em-pgrid',
        children: [
          jsxs('div', {
            className: 'em-pcol',
            children: [
              jsx('div', { className: 'em-ptitle', children: '记忆原子' }),
              jsxs('div', {
                className: 'em-prow',
                children: [
                  jsx(GraphSelect, { className: 'em-pselect', value: filters.project, options: projectOptions,
                    onChange: v => patch({ project: v }) }),
                  jsx(GraphSelect, { className: 'em-pselect', value: filters.session, options: sessionOptions,
                    onChange: v => patch({ session: v }) }),
                  jsx(GraphSelect, { className: 'em-pselect', value: filters.date, options: dateOptions,
                    onChange: v => patch({ date: v }) })
                ]
              }),
              jsxs('div', {
                className: 'em-prow',
                children: [
                  jsx('span', { className: 'em-plabel', children: '类型' }),
                  ...KINDS.map(k =>
                    jsx(FilterChip, {
                      on: filters.kinds.includes(k),
                      dot: KIND_COLORS[k],
                      title: `只看「${k}」`,
                      onClick: () => patch({ kinds: toggleValue(filters.kinds, k) }),
                      children: k
                    }, k)
                  )
                ]
              }),
              jsxs('div', {
                className: 'em-prow',
                children: [
                  jsx('span', { className: 'em-plabel', children: '序号' }),
                  jsx('input', {
                    className: 'em-input em-idinput',
                    value: filters.ids,
                    placeholder: '12, 30-42, #56',
                    onChange: e => patch({ ids: e.target.value }),
                    onKeyDown: e => {
                      if (e.key === 'Enter') {
                        e.preventDefault()
                        onIdEnter()
                      }
                    }
                  }),
                  idSpec.ids.size ? jsx('span', { className: 'em-hintline', children: `识别 ${idSpec.ids.size} 个序号` }) : null,
                  idSpec.bad.length
                    ? jsx('span', { className: 'em-warn', children: `无法识别：${idSpec.bad.slice(0, 3).join(' ')}` })
                    : null,
                  idUnknown ? jsx('span', { className: 'em-warn', children: `${idUnknown} 个不在图中` }) : null
                ]
              }),
              jsx('div', { className: 'em-hintline', children: '序号命中唯一一条时按回车，镜头直接飞过去。' })
            ]
          }),
          jsxs('div', {
            className: 'em-pcol',
            children: [
              jsx('div', { className: 'em-ptitle', children: '关系边' }),
              jsxs('div', {
                className: 'em-prow',
                children: [
                  ...RELS.map(r =>
                    jsx(FilterChip, {
                      on: filters.rels.includes(r),
                      line: REL_COLORS[r],
                      title: `只看「${r}」`,
                      onClick: () => patch({ rels: toggleValue(filters.rels, r) }),
                      children: r
                    }, r)
                  ),
                  jsx(FilterChip, {
                    on: filters.rels.includes('none'),
                    line: NONE_COLOR,
                    dash: true,
                    title: '只看判定为无关系的灰边',
                    onClick: () => patch({ rels: toggleValue(filters.rels, 'none') }),
                    children: '无'
                  })
                ]
              }),
              jsx('div', { className: 'em-ptitle', children: '语义检索' }),
              jsxs('div', {
                className: 'em-prow',
                children: [
                  jsx(GraphSelect, {
                    className: 'em-pselect',
                    value: String(semTop),
                    onChange: v => setSemTop(Number(v)),
                    options: [8, 16, 32, 64, 128].map(n => ({ value: String(n), label: `Top ${n}` }))
                  }),
                  jsx('span', { className: 'em-hintline', children: 'Embedding 相似度，与上面的条件取交集' }),
                  vectorCount ? null : jsx('span', { className: 'em-warn', children: '库里还没有事件向量' })
                ]
              }),
              sem && sem.error ? jsx('div', { className: 'em-warn', children: sem.error }) : null,
              sem && !sem.error
                ? jsxs('div', {
                    className: 'em-hintline',
                    children: [
                      `「${sem.q}」${sem.hits.length ? `命中 ${sem.hits.length} 条` : '没有命中'}`,
                      Number.isFinite(sem.scanned) ? ` · 扫描 ${sem.scanned} 条向量` : '',
                      sem.skipped ? ` · 跳过 ${sem.skipped} 条` : ''
                    ]
                  })
                : null,
              hits.length
                ? jsx('div', {
                    className: 'em-hits',
                    children: hits.slice(0, 24).map(h =>
                      jsx('button', {
                        type: 'button',
                        className: 'em-hit',
                        title: `选中 #${h.id}`,
                        onClick: () => onSelectHit(h.id),
                        children: [`#${h.id} `, jsx('b', { children: h.score.toFixed(3) })]
                      }, h.id)
                    )
                  })
                : null,
              sem && sem.q && !sem.error && !sem.hits.length
                ? jsx('div', { className: 'em-hintline', children: '换个说法试试，或把 Top 调大。' })
                : null,
              sem
                ? jsx('div', {
                    className: 'em-prow',
                    children: jsx('button', { type: 'button', className: 'em-fbtn', onClick: onClearSemantic, children: '清除语义筛选' })
                  })
                : null
            ]
          })
        ]
      })
    ]
  })
}

// --------------------------------------------------------------------------- scene layout

/**
 * Atoms live in per-project helixes on a horizontal ring: each project is a chronologically
 * wound column (old at the bottom), so time is legible and the cloud is genuinely 3D.
 */
/**
 * Atoms live on a 3D lattice: the newest memory sits at the centre and older ones fill
 * outward shells, so the cloud "grows" from the middle and looks the same from any angle.
 * Cell spacing is 1 world unit — the camera distance is what decides how much of it you see.
 * `pos` (id → {x,y,z}) carries positions the user has dragged/pulled and that we saved.
 */
function buildScene(data, pos) {
  const events = data.events || []
  const N = Math.max(1, events.length)

  const side = Math.max(1, Math.ceil(Math.cbrt(N)))
  const nx = side
  const nz = side
  const ny = Math.max(1, Math.ceil(N / (nx * nz)))
  const cells = []
  for (let i = 0; i < nx; i += 1) {
    for (let j = 0; j < ny; j += 1) {
      for (let k = 0; k < nz; k += 1) {
        const x = i - (nx - 1) / 2
        const y = j - (ny - 1) / 2
        const z = k - (nz - 1) / 2
        cells.push({
          x,
          y,
          z,
          d: x * x + y * y * 1.2 + z * z,
          tie: ((i * 73 + j * 131 + k * 197) % 977) / 977
        })
      }
    }
  }
  // 从中心向外一层层铺，最新的原子占最中间的格子。
  cells.sort((a, b) => a.d - b.d || a.tie - b.tie)

  const order = [...events].sort((a, b) => b.ts - a.ts || b.id - a.id)
  const atoms = []
  order.forEach((ev, idx) => {
    const c = cells[idx % cells.length]
    const j1 = (((ev.id * 2654435761) % 997) / 997 - 0.5) * 0.1
    const j2 = (((ev.id * 40503 + 17) % 977) / 977 - 0.5) * 0.1
    const j3 = (((ev.id * 7919 + 41) % 983) / 983 - 0.5) * 0.1
    const home = { x: c.x + j1, y: c.y + j2, z: c.z + j3 }
    const saved = pos && pos.get(ev.id)
    atoms.push({
      ...ev,
      project: ev.project || '未归类',
      home,
      x: saved ? saved.x : home.x,
      y: saved ? saved.y : home.y,
      z: saved ? saved.z : home.z
    })
  })

  const byId = new Map(atoms.map(a => [a.id, a]))
  const pairKeys = new Set((data.edges || []).map(e => `${e.src}-${e.dst}`))
  const edges = []
  for (const e of data.edges || []) {
    if (!byId.has(e.src) || !byId.has(e.dst)) continue
    edges.push({
      key: `r|${e.src}|${e.dst}|${e.rel}`,
      kind: 'rel',
      src: e.src,
      dst: e.dst,
      rel: e.rel,
      conf: e.conf,
      cross: Boolean(e.cross),
      probs: e.probs || null,
      edited: Boolean(e.edited_ts),
      manual: e.decided_by === 'human',
      decidedBy: e.decided_by
    })
  }
  for (const p of data.pairs || []) {
    if (!byId.has(p.src) || !byId.has(p.dst)) continue
    if (pairKeys.has(`${p.src}-${p.dst}`)) continue
    edges.push({
      key: `p|${p.src}|${p.dst}`,
      kind: 'none',
      src: p.src,
      dst: p.dst,
      rel: null,
      conf: 0,
      cross: false,
      probs: null,
      pair: p,
      edited: Boolean(p.edited_ts),
      manual: false
    })
  }

  // 每条边都给一段可见的弧（同一对之间的多条边左右错开），不要画成直线。
  const byPair = new Map()
  for (const e of edges) {
    const key = `${Math.min(e.src, e.dst)}-${Math.max(e.src, e.dst)}`
    if (!byPair.has(key)) byPair.set(key, [])
    byPair.get(key).push(e)
  }
  for (const list of byPair.values()) {
    list.forEach((e, i) => {
      let h = 0
      for (let j = 0; j < e.key.length; j += 1) h = (h * 31 + e.key.charCodeAt(j)) % 997
      const amp = 0.14 + ((h % 9) / 9) * 0.14
      const sign = i % 2 === 0 ? 1 : -1
      e.bow = clamp(sign * (amp + Math.floor(i / 2) * 0.16), -0.62, 0.62)
    })
  }

  let radius = 1.4
  for (const a of atoms) radius = Math.max(radius, Math.hypot(a.x, a.y, a.z))

  const seed = order[0]
  return { atoms, byId, edges, radius, newestId: seed ? seed.id : null, projects: [...new Set(atoms.map(a => a.project))].sort() }
}

function placeEdgeLabels(labels) {
  const kept = []
  for (const label of [...labels].sort((a,b) => Number(Boolean(b.selected)) - Number(Boolean(a.selected)))) {
    const width = Math.max(18, label.text.length * label.size) + 8
    const overlap = kept.some(other =>
      Math.abs(other.x - label.x) < (other.width + width) / 2 &&
      Math.abs(other.y - label.y) < (other.size + label.size) / 2 + 5)
    if (!overlap || label.selected) kept.push({ ...label, width })
  }
  return kept
}

function fitDistance(radius, aspect, fovDeg = FOV_DEG) {
  const half = ((fovDeg / 2) * Math.PI) / 180
  const horizontal = Math.atan(Math.tan(half) * Math.max(0.45, aspect))
  return (radius * 1.12) / Math.sin(Math.min(half, horizontal))
}

function animateCamera(cam, patch, ms = 520, delay = 0) {
  const to = {
    yaw: cam.yaw,
    pitch: cam.pitch,
    dist: cam.dist,
    fx: cam.fx,
    fy: cam.fy,
    fz: cam.fz,
    panX: cam.panX || 0,
    panY: cam.panY || 0,
    ...patch
  }
  cam.anim = {
    t0: performance.now() + delay,
    ms,
    from: {
      yaw: cam.yaw,
      pitch: cam.pitch,
      dist: cam.dist,
      fx: cam.fx,
      fy: cam.fy,
      fz: cam.fz,
      panX: cam.panX || 0,
      panY: cam.panY || 0
    },
    to
  }
  cam.dirty = true
}

// --------------------------------------------------------------------------- scene component

const FOV_DEG = 26
const FOV = (FOV_DEG * Math.PI) / 180

/** Eye position + orthonormal camera basis (forward / right / up) for a camera state. */
function cameraBasis(cam) {
  const cp = Math.cos(cam.pitch)
  const sp = Math.sin(cam.pitch)
  const cy = Math.cos(cam.yaw)
  const sy = Math.sin(cam.yaw)
  const ex = cam.fx + cam.dist * cp * sy
  const ey = cam.fy + cam.dist * sp
  const ez = cam.fz + cam.dist * cp * cy
  let fx = cam.fx - ex
  let fy = cam.fy - ey
  let fz = cam.fz - ez
  const fl = Math.hypot(fx, fy, fz) || 1
  fx /= fl
  fy /= fl
  fz /= fl
  let rx = -fz
  let rz = fx
  const rl = Math.hypot(rx, rz) || 1
  rx /= rl
  rz /= rl
  // up = cross(right, forward)，right 的 y 恒为 0
  const ux = -rz * fy
  const uy = rz * fx - rx * fz
  const uz = rx * fy
  return { ex, ey, ez, fx, fy, fz, rx, ry: 0, rz, ux, uy, uz }
}

function Scene({
  scene,
  focusInfo,
  sel,
  multi,
  focusReq,
  showLabels,
  camRef,
  posRef,
  onSelectAtom,
  onSelectEdge,
  onBackground,
  onLasso,
  onMoved
}) {
  const stageRef = useRef(null)
  const sizeRef = useRef({ w: 0, h: 0 })
  const atomEls = useRef(new Map())
  const backRef = useRef(null)
  const frontRef = useRef(null)
  const lassoElRef = useRef(null)
  // 上一帧每颗原子的屏幕位置：框选就用它做命中，和画出来的东西是同一份数据。
  const screenRef = useRef(new Map())
  const haloRef = useRef('#0f1115')
  // 选中时只留焦点邻域，其余原子整个收起来；按 (选择, 过滤器, 场景) 身份缓存。
  const visRef = useRef({ key: '', fi: null, scene: null, set: null })
  // All direct neighbors remain visible; a bounded sample of both layers is pulled out.
  const ringRef = useRef(null)
  const retreatEndRef = useRef(0)
  const fadeRef = useRef({ key: null, scene: null, values: new Map(), t: 0 })
  const avoidRef = useRef(null)
  const hitRef = useRef([])
  const dragRef = useRef(null)
  const movedRef = useRef(0)
  const atomAnims = useRef(new Map())
  const flightRef = useRef({ velocity: 0, last: null })
  const lastReqRef = useRef(0)
  const sceneRef = useRef(scene)
  sceneRef.current = scene
  const handlersRef = useRef({})
  handlersRef.current = { onSelectAtom, onSelectEdge, onBackground, onLasso, onMoved }
  const multiRef = useRef(multi)
  multiRef.current = multi
  const lassoRef = useRef({ timer: 0, armed: false, x0: 0, y0: 0, pointerId: 0, box: null })
  const focusRef = useRef(focusInfo)
  focusRef.current = focusInfo
  const selRef = useRef(sel)
  selRef.current = sel
  const labelsRef = useRef(showLabels)
  labelsRef.current = showLabels

  useEffect(() => {
    camRef.current.dirty = true
  }, [focusInfo, sel, camRef])

  // Closing an active inspector relinquishes its scheduled pull, without cancelling
  // unrelated reset-view camera animations when no inspector was open.
  const previousSelRef = useRef(sel)
  useEffect(() => {
    const previous = previousSelRef.current
    previousSelRef.current = sel
    if (sel || !previous) return
    const ids = [...atomAnims.current].filter(([, anim]) => anim.changed).map(([id]) => id)
    atomAnims.current.clear()
    camRef.current.anim = null
    if (ids.length) handlersRef.current.onMoved(ids)
  }, [sel, camRef])

  const setAtomEl = useCallback((id, el) => {
    if (el) atomEls.current.set(id, el)
    else atomEls.current.delete(id)
  }, [])

  useEffect(() => {
    const live = new Set(scene.atoms.map(a => a.id))
    for (const id of [...atomEls.current.keys()]) if (!live.has(id)) atomEls.current.delete(id)
    camRef.current.dirty = true
  }, [scene, camRef])

  // Size tracking + opening camera: sit a few cells away from the newest atom, not at the far wall.
  useEffect(() => {
    const el = stageRef.current
    if (!el) return undefined
    const measure = () => {
      const rect = el.getBoundingClientRect()
      const w = Math.max(1, Math.round(rect.width))
      const h = Math.max(1, Math.round(rect.height))
      if (w === sizeRef.current.w && h === sizeRef.current.h) return
      sizeRef.current = { w, h }
      const cam = camRef.current
      cam.fit = fitDistance(scene.radius, w / h)
      cam.min = Math.max(0.8, cam.fit * 0.16)
      cam.max = cam.fit * 2.8
      if (!cam.ready) {
        cam.ready = true
        cam.startDist = START_DIST
        // 相机眼睛落在最新原子处，视点在眼前；滚轮始终平移眼睛和视点。
        cam.dist = START_DIST
        const seed = scene.byId.get(scene.newestId)
        const b = cameraBasis(cam)
        cam.fx = (seed ? seed.x : 0) + b.fx * cam.dist
        cam.fy = (seed ? seed.y : 0) + b.fy * cam.dist
        cam.fz = (seed ? seed.z : 0) + b.fz * cam.dist
      }
      const cs = getComputedStyle(document.documentElement)
      haloRef.current = cs.getPropertyValue('--ui-bg-primary').trim() || '#0f1115'
      cam.dirty = true
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [scene, camRef])

  // One rAF loop; React never re-renders for camera/atom motion.
  useEffect(() => {
    let raf = 0
    const tick = now => {
      raf = requestAnimationFrame(tick)
      const cam = camRef.current
      const flight = flightRef.current
      const dt = flight.last === null ? 0.016 : clamp((now - flight.last) / 1000, 0, 0.05)
      flight.last = now
      if (Math.abs(flight.velocity) > 0.0001) {
        const b = cameraBasis(cam)
        const decay = Math.exp(-dt / 0.18)
        const distance = flight.velocity * 0.18 * (1 - decay)
        cam.fx += b.fx * distance
        cam.fy += b.fy * distance
        cam.fz += b.fz * distance
        flight.velocity *= decay
        cam.dirty = true
      } else flight.velocity = 0
      if (cam.anim) {
        const { t0, ms, from, to } = cam.anim
        const t = clamp((now - t0) / ms, 0, 1)
        const e = easeInOut(t)
        for (const k of ['yaw', 'pitch', 'dist', 'fx', 'fy', 'fz', 'panX', 'panY']) {
          if (to[k] !== undefined && from[k] !== undefined) cam[k] = from[k] + (to[k] - from[k]) * e
        }
        if (t >= 1) cam.anim = null
        cam.dirty = true
      }

      const anims = atomAnims.current
      if (anims.size) {
        const t = performance.now()
        const done = []
        for (const [id, a] of anims) {
          const atom = scene.byId.get(id)
          if (!atom) {
            anims.delete(id)
            continue
          }
          const p = clamp((t - a.t0) / a.ms, 0, 1)
          const e = easeInOut(p)
          atom.x = a.from.x + (a.to.x - a.from.x) * e
          atom.y = a.from.y + (a.to.y - a.from.y) * e
          atom.z = a.from.z + (a.to.z - a.from.z) * e
          if (p > 0 && Math.hypot(atom.x-a.from.x, atom.y-a.from.y, atom.z-a.from.z) > 1e-8) {
            a.changed = true
            posRef.current.set(id, { x: atom.x, y: atom.y, z: atom.z })
          }
          if (p >= 1) {
            if (a.changed) done.push(id)
            anims.delete(id)
          }
        }
        cam.dirty = true
        if (done.length) handlersRef.current.onMoved(done)
      }

      // Display drift requires continuous redraw, but never changes stored positions.
      cam.dirty = true
      if (!cam.dirty) return
      cam.dirty = false
      draw(now, cam)
    }

    const draw = (now, cam) => {
      const { w, h } = sizeRef.current
      if (!w || !h) return
      const b = cameraBasis(cam)
      const focal = h / 2 / Math.tan(FOV / 2)
      const startDist = SCALE_REF
      const fi = focusRef.current
      const active = fi.active
      const selNow = selRef.current
      const panX = cam.panX || 0
      const panY = cam.panY || 0


      const project = atom => {
        const vx = atom.x - b.ex
        const vy = atom.y - b.ey
        const vz = atom.z - b.ez
        // Clamp at the near plane instead of returning null: a point that slides behind the
        // camera then projects far off-screen, so the edge to it exits the view smoothly
        // instead of the whole edge vanishing and reappearing while the camera orbits.
        const depth = vx * b.fx + vy * b.fy + vz * b.fz
        const zc = Math.max(depth, 0.35)
        const xc = vx * b.rx + vz * b.rz
        const yc = vx * b.ux + vy * b.uy + vz * b.uz
        const unit = focal / zc
        return {
          x: w / 2 + xc * unit + panX,
          y: h / 2 - yc * unit + panY,
          z: zc,
          depth,
          distance: Math.hypot(vx, vy, vz),
          unit,
          k: clamp(startDist / zc, 0.3, 1.55)
        }
      }

      // ---- which atoms are on stage -----------------------------------------
      // 选中一个原子/一条边时只留焦点邻域；筛选器生效时只留命中的；其余隐藏。
      const vis = visRef.current
      const wantKey = selNow ? (selNow.type === 'atom' ? `a:${selNow.id}` : `e:${selNow.key}`) : active ? 'filter' : 'all'
      if (vis.key !== wantKey || vis.fi !== fi || vis.scene !== scene) {
        vis.key = wantKey
        vis.fi = fi
        vis.scene = scene
        if (wantKey === 'all') vis.set = null
        else if (wantKey === 'filter') vis.set = new Set(fi.focus)
        else {
          const set = new Set()
          if (selNow.type === 'atom') {
            set.add(selNow.id)
            if (ringRef.current) for (const id of ringRef.current) set.add(id)
            else {
              for (const e of scene.edges) {
                if (e.kind !== 'rel') continue
                if (e.src === selNow.id) set.add(e.dst)
                else if (e.dst === selNow.id) set.add(e.src)
              }
            }
          } else {
            const found = scene.edges.find(x => x.key === selNow.key)
            if (found) {
              set.add(found.src)
              set.add(found.dst)
            }
          }
          vis.set = set
        }
      }
      const shown = vis.set
      const fade = fadeRef.current
      const fresh = fade.scene !== scene
      const targetAlpha = id => {
        if (!shown || shown.has(id)) return 1
        const target = selNow && !active ? 0.28 : 0
        // The entire cloud stays put until the retreat has completed. Thereafter
        // dimming and display-only avoidance share the same continuous ramp.
        if (selNow && selNow.type === 'atom')
          return 1 + (target - 1) * smoothstep(0, 450, now - retreatEndRef.current)
        return target
      }
      if (fresh || fade.key !== wantKey || fade.shown !== shown) {
        const previous = fade.values
        fade.from = new Map(scene.atoms.map(atom => [atom.id, fresh ? targetAlpha(atom.id) : (previous.get(atom.id) ?? 0)]))
        fade.scene = scene
        fade.key = wantKey
        fade.shown = shown
        fade.t = now
      }
      const fadeT = clamp((now - fade.t) / 450, 0, 1)
      if (fadeT < 1) cam.dirty = true
      const alphaFor = id => {
        const target = targetAlpha(id)
        const alpha = (fade.from.get(id) ?? target) + (target - (fade.from.get(id) ?? target)) * smoothstep(0, 1, fadeT)
        fade.values.set(id, alpha)
        return alpha
      }

      const selectedCenter = selNow && selNow.type === 'atom' ? scene.byId.get(selNow.id) : null
      if (selectedCenter) avoidRef.current = { x: selectedCenter.x, y: selectedCenter.y, z: selectedCenter.z }
      const avoidCenter = selectedCenter || (!selNow && !active && fadeT < 1 ? avoidRef.current : null)
      const world = new Map()
      const stableWorld = new Map()
      for (const atom of scene.atoms) {
        let x = atom.x, y = atom.y, z = atom.z
        if (avoidCenter && (!shown || !shown.has(atom.id))) {
          const dx = atom.x - avoidCenter.x, dy = atom.y - avoidCenter.y, dz = atom.z - avoidCenter.z
          const d = Math.hypot(dx, dy, dz)
          if (d > 0.001 && d < 7) {
            const amount = (1 - smoothstep(0, 7, d)) * 1.5 * (1 - alphaFor(atom.id)) / 0.72 / d
            x += dx * amount; y += dy * amount; z += dz * amount
          }
        }
        stableWorld.set(atom.id, { x, y, z })
        const phase = atom.id * 2.399963, seconds = now / 1000
        const distance = Math.hypot(x - b.ex, y - b.ey, z - b.ez)
        const drift = 0.025 * (1 / 3 + 2 / 3 * smoothstep(SCALE_REF / 0.72, 12, distance))
        world.set(atom.id, { x: x + drift * Math.sin(seconds * 0.47 + phase),
          y: y + drift * Math.sin(seconds * 0.39 + phase * 1.7),
          z: z + drift * Math.cos(seconds * 0.43 + phase) })
      }

      const positions = new Map()
      for (const atom of scene.atoms) {
        const el = atomEls.current.get(atom.id)
        const visibility = alphaFor(atom.id)
        if (visibility <= 0.001) {
          positions.set(atom.id, null)
          if (el) el.style.display = 'none'
          continue
        }
        const p = project(world.get(atom.id))
        const stable = project(stableWorld.get(atom.id))
        if (stable.x < -4000 || stable.x > w + 4000 || stable.y < -4000 || stable.y > h + 4000) {
          positions.set(atom.id, null)
          if (el) el.style.display = 'none'
          continue
        }
        const isSelAtom = selNow && selNow.type === 'atom' && selNow.id === atom.id
        positions.set(atom.id, { x: p.x, y: p.y, z: p.z, depth: p.depth, distance: stable.distance, stable, k: p.k, el, isSelAtom, visibility })
        if (!el) continue
        const nearOpacity = smoothstep(0.05, 0.65, stable.depth)
        const opacity = nearOpacity * visibility
        el.style.display = opacity > 0.001 ? '' : 'none'
        el.style.opacity = String(opacity)
        el.style.pointerEvents = opacity > 0.5 && (!shown || shown.has(atom.id) || (selNow && selNow.type === 'atom' && visibility > 0.5)) ? '' : 'none'
        el.style.transform = `translate3d(${p.x.toFixed(1)}px, ${p.y.toFixed(1)}px, 0) translate(-50%, -50%)`
        el.dataset.selected = isSelAtom ? '1' : '0'
        el.dataset.multi = multiRef.current && multiRef.current.has(atom.id) ? '1' : '0'
        el.style.zIndex = isSelAtom ? '34000' : String(Math.round(20000 - p.z * 100))
      }

      // 卡片预算：钻进云团时把最近的一批变成卡片，更远的继续当小球，
      // 否则几百张卡会互相压住。（排名变化是连续的，过渡带也做了衰减，不会跳。）
      screenRef.current = positions
      const CARD_BUDGET = 46
      const ranked = []
      for (const [id, p] of positions) if (p && p.el) {
        // Behind-eye and off-screen nodes cannot consume the on-screen card budget.
        p.w = 20
        p.h = 20
        if (p.stable.depth > 0.35 && p.stable.x >= -88 && p.stable.x <= w + 88 && p.stable.y >= -28 && p.stable.y <= h + 28) ranked.push(p)
        else {
          p.el.style.setProperty('--m', '0')
          p.el.style.setProperty('--f', farFactor(startDist / Math.max(p.distance, 0.01)).toFixed(3))
        }
      }
      ranked.sort((a, b) => a.distance - b.distance)
      for (let i = 0; i < ranked.length; i += 1) {
        const p = ranked[i]
        // 远景收缩与球→卡共用同一个 rel：rel 越小（离得越远）越接近「一点颜色」。
        p.far = farFactor(startDist / Math.max(p.distance, 0.01))
        p.baseMorph = smoothstep(0.3, 0.72, clamp(startDist / Math.max(p.distance, 0.01), 0.26, 1.06))
        if (i >= CARD_BUDGET) p.baseMorph *= 1 - smoothstep(1, 1.6, i / CARD_BUDGET)
      }
      for (const p of ranked) {
        // Smoothly share the card space when near-equal depths exchange order.
        let m = p.baseMorph
        for (const q of ranked) {
          if (q === p || q.baseMorph < 0.1) continue
          const separation = Math.max(Math.abs(p.stable.x - q.stable.x) / 176, Math.abs(p.stable.y - q.stable.y) / 56)
          const priority = smoothstep(-0.3, 0.3, p.distance - q.distance)
          m *= 1 - priority * smoothstep(0.1, 0.5, q.baseMorph) * (1 - smoothstep(0.55, 1.08, separation))
        }
        p.m = m
        // 连线的锚点必须用和 CSS 同一套尺寸：小球基数 20px 随 --f 收到 6px，卡片再按 --m 长。
        const base = 20 - 14 * p.far
        p.w = base + 156 * m
        p.h = base + 36 * m
        p.el.style.setProperty('--m', m.toFixed(3))
        p.el.style.setProperty('--f', p.far.toFixed(3))
      }

      // ---- edges, on canvas -------------------------------------------------
      // One Path2D per (color, opacity, width) bucket: thousands of edges collapse into
      // a few dozen stroke() calls, and nothing in this loop touches the DOM.
      const dpr = Math.min(2, window.devicePixelRatio || 1)
      const prep = el => {
        if (!el) return null
        const pw = Math.round(w * dpr)
        const ph = Math.round(h * dpr)
        if (el.width !== pw || el.height !== ph) {
          el.width = pw
          el.height = ph
        }
        const c = el.getContext('2d')
        c.setTransform(dpr, 0, 0, dpr, 0, 0)
        c.clearRect(0, 0, w, h)
        return c
      }
      const back = prep(backRef.current)
      const front = prep(frontRef.current)

      // 连线从卡片/小球的边缘出发，不从中心穿过去。
      // 连线锚点用卡片“当前”的尺寸（小球 20px、满卡 176x56），随 --m 连续变化。
      const anchorOn = (p, dirX, dirY) => {
        const hw = p.w / 2
        const hh = p.h / 2
        const ax = Math.abs(dirX) || 1e-6
        const ay = Math.abs(dirY) || 1e-6
        const t = Math.min(hw / ax, hh / ay)
        return { x: p.x + dirX * t, y: p.y + dirY * t }
      }

      const screenEdges = []
      const selKey = selNow && selNow.type === 'edge' ? selNow.key : null
      const labelsOn = labelsRef.current
      const zoomOut = cam.dist / (cam.fit || cam.dist)
      const coarse = zoomOut > 1.6
      const midCoarse = !coarse && zoomOut > 0.95
      const sampleEdge = (e, n) => (e.key.length + e.key.charCodeAt(0) + e.key.charCodeAt(e.key.length - 1)) % n === 0
      const buckets = new Map()
      const labels = []
      const hotEdges = []
      let selEdge = null

      for (const edge of scene.edges) {
        let a = positions.get(edge.src)
        let c = positions.get(edge.dst)
        if (!a || !c || (a.depth <= 0.35 && c.depth <= 0.35)) continue
        // Clip the world-space segment at the near plane before projecting; clamping a
        // behind-eye endpoint independently invents a false line across the viewport.
        if (a.depth < 0.35 || c.depth < 0.35) {
          const aa = world.get(edge.src), cc = world.get(edge.dst)
          const t = (0.35 - a.depth) / (c.depth - a.depth)
          const clipped = project({ x: aa.x + (cc.x-aa.x)*t, y: aa.y + (cc.y-aa.y)*t, z: aa.z + (cc.z-aa.z)*t })
          const point = { ...clipped, w: 0, h: 0, visibility: Math.min(a.visibility, c.visibility) }
          if (a.depth < 0.35) a = point
          else c = point
        }
        const edgeVisibility = Math.min(a.visibility, c.visibility)
        const isSel = selKey === edge.key
        const hot = selNow && selNow.type === 'atom' && edge.kind === 'rel' && (edge.src === selNow.id || edge.dst === selNow.id)
        if (!isSel && !hot && coarse && !sampleEdge(edge, 4)) continue
        if (!isSel && !hot && midCoarse && !sampleEdge(edge, 2)) continue
        const offP = p => p.x < -80 || p.x > w + 80 || p.y < -80 || p.y > h + 80
        if (!isSel && offP(a) && offP(c)) continue

        const dx = c.x - a.x
        const dy = c.y - a.y
        const len = Math.hypot(dx, dy) || 1
        const bow = isSel ? 0.1 : edge.bow || 0.16
        const ctlX = (a.x + c.x) / 2 + (-dy / len) * bow * len
        const ctlY = (a.y + c.y) / 2 + (dx / len) * bow * len
        // 两端各自朝控制点方向裁到盒子上：连线贴着卡片边缘，而不是从卡片中心穿出来。
        const la = Math.hypot(ctlX - a.x, ctlY - a.y) || 1
        const lc = Math.hypot(ctlX - c.x, ctlY - c.y) || 1
        const A = anchorOn(a, (ctlX - a.x) / la, (ctlY - a.y) / la)
        const C = anchorOn(c, (ctlX - c.x) / lc, (ctlY - c.y) / lc)

        const color = edge.edited ? EDITED_COLOR : edge.kind === 'none' ? NONE_COLOR : REL_COLORS[edge.rel] || NONE_COLOR
        const crossFade = edge.cross ? 0.45 : 1
        const depthFade = clamp(1.45 - Math.max(a.z, c.z) / (startDist * 1.85), 0.16, 1)
        const far = coarse || midCoarse
        const k = clamp(startDist / Math.min(a.z, c.z), 0.45, 1.65)
        const width = far ? Math.max(0.9, 1.15 * (edge.cross ? 0.8 : 1) * k) : 1.15 * (edge.cross ? 0.8 : 1) * k
        const baseOpacity = far
          ? Math.max(0.16 * crossFade, coarse ? 0.12 : 0.09)
          : 0.17 * crossFade * depthFade

        const clickableEndpoint = p => p.depth > 0.35 && p.visibility > 0.5 && p.el && p.el.style.pointerEvents !== 'none'
        if (edgeVisibility > 0.5 && clickableEndpoint(a) && clickableEndpoint(c))
          screenEdges.push({ key: edge.key, edge, x1: A.x, y1: A.y, x2: C.x, y2: C.y, cx: ctlX, cy: ctlY })

        if (isSel) {
          selEdge = { color, x1: A.x, y1: A.y, cx: ctlX, cy: ctlY, x2: C.x, y2: C.y, width }
        } else if (hot) {
          // 选中原子时，它连着的每条线都提到前层高光，便于看清“它连去了哪”。
          hotEdges.push({ color, x1: A.x, y1: A.y, cx: ctlX, cy: ctlY, x2: C.x, y2: C.y, width: Math.max(width, 1.4), visibility: edgeVisibility })
        } else if (back) {
          const op = Math.round(baseOpacity * edgeVisibility * 100) / 100
          const wd = Math.round(width * 2) / 2
          const bucketKey = `${color}|${op}|${wd}`
          let bucket = buckets.get(bucketKey)
          if (!bucket) {
            bucket = { color, op, wd, path: new Path2D() }
            buckets.set(bucketKey, bucket)
          }
          bucket.path.moveTo(A.x, A.y)
          bucket.path.quadraticCurveTo(ctlX, ctlY, C.x, C.y)
        }

        const incident = selNow && selNow.type === 'atom' && edge.kind === 'rel' && (edge.src === selNow.id || edge.dst === selNow.id)
        const visible = isSel || (incident && len < 460) || (labelsOn && len > 120)
        if (!visible) continue
        labels.push({
          text: edge.kind === 'none' ? '无' : edge.rel,
          color,
          x: 0.25 * A.x + 0.5 * ctlX + 0.25 * C.x,
          y: 0.25 * A.y + 0.5 * ctlY + 0.25 * C.y,
          size: (isSel ? 11 : 9.5) * clamp(k, 0.8, 1.2),
          alpha: isSel ? 1 : 0.7,
          selected: isSel
        })
      }

      if (back) {
        back.lineCap = 'round'
        for (const b of buckets.values()) {
          back.globalAlpha = b.op
          back.strokeStyle = b.color
          back.lineWidth = b.wd
          back.stroke(b.path)
        }
        back.globalAlpha = 1
      }

      if (front && hotEdges.length) {
        front.lineCap = 'round'
        for (const h of hotEdges) {
          const path = new Path2D()
          path.moveTo(h.x1, h.y1)
          path.quadraticCurveTo(h.cx, h.cy, h.x2, h.y2)
          front.strokeStyle = h.color
          front.globalAlpha = 0.18 * h.visibility
          front.lineWidth = h.width * 3.6
          front.stroke(path)
          front.globalAlpha = 0.95 * h.visibility
          front.lineWidth = h.width
          front.stroke(path)
        }
        front.globalAlpha = 1
      }

      if (front && selEdge) {
        const path = new Path2D()
        path.moveTo(selEdge.x1, selEdge.y1)
        path.quadraticCurveTo(selEdge.cx, selEdge.cy, selEdge.x2, selEdge.y2)
        front.lineCap = 'round'
        front.strokeStyle = selEdge.color
        front.globalAlpha = 0.34
        front.lineWidth = selEdge.width * 4.4
        front.stroke(path)
        front.globalAlpha = 1
        front.lineWidth = selEdge.width + 2
        front.stroke(path)
      }

      if (front && labels.length) {
        const halo = haloRef.current
        front.textAlign = 'center'
        front.textBaseline = 'alphabetic'
        front.lineJoin = 'round'
        for (const lb of placeEdgeLabels(labels)) {
          front.font = `600 ${lb.size.toFixed(1)}px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`
          front.globalAlpha = 0.9
          front.lineWidth = 3
          front.strokeStyle = halo
          front.strokeText(lb.text, lb.x, lb.y - 6)
          front.fillStyle = lb.color
          front.globalAlpha = lb.alpha
          front.fillText(lb.text, lb.x, lb.y - 6)
        }
        front.globalAlpha = 1
      }

      hitRef.current = screenEdges
    }

    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [scene, camRef, posRef])

  // Select an atom: highlight, retreat to fit all direct neighbors, then expand layers.
  // Explicit pulls persist; display drift and avoidance never change stored coordinates.
  useEffect(() => {
    const req = focusReq
    if (!req || !scene) return
    if (req.seq === lastReqRef.current) return
    const { w, h } = sizeRef.current
    if (!w || !h) return
    lastReqRef.current = req.seq
    const cam = camRef.current
    flightRef.current.velocity = 0
    // A new selection owns the motion: persist the current partial positions before
    // replacing the previous trajectories, never let old pulls continue in the background.
    const interrupted = [...atomAnims.current].filter(([, anim]) => anim.changed).map(([id]) => id)
    atomAnims.current.clear()
    if (interrupted.length) handlersRef.current.onMoved(interrupted)
    ringRef.current = null
    const b = cameraBasis(cam)
    const focal = h / 2 / Math.tan(FOV / 2)
    const usableW = Math.max(320, w - 404)
    // 检查器占右边 404px：把投影中心左移，焦点才落在「真正可用」的画面中间。
    const panX = usableW / 2 - w / 2
    const D = FOCUS_DIST
    const startAnim = (atom, to, delay = 0) =>
      atomAnims.current.set(atom.id, {
        from: { x: atom.x, y: atom.y, z: atom.z },
        to,
        t0: performance.now() + delay,
        ms: 720
      })

    if (req.mode === 'atom') {
      const a = scene.byId.get(req.id)
      if (!a) return
      // 后撤至当前星团之外，再按图距离依次拉开两层；保持每个方向的三维分量。
      // Keep the focus anchored. Fixed shell radii make repeat expansion idempotent;
      // using the current cloud radius (or multiplying offsets) creates runaway growth.
      const to = { x: a.x, y: a.y, z: a.z }
      // Expand a deterministic sample of true relationships, leaving the full edge list
      // untouched for the inspector. Give both layers room even for high-degree nodes.
      const adjacency = new Map()
      for (const e of scene.edges) {
        if (e.kind !== 'rel') continue
        if (!adjacency.has(e.src)) adjacency.set(e.src, new Set())
        if (!adjacency.has(e.dst)) adjacency.set(e.dst, new Set())
        adjacency.get(e.src).add(e.dst)
        adjacency.get(e.dst).add(e.src)
      }
      const direct = [...(adjacency.get(a.id) || [])].filter(id => scene.byId.has(id)).sort((x,y) => x-y)
      // Fit the actual 3D positions of every direct neighbor before moving them.
      // Depth matters: a neighbor nearer the eye needs more retreat than one behind.
      let retreat = Math.max(FOCUS_DIST, fitDistance(5, usableW / h))
      for (const id of direct) {
        const mate = scene.byId.get(id)
        const dx = mate.x - to.x, dy = mate.y - to.y, dz = mate.z - to.z
        const forward = dx*b.fx + dy*b.fy + dz*b.fz
        const horizontal = Math.abs(dx*b.rx + dy*b.ry + dz*b.rz)
        const vertical = Math.abs(dx*b.ux + dy*b.uy + dz*b.uz)
        retreat = Math.max(retreat, 0.75 - forward,
          focal * horizontal / Math.max(1, usableW / 2 - 30) - forward,
          focal * vertical / Math.max(1, h / 2 - 30) - forward)
      }
      animateCamera(cam, { fx: to.x, fy: to.y, fz: to.z, dist: retreat, panX, panY: 0 }, 800, 250)
      retreatEndRef.current = performance.now() + 1050
      const directSet = new Set(direct)
      const first = direct.slice(0, 16)
      const second = new Set()
      for (const id of first) for (const other of adjacency.get(id) || []) {
        if (other !== a.id && !directSet.has(other) && scene.byId.has(other)) second.add(other)
      }
      const levels = new Map([[a.id, 0]])
      for (const id of first) levels.set(id, 1)
      for (const id of [...second].sort((x,y) => x-y).slice(0, 16)) levels.set(id, 2)
      ringRef.current = new Set([a.id, ...direct, ...levels.keys()])
      for (const [id, depth] of levels) {
        if (!depth) continue
        const mate = scene.byId.get(id)
        const layer = depth === 1 ? first : [...levels].filter(([,d]) => d === 2).map(([i]) => i)
        const index = layer.indexOf(id)
        // Fibonacci sphere slots keep the layers volumetric rather than flattening
        // them into camera-facing rings. Stable slots make repeat expansion settle.
        const angle = index * 2.399963229728653 + depth * 0.7
        const z = 1 - 2 * (index + 0.5) / layer.length
        const r = Math.sqrt(1-z*z)
        const target = depth === 1 ? 2.8 : 5
        const x = r * Math.cos(angle), y = r * Math.sin(angle)
        startAnim(mate, {
          x: to.x + target * (b.rx*x + b.ux*y + b.fx*z),
          y: to.y + target * (b.ry*x + b.uy*y + b.fy*z),
          z: to.z + target * (b.rz*x + b.uz*y + b.fz*z)
        }, depth === 1 ? 1050 : 1450)
      }
    } else {
      const a = scene.byId.get(req.src)
      const c = scene.byId.get(req.dst)
      if (!a || !c) return
      const mid = { x: (a.x + c.x) / 2, y: (a.y + c.y) / 2, z: (a.z + c.z) / 2 }
      const focus = { x: mid.x + b.fx * PULL_IN, y: mid.y + b.fy * PULL_IN, z: mid.z + b.fz * PULL_IN }
      const half = ((usableW * 0.25) * D) / focal
      startAnim(a, { x: focus.x - b.rx * half, y: focus.y - b.ry * half, z: focus.z - b.rz * half })
      startAnim(c, { x: focus.x + b.rx * half, y: focus.y + b.ry * half, z: focus.z + b.rz * half })
      animateCamera(cam, { fx: focus.x, fy: focus.y, fz: focus.z, dist: D, panX, panY: 0 }, 820)
    }
    cam.dirty = true
  }, [focusReq, scene, camRef])

  const stopMotion = useCallback(() => {
    const cam = camRef.current
    cam.anim = null
    flightRef.current.velocity = 0
    const ids = [...atomAnims.current].filter(([, anim]) => anim.changed).map(([id]) => id)
    atomAnims.current.clear()
    if (ids.length) handlersRef.current.onMoved(ids)
  }, [camRef])

  // 框选的矩形只是纯视觉：直接写 style，不走 React state，免得每帧重渲染。
  const paintLasso = (x0, y0, x1, y1) => {
    const el = lassoElRef.current
    if (!el) return
    const left = Math.min(x0, x1)
    const top = Math.min(y0, y1)
    el.style.display = 'block'
    el.style.left = `${left}px`
    el.style.top = `${top}px`
    el.style.width = `${Math.abs(x1 - x0)}px`
    el.style.height = `${Math.abs(y1 - y0)}px`
  }
  const hideLasso = () => {
    const el = lassoElRef.current
    if (el) el.style.display = 'none'
  }

  const onPointerDown = useCallback(
    e => {
      // 左键拖空白 = 平移相机；中键拖 = 转视角；左键按在原子 = 拖那颗原子；右键长按 = 框选。
      const rotate = e.button === 1
      if (e.button !== 0 && !rotate && e.button !== 2) return
      e.preventDefault()
      const cam = camRef.current
      movedRef.current = 0

      if (e.button === 2) {
        // 先按住不动，够 LASSO_HOLD_MS 才进框选；期间移动超过 4px 就当作误触取消。
        const lasso = lassoRef.current
        clearTimeout(lasso.timer)
        lasso.armed = false
        lasso.box = null
        lasso.x0 = e.clientX
        lasso.y0 = e.clientY
        lasso.pointerId = e.pointerId
        lasso.timer = setTimeout(() => {
          lasso.timer = 0
          const stage = stageRef.current
          if (!stage) return
          const box = stage.getBoundingClientRect()
          lasso.box = box
          lasso.armed = true
          dragRef.current = { mode: 'lasso', x0: lasso.x0 - box.left, y0: lasso.y0 - box.top }
          try {
            stage.setPointerCapture(lasso.pointerId)
          } catch {
            /* ignore */
          }
          paintLasso(lasso.x0 - box.left, lasso.y0 - box.top, lasso.x0 - box.left, lasso.y0 - box.top)
        }, LASSO_HOLD_MS)
        return
      }

      const card = e.button === 0 && e.target && e.target.closest ? e.target.closest('.em-atom') : null
      if (card && card.dataset.id) {
        const id = Number(card.dataset.id)
        const atom = sceneRef.current.byId.get(id)
        if (atom) {
          const b = cameraBasis(cam)
          const h = sizeRef.current.h || 1
          const focal = h / 2 / Math.tan(FOV / 2)
          const zc = Math.max(
            0.15,
            (atom.x - b.ex) * b.fx + (atom.y - b.ey) * b.fy + (atom.z - b.ez) * b.fz
          )
          // 直接拖着走：把鼠标位移换算成该深度上的世界位移（屏幕平面内）。
          dragRef.current = {
            mode: 'atom',
            id,
            pointerId: e.pointerId,
            x: e.clientX,
            y: e.clientY,
            base: { x: atom.x, y: atom.y, z: atom.z },
            b,
            scale: zc / focal,
            captured: false
          }
          return
        }
      }
      dragRef.current = {
        mode: rotate ? 'rotate' : 'pan',
        x: e.clientX,
        y: e.clientY,
        yaw: cam.yaw,
        pitch: cam.pitch,
        panX: cam.panX || 0,
        panY: cam.panY || 0
      }
      // Capture retargets the following `click` at the stage, which would swallow every card
      // click. Background drags capture; a drag that starts on a card keeps the card's click.
      if (!rotate) {
        try {
          e.currentTarget.setPointerCapture(e.pointerId)
        } catch {
          /* ignore */
        }
      }
    },
    [camRef, stopMotion]
  )

  const onPointerMove = useCallback(
    e => {
      // 右键还没按住够时长就动了 → 当误触，别再进框选。
      const lasso = lassoRef.current
      if (lasso.timer && !lasso.armed &&
          Math.hypot(e.clientX - lasso.x0, e.clientY - lasso.y0) > 4) {
        clearTimeout(lasso.timer)
        lasso.timer = 0
      }
      const drag = dragRef.current
      if (!drag) return
      if (drag.mode === 'lasso') {
        if (!lasso.box) return
        paintLasso(drag.x0, drag.y0, e.clientX - lasso.box.left, e.clientY - lasso.box.top)
        return
      }
      const dx = e.clientX - drag.x
      const dy = e.clientY - drag.y
      movedRef.current = Math.max(movedRef.current, Math.hypot(dx, dy))
      const cam = camRef.current
      if (!drag.interrupted && movedRef.current > 4) {
        drag.interrupted = true
        stopMotion()
        if (drag.mode === 'atom') {
          const atom = sceneRef.current.byId.get(drag.id)
          if (atom) drag.base = { x: atom.x, y: atom.y, z: atom.z }
        } else if (drag.mode === 'pan') {
          drag.base = { x: cam.fx, y: cam.fy, z: cam.fz }
          drag.b = cameraBasis(cam)
          drag.scale = cam.dist / ((sizeRef.current.h || 1) / 2 / Math.tan(FOV / 2))
        }
      }
      if (drag.mode === 'atom') {
        if (movedRef.current <= 4) return
        if (!drag.captured && movedRef.current > 4) {
          drag.captured = true
          atomAnims.current.delete(drag.id)
          try {
            e.currentTarget.setPointerCapture(drag.pointerId)
          } catch {
            /* ignore */
          }
        }
        const atom = sceneRef.current.byId.get(drag.id)
        if (atom) {
          atom.x = drag.base.x + (drag.b.rx * dx - drag.b.ux * dy) * drag.scale
          atom.y = drag.base.y + (drag.b.ry * dx - drag.b.uy * dy) * drag.scale
          atom.z = drag.base.z + (drag.b.rz * dx - drag.b.uz * dy) * drag.scale
          posRef.current.set(drag.id, { x: atom.x, y: atom.y, z: atom.z })
          cam.dirty = true
        }
      } else if (drag.mode === 'pan') {
        if (!drag.base) return
        cam.fx = drag.base.x + (-drag.b.rx * dx + drag.b.ux * dy) * drag.scale
        cam.fy = drag.base.y + (-drag.b.ry * dx + drag.b.uy * dy) * drag.scale
        cam.fz = drag.base.z + (-drag.b.rz * dx + drag.b.uz * dy) * drag.scale
        cam.dirty = true
      } else {
        cam.yaw = drag.yaw - dx * 0.006
        cam.pitch = clamp(drag.pitch + dy * 0.006, -1.36, 1.36)
        cam.dirty = true
      }
    },
    [camRef, posRef, stopMotion]
  )

  const onPointerUp = useCallback(e => {
    const lasso = lassoRef.current
    if (lasso.timer) {
      clearTimeout(lasso.timer)
      lasso.timer = 0
    }
    const drag = dragRef.current
    dragRef.current = null
    if (drag && drag.mode === 'lasso') {
      hideLasso()
      const box = lasso.box
      if (box && lasso.armed) {
        const x = e.clientX - box.left
        const y = e.clientY - box.top
        const rect = {
          x0: Math.min(drag.x0, x),
          x1: Math.max(drag.x0, x),
          y0: Math.min(drag.y0, y),
          y1: Math.max(drag.y0, y)
        }
        // 太小的框是误触：不当成「全不选」，直接不动选区。
        if (rect.x1 - rect.x0 > 3 && rect.y1 - rect.y0 > 3) {
          handlersRef.current.onLasso(lassoPick(screenRef.current, rect))
        }
      }
      lasso.armed = false
      lasso.box = null
      try {
        e.currentTarget.releasePointerCapture(e.pointerId)
      } catch {
        /* ignore */
      }
      return
    }
    if (drag && drag.mode === 'atom' && movedRef.current > 4) handlersRef.current.onMoved([drag.id])
    try {
      e.currentTarget.releasePointerCapture(e.pointerId)
    } catch {
      /* ignore */
    }
  }, [])

  const onWheel = useCallback(
    e => {
      e.preventDefault()
      const cam = camRef.current
      if (cam.anim || atomAnims.current.size) stopMotion()
      const b = cameraBasis(cam)
      let nearest = Infinity
      for (const atom of sceneRef.current.atoms) {
        const dx = atom.x-b.ex, dy = atom.y-b.ey, dz = atom.z-b.ez
        nearest = Math.min(nearest, Math.hypot(dx,dy,dz))
      }
      const scale = clamp(Number.isFinite(nearest) ? nearest / 4 : 3, 0.35, 40)
      const pixels = e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? (sizeRef.current.h || 800) : 1)
      flightRef.current.velocity = clamp(flightRef.current.velocity - clamp(pixels, -1600, 1600) * 0.025 * scale, -480, 480)
      cam.dirty = true
    },
    [camRef, stopMotion]
  )

  useEffect(() => {
    const el = stageRef.current
    if (!el) return undefined
    const handler = e => onWheel(e)
    el.addEventListener('wheel', handler, { passive: false })
    return () => el.removeEventListener('wheel', handler)
  }, [onWheel])

  const atomsTree = useMemo(
    () =>
      scene.atoms.map(a =>
        jsxs(
          'div',
          {
            ref: el => setAtomEl(a.id, el),
            className: 'em-atom',
            'data-id': a.id,
            'data-kind': a.kind,
            'data-selected': '0',
            'data-edited': a.edited_ts ? '1' : '0',
            title: `${a.text}\n\n${fmtFull(a.ts)} · ${a.project || '未归类'}`,
            onClick: e => {
              e.stopPropagation()
              if (movedRef.current > 5) return
              handlersRef.current.onSelectAtom(a.id)
            },
            children: jsxs('div', {
              className: 'em-atom-body',
              children: [
                jsx('span', { className: 'em-atom-num', children: `${a.id}` }),
                jsxs('div', {
                  className: 'em-atom-card',
                  children: [
                    jsxs('div', {
                      className: 'em-atom-head',
                      children: [
                        jsx('span', { className: 'em-atom-id', children: `#${a.id}` }),
                        jsx('span', {
                          className: 'em-atom-kind',
                          style: { color: KIND_COLORS[a.kind] || 'var(--ui-text-secondary)' },
                          children: a.kind
                        }),
                        jsx('span', { className: 'em-atom-time', children: fmtTime(a.ts) })
                      ]
                    }),
                    jsx('div', { className: 'em-atom-text', children: a.text }),
                    a.edited_ts ? jsx('span', { className: 'em-atom-edited', title: '人工改过', children: '✎' }) : null
                  ]
                })
              ]
            })
          },
          a.id
        )
      ),
    [scene, setAtomEl]
  )

  return jsxs('div', {
    ref: stageRef,
    className: 'em-stage',
    onPointerDown,
    onPointerMove,
    onPointerUp,
    onPointerCancel: onPointerUp,
    onMouseDown: e => {
      if (e.button === 1) e.preventDefault()
    },
    onAuxClick: e => e.preventDefault(),
    // 右键是框选手势，不能弹出上下文菜单。
    onContextMenu: e => e.preventDefault(),
    onClick: e => {
      if (movedRef.current > 5) return
      if (e.target !== e.currentTarget) return
      const rect = stageRef.current.getBoundingClientRect()
      const x = e.clientX - rect.left
      const y = e.clientY - rect.top
      let best = null
      let bestDist = 10
      for (const seg of hitRef.current) {
        let px = seg.x1
        let py = seg.y1
        for (let i = 1; i <= 12; i += 1) {
          const t = i / 12
          const mt = 1 - t
          const qx = mt * mt * seg.x1 + 2 * mt * t * seg.cx + t * t * seg.x2
          const qy = mt * mt * seg.y1 + 2 * mt * t * seg.cy + t * t * seg.y2
          const dx = qx - px
          const dy = qy - py
          const len2 = dx * dx + dy * dy || 1
          const tt = clamp(((x - px) * dx + (y - py) * dy) / len2, 0, 1)
          const d = Math.hypot(x - (px + dx * tt), y - (py + dy * tt))
          if (d < bestDist) {
            bestDist = d
            best = seg
          }
          px = qx
          py = qy
        }
      }
      if (best) handlersRef.current.onSelectEdge(best.edge)
      else handlersRef.current.onBackground()
    },
    children: [
      jsx('div', { className: 'em-atom-layer', children: atomsTree }),
      jsx('canvas', { ref: backRef, className: 'em-edges em-edge-back' }),
      jsx('canvas', { ref: frontRef, className: 'em-edges em-edges-front' }),
      jsx('div', { ref: lassoElRef, className: 'em-lasso' })
    ]
  })
}

// --------------------------------------------------------------------------- inspector bits

function ProbBlock({ title, probs, chosen }) {
  if (!probs || !Object.keys(probs).length) {
    return jsx('div', { className: 'em-prob-empty', children: '（这一问没有留档）' })
  }
  const entries = Object.entries(probs).sort((a, b) => b[1] - a[1])
  const max = Math.max(...entries.map(e => e[1]), 0.0001)
  return jsxs('div', {
    className: 'em-prob',
    children: [
      jsx('div', { className: 'em-prob-title', children: title }),
      ...entries.map(([label, p]) =>
        jsxs(
          'div',
          {
            className: 'em-prob-row',
            'data-hi': label === chosen ? '1' : '0',
            children: [
              jsx('span', { className: 'em-prob-label', children: label }),
              jsx('span', {
                className: 'em-prob-track',
                children: jsx('span', {
                  className: 'em-prob-fill',
                  style: {
                    width: `${Math.max(1, Math.round((p / max) * 100))}%`,
                    background: REL_COLORS[label] || (label === '都不是' ? NONE_COLOR : 'var(--ui-text-quaternary)')
                  }
                })
              }),
              jsx('span', { className: 'em-prob-num', children: p.toFixed(3) })
            ]
          },
          label
        )
      )
    ]
  })
}

function EndpointCard({ atom, onSelect, role }) {
  if (!atom) return null
  return jsx('button', {
    type: 'button',
    className: 'em-endpoint',
    onClick: () => onSelect(atom.id),
    children: jsxs('span', {
      children: [
        jsxs('span', {
          className: 'em-endpoint-head',
          children: [
            jsx('span', { className: 'em-endpoint-id', children: `#${atom.id}` }),
            jsx('span', {
              className: 'em-endpoint-kind',
              style: { color: KIND_COLORS[atom.kind] || 'var(--ui-text-secondary)' },
              children: atom.kind
            }),
            jsx('span', { className: 'em-endpoint-role', children: role })
          ]
        }),
        jsx('span', { className: 'em-endpoint-text', children: atom.text })
      ]
    })
  })
}

function GraphSelect({ value, options, onChange, className = '' }) {
  const [open, setOpen] = useState(false)
  const box = useRef(null)
  useEffect(() => {
    if (!open) return undefined
    const outside = e => { if (box.current && !box.current.contains(e.target)) setOpen(false) }
    const escape = e => { if (e.key === 'Escape') setOpen(false) }
    document.addEventListener('mousedown', outside)
    document.addEventListener('keydown', escape)
    return () => { document.removeEventListener('mousedown', outside); document.removeEventListener('keydown', escape) }
  }, [open])
  const selected = options.find(o => o.value === value)
  return jsxs('div', { ref: box, className: `em-dd ${className}`, children: [
    jsxs('button', { type: 'button', className: 'em-select em-dd-btn', 'aria-haspopup': 'listbox', 'aria-expanded': open,
      onClick: () => setOpen(v => !v), children: [selected?.label || '—', '▾'] }),
    open ? jsx('div', { className: 'em-dd-list', role: 'listbox', children: options.map(o => jsx('button', {
      type: 'button', role: 'option', className: 'em-dd-item', 'aria-selected': o.value === value,
      onClick: () => { setOpen(false); onChange(o.value) }, children: o.label
    }, o.value)) }) : null
  ] })
}

function RelationPicker({ value, onChange, allowEmpty }) {
  return jsx(GraphSelect, { className: 'em-rel-select', value: value || '', onChange,
    options: [...(allowEmpty ? [{ value: '', label: '— 选一个关系 —' }] : []), ...RELS.map(rel => ({ value: rel, label: rel }))] })
}

function AtomInspector({ atom, scene, projects, ctx, onSaved, onSelectEdge, onClose }) {
  const [text, setText] = useState(atom.text)
  const [kind, setKind] = useState(atom.kind)
  const [project, setProject] = useState(atom.project || '未归类')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [note, setNote] = useState(null)
  useEffect(() => {
    setText(atom.text)
    setKind(atom.kind)
    setProject(atom.project || '未归类')
    setErr('')
  }, [atom.id, atom.text, atom.kind, atom.project])

  const projectOptions = useMemo(
    () => [
      { value: '未归类', label: '未归类' },
      ...(projects || []).filter(p => p.status !== 'archived').map(p => ({ value: p.name, label: p.name }))
    ],
    [projects]
  )

  const dirty = text.trim() !== atom.text || kind !== atom.kind || project !== (atom.project || '未归类')
  const mine = scene.edges.filter(e => e.src === atom.id || e.dst === atom.id)
  const says = note && note.id === atom.id ? note.text : ''

  const save = async () => {
    setBusy(true)
    setErr('')
    setNote(null)
    try {
      // 只发真的改了的字段：后端按「出现即改」处理，发全量会让一次改正文也触发候选重选。
      const body = {}
      if (text.trim() !== atom.text) body.text = text
      if (kind !== atom.kind) body.kind = kind
      if (project !== (atom.project || '未归类')) body.project = project === '未归类' ? null : project
      const res = await ctx.rest(`/events/${atom.id}`, { method: 'PATCH', body })
      // 后端会连同 Embedding 向量一起重算；编码器不可用时只有正文落下，必须说出来。
      const saved = res && res.vector === 'updated'
        ? `已保存，Embedding 向量已重算（${res.dim} 维）`
        : `已保存；向量没有更新：${(res && res.note) || '未知原因'}`
      setNote({
        id: atom.id,
        text: saved + (res && res.reseat
          ? '。项目/类型变了：下一轮连边会从这条之前重选候选，判过的对不再问，已有的边一条不删'
          : '')
      })
      await onSaved()
    } catch (e) {
      setErr(String(e?.message || e))
    } finally {
      setBusy(false)
    }
  }

  return jsxs('div', {
    className: 'em-insp',
    children: [
      jsxs('div', {
        className: 'em-insp-head',
        children: [
          jsx('span', { className: 'em-insp-title', children: `#${atom.id}` }),
          jsx('span', { className: 'em-insp-sub', children: atom.kind }),
          jsx('button', { type: 'button', className: 'em-x', onClick: onClose, title: '关闭', children: '✕' })
        ]
      }),
      jsxs('div', {
        className: 'em-meta',
        children: [
          jsx('div', { children: fmtFull(atom.ts) }),
          jsx('div', { children: `会话：${shortSession(atom.session)}` }),
          atom.edited_ts
            ? jsx('div', {
                className: 'em-edited-note',
                children: `✎ 人工修改于 ${fmtTime(atom.edited_ts)}（彩虹描边）`
              })
            : null
        ]
      }),
      jsxs('label', {
        className: 'em-field',
        children: [
          jsx('span', { className: 'em-label', children: '类型' }),
          jsx(GraphSelect, {
            value: kind,
            onChange: setKind,
            options: KINDS.map(k => ({ value: k, label: k }))
          })
        ]
      }),
      jsxs('label', {
        className: 'em-field',
        children: [
          jsx('span', { className: 'em-label', children: '所属项目' }),
          jsx(GraphSelect, { value: project, onChange: setProject, options: projectOptions })
        ]
      }),
      jsxs('label', {
        className: 'em-field',
        children: [
          jsx('span', { className: 'em-label', children: '正文' }),
          jsx('textarea', {
            className: 'em-textarea',
            rows: 5,
            value: text,
            onChange: e => setText(e.target.value)
          })
        ]
      }),
      err ? jsx('div', { className: 'em-error', children: err }) : null,
      says ? jsx('div', { className: 'em-hintline', children: says }) : null,
      jsxs('div', {
        className: 'em-actions',
        children: [
          jsx(Button, {
            size: 'sm',
            variant: 'default',
            disabled: busy || !dirty || !text.trim(),
            onClick: save,
            children: busy ? '保存中…' : '保存并更新向量'
          }),
          dirty
            ? jsx('span', { className: 'em-hint', children: '保存会重算这条的 Embedding 向量（彩虹描边）' })
            : jsx('span', { className: 'em-hint', children: '未改动' })
        ]
      }),
      jsxs('div', {
        className: 'em-section',
        children: [
          jsx('div', { className: 'em-section-title', children: `连着的边（${mine.length}）` }),
          jsx('div', { className: 'em-hint', children: '全部一跳真实关系保持可见；每层最多 16 个原子移动展开，完整边列表在下方。' }),
          mine.length === 0
            ? jsx('div', { className: 'em-hint', children: '没有边' })
            : jsx('div', {
                className: 'em-edge-list',
                children: mine.map(e => {
                  const other = e.src === atom.id ? e.dst : e.src
                  const dir = e.src === atom.id ? '←' : '→'
                  return jsx(
                    'button',
                    {
                      type: 'button',
                      className: 'em-edge-row',
                      onClick: () => onSelectEdge(e),
                      children: jsxs('span', {
                        children: [
                          jsx('span', {
                            className: 'em-edge-dot',
                            style: { background: e.kind === 'none' ? NONE_COLOR : REL_COLORS[e.rel] || NONE_COLOR }
                          }),
                          jsx('span', { className: 'em-edge-row-rel', children: e.kind === 'none' ? '无' : e.rel }),
                          jsx('span', { className: 'em-edge-row-id', children: `${dir} #${other}` }),
                          e.edited ? jsx('span', { className: 'em-edge-row-flag', children: '✎' }) : null
                        ]
                      })
                    },
                    e.key
                  )
                })
              })
        ]
      })
    ]
  })
}

function BatchPanel({ ids, projects, ctx, onDone, onClose }) {
  const [project, setProject] = useState('')
  const [kind, setKind] = useState('')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const [note, setNote] = useState('')

  const projectSelect = useMemo(
    () => [
      { value: '', label: '— 不改项目 —' },
      { value: '__none__', label: '未归类（清空项目）' },
      ...(projects || []).filter(p => p.status !== 'archived').map(p => ({ value: p.name, label: p.name }))
    ],
    [projects]
  )
  const kindSelect = useMemo(() => [{ value: '', label: '— 不改类型 —' }, ...KINDS.map(k => ({ value: k, label: k }))], [])
  const dirty = Boolean(project || kind)

  const apply = async () => {
    if (!dirty || busy) return
    setBusy(true)
    setErr('')
    setNote('')
    try {
      const body = { ids }
      if (project) body.project = project === '__none__' ? null : project
      if (kind) body.kind = kind
      const res = await ctx.rest('/events/batch', { method: 'POST', body })
      const v = res?.vector || {}
      const bits = [`已改 ${res?.updated ?? ids.length} 条`]
      if (v.updated || v.skipped) {
        bits.push(v.skipped
          ? `向量 ${v.updated} 条重算 / ${v.skipped} 条跳过（${v.note || '原因未知'}）`
          : `向量 ${v.updated} 条已重算`)
      }
      if (res?.cross) bits.push(`跨项目标记 ${res.cross} 条已修正`)
      bits.push(res?.reseat
        ? '已标记重算候选边：下一轮连边会从最早的这条之前开始，判过的对不再问'
        : '候选边不用重算：游标已经在这批事件之前')
      if (res?.missing?.length) bits.push(`${res.missing.length} 条不存在，已跳过`)
      setNote(bits.join(' · '))
      setProject('')
      setKind('')
      await onDone()
    } catch (e) {
      setErr(String(e?.message || e))
    } finally {
      setBusy(false)
    }
  }

  return jsxs('div', {
    className: 'em-batch',
    children: [
      jsxs('div', {
        className: 'em-batch-head',
        children: [
          jsx('span', { className: 'em-batch-title', children: '批量编辑' }),
          jsx('span', { className: 'em-batch-sub', children: `${ids.length} 个原子` }),
          jsx('button', { type: 'button', className: 'em-x', onClick: onClose, title: '取消选择', children: '✕' })
        ]
      }),
      jsxs('label', {
        className: 'em-field',
        children: [
          jsx('span', { className: 'em-label', children: '所属项目' }),
          jsx(GraphSelect, { value: project, onChange: setProject, options: projectSelect })
        ]
      }),
      jsxs('label', {
        className: 'em-field',
        children: [
          jsx('span', { className: 'em-label', children: '类型' }),
          jsx(GraphSelect, { value: kind, onChange: setKind, options: kindSelect })
        ]
      }),
      err ? jsx('div', { className: 'em-error', children: err }) : null,
      note ? jsx('div', { className: 'em-hintline', children: note }) : null,
      jsxs('div', {
        className: 'em-actions',
        children: [
          jsx(Button, {
            size: 'sm',
            variant: 'default',
            disabled: busy || !dirty,
            onClick: apply,
            children: busy ? '应用中…' : '应用'
          }),
          jsx('span', {
            className: 'em-hint',
            children: dirty ? '改项目会重算向量与跨项目标记；已有的边一条不删' : '选项目或类型后应用'
          })
        ]
      }),
      jsx('div', {
        className: 'em-hint',
        children: `编号：${ids.slice(0, 12).join('、')}${ids.length > 12 ? ' …' : ''}`
      })
    ]
  })
}

function EdgeInspector({ edge, scene, data, ctx, onSaved, onSelectAtom, onClose }) {
  const [rel, setRel] = useState(edge.rel || '')
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  useEffect(() => {
    setRel(edge.rel || '')
    setErr('')
  }, [edge.key, edge.rel])

  const src = scene.byId.get(edge.src)
  const dst = scene.byId.get(edge.dst)
  const pair = useMemo(() => {
    if (edge.pair) return edge.pair
    return (data.pairs || []).find(p => p.src === edge.src && p.dst === edge.dst) || null
  }, [data, edge])

  const ownAxis = edge.probs && Object.keys(edge.probs).length >= 4 ? edge.probs : null
  const ownDetail = edge.probs && Object.keys(edge.probs).length === 3 ? edge.probs : null
  const axisProbs = pair?.axis_probs || ownAxis
  const detailProbs = pair?.detail_probs || ownDetail

  const save = async () => {
    if (!rel) return
    setBusy(true)
    setErr('')
    try {
      await ctx.rest('/edges', {
        method: 'PATCH',
        body: { src: edge.src, dst: edge.dst, rel, old_rel: edge.kind === 'rel' ? edge.rel : null, conf: edge.conf }
      })
      await onSaved()
    } catch (e) {
      setErr(String(e?.message || e))
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    if (edge.kind !== 'rel') return
    setBusy(true)
    setErr('')
    try {
      await ctx.rest(
        `/edges?src=${edge.src}&dst=${edge.dst}&rel=${encodeURIComponent(edge.rel)}`,
        { method: 'DELETE' }
      )
      await onSaved()
    } catch (e) {
      setErr(String(e?.message || e))
    } finally {
      setBusy(false)
    }
  }

  return jsxs('div', {
    className: 'em-insp',
    children: [
      jsxs('div', {
        className: 'em-insp-head',
        children: [
          jsx('span', {
            className: 'em-insp-title',
            style: { color: edge.kind === 'none' ? 'var(--ui-text-secondary)' : REL_COLORS[edge.rel] },
            children: edge.kind === 'none' ? '判定：无关系' : edge.rel
          }),
          jsx('span', { className: 'em-insp-sub', children: `${edge.src} → ${edge.dst}` }),
          jsx('button', { type: 'button', className: 'em-x', onClick: onClose, title: '关闭', children: '✕' })
        ]
      }),
      jsxs('div', {
        className: 'em-meta',
        children: [
          jsx('div', { children: `#${edge.src}（后） → #${edge.dst}（前）` }),
          edge.kind === 'rel'
            ? jsx('div', {
                children: `置信 ${edge.conf.toFixed(3)} · ${edge.decidedBy === 'human' ? '人工' : edge.decidedBy === 'jev' ? '模型判定' : edge.decidedBy}`
              })
            : jsx('div', { children: '这一对问过模型，但没有过 margin —— 没有连边' }),
          edge.edited ? jsx('div', { className: 'em-edited-note', children: '✎ 人工改过（彩虹描边）' }) : null
        ]
      }),
      jsxs('div', {
        className: 'em-endpoints',
        children: [
          jsx(EndpointCard, { atom: src, role: '后', onSelect: onSelectAtom }),
          jsx(EndpointCard, { atom: dst, role: '前', onSelect: onSelectAtom })
        ]
      }),
      jsx(ProbBlock, { title: '问题一 · 后一条相对前一条', probs: axisProbs, chosen: pair?.axis_label || edge.rel }),
      jsx(ProbBlock, {
        title: '问题二 · 补充关系',
        probs: detailProbs,
        chosen: pair?.detail_label || (detailProbs === edge.probs ? edge.rel : null)
      }),
      jsxs('div', {
        className: 'em-field',
        children: [
          jsx('span', { className: 'em-label', children: edge.kind === 'rel' ? '改这条边的关系' : '人工连一条边' }),
          jsx(RelationPicker, { value: rel, onChange: setRel, allowEmpty: edge.kind !== 'rel' })
        ]
      }),
      err ? jsx('div', { className: 'em-error', children: err }) : null,
      jsxs('div', {
        className: 'em-actions',
        children: [
          jsx(Button, {
            size: 'sm',
            variant: 'default',
            disabled: busy || !rel || rel === edge.rel,
            onClick: save,
            children: busy ? '保存中…' : edge.kind === 'rel' ? '保存关系' : '连上'
          }),
          edge.kind === 'rel'
            ? jsx(Button, { size: 'sm', variant: 'ghost', disabled: busy, onClick: remove, children: '断开' })
            : null,
          jsx('span', { className: 'em-hint', children: '人工改过的边会蒙上彩虹光' })
        ]
      })
    ]
  })
}

// --------------------------------------------------------------------------- page

function GraphPage({ ctx }) {
  const qc = useQueryClient()
  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ['eventmem-graph', 'graph'],
    queryFn: () => ctx.rest('/graph'),
    staleTime: 8000,
    refetchOnWindowFocus: false
  })

  const [filters, setFilters] = useState({ project: '', session: '', date: '', kinds: [], rels: [], ids: '' })
  const [mode, setMode] = useState('only')
  const [showLabels, setShowLabels] = useState(false)
  const [panelOpen, setPanelOpen] = useState(false)
  const [semQ, setSemQ] = useState('')
  const [semTop, setSemTop] = useState(16)
  const [sem, setSem] = useState(null)
  const [semBusy, setSemBusy] = useState(false)
  const [pulled, setPulled] = useState(() => new Set())
  const [sel, setSel] = useState(null)
  // 右键框选出来的批量选择：和「选中（检查器）」是两套东西，互不覆盖。
  const [multi, setMulti] = useState(() => new Set())
  const [focusReq, setFocusReq] = useState(null)
  const [layoutEpoch, setLayoutEpoch] = useState(0)
  const seqRef = useRef(0)
  // 用户拖过/拉过的位置：本地权威，落库前也不回弹。
  const posRef = useRef(new Map())
  const seededRef = useRef(false)
  const pendingRef = useRef(new Set())
  const saveTimer = useRef(0)
  const barRef = useRef(null)
  const camRef = useRef({
    yaw: 0.55,
    pitch: 0.26,
    dist: START_DIST,
    fx: 0,
    fy: 0,
    fz: 0,
    panX: 0,
    panY: 0,
    fit: 40,
    min: 6,
    max: 300,
    anim: null,
    dirty: true,
    ready: false
  })

  const rawScene = useMemo(() => {
    if (!data) return null
    if (!seededRef.current) {
      seededRef.current = true
      for (const p of data.positions || []) posRef.current.set(p.id, { x: p.x, y: p.y, z: p.z })
    }
    return buildScene(data, posRef.current)
  }, [data, layoutEpoch])

  // 关系筛选直接裁剪边表：被筛掉的边既不画、也进不了命中表。
  const scene = useMemo(() => {
    if (!rawScene) return null
    if (!filters.rels.length) return rawScene
    const keep = new Set(filters.rels)
    return { ...rawScene, edges: rawScene.edges.filter(e => keep.has(edgeRelKey(e))) }
  }, [rawScene, filters.rels])

  const totals = useMemo(() => {
    if (!rawScene) return { atoms: 0, rel: 0, none: 0 }
    let rel = 0
    let none = 0
    for (const e of rawScene.edges) {
      if (e.kind === 'none') none += 1
      else rel += 1
    }
    return { atoms: rawScene.atoms.length, rel, none }
  }, [rawScene])

  const idSpec = useMemo(() => parseIdSpec(filters.ids), [filters.ids])

  const idUnknown = useMemo(() => {
    if (!data || !idSpec.ids.size) return 0
    const known = new Set(data.events.map(ev => ev.id))
    let missing = 0
    for (const id of idSpec.ids) if (!known.has(id)) missing += 1
    return missing
  }, [data, idSpec])

  const semActive = Boolean(sem && sem.q && !sem.error)
  // 分面：每个下拉的选项只来自「其它条件都满足的事件」，选了项目之后会话列表就只剩这个项目里的。
  const semIds = useMemo(
    () => (semActive ? new Set(sem.hits.map(h => h.id)) : null),
    [semActive, sem]
  )
  // 关系筛选也算一条约束：选了某类关系，下拉里只剩下落在这类边端点上的值。
  const relPool = useMemo(() => {
    if (!filters.rels.length) return null
    const ids = new Set()
    for (const e of scene?.edges || []) {
      ids.add(e.src)
      ids.add(e.dst)
    }
    return ids
  }, [filters.rels, scene])
  const facets = useMemo(
    () => facetCounts(data?.events || [], filters, idSpec.ids, semIds, semActive, relPool),
    [data, filters, idSpec, semIds, semActive, relPool]
  )
  const projectOptions = useMemo(() => {
    const list = [...facets.project.entries()]
      .sort((a, b) => a[0].localeCompare(b[0], 'zh'))
      .map(([v, r]) => ({ value: v, label: `${v} · ${r.n} 条` }))
    // 选中的值即使被别的条件筛成 0 条也要留在列表里，否则下拉会显示成空、看不出在筛什么。
    if (filters.project && !facets.project.has(filters.project)) {
      list.unshift({ value: filters.project, label: `${filters.project} · 0 条` })
    }
    return [{ value: '', label: '项目：不限' }, ...list]
  }, [facets, filters.project])
  const sessionOptions = useMemo(() => {
    const list = [...facets.session.entries()]
      .sort((a, b) => b[1].last - a[1].last)
      .map(([v, r]) => ({ value: v, label: `${shortSession(v)} · ${r.n} 条` }))
    if (filters.session && !facets.session.has(filters.session)) {
      list.unshift({ value: filters.session, label: `${shortSession(filters.session)} · 0 条` })
    }
    return [{ value: '', label: '会话：不限' }, ...list]
  }, [facets, filters.session])
  const dateOptions = useMemo(() => {
    const list = [...facets.date.entries()]
      .sort((a, b) => (a[0] < b[0] ? 1 : -1))
      .map(([v, r]) => ({ value: v, label: `${v} · ${r.n} 条` }))
    if (filters.date && !facets.date.has(filters.date)) {
      list.unshift({ value: filters.date, label: `${filters.date} · 0 条` })
    }
    return [{ value: '', label: '日期：不限' }, ...list]
  }, [facets, filters.date])

  const activeCount = [
    filters.project,
    filters.session,
    filters.date,
    filters.kinds.length ? 1 : 0,
    filters.rels.length ? 1 : 0,
    idSpec.ids.size ? 1 : 0,
    semActive ? 1 : 0
  ].filter(Boolean).length

  const filterInfo = useMemo(() => {
    if (!scene) {
      return { active: false, match: new Set(), focus: new Set(), promo: new Set(),
        count: 0, edgeCount: 0, atomActive: false, relActive: false }
    }
    return computeFilterInfo({
      events: data?.events || [],
      edges: scene.edges,
      filters,
      idSet: idSpec.ids,
      semIds: semActive ? new Set(sem.hits.map(h => h.id)) : null,
      semActive,
      mode,
      pulled
    })
  }, [data, scene, filters, idSpec, sem, semActive, mode, pulled])

  const filterKey = JSON.stringify([
    filters.project, filters.session, filters.date, filters.kinds, filters.rels, filters.ids,
    sem?.q || '', sem?.error || '', (sem?.hits || []).map(h => h.id), mode
  ])
  useEffect(() => {
    setPulled(new Set())
  }, [filterKey])

  // 筛完之后命中的原子可能全在镜头外（默认镜头贴着最新原子），空白舞台等于「筛坏了」。
  // 命中集变化时把镜头收拢到它的包围球；正在看检查器时不抢镜头。
  const fittedRef = useRef('')
  useEffect(() => {
    if (!filterInfo.active) {
      fittedRef.current = ''
      return undefined
    }
    if (!scene || sel || !filterInfo.focus.size) return undefined
    if (fittedRef.current === filterKey) return undefined
    const timer = setTimeout(() => {
      fittedRef.current = filterKey
      const atoms = []
      let n = 0
      let cx = 0
      let cy = 0
      let cz = 0
      for (const id of filterInfo.focus) {
        const a = scene.byId.get(id)
        if (!a) continue
        atoms.push(a)
        cx += a.x
        cy += a.y
        cz += a.z
        n += 1
      }
      if (!n) return
      cx /= n
      cy /= n
      cz /= n
      let radius = 1.5
      for (const a of atoms) radius = Math.max(radius, Math.hypot(a.x - cx, a.y - cy, a.z - cz))
      const cam = camRef.current
      // cam.fit 里已经带着当前视口的宽高比，反推同一个 sin(半角) 再按命中球半径算距离。
      const sinHalf = Math.min(0.95, Math.max(0.06, ((scene.radius || 1) * 1.12) / Math.max(1e-6, cam.fit)))
      const dist = Math.max(cam.min, Math.min(cam.max, (radius * 1.12) / sinHalf))
      // cam.fx/fy/fz 是注视点，不是眼睛：eye = 注视点 + dist * 方向（cameraBasis）。
      animateCamera(cam, { dist, fx: cx, fy: cy, fz: cz, panX: 0, panY: 0 }, 620)
    }, 260)
    return () => clearTimeout(timer)
  }, [filterInfo, filterKey, scene, sel, camRef])

  useEffect(() => {
    if (!panelOpen) return undefined
    const outside = e => {
      if (barRef.current && !barRef.current.contains(e.target)) setPanelOpen(false)
    }
    const escape = e => {
      if (e.key === 'Escape') setPanelOpen(false)
    }
    document.addEventListener('mousedown', outside)
    document.addEventListener('keydown', escape)
    return () => {
      document.removeEventListener('mousedown', outside)
      document.removeEventListener('keydown', escape)
    }
  }, [panelOpen])

  const patch = useCallback(update => {
    setFilters(prev => ({ ...prev, ...update }))
    setSel(null)
    setMulti(new Set())
  }, [])

  const clearAll = useCallback(() => {
    setFilters({ project: '', session: '', date: '', kinds: [], rels: [], ids: '' })
    setSem(null)
    setSemQ('')
    setMode('only')
    setPulled(new Set())
    setSel(null)
    setMulti(new Set())
  }, [])

  const clearSemantic = useCallback(() => {
    setSem(null)
    setSemQ('')
  }, [])

  const runSemantic = useCallback(async () => {
    const q = semQ.trim()
    if (!q) {
      setSem(null)
      return
    }
    setSemBusy(true)
    setPanelOpen(true)
    try {
      const res = await ctx.rest('/semantic', { method: 'POST', body: { q, top: semTop } })
      const hits = Array.isArray(res?.hits)
        ? res.hits
            .map(h => ({ id: Number(h.id), score: Number(h.score) }))
            .filter(h => Number.isFinite(h.id) && Number.isFinite(h.score))
        : []
      setSem({ q, hits, scanned: Number.isFinite(Number(res?.scanned)) ? Number(res.scanned) : null,
        skipped: Number(res?.skipped ?? 0), error: '' })
    } catch (e) {
      setSem({ q, hits: [], scanned: null, skipped: 0, error: String(e?.message || e) })
    } finally {
      setSemBusy(false)
    }
  }, [ctx, semQ, semTop])

  const sceneRef = useRef(scene)
  sceneRef.current = scene

  const selectAtom = useCallback(
    (id, force) => {
      // 已经有焦点时，点别的地方只负责退出；再点一次才选中新的。
      if (sel && !force) {
        setSel(null)
        haptic('tap')
        return
      }
      setSel({ type: 'atom', id })
      haptic('tap')
      seqRef.current += 1
      setFocusReq({ seq: seqRef.current, mode: 'atom', id })
    },
    [sel]
  )

  const selectEdge = useCallback(
    (edge, force) => {
      if (sel && !force) {
        setSel(null)
        haptic('tap')
        return
      }
      // 筛选时把这根边的两头都算进焦点：拉过之后它们才点得动。
      if (filterInfo.active) {
        setPulled(prev => {
          const next = new Set(prev)
          next.add(edge.src)
          next.add(edge.dst)
          return next
        })
      }
      setSel({ type: 'edge', key: edge.key })
      haptic('tap')
      seqRef.current += 1
      setFocusReq({ seq: seqRef.current, mode: 'edge', src: edge.src, dst: edge.dst })
    },
    [sel, filterInfo]
  )

  const onIdEnter = useCallback(() => {
    const { ids } = parseIdSpec(filters.ids)
    if (ids.size !== 1) return
    const [id] = [...ids]
    if (!sceneRef.current || !sceneRef.current.byId.has(id)) return
    selectAtom(id, true)
  }, [filters.ids, selectAtom])

  const resetView = useCallback(() => {
    const cam = camRef.current
    const seed = rawScene?.newestId != null ? rawScene.byId.get(rawScene.newestId) : null
    const direction = cameraBasis({ ...cam, yaw: 0.55, pitch: 0.26 })
    animateCamera(cam, {
      yaw: 0.55,
      pitch: 0.26,
      dist: START_DIST,
      fx: (seed ? seed.x : 0) + direction.fx * START_DIST,
      fy: (seed ? seed.y : 0) + direction.fy * START_DIST,
      fz: (seed ? seed.z : 0) + direction.fz * START_DIST,
      panX: 0,
      panY: 0
    })
  }, [rawScene])

  const onSaved = useCallback(async () => {
    await qc.invalidateQueries({ queryKey: ['eventmem-graph', 'graph'] })
  }, [qc])

  const flushPositions = useCallback(() => {
    const ids = [...pendingRef.current]
    pendingRef.current.clear()
    if (!ids.length) return
    const positions = ids
      .map(id => {
        const p = posRef.current.get(id)
        return p ? { id, x: p.x, y: p.y, z: p.z } : null
      })
      .filter(Boolean)
    if (!positions.length) return
    ctx.rest('/positions', { method: 'PUT', body: { positions } }).catch(() => {})
  }, [ctx])

  // 位置落库是防抖的：连续拖动/连点只在停下来 450ms 后写一次。
  const onMoved = useCallback(
    ids => {
      for (const id of ids) pendingRef.current.add(id)
      if (saveTimer.current) clearTimeout(saveTimer.current)
      saveTimer.current = setTimeout(flushPositions, 450)
    },
    [flushPositions]
  )

  const resetLayout = useCallback(async () => {
    try {
      await ctx.rest('/positions', { method: 'DELETE' })
    } catch {
      /* 后端不在也照样回到默认网格 */
    }
    posRef.current = new Map()
    pendingRef.current.clear()
    setSel(null)
    setMulti(new Set())
    setLayoutEpoch(e => e + 1)
    resetView()
  }, [ctx, resetView])

  const selectedAtom = sel?.type === 'atom' ? scene?.byId.get(sel.id) : null
  const selectedEdge =
    sel?.type === 'edge' ? scene?.edges.find(e => e.key === sel.key) || rawScene?.edges.find(e => e.key === sel.key) : null
  // 框选之后图可能已经刷新：面板只认还在这张图里的原子。
  const multiIds = [...multi].filter(id => scene && scene.byId.has(id)).sort((a, b) => a - b)

  if (isLoading) {
    return jsx('div', {
      className: 'em-root em-center',
      children: jsx(GlyphSpinner, { ariaLabel: '载入记忆图谱' })
    })
  }
  if (error) {
    return jsxs('div', {
      className: 'em-root em-center',
      children: [
        jsx('div', { className: 'em-error-title', children: '读不到 eventmem 图谱' }),
        jsx('div', { className: 'em-error-body', children: String(error?.message || error) }),
        jsx('div', {
          className: 'em-hint',
          children: '后端未挂载时先确认插件 eventmem-graph 已启用（hermes plugins enable eventmem-graph）。'
        }),
        jsx(Button, { size: 'sm', onClick: () => refetch(), children: '重试' })
      ]
    })
  }
  if (!data || !scene || scene.atoms.length === 0) {
    return jsx('div', {
      className: 'em-root em-center',
      children: jsx(EmptyState, { title: '还没有记忆原子', description: 'eventmem 里写过事件之后这里才有东西。' })
    })
  }

  const vectorCount = Number(data.vectors?.events || 0)

  return jsxs('div', {
    className: 'em-root',
    children: [
      jsx(Scene, {
        scene,
        focusInfo: filterInfo,
        sel,
        multi,
        focusReq,
        showLabels,
        camRef,
        posRef,
        onMoved,
        onSelectAtom: selectAtom,
        onSelectEdge: selectEdge,
        onLasso: ids => {
          setSel(null)
          setMulti(new Set(ids))
          haptic('tap')
        },
        onBackground: () => {
          setSel(null)
          setMulti(new Set())
        }
      }),

      jsxs('div', {
        className: 'em-bar',
        ref: barRef,
        children: [
          jsx('span', { className: 'em-brand', children: '记忆图谱' }),
          jsxs('button', {
            type: 'button',
            className: 'em-fbtn',
            'data-on': panelOpen ? '1' : '0',
            onClick: () => setPanelOpen(v => !v),
            title: '筛选：项目 / 会话 / 日期 / 类型 / 序号 / 关系 / 语义',
            children: [
              '筛选',
              activeCount ? jsx('span', { className: 'em-fbadge', children: activeCount }) : null
            ]
          }),
          jsxs('div', {
            className: 'em-sem',
            children: [
              jsx('input', {
                className: 'em-input em-seminput',
                value: semQ,
                placeholder: vectorCount ? '语义检索：描述你要找的记忆…' : '语义检索：库里还没有事件向量',
                'aria-label': '语义检索',
                onChange: e => setSemQ(e.target.value),
                onKeyDown: e => {
                  if (e.key === 'Enter') {
                    e.preventDefault()
                    runSemantic()
                  }
                }
              }),
              jsx(Button, {
                size: 'xs',
                variant: 'ghost',
                disabled: semBusy || !vectorCount,
                onClick: runSemantic,
                children: semBusy ? '检索中…' : '检索'
              }),
              sem ? jsxs('span', {
                className: 'em-semtag',
                title: sem.error ? sem.error : `${sem.q} · Top ${semTop}`,
                children: [
                  sem.error ? '语义不可用' : `语义 ${sem.hits.length}`,
                  jsx('button', { type: 'button', className: 'em-x-inline', title: '清除语义筛选', onClick: clearSemantic, children: '✕' })
                ]
              }) : null
            ]
          }),
          filterInfo.active
            ? jsxs('div', {
                className: 'em-seg',
                title: '焦点范围',
                children: [
                  ['only', '仅命中'],
                  ['neighbors', '含一跳邻居']
                ].map(([k, label]) =>
                  jsx(
                    'button',
                    {
                      type: 'button',
                      className: 'em-seg-btn',
                      'data-on': mode === k ? '1' : '0',
                      onClick: () => setMode(k),
                      children: label
                    },
                    k
                  )
                )
              })
            : null,
          jsx('label', {
            className: 'em-check',
            children: [
              jsx('input', {
                type: 'checkbox',
                checked: showLabels,
                onChange: e => setShowLabels(e.target.checked)
              }),
              '关系名'
            ]
          }),
          jsxs('span', {
            className: 'em-counts',
            children: [
              `${totals.atoms} 原子 · ${totals.rel} 边`,
              totals.none ? ` · ${totals.none} 灰边` : '',
              filterInfo.active ? ` · 命中 ${filterInfo.count} 原子 / ${filterInfo.edgeCount} 边` : ''
            ]
          }),
          jsx('span', { className: 'em-spacer' }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: resetView, children: '重置视角' }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: resetLayout, children: '复原布局' }),
          jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => refetch(), children: '刷新' }),
          panelOpen
            ? jsx(FilterPanel, {
                filters,
                patch,
                projectOptions,
                sessionOptions,
                dateOptions,
                idSpec,
                idUnknown,
                onIdEnter,
                sem,
                semTop,
                setSemTop,
                onSelectHit: id => {
                  if (scene.byId.has(id)) selectAtom(id, true)
                },
                onClearSemantic: clearSemantic,
                vectorCount,
                activeCount,
                onClearAll: clearAll
              })
            : null
        ]
      }),

      jsxs('div', {
        className: 'em-legend',
        children: [
          ...KINDS.map(k =>
            jsxs(
              'span',
              { className: 'em-legend-item', children: [
                jsx('span', { className: 'em-legend-dot', style: { background: KIND_COLORS[k] } }),
                k
              ] },
              k
            )
          ),
          jsx('span', { className: 'em-legend-sep' }, 'sep'),
          ...RELS.map(r =>
            jsxs(
              'span',
              { className: 'em-legend-item', children: [
                jsx('span', { className: 'em-legend-line', style: { background: REL_COLORS[r] } }),
                r
              ] },
              r
            )
          ),
          jsx(
            'span',
            {
              className: 'em-legend-item',
              children: [
                jsx('span', { className: 'em-legend-line em-legend-dash', style: { borderColor: NONE_COLOR } }),
                '无'
              ]
            },
            'none'
          )
        ]
      }),

      selectedAtom
        ? jsx(AtomInspector, {
            atom: selectedAtom,
            scene,
            projects: data.projects || [],
            ctx,
            onSaved,
            onSelectEdge: e => selectEdge(e, true),
            onClose: () => setSel(null)
          })
        : null,
      multiIds.length
        ? jsx(BatchPanel, {
            ids: multiIds,
            projects: data.projects || [],
            ctx,
            onDone: async () => {
              await onSaved()
            },
            onClose: () => setMulti(new Set())
          })
        : null,
      selectedEdge
        ? jsx(EdgeInspector, {
            edge: selectedEdge,
            scene,
            data,
            ctx,
            onSaved,
            onSelectAtom: id => selectAtom(id, true),
            onClose: () => setSel(null)
          })
        : null
    ]
  })
}

// --------------------------------------------------------------------------- register

export default {
  id: ID,
  name: 'eventmem graph',
  description: 'eventmem 的三维记忆图谱（作为「记忆后台 → 图谱」页渲染）：原子按类型着色、关系边着色，可筛选、聚焦与人工校正。',
  register(ctx) {
    // Headless view provider: no route, no status-bar chip — the graph is a tab inside
    // `eventmem-gui` now. This plugin only hands its page over and keeps the data path
    // (/api/plugins/eventmem-graph/*) alive.
    //
    // Runtime plugins are evaluated from blob: URLs, so a relative import is impossible;
    // a global is the only way to hand a component over.
    globalThis.__eventmemGraph = { GraphPage, ctx, version: 1 }
    ctx.onDispose(() => {
      if (globalThis.__eventmemGraph && globalThis.__eventmemGraph.ctx === ctx) {
        delete globalThis.__eventmemGraph
      }
    })

    const style = document.createElement('style')
    style.dataset.plugin = ID
    style.textContent = CSS
    document.head.append(style)
    ctx.onDispose(() => style.remove())
  }
}
