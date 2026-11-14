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
2. 在 DSH 中选择 **Plugins → Add plugin**，选择克隆目录中的 `D:\eventmem\adapters\dsh`。严格同机使用应设置 `tunnel: false`。默认值却是 `tunnel: true`、`tunnelHost: mint`；`mint` 只是 SSH 别名示例，插件不提供该主机。如果本地端点的探测返回符合下述采用条件的响应，插件会采用该端点，否则默认配置可能尝试 SSH 连接 `mint`。
3. 在 PowerShell 中检查 `Invoke-RestMethod http://127.0.0.1:9123/api/health`，将返回的 `db` 与预期数据库核对；安装器也会运行自检。随后在 DSH 侧边栏打开 **记忆后台**查看服务。

## 配置 profile

本插件没有 Config schema 或配置表单。修改选项需要熟悉 DSH profile 的人，通过更高层的 profile patch 覆盖已挂载的 `eventmem` 行；不熟悉时请寻求协助。编辑前备份**当前使用的 profile** 的 `cordis.patch.yml`：路径为 `<DSH_HOME>/profiles/<profile>/cordis.patch.yml`；Windows 上未设置 `DSH_HOME` 时默认为 `%USERPROFILE%\.dsh`。`desktop` 只是 profile 名称示例，不一定是当前 profile，请参考 DSH profile 配置文档确认。

保留原有行和注释；若文件仅为 `[]`，用下面的序列替换空列表。若已有 `eventmem` 覆盖项，应更新该项，不要添加重复 ID。同机 daemon 的完整配置示例：

```yaml
- id: eventmem
  name: dsh-eventmem
  config:
    baseUrl: http://127.0.0.1:9123
    agent: dsh
    lang: zh
    tunnel: false
    tunnelHost: mint
    tunnelLocalPort: 9123
    tunnelRemotePort: 9123
```

覆盖会**整体替换而不是合并** `config`，因此要保留上面字段及所需的其他自定义字段。远程使用时，将 `tunnel` 设为 `true`，`tunnelHost` 改为远端可用的免密码 SSH 别名。若本地 9123 被占用，应同时修改 `baseUrl` 的端口和 `tunnelLocalPort`，并保持 `tunnelRemotePort` 与远端 daemon 一致。更高优先级的层还可能覆盖此配置。同 ID 替换及非深度合并已在隔离 profile 中通过 `dump-config` 验证，**未以启用的插件验证**；请在自己的宿主中确认实际生效配置与连接。

## 远程共享与安全

Linux/Hermes 的安装和 daemon 启动方法见[仓库快速开始](https://github.com/wackyju2-beep/eventmem/blob/main/README.md)；`install.sh` 链接适配器，但不会启动 daemon。远端 daemon 应使用 Hermes 所用的同一个数据库，每个数据库只运行一个 daemon。

**注意错连数据库：**启用隧道时，插件尝试 SSH 前先探测 `baseUrl` 的 `/api/health`；只要 HTTP 成功且解析后的 JSON 值为真值，就采用现有端点，既不校验 `status`、`schema` 或 `db`，也不认证对端。因此本地 9123 上的其他服务可能被误认，探测成功不证明连到了远端。可在检查并确认上文所述卸载风险后，在仓库目录的 PowerShell 中运行 `.\install.cmd -Uninstall`；或者使用空闲本地端口，例如 `baseUrl: http://127.0.0.1:9124`、`tunnelLocalPort: 9124`，远端仍用 9123 时保留 `tunnelRemotePort: 9123`。手动检查所选本地端点的 `/api/health` 的 `status`、`schema` 和 `db`，将 `db` 与目标远端数据库核对；这些字段也不是对端身份认证。还可写入一条易辨认的事件，再从另一宿主召回以验证共享。不要将 SQLite/WAL 放在网络共享上。Windows 缺少 daemon 的 `flock` 单实例保护，不能依赖它阻止第二个 daemon 打开同一数据库。

**安全边界：**daemon 数据 API 和管理台没有内置鉴权或 TLS，能访问回环地址的本地用户和进程也不经认证。保持 daemon 仅监听回环地址，信任或隔离本地账户与进程，远程访问使用 SSH，不要暴露到不可信网络。未显式指定 `--host` 时，`EVENTMEM_DAEMON_HOST` 可改变 daemon 的默认绑定地址；检查已有进程的实际监听地址。SSH `-L` 的监听也受客户端 `GatewayPorts` 设置影响，应确认实际转发监听仅限回环。详见[数据面 API](https://github.com/wackyju2-beep/eventmem/blob/main/docs/api.md)。

## 模型前置条件

基本存储和字面召回不需要关系判定器。回合末抽取需要可用的 DSH 宿主模型；如果会话没有选择模型，可以设置 `auxProvider` 和 `auxModel`。语义召回还需要向量运行时，以及放在 `<HERMES_HOME>\eventmem\models`（默认 `%USERPROFILE%\.hermes\eventmem\models`）中的 Qwen3-Embedding-0.6B ONNX `tokenizer.json` 和 `model_int8.onnx`。自动关系需要运行中的 daemon 和已配置的判定后端；可在记忆后台配置后端或下载模型，参见[后端配置](https://github.com/wackyju2-beep/eventmem/blob/main/docs/backends.md)。

## 使用与限制

- 每回合第一步召回相关记忆，将结果块作为额外的用户角色消息注入；项目提示跟在结果块之后。
- 提供七个工具：`evt_add` 写事件（默认每次 0–3 条）、`mem_recall` 召回、`expand` 查看事件及原文、`mem_neighbors` 查看图邻居、`proj_add` 新建项目、`proj_list` 列出项目、`evt_forget` 永久删除事件。项目名须使用已有项目或 `unfiled`，新项目须先显式建立。
- 回合未写入事件时，回合末可通过辅助模型抽取 0–3 条；状态变更工具的结果会附带记忆提醒。会话项目由 daemon 持久保存，后续抽取继承该项目。
- **记忆后台**提供概览、后端、模型、调度、日志和图谱；图谱支持筛选、检查及编辑记忆。插件通过宿主代理访问 daemon，不直接操作数据库。
- `tunnel: true` 时，插件按上述条件探测并采用现有端点，否则启动并监督 SSH；卸载时只终止由插件启动的 SSH，不终止它采用的外部隧道。记忆服务或辅助调用失败时，自动召回和抽取降级，不阻塞回合；显式工具调用可返回错误。
- 数据面没有写入关系的端点：`evt_add` 会校验 `links`，有效项报告为跳过，**不会写入关系**。插件不启动 worker，也不调用 `POST /api/run-once`；召回排序和关系判定由 daemon 负责。

更多配置和运行细节见 [DSH 适配器指南](https://github.com/wackyju2-beep/eventmem/blob/main/adapters/dsh/README.md)。
