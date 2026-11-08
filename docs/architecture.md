# 架构

三句话：**事件只追加**，**关系夜间算**，**召回先检索再沿边展开**。
下面的表名、字段名与 `schema.py` 一一对应（schema v7）。

## 1. 数据模型

| 表 | 作用 | 关键点 |
|---|---|---|
| `events` | 记忆本体 | `ts / kind / text / project_id / session / art_id / art_path / vec / edited_ts`，`AUTOINCREMENT` 不复用 id |
| `projects` | 人工命名的项目 | `name UNIQUE`、`status active/archived`；模型只能选不能造 |
| `session_project` | 会话粘性 | 一段会话里说一次项目名，之后默认归属它 |
| `artifacts` | 大块原文 | 命令/配置/日志/代码按 `sha` 去重存全文，事件只留 `art_id` |
| `event_projects` | 多对多 | 一个事件可以同时挂两个项目（`is_primary` 标主项目） |
| `edges` | 修正类关系 | 主键 `(src,dst,rel)`；`src` 是后来的事件，`dst` 是被修正的旧事件；`probs` 存判定时的原始分布 |
| `pair_evals` | 判定留档 | 每对**问过模型**的事件都写一行，不管最后连没连边——图谱里的灰虚线来自这里 |
| `meta` | 游标/开关 | `nightly_since_ts` 等 |
| `events_fts` / `artifacts_fts` | FTS5 trigram | 触发器同步；配置项、型号、报错码靠字面命中 |
| `atom_pos` | 图谱里原子的位置 | `id → (x,y,z)`，人工拖过才有一行；没有就用晶格默认位置 |

几条刻意的选择：

- **`text` ≤ 120 字、第一人称**。事件是「我当时得出的结论」，不是会议纪要。
- **事件不可改**。世界变了就再写一条「变更」，用边指回旧的那条——历史因此可追溯。
- **`vec` 编码的是 `[项目名] 类型 text`**，不是裸 `text`。中文里大量无主语残句（「改成 2 了」），
  只编码正文会让检索失明。
- **`edges.probs`** 存分布而不是只存标签：以后调 margin、换阈值，不用重跑模型。
- **`pair_evals` 与 `edges` 分开**：判过但判成「都不是」也要留证据，否则没法回答「这两条到底算过没有」。

## 2. 写入路径

```
工具结果（write_file / patch / 变更类命令）
   └─ tool_execution 中间件追加一行：「[memory] …请调用 evt_add」
模型在同一回合调 evt_add（1–3 条）
   └─ store.add_event：写 artifacts → 编码向量 → 写 events → 挂项目
回答发出后 sync_turn（后台线程）
   └─ 本回合没写过事件？→ auxiliary LLM 再抽 0–3 条；抽取失败才退化成机械摘要
```

为什么是「回合最后一个工具调用」：工具循环中途还没看到结果，写下去的都是半成品。
为什么要有中间件：hook 回调不能发起工具调用，但中间件能改写工具结果——提醒只能从这里塞。

`sync_turn` 是兜底，不是主路径；模型已经写过就不重复。空数组不算「写过」，
所以闲聊以外不会有整轮丢失。

## 3. 关系（夜间连边）

候选**不是全组合**，是「活跃链头」——没有被 改变/否定/重复 过的事件：

| 来源 | 取法 |
|---|---|
| 语义 | 全局活跃链头里 cos ≥ 0.35 的 top-10 |
| 桥 | 同会话最近的 10 个活跃链头 + 共享同一 artifact 的全部链头 |
| 跨项目 | 只有 变更/决定 → 变更/决定 才触发，cos > 0.7，top-3 |
| 同会话 | 最近 3 条 + 最近 2 条未闭环的教训 + 最新 5 条未被改变/否定的决定 |

一对事件问两个问题，一次请求出两个分布：

| 问 | 选项 |
|---|---|
| `axis` | 重复 / 改变 / 否定 / 都不是 |
| `detail` | 因为 / 细化 / 都不是 |

写边规则：两问各自在**原始四项分布**（含服务端的「都不是」）上取第一名，
`p1 − p2 ≥ 0.15` 且 p1 不是「都不是」才写。`重复` 命中时抑制 detail 轴。
每对先写 `pair_evals` 留档，再写 `edges`——所以「算过但没连上」是可查的。

没有 7 天窗口，也没有每晚上限：worker 每 60 s 看一次 `max(events.id)` 与游标，
有新的就算，算完把游标推到 `run_start − 120s`（两分钟重叠，边是 `INSERT OR REPLACE`，幂等）。

## 4. 召回

```
query ─┬─ 向量：cos ≥ 0.35，top-k
       └─ FTS5：trigram 字面命中
            └─ RRF 融合 → 按优先级装填（每段各自 max_items / budget）
```

每轮最多两段，空行分隔：

1. **骨架段**（每会话每项目一次，独立预算）：最早决定 → 最新决定 → 最新结果 → 最新变更 → 最新教训，
   最多 5 条，缺角色跳过，**不按时间重排**。目的：让模型知道这个项目从哪来、现在什么样。
2. **命中段**（每轮）：RRF 命中 + 一跳「改变/否定」邻居 + 同会话邻居，按 ts 倒序。

命中旧事件时会顺着边反向追到新事件，渲染成 `[已失效·被11-06取代]`。
本会话已经给过的整条只留指针行 `#63 #60 同上文`，不占正文预算。

**第三行：项目候选。** `prefetch` 在块尾（召回为空时单独）给一行
`[eventmem] 可用项目（按本回合相关度）：A / B / C；都不合适就先 proj_add 再 evt_add`。
候选 = 每个项目全部事件向量的均值（质心，事件向量本来就编码了 `[项目名]`）与本回合 query 的 cos，
top-5，现算不落库；空项目（还没有事件）退回 `[项目名]` 的名字向量（进程内缓存；名字对「这块 4060 显存」
这种 query 仍然有效，而空项目最可能承接新工作）；编码器缺席时退化成按活跃度列名字。
prefetch 只 encode 一次 query，向量以 `qvec=` 同时传给 `recall()` 与 `project_hint()`。目的：模型写事件时「只能选不能造」，
但它原本无从得知有哪些项目，只能先写错一次再看错误里的全表。

## 5. 图谱

后端 `plugins/eventmem-graph`：一个 FastAPI `router`，读写同一张库，不 import `eventmem.*`
（避免 provider 被加载两次）。路由：

```
GET    /health
GET    /graph                  → events / edges / pairs / projects / sessions / positions
PATCH  /events/{id}            → 改文本、类型（写 edited_ts，图谱上描彩虹边）
PATCH  /edges                  → 改关系（decided_by='human'）
DELETE /edges?src&dst&rel
PUT    /positions              → 批量存原子坐标（人工拖动的结果）
DELETE /positions              → 复原成默认晶格
```

前端 `desktop-plugins/eventmem-graph/plugin.js`：一个文件，宿主注入 React 与 `@hermes/plugin-sdk`。
布局是**立方晶格**（最新事件占中心，旧事件往外铺），相机 FOV 26° 长焦，默认只看到十来个原子。
渲染用 rAF，每帧只改 `transform` 与 SVG 属性；拖动/焦点飞行的位置写回 `atom_pos`，
所以「拉过的地方」刷新后还在——和在一堆风铃里挑拣一样，挑开就留在那。

## 6. 迁移

`ensure_schema()` 幂等：`user_version` 落后就逐版本 `ALTER TABLE` 补列、建表、建索引。
v3 → v4 删过 `pending_turns`（兜底方案被否），v5 加 `edges.probs`，v6 加 `pair_evals`，
v7 加 `events.art_path`（sha 是内容地址、路径才是文件身份，Tier-2 靠它桥接同一文件的多次改动）
与两处 `edited_ts`。降级不支持：旧版本读到新库会因为没有列而报错，请不要回退。

## 7. 语言

模型看到的文本全部经过 `i18n.tr(zh, en)`：工具描述、中间件提醒、回合末抽取提示词、召回块表头
与失效标记、两问的题干和选项定义。取语言的顺序是 `EVENTMEM_LANG` → 宿主 `agent.i18n.get_language()` →
`config.yaml` 的 `display.language` → 英文（Hermes 自己的默认）。只有 `zh*` 和 `en` 两套，其余回落英文。

**存进库的值不翻译**：`kind` 仍然是 `决定/尝试/结果/变更/教训`，`rel` 仍然是
`改变/否定/重复/因为/细化`。英文模式下下发给模型的选项标签是 `repeat/change/negate`、
`because/refine`，回来的答案在 `_canonical_answer()` 里映射回中文再落库——所以换语言不会分裂历史数据，
也不会让 `_dist()`/`_accepted()` 这类按标签取值的代码失效。

图谱（`desktop-plugins/`）的界面文案目前仍是中文，未接入 i18n。
