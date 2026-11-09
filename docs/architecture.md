# 架构：core / adapter

一句话：**core 负责记忆逻辑，adapter 负责让某个宿主以为这是它自己的记忆。**

```
                 ┌─────────────────────────────┐
   Hermes ──────▶│ adapters/hermes/eventmem    │
                 │  provider · tools ·         │
                 │  middleware · extract ·     │
                 │  settings · cli             │
                 └──────────────┬──────────────┘
                                │  EventMem / CoreSettings
                 ┌──────────────▼──────────────┐
   DeepSeek ────▶│ core/eventmem_core          │
   Harness        │  core.py   EventMem 门面    │
   (下一站)        │  store / recall / embed     │
                 │  nightly / worker / jev     │
                 └─────────────────────────────┘
```

## 边界怎么划的

| 属于 core | 属于 adapter |
|---|---|
| events.db 的 schema 与迁移 | 数据库放在哪个目录（`~/.hermes/eventmem`） |
| 事件的写入、向量编码、FTS/向量融合召回 | 召回块注入到哪一轮（`prefetch`） |
| 项目词表与归档规则 | 工具的 JSON schema 与错误文案 |
| 关系边的判定（margin、候选、tier） | 配置从哪读（`config.yaml` / 环境变量 / 远端） |
| 夜间补算与游标 | 什么时候跑（systemd / cron / 宿主线程） |
| 渲染块的行格式与预算 | 渲染给谁看（模型 / 用户 / 另一个 agent） |

判据很简单：**换一个宿主，这段代码还成立吗？** 成立就在 core。

## core 的三个入口

```python
from eventmem_core import EventMem, CoreSettings, apply

settings = apply(CoreSettings(db_path="/path/events.db"), {
    "edges": {"margin": 0.2},
    "recall": {"budget_tokens": 800},
})
mem = EventMem(settings)          # 打开库、懒加载编码器
mem.add_event("结果", "……", project="小主机", session="s1")
mem.recall("上次那个盘怎么选的？", state=mem.new_recall_state())
mem.build_edges(since_ts)          # 一轮连边（需要决策模型）
mem.close()
```

- `EventMem` 是**唯一**的 API 面。adapter 不许 import `eventmem_core.store` 去拼 SQL。
- `CoreSettings` 是 frozen dataclass；要改就 `apply()` 出新对象，不要原地改。
- `apply()` 的契约：未知键告警忽略、坏值告警回落、越界值拒绝。**配置写错不能让记忆挂掉。**

## adapter 的形状（以 Hermes 为例）

| 文件 | 干什么 | 换宿主时要重写吗 |
|---|---|---|
| `__init__.py` | `register(ctx)`：装 provider、中间件、aux 任务 | 是（别的宿主的插件协议不同） |
| `provider.py` | `MemoryProvider` 生命周期、prefetch、sync_turn | 是 |
| `tools.py` | 7 个工具的 schema + 分发 + 参数校验 | 大部分是（工具名/文案） |
| `middleware.py` | 「这个工具改了状态」判定（Hermes 工具名 + shell 语法） | 是 |
| `extract.py` | 回合末抽取的提示词与 JSON 解析 | 基本可复用 |
| `settings.py` | `config.yaml` → `CoreSettings` | 是 |
| `cli.py` / `run.py` | 脱离会话的 nightly / worker 入口 | 是 |
| `host.py` | 所有 `agent.*` 依赖集中在这一个文件 | 是 |

写新 adapter 的时候，先把 `host.py` 对应的那几个能力找齐：
**调一次辅助模型、脱敏、判断「这句话不值得记」、在宿主上下文里起线程。**

## 数据流

**写**：模型调 `evt_add` → adapter 校验（kind、项目词表、批次上限、links 方向）
→ 脱敏 → `EventMem.add_event()`（编码向量、写 events/event_projects）
→ 批次内 links 走 `EventMem.add_link()`。

**读**：宿主每轮问 `prefetch(query)` → adapter 编一次 query 向量
→ `EventMem.recall()`（向量 + FTS → RRF → 骨架段 + 命中段）
→ 拼上 `project_hint` 一行 → 返回给宿主注入。

**边**：worker 每 60 s 看游标之后有没有新事件 → 有就 `build_edges`
→ 候选（活跃链头 / 同会话 / 跨项目 / 决策骨架）→ 两问 choice
→ `margin` 判定 → `add_edge` → 推游标（高水位）。

## 为什么这样拆

- **一个宿主多个进程**：Hermes 的 dashboard / gateway / 每个桌面会话都是完整后端，
  插件会被加载很多次。连边必须是独立进程，不是「插件里的线程」——所以 worker 在 core。
- **宿主会换**：DeepSeek Harness 的插件协议、配置位置、模型调用都和 Hermes 不同，
  但「margin 0.15 才写边」「骨架 5 条」「RRF k=60」这些不该跟着重写。
- **参数要能说清**：之前所有阈值是模块常量，改一个要改代码。现在 default 仍在 core，
  adapter 只决定从哪里读——于是 `--print-settings` 能一眼看到这次到底用了什么。

## 不再做的事

- 不在 core 里 `import agent.*`（`tests/test_settings.py::test_core_never_imports_a_host`）。
- 不在 adapter 里写 SQL 或召回逻辑。
- 不复制默认值到 adapter：`test_defaults_are_the_pre_split_values` 钉住 core 的那一份。
