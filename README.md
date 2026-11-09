# eventmem

**事件型本地记忆：append-only 事件 + 人工命名的项目 + 夜间关系边。**
SQLite / FTS5 / 可选 ONNX 向量，无网络、无守护进程（连边 worker 是可选的独立进程）。

> 本仓库是 `hermes-eventmem` 的延续：v0.1.0 之后重构为 core + adapter 结构，
> 插件目录从 `plugins/eventmem/` 移到 `core/` 与 `adapters/hermes/`。
>
> eventmem is an event-shaped memory store for local AI agents. The **core** is pure
> Python and knows nothing about any agent framework; each **adapter** plugs it into one
> harness (Hermes today, DeepSeek Harness next). See `docs/architecture.md`.

## 这个仓库是什么

```
core/eventmem_core/           记忆逻辑：schema / store / recall / embed / edges / worker
adapters/hermes/              Hermes adapter：provider 接线、工具面、中间件、回合末抽取
tests/                        8 个纯脚本测试套件（237 项，无 pytest）
docs/                         architecture.md（边界）、settings.md（全部参数）
```

- **Core 不知道宿主存在。** 它没有 `import agent.*`（有测试盯着），也不知道
  `~/.hermes`、`evt_add`、中间件或任何工具名。
- **Adapter 只做翻译。** 把宿主的工具调用翻成一个 `EventMem` 方法调用、把宿主的
  配置翻成 `CoreSettings`、把宿主的模型翻成一次抽取请求。它不写 SQL、不做召回融合。
- **参数只在 core 定义一次。** 默认值就是拆分前的老值；adapter 负责从哪里读、怎么校验。

## 装到 Hermes

```bash
./install.sh                       # 把 ~/.hermes/plugins/eventmem 软链到本仓库
```

然后 `config.yaml`：

```yaml
memory:
  provider: 'eventmem'
plugins:
  entries:
    eventmem:
      settings: {}                 # 可选；不写就是全部默认值
```

重启宿主里跑会话的进程（dashboard / gateway / 桌面后端）才换新代码。连边 worker：

```bash
systemctl --user edit eventmem-worker
# ExecStart=%h/.hermes/hermes-agent/.venv/bin/python \
#   %h/eventmem/adapters/hermes/run.py worker --db %h/.hermes/eventmem/events.db
systemctl --user restart eventmem-worker
```

夜间补算（cron 可调）：

```bash
~/eventmem/adapters/hermes/bin/nightly.sh --since-hours 24
```

## 调参

所有阈值都在 `plugins.entries.eventmem.settings` 下，默认值见 `docs/settings.md`。
最常动的一个——写边要求的 top1 与 top2 概率差：

```yaml
plugins:
  entries:
    eventmem:
      settings:
        edges:
          margin: 0.15        # 默认；调大 = 边更少更硬
```

改完重启 worker（配置只在启动时读）。坏值不会让记忆挂掉：告警 + 回落默认值。

## 测试

```bash
for t in store recall nightly worker i18n plugin settings hermes_settings; do
  python tests/test_$t.py | tail -1
done
```

纯脚本，没有 pytest、没有 fixture，也不碰 `~/.hermes` 的真库。

## 许可

MIT，见 `LICENSE`。
