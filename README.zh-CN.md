# eventmem

[![tests](https://github.com/wackyju2-beep/hermes-eventmem/actions/workflows/tests.yml/badge.svg)](https://github.com/wackyju2-beep/hermes-eventmem/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[English](README.md) · **中文**

给 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 用的记忆层：存「事件」而不是聊天切块。
一个 SQLite 文件，不需要 API key，事件之间的关系由一个跑在 CPU 上的 4B 小模型判。

这套东西是我给自己的 agent 写的，跑在一台小主机上。起因很具体：切块记忆每次递过来一整段话，
里面总有一半句子已经被更新的东西替代了，而库里没有任何字段告诉我哪一半。eventmem 把这件事写成数据：
哪条事件、在哪天、以多大概率，改变了哪一条。

![记忆图谱](docs/img/graph-default.png)

## 单位是事件

一条事件 = 时间戳 + 类型 + 一句话。

```
[11-05] 决定  小主机用 256G SSD 做系统盘
[11-06] 变更  小主机换 2T NVMe 做系统盘        -> 改变上面那条
```

类型只有五种：`决定 / 尝试 / 结果 / 变更 / 教训`。事件只追加，改历史的方式是再写一条修正。
项目由人命名。命令、配置、日志、代码放进 artifact，按内容哈希存一次，事件里只留引用，
所以事件正文永远是一行。

召回命中旧事件时，会顺着边找到新的那条，并把旧的渲染成 `[已失效·被11-06取代]`。
模型不需要自己判断 256G 这个数是不是过时了，它被告知了。

## 和别的记忆层比，不一样在哪

大多数 agent 记忆层做三件事：切块、向量化、取 top-k。eventmem 把这三件事留着做召回，
在它上面加了一层大多数方案跳过的内容：两条事件之间一条有方向的、带类型的**修正关系**。

这层之所以能塞进一台个人机器，靠的是四个选择。

判关系的是一个跑在 CPU 上的小模型。不调 API，不碰显卡。实测是 llama.cpp 加载一个 4B 的
decision 模型，一对事件一次前向，六线程约 6.7 秒，两线程 16–18 秒。这一次前向里问两个独立的问题：
后一条对前一条做了什么（重复 / 改变 / 否定 / 都不是），以及它是否解释了原因（因为 / 细化 / 都不是）。

候选集是常数级的。新事件只和「活跃链头」比，也就是还没被改变、否定、重复过的那些。
候选来自四路：向量 top-10、同会话最近的链头、共享同一个 artifact 或同一个文件路径的、
以及两端都是变更/决定时的跨项目对。第二问另配几组上下文。42 条事件在这个规则下产生 113 对；
之前「同项目七天全组合」的规则在同样语料上是 861 对，而且库越大差得越多。

模型判过的每一对都会留下记录，哪怕最后没连边。原始概率分布写进 `edges.probs`，
判成「无关」的对写进 `pair_evals`。以后想调阈值，是一条 SQL 的事，不用再跑一晚上推理。
图谱里那些判过但没连上的对会画成灰色虚线，能看见模型考虑过什么、否掉了什么。

上面这些都能当场翻开。`mem_neighbors 62` 会告诉你 #62 取代了谁、谁改了它、同会话前后还有哪些。
桌面版把整库画成 3D 图：方框是事件，曲线是修正关系，检查器里能看每条线背后的概率分布。
哪条关系判错了，可以直接改，改过的会描一圈彩虹边。

这些都不重。整库一个 SQLite 文件。向量臂可选，缺 onnxruntime 就静默降级到纯 FTS5。
判关系的模型也可选，没有它就只写事件、照常召回，只是没有边。后台只有一个进程，
而且只有出现新事件时才醒。

## 安装

把文件放到 Hermes 能看到的位置：

```bash
git clone https://github.com/wackyju2-beep/hermes-eventmem ~/hermes-eventmem
cd ~/hermes-eventmem && ./install.sh          # 软链到 ~/.hermes/{plugins,desktop-plugins}
```

或者手抄三份目录：

```
plugins/eventmem               -> ~/.hermes/plugins/eventmem
plugins/eventmem-graph         -> ~/.hermes/plugins/eventmem-graph            （图谱后端，可选）
desktop-plugins/eventmem-graph -> ~/.hermes/desktop-plugins/eventmem-graph    （图谱前端，可选）
```

然后启用：

```bash
hermes config set memory.provider eventmem    # 外部 provider 同时只能有一个
hermes plugins enable eventmem-graph          # 要图谱才需要
```

重启会话后生效。数据库建在 `~/.hermes/eventmem/events.db`，第一次写入时自动建表（schema v7）。

## 关系判定（可选）

边由本地决策模型判。`plugins/eventmem/worker.py` 是常驻进程，每 60 秒看一次有没有新事件；
`plugins/eventmem/bin/nightly.sh --since-hours 24` 是只跑一遍就退出的版本。

worker 通过 HTTP 找任何能做「四选一 + 给概率」的 OpenAI 兼容 `/v1/chat/completions`
（`EVENTMEM_JEV_URL`，缺省 `http://127.0.0.1:18090`）。服务没起来时它会去跑
`EVENTMEM_START_SCRIPT` 指向的脚本，没配就只打一条 warning。任何本地服务都行；
我用的是 llama.cpp 加载 StartLux 4B Instant，Q8 量化，纯 CPU。

模型权重不在这个仓库里。我实测用的那个是 CC BY-NC 4.0。

## 向量臂（可选）

```bash
pip install onnxruntime tokenizers numpy
```

再把 Qwen3-Embedding-0.6B 的 ONNX 放到 `~/localmem/models/`（512 维 int8，
文件名 `model_int8.onnx` / `model_quantized.onnx` / `model.onnx` 任一，也可以自己传 `model_dir`）。
缺任何一个都只是降级到纯 FTS，不报错。

## 用起来

模型自己调工具，人一般不用管。手动查：

```
mem_recall "系统盘 迁移"      # 语义 + 字面混合检索
mem_neighbors 88              # 88 取代了谁 / 谁改了它 / 同会话邻居
expand 88                     # 取某条事件的 artifact 原文
proj_list                     # 看项目
evt_add [{"kind":"变更","text":"…","project":"小主机"}]   # 也可以自己写
```

类型和关系名在库里、在工具参数里都是中文：`决定/尝试/结果/变更/教训`，
`改变/否定/重复/因为/细化`。本文里的英文只是给人读的标签，决策模型的提示词也是中文。

模型每回合还会收到一行
`[eventmem] 可用项目（按本回合相关度）：小主机 / eventmem / 有机实验`。
候选按每个项目事件向量的质心打分，所以它在选项目、或者决定新建项目之前，
先知道有哪些项目已经存在。

图谱在桌面版：状态栏右侧的「◈ 记忆图谱」，或路由 `/eventmem-graph`。

| 操作 | 效果 |
|---|---|
| 左键空白拖动 / 滚轮 | 转视角 / 缩放 |
| 中键拖动 | 平移 |
| 点方框 | 相机飞过去 + 把方框拉到眼前，右侧改文本和类型 |
| 点边 | 两端拉到画面左右，中间是线；右侧看两问概率、改关系、删边 |
| 左键拖方框 | 随手摆到任意位置（位置存库，刷新还在） |
| 点空白 | 只退出焦点，不回弹 |
| 筛选 | 不满足的原子变灰后退；可只看命中，或带上命中原子的一跳邻居 |

![点开一个原子](docs/img/graph-atom.png)
![点开一条边](docs/img/graph-edge.png)

## 一个回合是怎么写进去的

两条路，都是模型自己的活。

工具中间件会在每个「改变了状态」的工具结果末尾追加一行提醒，让模型在同一个回合里把事件写掉，
而不是等到对话末尾，那时候「为什么」已经凉了。如果整回合一条都没写，`sync_turn` 会在回合结束后
再调一次 auxiliary 模型，从这轮问答里抽 0–3 条。纯问答也这么记，寒暄在调模型之前就被挡住了。

召回是反过来的。骨架段每会话每项目给一次，是项目的形状：最早的决定、最新的决定、最新的结果、
最新的变更、最新的教训。命中段做向量 + FTS5 的 RRF，再沿修正边展开一跳，加上同会话邻居。
本会话已经给过的条目只留一行 `#63 #60 同上文`。

## 目录

| 路径 | 职责 |
|---|---|
| `plugins/eventmem/schema.py` | DDL、迁移、两张 FTS5 trigram 虚表 + 同步触发器 |
| `plugins/eventmem/store.py` | 所有 SQL：事件、项目、边、artifact、游标、遗忘 |
| `plugins/eventmem/recall.py` | 向量 + FTS -> RRF -> 两段式装填 -> 按项目分组渲染 |
| `plugins/eventmem/embed.py` | Qwen3-Embedding-0.6B ONNX 编码器（512 维，int8）+ 降级自检 |
| `plugins/eventmem/jev.py` | 本地决策模型客户端（两问、要概率） |
| `plugins/eventmem/nightly.py` | 挑候选、问模型、按 margin 连边、推进游标 |
| `plugins/eventmem/worker.py` | 常驻进程：有变化就算，没有就睡 |
| `plugins/eventmem/i18n.py` | 语言解析（`display.language`）+ 中英切换 |
| `plugins/eventmem/__init__.py` | `MemoryProvider` + 工具中间件 + 七个工具 + 回合末抽取 |
| `plugins/eventmem-graph/` | 图谱后端（FastAPI），读写同一张库 |
| `desktop-plugins/eventmem-graph/` | 图谱前端：一个 `plugin.js`，宿主注入 React |
| `docs/architecture.md` | 数据模型、写入路径、召回算法、连边细节 |

## 测试

纯脚本，不需要 pytest：

```bash
cd plugins/eventmem
for t in tests/test_*.py; do python3 "$t"; done
```

```
test_store.py    PASS 31
test_recall.py   PASS 33
test_plugin.py   PASS 43
test_nightly.py  PASS 93
test_worker.py   PASS 3
test_i18n.py     PASS 9
```

212 项检查，每个用例一个自己的临时库，不碰 `~/.hermes`。召回测试用 `FakeEncoder`，
没有 numpy / onnx 也能全绿。`test_plugin.py` 需要宿主的 `agent.memory_provider`，
没装 Hermes 的机器（CI 就是）会换成 `tests/_host_stub.py` 里的最小桩，
所以这套测试在裸解释器上也是 212 全过。

## 已知限制

- 不迁移旧数据。别的记忆库里的历史块不会变成事件，只能从启用之后开始积累。
- 项目由模型选。工具会拒绝不存在的项目名，但选错一个真实存在的项目它发现不了，
  只能人眼在图谱里改。
- 关系的质量就是模型的质量。我用的 4B Instant 在成对判别上比它的思考版弱不少
  （DecisionPivot 41.5 对 59.9），所以阈值和提示词措辞比换模型更值得花时间。
- 阈值（向量 0.35、margin 0.15、预算 600/850）是在小语料上定的，攒到几千条事件后应该重标。
- 图谱界面和库里存的类型/关系名是中文；模型读到的文本是中英双语。

## 许可

MIT，见 `LICENSE`。随便用、随便改、可以商用，保留版权声明即可。

第三方权重不随仓库分发。嵌入模型和决策模型的许可各自独立，
我实测用的 decision 模型权重是 CC BY-NC 4.0。
