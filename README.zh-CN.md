# eventmem

[![tests](https://github.com/wackyju2-beep/eventmem/actions/workflows/tests.yml/badge.svg)](https://github.com/wackyju2-beep/eventmem/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

[English] · **中文**(README.md "README.md")  

***记住「变化」本身.***  
一套可由多个 AI Agent 共享的持久记忆，不仅记住项目历史，也记录事实如何变化。通过现有的 [DeepSeek Harness（DSH）和 Hermes 适配器，一个 Agent 可以记录决定和结果，另一个 Agent 可以在需要时召回，不必共享各自的会话上下文。前提是两个适配器都配置到同一份 eventmem 数据库。](adapters/dsh/README.md "adapters/dsh/README.md")  
- **跨 Agent 交接**：DSH 记录的工作，Hermes 可以检索；反过来也一样。  
- **追踪事实变化**：保留旧事件，并记录新事件对旧事实的改变或否定。  
- **可检查、可纠错**：在图谱里查看事件和关系，检查判定并手动修正。  
**为什么用 eventmem？**  
**事实变了。** 不必只留下两段相互竞争的文本，让 Agent 自己判断哪个更新：eventmem 可以把新旧事件连起来。  
```
[11-05] 决定  小主机用 256 GB SSD 做系统盘
 [11-06] 变更  小主机换成 2 TB NVMe 系统盘 → 改变上面的决定

```
当召回命中旧事件、且关系已经存在时，系统可以标记旧事实已被取代，并带出新事件。关系也可能漏判或误判，见[局限与取舍。](#anchor-1 "#anchor-1")  
**工作换了 Agent。** 比如 DSH 将一次部署结果记入共享项目，稍后 Hermes 会话查询这个项目时就可以召回它。共享的是*记忆*，不是对话上下文；能否召回取决于实际写入的内容与查询，不保证每次都找到所有相关事件。  
上面是功能示意，并非已录制的端到端演示。下图是**历史**图谱截图，不代表当前的连边密度。  
![](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAABmJLR0QA/wD/AP+gvaeTAAAACXBIWXMAAA7EAAAOxAGVKw4bAAAAxElEQVR4nO1W0Q6AIAiE/v+bpaeaYyYc6NLVvbRm484DL1lEKAIRIWaG1zQ4IgAhsHCMJkc31BXQKmbtvF73iOkKQIvV8LbpFqAJ9Dvac2Z2iQ4N4UiYQ2gJ9G7g6TsupQw7UpHjefTCBCEmwueEaPAMhBzQBVDC+tkit2pOOQWXEx5H0gIy/ScK/Atq0os4k5ihHOilHJyYyyfhTIjIJg48idQ5EME6DrwlZB0HthaQcRG+FbeQuaZ/ewb2ScJfQBSe9p77EZ7a26s/yQAAAABJRU5ErkJggg==)  
**快速开始**  
**需要安装什么？**  

|  |  |  
| - | - |  
| **能力** |  **依赖** |  
| 事件存储和字面检索 |  Python 3.11+、带 FTS5 及其 trigram tokenizer 的 SQLite 和适配器；不需要关系判定模型 |  
| 语义检索 |  可选的向量依赖和 embedding 模型 |  
| 自动判定关系 |  运行 daemon 并配置决策后端；可以使用本地 CPU 后端 |  
| 共享记忆和图谱 |  各适配器指向同一数据库，加上 daemon/控制台；跨机器通过 SSH 访问 HTTP API |  


适配器会调用宿主的辅助模型进行回合末抽取，宿主模型可以是本地或远端。远端决策后端也可能需要凭据。基础存储和字面检索不需要 GPU 或判边模型。  
**Windows + DSH**  
```
git clone https://github.com/wackyju2-beep/eventmem.git D:\eventmem
cd D:\eventmem
.\install.cmd

```
安装器会创建虚拟环境，安装 core 和可选的向量运行时，启动本地 daemon 并设置登录自启。如果缺少 Python，它可能通过 winget 安装 Python 3.12。给 `install.cmd` 加上 `-NoPythonInstall`、`-NoVector` 或 `-NoAutostart`，可分别跳过这些操作。数据默认保存在 `%USERPROFILE%\.hermes\eventmem`（设置了 `%HERMES_HOME%` 时则在其下）。  
在 DSH 中打开**插件 → 添加插件**，选择 `D:\eventmem\adapters\dsh`。适配器可以连接 `http://127.0.0.1:9123` 上的本地 daemon；远程 SSH 和配置见 [DSH 指南。需要自动判边时，在记忆后台配置后端或下载模型；需要语义检索时，还要把 Qwen3-Embedding-0.6B ONNX 的 `tokenizer.json` 和 `model_int8.onnx` 放进 `<HERMES_HOME>\eventmem\models\`（默认 `%USERPROFILE%\.hermes\eventmem\models\`），仅安装向量运行时不够。](adapters/dsh/README.md "adapters/dsh/README.md")  
在仓库目录下可随时自检：  
```
~\.hermes\eventmem\venv\Scripts\python.exe tools\selfcheck.py

```
如果设置了 `HERMES_HOME`，请相应调整路径。  
**Linux + Hermes**  
安装 Hermes 后，克隆仓库并链接适配器：  
```
git clone https://github.com/wackyju2-beep/eventmem.git ~/eventmem
cd ~/eventmem && bash ./install.sh

```
在 Hermes 的 `~/.hermes/config.yaml` 中选择 provider：  
```
memory:
  provider: 'eventmem'
plugins:
  entries:
    eventmem:
      settings: {}

```
重启提供会话的 Hermes 进程，并开始新会话。`install.sh` 只建立软链接，不会启动 daemon。若使用默认数据库，可以用安装 Hermes 的 Python 环境在前台启动：  
```
~/.hermes/hermes-agent/.venv/bin/python ~/eventmem/adapters/hermes/run.py daemon --db "$HOME/.hermes/eventmem/events.db"

```
请按实际安装位置和 `HERMES_HOME` 调整 Python 与数据库路径；在另一个终端运行 `curl http://127.0.0.1:9123/api/health` 检查服务。需要 Hermes 图谱/控制台时，在 `plugins.enabled` 中启用 `eventmem-graph` 和 `eventmem-gui` **两个**插件；`install.sh` 会打印更多步骤。需要退出登录后继续运行时，请使用服务管理器启动 daemon。可选的判定模型和 embedding 配置见[后端和](docs/backends.md "docs/backends.md")[设置。](docs/settings.md "docs/settings.md")  
要与另一台机器上的 DSH 共享，请在 Hermes 所在机器启动使用 **Hermes 同一数据库**的 daemon，并按 DSH 指南通过 profile patch 将 `tunnelHost` 设为指向该机器、可免交互登录的 SSH 别名。如果 Windows 安装器已经在 9123 启动本地 daemon，DSH 默认会直接连接它，而不会建立隧道。此时可先仔细检查匹配的进程和自启项，再在仓库目录运行 `.\install.cmd -Uninstall`。该命令会强制终止命令行同时包含 `run.py`、`daemon` 和配置数据库路径的 Python 进程，可能影响其他同库服务；还会删除固定名 `eventmem-daemon` 的计划任务和启动快捷方式，但保留数据库。也可以选择未占用的本地端口，同时设置 `baseUrl`（如 `http://127.0.0.1:9124`）和 `tunnelLocalPort`（`9124`），`tunnelRemotePort` 保持为远端 daemon 的端口。隧道设置详见 [DSH 指南。不要把 SQLite/WAL 放到网络共享目录。](adapters/dsh/README.md "adapters/dsh/README.md")
\*\*试一次交接：\*\*让 DSH 把一条容易辨认的结果记入某个项目，再让新的 Hermes 会话召回它，并用 `expand` 查看返回事件的来源和项目；反过来也可以试。这里是建议的验证步骤，不代表已经在你的环境中录制或实测；召回效果取决于实际写入和查询。  
**工作流程**  
1. **记录**：Agent 写入事件，或由适配器在回合末调用宿主辅助模型抽取事件。命令、日志等较长内容可以存为 artifact。  
2. **建立关系**：可选的后台 worker 选择候选事件对，交给配置好的决策后端判定，再保存被接受的关系。  
3. **召回**：字面检索以及配置后的语义检索找出相关事件，结果可以包含已保存的修正关系和邻近上下文。  
事件分为**决定、尝试、结果、变更、教训**；关系分为**改变、否定、重复、因为、细化**。项目用于组织记忆；Agent 可通过 `mem_recall`、`mem_neighbors` 和 `expand` 搜索事件、查看变化历史和读取 artifact。图谱也允许人工修正事件及关系。  
**架构**  
```
 Hermes adapter ──────┐
                       ├── eventmem Core ── 共享 SQLite 记忆
  DSH adapter ─ daemon ┘    存储 · 召回       事件 · 项目 · 关系
                            关系判定

```
Hermes 适配器在本机调用 core；DSH 适配器通过 HTTP 数据面连接 daemon。适配器负责对接宿主和调用宿主模型进行抽取，core 本身不是通用的事件抽取模型。新写入的数据带有 Agent/会话标记。其他宿主需要单独开发适配器或 API 集成，并非自动支持。  
**每个数据库只运行一个 daemon。** API 和管理台没有内置鉴权，默认只监听本机回环地址；不要暴露到不可信网络。daemon 也允许更改监听地址，请保持其私有。详见[架构与](docs/architecture.md "docs/architecture.md")[数据面 API。](docs/api.md "docs/api.md")  
**文档**  
- [架构与数据流 ·](docs/architecture.md "docs/architecture.md") [关系与连边](docs/relations.md "docs/relations.md")  
- [决策后端与模型安装 ·](docs/backends.md "docs/backends.md") [设置 ·](docs/settings.md "docs/settings.md") [API](docs/api.md "docs/api.md")  
- [DSH 适配器、控制台和 SSH ·](adapters/dsh/README.md "adapters/dsh/README.md") [后端配置示例](examples/backends/ "examples/backends/")  
**局限与取舍**  
- eventmem 主要面向动态事实、项目历史和明确的事件关系；如果只是检索大量静态文档，普通 RAG 可能更简单。  
- 自动关系判定并不保证正确。策略倾向于漏掉不确定的边，也不把缺乏证据的关系永久写入；**连边更少并不等于准确率更高**，仍需要人工标注评估误连和漏连。没有边的事件仍可通过普通检索找到，但检索未必能识别它们的关系。  
- 其他记忆系统里的旧文本不会自动迁移；Agent 也可能选错一个已存在的项目。重要内容应检查并修正。  
- DSH 的 `evt_add` 会校验传入的 links，但目前不会写入这些边；后台连边单独进行，也不保证重建相同关系。详见[适配器指南。](adapters/dsh/README.md "adapters/dsh/README.md")  
- 这是个人项目，不提供托管服务；schema 和接口可能变化。图谱标签及存储的事件/关系名为中文。  
**为什么做它**  
这套东西最初是给我自己的 Agent 写的，机器是一台没有显卡的小主机。切块记忆经常递来一整段话，其中一些事实早已被更新的内容取代，库里却没有告诉我哪一些。模型只能猜，猜错时就把旧参数当成当前值。我想把「变化」本身写成数据：一条可以追溯、也可以纠错的关系，而不是每次召回时重新猜。  
**开发与许可**  
CI 使用 Python 3.11 和 3.12 运行 `tests/test_*.py` 下的脚本；依赖及当前命令见 [CI 配置。测试数量和模型速度随版本及环境变化。](.github/workflows/tests.yml ".github/workflows/tests.yml")  
MIT，见 [LICENSE。第三方模型权重不随仓库分发，其许可另行规定。](LICENSE "LICENSE")  
