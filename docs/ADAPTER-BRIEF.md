# 给 dsh 的 adapter 交接

你要给 eventmem 写第二个宿主的 adapter。**core 已经宿主无关：不要改它、不要 fork 它。**

你跑在 **Windows**，core 和数据库在小主机（Mint，hostname `UnwantedMINIPC`）。
所以你的 adapter 是一个 **HTTP 客户端**：不 `import eventmem_core`、不碰 `events.db`、
也不需要 `pip install eventmem-core`。库文件永远不出 Mint。

## 0. 五步跑通（先做这个，再读代码）

```bash
# 1. 隧道：把 Mint 的 9123 搬到本地（常驻用 autossh / systemd 单元）
ssh -N -L 9123:127.0.0.1:9123 <mint>

# 2. 活着吗
curl -s http://127.0.0.1:9123/api/health        # {"status":"ok", "schema":12, …}

# 3. 有哪些项目（封闭词表，写事件只能从里面选）
curl -s http://127.0.0.1:9123/api/mem/projects

# 4. 写一条，再读回来（agent 应该是 dsh）
curl -s -X POST http://127.0.0.1:9123/api/mem/events -H 'Content-Type: application/json' \
  -d '{"kind":"结果","text":"dsh 的第一条事件","project":"小主机","session":"dsh:s1","agent":"dsh"}'
curl -s http://127.0.0.1:9123/api/mem/events/<刚返回的 id>

# 5. 召回：换个措辞能命中它
curl -s -X POST http://127.0.0.1:9123/api/mem/recall -H 'Content-Type: application/json' \
  -d '{"query":"dsh 写的","session":"dsh:s1"}'
```

五步都通，剩下的就是把工具面和回合钩子接上。

## 1. 代码与运行时在哪

- 仓库：Mint `~/eventmem` ↔ Windows `D:\eventmem`（同一份，Mint 侧用 tar-over-ssh 单向推）。
- core 只在 Mint 的 daemon 进程里跑。**你不需要装它**；你要读的是 `core/eventmem_core/*.py` 里
  `EventMem` 的方法签名（数据面就是它的镜像），以及 `adapters/hermes/eventmem/*.py` 这份参考实现。
- 库：`/home/wacky_ju2/.hermes/eventmem/events.db`（schema **v12**；WAL；当前 492 事件 / 1234 边 / 5 项目）。
  模型是 Qwen3-Embedding-0.6B int8，放在 `~/.hermes/eventmem/models/`，归 eventmem 自己所有。
- daemon：`127.0.0.1:9123`。**管理面**（配置/后端/日志/跑一轮/GUI）和**数据面**（`/api/mem/*`）在同一个端口，
  没有 token、没有 TLS，只绑回环——所以跨机必须走 SSH 隧道，不要建议谁把它绑到 `0.0.0.0`。
- 读文档顺序：`docs/api.md`（数据面规格）→ `docs/architecture.md`（core/adapter 边界）→
  `adapters/hermes/eventmem/*.py`（完整参考实现）→ `HANDOFF.md`。

## 2. 动手前先回答这四个问题，再写代码

你对自己跑的环境最熟，先把事实说清楚：

1. **进程模型**：常驻进程还是每次调用一个进程？**一个进程里会不会并发跑多个会话？**
   —— 决定 `session` 怎么拼：同一个 session 会共享「这一轮已经给过什么」的去重状态，
   两段对话共用一个 id 就会互相把正文吞成 `#id 同上文`。
2. **回合边界**：有没有「用户回合结束」的钩子？有没有「压缩 / 清空会话 / 撤销」的通知？
   —— 这些边界要发 `{"session": …, "reset": true}`，否则用户会看到悬空指针。
3. **辅助模型**：能不能低成本调一次小模型（temperature 0、≤500 token、超时 60 s）？
   —— 回合末抽取必须用它。没有的话退化成「只有模型主动调 evt_add 才记」，纯问答回合会全丢。
4. **工具协议**：怎么注册工具？工具返回后有没有回调（`on_tool_result`）？
   —— 中间件那条「你刚改了状态，补一条事件」的提醒，是事件量的主来源。

## 3. 数据面：你唯一的接口面

全部是 JSON。错误一律 `4xx` + `{"error": "..."}`，值错误是调用方的问题，daemon 不会 500。

| 你的工具 | 数据面调用 |
|---|---|
| `evt_add` | `POST /api/mem/events` —— 一次一条；批量、`links`、脱敏在你这边循环 |
| `mem_recall` | `POST /api/mem/recall` |
| `expand` | `GET /api/mem/events/<id>` |
| `mem_neighbors` | `POST /api/mem/neighbors` |
| `proj_add` / `proj_list` | `POST` / `GET /api/mem/projects` |
| 会话→项目 的粘性 | `POST /api/mem/session/project`（`{session}` 读，`{session, project}` 写） |
| `evt_forget` | `POST /api/mem/events/<id>/forget` |

```http
POST /api/mem/events
  {"kind":"结果","text":"……","project":"小主机","session":"dsh:xxx","agent":"dsh",
   "extra_projects":["eventmem"],"path":"/abs/file","artifact":"原始输出","artifact_kind":"日志"}
→ {"ok":true,"id":485,"agent":"dsh"}

POST /api/mem/recall
  {"query":"……","session":"dsh:xxx","reset":false,"budget_tokens":600}
→ {"block":"[eventmem] 需要原文用 expand(#id)\n[日期] #id …","hint":"[eventmem] 可用项目：…",
   "count":3,"stateless":false}
```

四条规则：

- **`agent` 必须带，而且只在一个地方声明**：它是「这条是谁记的」的权威答案。你用 `"dsh"`。
- **会话 id 带前缀**：`dsh:<你的会话 id>`。项目黏性表按 session 存，不前缀就会被 Hermes 的同名会话抢走。
- **`reset: true` 的时机**：新会话、压缩、撤销。不清的话，第二次召回只给 `#id 同上文` 指针、没有正文。
- **`hint` 是单独一个字段**：项目候选那一行，宿主自己决定拼在召回块尾部还是别处（Hermes 是拼块尾）。
  不带它，模型不知道有哪些项目名可写，实测 18/109 条会落 `unfiled`。

数据面**不**替你做的事（都在你这边）：批量上限（Hermes 是 3 条/次）、批内 `links` 的方向校验
（**src 必须比 dst 严格新**）、脱敏、条目长度的软约束、「这个回合已经写过事件」的判断。

**项目粘性**是 adapter 的事，但「哪个会话写过哪个项目」要落到库里：`POST /api/mem/session/project`
读写的就是 `session_project` 表（Hermes 一直用它）。宿主进程重启、插件重载、会话改名之后，
同一会话的下一条事件仍然回到同一个项目，而不是落 `unfiled` 或让模型重新选一个。

落库的**字面值不翻译**：kind 仍是 `决定/尝试/结果/变更/教训`，rel 仍是 `改变/否定/重复/因为/细化`。
要双语就翻译**文案**，别翻译落库的值——翻了两套历史数据就劈了。

## 4. 你要写的文件（跨机版）

| 文件 | 干什么 |
|---|---|
| 入口 / 注册 | 把你的 provider、中间件、工具装进宿主（宿主协议各不相同） |
| provider 等价物 | 生命周期、`prefetch`（每回合一次）、回合结束抽取、`system_prompt_block` |
| tools | 7 个工具的 schema + 分发；参数校验照参考实现抄，文案自己写 |
| middleware | 「这个工具改了状态 → 在结果里追加一行提醒」；参考实现里那张 Hermes 工具名表 + shell 启发式对你无用，照语义重写 |
| extract | 回合末抽取的提示词与 JSON 解析，**基本可直接复用** |
| net | 一个很薄的 HTTP 客户端：超时、重试、**隧道断了要能降级**（见下） |
| config | 隧道地址、超时、`agent` 名——不是 `CoreSettings`，你不用碰它 |

三个必须做对的语义：

- **`prefetch` 只读、每回合一次**，把 `block` 原样注入（表头 + 行首 `#id` 是用户定的格式，别美化）；
  `hint` 拼在后面。**任何失败都返回空**——隧道断了、daemon 没起，都不能拖垮宿主的一个回合。
  Hermes 侧所有召回路径都是「失败即静默降级」，这是硬要求。
- **回合末抽取**：该回合没写过事件时，问你的小模型要 0–3 条事件，然后逐条 `POST /api/mem/events`。
  返回 `[]` = 不值得记。**调用失败或解析失败时只记一条本地通知，绝不自己编一条「用户问了…我答了…」的机械事件**
  （这条是被明确否掉的设计：那一轮本来就没结论，机械复述只是噪声，还盖住真故障）。
  抽取提示词里「问答也要记：用户问了什么 + 结论/依据是什么；只有寒暄、纯确认才返回空」这句判据，
  必须与 `evt_add` 的工具描述**逐字同口径**，改一处要同时改另一处。
- **`system_prompt_block()` 返回空串**：故意留空，保宿主 prompt 的缓存前缀。常驻事实走召回，不塞 system prompt。

## 5. 边界与硬缺口（别假设有）

1. **召回块里不显示来源**。`agent` 在 `expand(#id)` 的返回里，渲染行里没有 `[dsh]` 前缀
   （加前缀就是改用户定的渲染契约）。
2. **一个库只能有一个夜间 worker**。单实例靠 `<db>.daemon.lock` 的 flock。共享这个库就
   **不要起你自己的 daemon / worker**，也不要碰 `POST /api/run-once`——否则两个进程抢同一个
   `--parallel 1` 的单槽模型、互相改游标。
3. **数据面只做「一条一条」**。批量、批内链接、脱敏、黏性都在 adapter。这不是缺陷，是边界。
4. **隧道是单点**。daemon 挂了、SSH 断了，你的召回和写入都会失败——设计成降级（这一回合没有记忆），
   不要重试到把回合卡住。

## 6. 落地验收（这六条过了才算接上）

1. **隧道通**：`GET /api/health` 回 `{"status":"ok","schema":12}`。
   connection refused = 隧道没起；`schema: 11` = daemon 还在跑旧代码（`systemctl --user restart eventmem-worker`）。
2. **写一条再召回**：`POST /api/mem/events` 写 → 换个措辞 `POST /api/mem/recall` 能命中它，
   `GET /api/mem/events/<id>` 里 `agent` 是你。
   命中不到先怀疑**编码器**（Mint 上 `~/.hermes/eventmem/models/` 里有没有 `tokenizer.json` +
  `model_int8.onnx`；daemon 日志应有 `encoder ready`），不是 core 坏了。
3. **召回块形态**：表头 + 行首 `#id`；同一 `session` 第二轮只给 `#id 同上文` 指针；
   `reset: true` 之后恢复首块形态。
4. **没有第二个 worker**：`~/.hermes/eventmem/events.db.daemon.lock` 由 `eventmem-worker` 持有；
   你那边没有自己的连边 pass 日志。
5. **坏输入不 500**：未知项目 / 缺 `kind` / 不存在的事件 id → 400 / 400 / 404。
6. **降级**：停掉隧道，你的 `prefetch` 返回空、回合照常结束，不抛异常到用户面上。

## 7. 不要做的事

- 不要改 core 去适配你（真正的宿主无关 bug 走 PR）。core 有测试
  `tests/test_settings.py::test_core_never_imports_a_host` 扫 `from agent|hermes_cli|plugins|tools`，命中即红。
- **不要自己实现召回、连边判定、骨架预算**——它们正是这次拆分要保住的唯一资产。
- 不要同步 / 复制 `events.db`。SQLite/WAL 放网络盘会静默损坏，这正是数据面存在的理由。
- 不要给「抽取失败」补机械兜底事件。
- 不要给 `events.db` 加表加列（你也不该直接碰库）：schema 由 core 的 `ensure_schema()` 单向迁移。
- 不要在生产库上做实验。

## 8. 代码放哪

优先放进同一个仓库 `adapters/dsh/` —— core 一改，你的 adapter 跟着一起被测到。
拿不到仓库就单开一个包：**它只依赖一个 HTTP 客户端**，不依赖 `eventmem-core`（也不该依赖）。

---

## 附录：如果哪天你和 core 同机

跨机形态下这些都用不上，但别丢掉：同机时可以直接 `import eventmem_core`，用
`EventMem(settings, agent="dsh")` 这个门面（`add_event` / `recall` / `expand` / `neighbours` /
`add_project` / `forget`），语义与数据面一一对应。那时要守的规则是：
`EventMem` 是唯一入口（不许 import `store` 拼 SQL）、`CoreSettings` 是 frozen dataclass（改就 `apply()` 出新的，
未知键告警忽略 / 坏值告警回落 / 越界拒绝）、不要把默认值抄进自己的加载器、
模型目录用 `CoreSettings.models_root` 而不是猜。
