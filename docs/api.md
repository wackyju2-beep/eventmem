# 数据面：把同一个库开放给另一个宿主

eventmem 的默认形态是**一台机器**：宿主进程直接 `import eventmem_core`，`events.db` 是本地文件。
当第二个宿主跑在另一台机器上（Windows 上的 dsh、容器、树莓派），它没法直接读这个文件——
SQLite/WAL 不能放网络盘，文件锁在网络文件系统上不可靠，会静默损坏。

所以 daemon 额外挂了**一层很薄的数据面**：`EventMem` 的 REST 镜像。库文件永远只在本机，
另一台机器上的宿主通过 HTTP 读写同一份记忆。

```
   Windows (dsh)                       小主机 (Mint)
   ┌────────────┐   ssh -L 9123   ┌──────────────────────────┐
   │ adapter    │────────────────▶│ eventmem daemon :9123    │
   │ (HTTP)     │  127.0.0.1       │  /api/mem/* → EventMem   │
   └────────────┘                  │            → events.db   │
                                   └──────────────────────────┘
```

## 绑定与鉴权

- daemon 默认绑定 `127.0.0.1`，但监听地址可配置；它**没有 token、没有 TLS**。跨机访问建议走 SSH 隧道：
  `ssh -N -L 9123:127.0.0.1:9123 mint`，远端把 `http://127.0.0.1:9123` 当本地用。
- 不要为了"方便"把 daemon 绑到 `0.0.0.0`：管理面（`/api/config`、`/api/install`、`/api/logs`）
  和数据面在同一个端口上，等于把整台机器的事件库和配置一起开放出去。真要对外，自己加反向代理 + 鉴权。

## 端点

都是 JSON；错误一律 `4xx` + `{"error": "..."}`，不会 500（值错误是调用方的问题）。

### `POST /api/mem/events` — 写一条事件

```json
{"kind": "结果", "text": "……", "project": "小主机", "session": "dsh:…",
 "agent": "dsh", "extra_projects": ["eventmem"], "path": "/abs/file",
 "artifact": "原始输出", "artifact_kind": "日志"}
```

```json
{"ok": true, "id": 485, "agent": "dsh"}
```

- `kind` 与 `text` 必填；`kind` 必须是 `决定/尝试/结果/变更/教训`。
- `project` 是**封闭词表**：未知名字返回 400（`UnknownProject`），不会自动新建。
- `agent` 是**调用方的身份**，必须自己带上；不传就是 NULL（等同于 v11 之前的老事件，事后无法分辨）。
- 一次一条。批量、`links`（同批内的 `因为/细化` 边）、脱敏、项目黏性都是 **adapter 的事**：
  这个接口只镜像 `EventMem.add_event`。

### `GET /api/mem/events/<id>` — 一条事件 + 原文

返回 `EventMem.expand()` 的形状：`{id, date, kind, text, project, artifact, note?, path?, agent?}`。
没有这条事件 → 404。

### `POST /api/mem/events/<id>/forget` — 忘记一条

`{"id": 485, "removed": true}`。删除和墓碑规则与本地写入一致。

### `POST /api/mem/neighbors` — 看一条事件周围

```json
{"event_id": 485, "rels": ["改变", "否定"], "depth": 1}
```

`rels` 省略 = 默认只跟 `改变/否定/因为`；`depth` 上限 3。返回渲染好的文本块
（`{"event_id", "context", "found"}`），格式与工具里 `mem_neighbors` 的输出一致。

### `POST /api/mem/recall` — 召回

```json
{"query": "上次那个盘怎么选的", "session": "dsh:abc", "budget_tokens": 600}
```

```json
{"block": "[eventmem] 需要原文用 expand(#id)\n…", "hint": "[eventmem] 可用项目：…",
 "count": 3, "stateless": false}
```

- `block` 是渲染好的召回块（骨架段 + 命中段，行首 `#id`）；`hint` 是项目候选那一行，**分开放**，
  由宿主决定拼在块尾还是别处（Hermes 是拼块尾）。
- `session` 决定「这一轮已经给过什么」的去重状态：同一 session 第二次调用只给 `#id 同上文` 指针。
  新建会话、压缩、撤销时带 `"reset": true` 清掉它。daemon 按 LRU 保留最近 64 个会话。
- 不传 `session` = 无状态（每次都完整给，`stateless: true`）——适合一次性脚本，不适合对话。

### `GET /api/mem/projects` / `POST /api/mem/projects`

```json
{"projects": [{"id": 1, "name": "小主机", "status": "active", "event_count": 378}]}
```

`POST {"name": "新项目"}` 是项目**唯一**的出生方式，返回 `{"project": {...}}`；同名已存在时返回已有的。

### `POST /api/mem/session/project` — 会话的项目粘性

```json
{"session": "dsh:s1"}                          → {"session": "dsh:s1", "project": "小主机"}
{"session": "dsh:s1", "project": "小主机"}      → {"ok": true, "session": "dsh:s1", "project": "小主机"}
{"session": "dsh:s1", "project": null}         → 清掉（"" / "unfiled" 同义）
```

不带 `project` 是读，带了是写；没记过的会话读回来是 `""`。未知项目名 400。
粘性存在 `session_project` 表里，宿主进程重启后还在——adapter 靠它把
「一个会话 = 一个项目」做实（Hermes 侧一直用同一张表，dsh 侧现在也走数据面）。

## 图谱（可视化 + 人工校正）

图不是召回：它要**整张**事件表和边表，写的是少数几行人工修改。所以它在数据面里单独一组路由，
仍然只用 GET/POST（daemon 的 HTTP 层没有 PATCH/DELETE），动作写在路径里。

### `GET /api/mem/graph`

```json
{"ok": true, "db": "…/events.db", "generated_at": 1794660000.1,
 "events": [{"id": 1, "ts": …, "kind": "结果", "text": "…", "project": "小主机",
             "project_id": 1, "session": "…", "edited_ts": null}],
 "edges":  [{"src": 2, "dst": 1, "rel": "因为", "conf": 0.8, "decided_by": "jev",
             "cross": 0, "probs": {"…": 0.9}, "edited_ts": null}],
 "pairs":  [{"src": 2, "dst": 1, "axis_probs": {…}, "detail_probs": {…},
             "axis_label": "因为", "detail_label": null, "ts": …}],
 "positions": [{"id": 1, "x": 1.5, "y": -0.5, "z": 2}],
 "projects": [{"id": 1, "name": "小主机", "status": "active"}],
 "sessions": [{"id": "…", "count": 12, "first": …, "last": …}],
 "kinds": ["决定", "尝试", "结果", "变更", "教训"],
 "rels": ["改变", "否定", "重复", "因为", "细化"],
 "vectors": {"events": 512, "dim": 512}}
```

`pairs` 取最近 4000 条（`pair_evals` 是最先该截断的一项）；`positions` 是拖过的原子坐标。

### `POST /api/mem/graph/semantic`

`{"q": "描述", "top": 16, "min_score": 0.3}` → `{"hits": [{"id": 12, "score": 0.71}], "scanned": 512,
"skipped": 0, "dim": 512}`。查询走和召回同一个编码器（`is_query=True`），存的向量是单位向量，
所以点积就是余弦。编码器不可用时回 **503**，不是空结果。

### `POST /api/mem/graph/event` — 改一条

`{"id": 12, "text": "新正文", "kind": "结果", "project": "小主机"}`，三个字段**出现即改**
（`project: null` = 未归类）。正文 ≤500 字，`kind` 必须在五种里，项目必须已存在（不会被凭空创建）。
返回：

```json
{"ok": true, "id": 12, "vector": "updated", "dim": 512, "note": "",
 "cross": 0, "reseat": 1794660000.0}
```

- 改正文/项目会**重算向量**（payload 同写入：`[项目] 类型 正文`）；编码器缺失时 `vector: "skipped"`
  且 `note` 说明原因，正文照样落下。
- 改项目会同时改 `event_projects` 的主项目行、重算相关边的 `edges.cross`（`cross` = 改了几条）。
- 改项目/类型会给下一轮连边留一个 **reseat 标记**（`reseat` = 起始时间戳）：候选要重选，
  但已经判过的对不再问，**已有的边一条不删**。

### `POST /api/mem/graph/events` — 批量

`{"ids": [1,2,3], "project": "小主机", "kind": "教训"}`，至少给一个字段；一次最多 500 条。
返回 `{"updated": 3, "missing": [], "vector": {"updated": 3, "skipped": 0, "note": ""},
"cross": 0, "reseat": …}`。不存在的 id 进 `missing`，不会新建事件。

### `POST /api/mem/graph/edge` / `POST /api/mem/graph/edge/delete`

`{"src": 2, "dst": 1, "rel": "改变", "old_rel": "因为", "conf": 0.9}` 写或改一条人工关系
（`decided_by='human'`，打 `edited_ts`）；`old_rel` 存在时先删旧关系并记进 `edge_deletions`。
删除用 `{"src": 2, "dst": 1, "rel": "改变"}` → `{"deleted": 1}`。两条都会顺带算好 `cross`。

### `POST /api/mem/graph/positions` / `…/positions/clear`

`{"positions": [{"id": 12, "x": 1.5, "y": -0.5, "z": 2}]}`（最多 500 条，坐标必须有限）→
`{"saved": 1}`；`/positions/clear` → `{"cleared": 25}`。存的是「人把原子拖到了哪」，
没有这张表图谱照样能画（回到确定性格子），有它刷新之后才不会弹回去。

## 它**不**做什么

- 远端 adapter 不负责启动后台连边。每个数据库只运行一个 daemon/worker；Windows 缺少 `fcntl` 时单实例锁不生效，不能依赖它阻止重复启动。
- 不做校验以外的任何加工：脱敏、长度限制、批量上限、会话项目黏性都在 adapter。
- 不给你 SQL。要 SQL 就说明这件事应该进 core（提 PR），而不是让每个 adapter 各写一套查询。

## 用起来的最小脚本

```bash
# 本机（或隧道另一头）
curl -s http://127.0.0.1:9123/api/mem/projects
curl -s -X POST http://127.0.0.1:9123/api/mem/events -H 'Content-Type: application/json' \
     -d '{"kind":"结果","text":"数据面写的第一条","project":"小主机","agent":"dsh","session":"dsh:s1"}'
curl -s -X POST http://127.0.0.1:9123/api/mem/recall -H 'Content-Type: application/json' \
     -d '{"query":"数据面","session":"dsh:s1"}'
```
