# 架构：core / adapter

**Core 负责共享的记忆逻辑；adapter 负责对接各宿主的会话、工具和模型。** Hermes 与 DSH 已有适配器，其他宿主需要自行实现集成。

```text
Hermes ── Hermes adapter ──────────────────┐
                                           ├── eventmem Core ── SQLite events.db
DSH ── DSH adapter ── HTTP ── daemon ──────┘
```

Hermes 适配器在数据库所在机器调用 core；DSH 适配器是 daemon 数据面的 HTTP 客户端。两者要共享记忆，必须指向同一个数据库：同机可连接本地 daemon，跨机可用 SSH 将远端 daemon 的回环端口转到本机。各 Agent 共享事件和项目历史，不共享会话上下文本身；事件带有 Agent 标记，会话 ID 使用宿主前缀。旧数据可能没有 Agent 标记，不做猜测。

## 边界

| core | adapter |
|---|---|
| SQLite schema、事件与项目存储 | 适配宿主的插件、工具、配置与生命周期 |
| FTS / 可选向量检索与召回渲染 | 将召回结果注入宿主会话 |
| 候选选择、关系判定策略与后台 worker | 调用宿主辅助模型抽取事件 |
| 决策后端和 daemon 数据面 | 对宿主侧输入做校验及转换 |

Core 不负责调用通用模型从对话里抽事件；由各 adapter 接入各自的宿主模型。Hermes 通过 `EventMem` 接口调用 core；DSH 不直接访问 SQLite，而是通过 [数据面 API](api.md) 读写。DSH 的 `evt_add` 接受并校验 `links`，但目前不会通过数据面写入它们，后台判边也不保证复现这些链接；详见 [DSH adapter](../adapters/dsh/README.md)。

## 数据流

1. **写入**：Agent 主动调用工具，或 adapter 在回合末调用宿主辅助模型抽取事件；事件和可选 artifact 写入数据库。
2. **连边**：worker 选择候选事件对，调用配置的决策后端并按策略保存结果。参见[关系与连边](relations.md)。
3. **召回**：FTS5 和可选的向量检索寻找事件，按召回策略加入相关关系和上下文；adapter 把结果提供给宿主。
4. **人工检查**：控制台图谱可以查看和修正事件及关系。事件通常以新增记录表达变化，但手工编辑和删除可修改已有数据，因此不应把数据库视为不可变审计日志。

## 部署边界

- 一个数据库只运行一个 daemon。不要依赖自动锁来避免重复实例：Windows 上缺少 `fcntl` 时 daemon 的单实例锁会禁用。
- SQLite/WAL 留在本机磁盘，不放网络共享目录。跨机器通过 daemon 的 HTTP API 和 SSH 隧道共享数据，不直接共享数据库文件。
- daemon 的 API 与管理台默认只监听 `127.0.0.1`，并可要求 bearer token（`--token` / `--token-file`）；非回环绑定且没有 token 时拒绝启动。带外站 `Origin` 的 POST 会被拒绝（CSRF 防护；服务端到服务端的代理不带 Origin，不受影响）。没有 token 时，能访问该端口的本地进程仍是信任边界。不要暴露到不可信网络；跨机访问使用 SSH。更多安全注意事项见 [API](api.md)。
- 向量模型和关系判定后端都是可选项：缺少 encoder 时只使用 FTS5；没有可用的判定后端时事件存储和召回仍可使用，但不会自动产生关系。

## Core API 示例

```python
from eventmem_core import EventMem, CoreSettings, apply

settings = apply(CoreSettings(db_path="/path/events.db"), {
    "edges": {"margin": 0.2},
    "recall": {"budget_tokens": 800},
})
mem = EventMem(settings, agent="hermes")
mem.add_project("小主机")
mem.add_event("结果", "……", project="小主机", session="hermes:s1")
mem.recall("上次那个盘怎么选的？", state=mem.new_recall_state())
mem.close()
```

示例中的数据库父目录须已存在；项目须先创建（或使用已有项目）。配置细节见 [settings](settings.md)，决策后端见 [backends](backends.md)。
