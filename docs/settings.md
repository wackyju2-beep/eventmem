# 参数

所有阈值都在 core 里定义一次（`core/eventmem_core/settings.py` 的 dataclass 默认值），
adapter 只负责**从哪读**。Hermes adapter 读 `~/.hermes/config.yaml` 的这一段：

```yaml
plugins:
  entries:
    eventmem:
      settings:
        edges:
          margin: 0.15
        recall:
          budget_tokens: 600
        decision:
          url: http://127.0.0.1:18090
```

写法：**嵌套（上面这种）和点号（`edges.margin: 0.15`）都认**。旧版的
`plugins.entries.eventmem.config` 也还能读。

三条契约（`tests/test_settings.py` 钉住）：

1. **不写 = 拆分前的老行为**，一个数都不差。
2. **未知键**：告警 + 忽略，不影响其它键。
3. **坏值 / 越界**（`margin: 1.7`、`heads_vec_top_k: 0`、`neighbour_rels: [nope]`）：
   告警 + 该项回落默认，其它项照常生效。

改完要**重启 worker**（配置在进程启动时读一次）；会话侧在下一轮 `initialize()` 生效。

## 顶层

| 键 | 默认 | 说明 |
|---|---|---|
| `db_path` | `<hermes_home>/eventmem/events.db` | adapter 填的；core 只负责打开 |
| `language` | `""`（自动） | `zh` / `en`；空 = 宿主语言 → config → en |
| `max_events_per_call` | `3` | 一次 `evt_add` 的上限，工具 schema 同步显示 |
| `models_root` | `<hermes_home>/eventmem/models` | ONNX 编码器目录（向量臂）；没有该目录时回退到旧的 `localmem/models` |

## `edges` — 夜间连边

| 键 | 默认 | 说明 |
|---|---|---|
| **`margin`** | **0.15** | **top1 概率 − top2 概率 ≥ 这个值才写边**；「都不是」也参与排名。调大 = 更少更硬的边 |
| `heads_cos_min` | `0.35` | Q1 候选：活跃链头的最低余弦 |
| `heads_vec_top_k` | `10` | Q1 候选：最多问几个链头 |
| `session_heads_top_k` | `10` | 桥接候选：同会话最近几个链头 |
| `cross_kinds` | `["变更","决定"]` | tier3 跨项目连边的两端类型 |
| `cross_cos_min` | `0.7` | tier3 的余弦门槛 |
| `cross_top_k` | `3` | tier3 最多几条 |
| `session_recent` | `3` | Q2 候选：同会话最近几条 |
| `unclosed_lessons` | `2` | Q2 候选：最多几条未闭环教训 |
| `decision_skeleton` | `5` | Q2 候选：最新几条未被否定的决定 |
| `max_fail_streak` | `3` | 连续几次决策调用失败就中止本轮（服务挂了别空转到天亮） |
| `poll_seconds` | `60` | worker 轮询间隔 |
| `first_run_hours` | `24` | 库里没有游标时的首次回看窗口 |

## `decision` — 决策模型

| 键 | 默认 | 说明 |
|---|---|---|
| `url` | `http://127.0.0.1:18090` | 决策服务根地址；**空串 = 关闭连边** |
| `path` | `/v1/systemone` | 决策端点（System One / Jev 协议；OpenAI 版是 `/v1/decisions`） |
| `model` | `jev` | 请求体里的模型名（本地服务多忽略；云端要写 `jev-latest` 之类） |
| `timeout` | `900` | 单请求超时（一次完整判断，不是单 token） |
| `think_mode` | `""` | AutoThink 专用；空 = 什么都不发（Instant 模型会拒） |
| `start_script` | `""` | worker 拉起服务用的脚本；空/不存在 = 只告警 |
| `backends` | `()` | **后端 pack 名，按顺序回退**（`a,b` 或列表）。空 = 只用上面的 `url` |
| `packs_dir` | `""` | pack 目录；adapter 默认填 `<hermes_home>/eventmem/backends` |
| `probe_timeout` | `2.0` | 探活超时（一次 GET，不花 token） |

`backends` 的效率契约：**一轮只探活一次**，第一个能应答的 pack 负责整轮；中途失败就中止，
不半路换模型——两个模型的概率分布不同，同一晚的边混起来以后没法重算阈值。
401/403 记 `needs_key`（还没拿到 key，不算故障）；每条边把来源写进 `edges.backend`。

## `recall` — 召回与渲染

| 键 | 默认 | 说明 |
|---|---|---|
| `budget_tokens` | `600` | 命中段的注入预算（CJK 1 字 ≈ 1 token） |
| `skeleton_budget` | `850` | 骨架段独立预算（每会话每项目一次） |
| `skeleton_max` | `5` | 骨架最多几条（最早决定/最新决定/结果/变更/教训） |
| `top_k` | `20` | 融合前每一路取多少 |
| `max_items` | `12` | 一块最多渲染几条正文 |
| `min_score` | `0.35` | 向量命中的余弦下限 |
| `rrf_k` | `60` | RRF 的 k |
| `chain_max_depth` | `1` | 命中后沿 改变/否定 链展开几跳 |
| `repeat_max` | `8` | 同会话重复条目最多合成几个 `#id` 指针 |
| `neighbour_rels` | `["改变","否定","因为"]` | `mem_neighbors` 默认跟哪几种关系 |
| `neighbour_cap` | `30` | 一次导航最多渲染几条 |
| `neighbour_depth_max` | `3` | `depth` 的上限 |
| `project_hint_limit` | `5` | 每回合最多给几个项目候选 |
| `head_resolve` | `true` | 命中被取代的旧事件时，是否沿 `改变` 追到同一条取值的最新值 |
| `head_max_hops` | `3` | 追「当前值」最多几跳；还有后继就走不完 → 不宣称当前 |
| `head_min_cos` | `0.72` | 每跳要求 cos ≥ 这个值（同一个东西换了值）；不够就停 |

## 看当前生效值

```bash
# 宿主侧（有一份 config 就够）
~/.hermes/hermes-agent/.venv/bin/python ~/eventmem/adapters/hermes/run.py worker --print-settings

# 指定另一个 home / profile
... run.py worker --print-settings --hermes-home ~/.hermes/profiles/work
```

输出是一行 JSON，包含两个 policy 的全部字段——排「为什么这次没写边」时先看它。

## 三层配置文件（GUI 写的是中间那层）

| 文件 | 归谁 | 谁写 |
|---|---|---|
| 代码里的默认值（`core/eventmem_core/settings.py`） | eventmem | — |
| `~/.hermes/eventmem/settings.yaml` | eventmem | **GUI**、手写、`curl POST /api/config` |
| `~/.hermes/config.yaml` 的 `plugins.entries.eventmem.settings` | Hermes | 宿主配置 |

后面的层覆盖前面的层；**第三层（宿主）最后说话**，所以在 GUI 里改了没反应，先看
`plugins.entries.eventmem.settings` 是不是也写了同一个键。

`settings.yaml` 的规矩：

- 写之前严格校验，坏值回 400，**磁盘不动**；
- 传 `null` 删掉某项覆盖（回到默认）；
- daemon 写完后立刻重读，`poll_seconds` 也是每轮重读，不用重启。

```bash
# 不用开浏览器也能改（GUI 调的就是这条）
curl -s -X POST -H 'Content-Type: application/json' \
     -d '{"edges":{"heads_vec_top_k":8}}' http://127.0.0.1:9123/api/config
```
