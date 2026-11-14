# eventmem：DeepSeek Harness 事件记忆

本插件**仅是客户端**：通过 HTTP 调用独立的 eventmem daemon，不安装或启动 daemon、数据库、模型或 worker，也不直接打开数据库。daemon 可与 DSH 同机运行，也可通过 SSH 连接到远端；不同宿主共享记忆时必须连接同一个数据库。

## Windows 同机安装

1. 安装 DSH、Git 和 Python 3.11 或更高版本；SQLite 需要 FTS5 及其 trigram 分词器。在 PowerShell 中克隆仓库并安装 daemon：

   ```powershell
   git clone https://github.com/wackyju2-beep/eventmem.git D:\eventmem
   cd D:\eventmem
   .\install.cmd
   ```

   安装器会创建虚拟环境、安装 core 和可选的向量运行时、在 `127.0.0.1:9123` 启动 daemon、设置登录自启动并运行自检；如果缺少 Python，可能通过 `winget` 安装 Python 3.12。数据默认位于 `%USERPROFILE%\.hermes\eventmem`，设置了 `HERMES_HOME` 时位于 `%HERMES_HOME%\eventmem`。可用 `-NoPythonInstall`、`-NoVector`、`-NoAutostart` 分别关闭相应步骤，详见[仓库快速开始](https://github.com/wackyju2-beep/eventmem/blob/main/README.md)。

   新生成或更新的启动命令显式绑定回环地址，但已运行的 daemon 可能仍使用旧命令，应检查其实际监听地址。如果安装器提示 `daemon.cmd` 需要更新，可在仓库目录的 PowerShell 中运行 `.\install.cmd -Uninstall` 后再运行 `.\install.cmd` 重新生成启动命令（数据库保留）。**操作前先检查受影响的进程和自启动项**：`-Uninstall` 会强制停止命令行同时包含 `run.py`、`daemon` 和当前数据库路径的所有 `python.exe`，并删除固定名计划任务 `eventmem-daemon` 和启动项 `eventmem-daemon.lnk`；这可能影响使用同一数据库的其他服务或同名自启动项。删除计划任务及终止进程的错误会被静默忽略，不能仅凭输出判断成功；操作后应检查目标 PID、固定名计划任务与启动快捷方式确实消失，重装后再检查实际监听地址。
2. 在 DSH 中选择 **Plugins → Add plugin**，选择克隆目录中的 `D:\eventmem\adapters\dsh`。默认就是同机模式：`mode: local`、`baseUrl: http://127.0.0.1:9123`，不建立 SSH，也不含任何私人主机名。打开 **记忆后台 → 连接** 可查看依赖状态：选择 **本机记忆** 或 **远程记忆**，填写地址（远程再填 SSH 别名），点 **测试连接**，再点 **保存并重新加载**。页面通过 DSH 的 profile 配置编辑器写入，不需要手改 YAML；若编辑器不可用，仍可用下面的 profile patch。
3. 在 PowerShell 中检查 `Invoke-RestMethod http://127.0.0.1:9123/api/health`。适配器不只看 HTTP 200：`service` 必须是 `eventmem-daemon`、`api` 必须是 `1`、`schema` 必须是数字，配置了 `expectDbId` 时还会核对 `db_id`；占用同一端口的其他服务会在任何写入前被拒绝。安装器也会运行自检。随后在 DSH 侧边栏打开 **记忆后台**查看服务。若 daemon 启用了 token，在「连接」页把 `tokenFile` 指向安装器生成的 `daemon.token`。

## 配置 profile

优先用 **记忆后台 → 连接**。必须手改时，先备份**当前使用的 profile** 的 `cordis.patch.yml`：路径为 `<DSH_HOME>/profiles/<profile>/cordis.patch.yml`；Windows 上未设置 `DSH_HOME` 时默认为 `%USERPROFILE%\.dsh`。`desktop` 只是 profile 名称示例，不一定是当前 profile，请参考 DSH profile 配置文档确认。

保留原有行和注释；若文件仅为 `[]`，用下面的序列替换空列表。若已有 `eventmem` 覆盖项，应更新该项，不要添加重复 ID。同机 daemon 的完整配置示例：

```yaml
- id: eventmem
  name: dsh-eventmem
  config:
    mode: local
    baseUrl: http://127.0.0.1:9123
    agent: dsh
    lang: zh
```

覆盖会**整体替换而不是合并** `config`，因此要保留上面字段及所需的其他自定义字段。远程使用时改为 `mode: remote`，`tunnelHost` 填远端可用的免密码 SSH 别名；若本地 9123 被占用，应同时修改 `baseUrl` 的端口和 `tunnelLocalPort`，并保持 `tunnelRemotePort` 与远端 daemon 一致。`tunnelHost` 只在远程模式必填。更高优先级的层还可能覆盖此配置。同 ID 替换及非深度合并已在隔离 profile 中通过 `dump-config` 验证；「连接」页的保存路径由 config-editor 桩测试覆盖，**未以启用的插件验证**；请在自己的宿主中确认实际生效配置与连接。

## 远程共享与安全

Linux/Hermes 的安装和 daemon 启动方法见[仓库快速开始](https://github.com/wackyju2-beep/eventmem/blob/main/README.md)；`install.sh` 链接适配器，但不会启动 daemon。远端 daemon 应使用 Hermes 所用的同一个数据库，每个数据库只运行一个 daemon。远程使用需要 `mode: remote` 和可用的免密码 SSH 别名，优先用「连接」页，或请熟悉 profile patch 的人覆盖。

**注意错连数据库：**探测会校验服务标识、API 版本和（配置了的话）`db_id`，占用端口的其他服务会被拒绝；但同一端点上另一个 *eventmem 数据库* 需要 `expectDbId` 才能识别。「连接」页会显示当前 `db_id`，把它填进 **预期数据库 ID** 即可钉住。仍可检查并确认上文所述卸载风险后，在仓库目录的 PowerShell 中运行 `.\install.cmd -Uninstall`；或者使用空闲本地端口，例如 `baseUrl: http://127.0.0.1:9124`、`tunnelLocalPort: 9124`，远端仍用 9123 时保留 `tunnelRemotePort: 9123`。不要将 SQLite/WAL 放在网络共享上。Windows 缺少 daemon 的 `flock` 单实例保护，不能依赖它阻止第二个 daemon 打开同一数据库。

**安全边界：**daemon 默认只监听回环地址，并可要求 bearer token（`--token` 或 `--token-file`）。设置 token 后，除最小化的 health 探测外所有路由都需要它；非回环绑定且没有 token 时 daemon 会拒绝启动。带有外站 `Origin` 的 POST 会被拒绝（CSRF 防护）；管理台代理这类服务端调用不带 Origin，不受影响。没有 token 的回环地址仍是本地信任边界：任何能访问该端口的本地用户或进程都可读写数据库。共享机器上应启用 token，保持仅回环监听，远程访问走 SSH，不要暴露到不可信网络。未显式指定 `--host` 时，`EVENTMEM_DAEMON_HOST` 可改变默认绑定地址，但 token 约束仍然生效。SSH `-L` 的监听也受客户端 `GatewayPorts` 设置影响，应确认实际转发监听仅限回环。详见[数据面 API](https://github.com/wackyju2-beep/eventmem/blob/main/docs/api.md)。

## 模型前置条件

基本存储和字面召回不需要关系判定器。回合末抽取需要可用的 DSH 宿主模型；如果会话没有选择模型，可以设置 `auxProvider` 和 `auxModel`。语义召回还需要向量运行时，以及放在 `<HERMES_HOME>\eventmem\models`（默认 `%USERPROFILE%\.hermes\eventmem\models`）中的 Qwen3-Embedding-0.6B ONNX `tokenizer.json` 和 `model_int8.onnx`。自动关系需要运行中的 daemon 和已配置的判定后端；可在记忆后台配置后端或下载模型，参见[后端配置](https://github.com/wackyju2-beep/eventmem/blob/main/docs/backends.md)。

## 使用与限制

- 每回合第一步召回相关记忆，将结果块作为额外的用户角色消息注入；项目提示跟在结果块之后。
- 提供七个工具：`evt_add` 写事件（默认每次 0–3 条）、`mem_recall` 召回、`expand` 查看事件及原文、`mem_neighbors` 查看图邻居、`proj_add` 新建项目、`proj_list` 列出项目、`evt_forget` 永久删除事件。项目名须使用已有项目或 `unfiled`，新项目须先显式建立。
- 回合未写入事件时，回合末可通过辅助模型抽取 0–3 条；状态变更工具的结果会附带记忆提醒。会话项目由 daemon 持久保存，后续抽取继承该项目。
- **记忆后台**提供概览、后端、模型、调度、日志和图谱；图谱支持筛选、检查及编辑记忆。插件通过宿主代理访问 daemon，不直接操作数据库。
- `mode: remote`（旧配置的 `tunnel: true` 同义）时，插件按上述条件探测并采用现有端点，否则启动并监督 SSH；卸载时只终止由插件启动的 SSH，不终止它采用的外部隧道。默认 `mode: local`，同机部署不会启动 SSH。记忆服务或辅助调用失败时，自动召回和抽取降级，不阻塞回合；显式工具调用可返回错误。
- **隐私：**回合末抽取会把该回合的一小段（先经 `lib/redact.js` 尽力脱敏）交给 DSH 宿主配置的模型，该模型可能是远端服务，因此片段会离开本机并可能产生费用。脱敏是基于规则的尽力而为，不是秘密检测器，可能漏掉秘密也可能误伤正文；请把抽取模型视为你自己选择的接收方。
- 数据面没有写入关系的端点：`evt_add` 会校验 `links`，有效项报告为跳过，**不会写入关系**。插件不启动 worker，也不调用 `POST /api/run-once`；召回排序和关系判定由 daemon 负责。

更多配置和运行细节见 [DSH 适配器指南](https://github.com/wackyju2-beep/eventmem/blob/main/adapters/dsh/README.md)。
