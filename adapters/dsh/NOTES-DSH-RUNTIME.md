# DSH 侧四个环境事实（写 adapter 前先钉死）

依据是**这台机器上正在跑的** DeepSeek Harness：`DSH_INSTALL_DIR` 指向
`…\resources\app.asar\dsh\node_modules\@deepseek-ai\dsh`（profile `desktop`，Web GUI `127.0.0.1:14095`）。
下面每条都附了可复核的出处，不靠记忆。

## 1. 进程模型：常驻单进程，进程内并发多个会话

- 宿主是一个长驻 Node/Electron 进程：`DeepSeek Harness.exe --expose-internals
  …\@deepseek-ai\dsh-desktop-host\lib\index.js <install> <profile> …`（本机 PID 34220）。
  Web GUI 只是它的客户端，不是会话容器。
- 进程里有一个 `AgentRegistry`，"one entry per live agent"（`dsh-agent/README.md`，Registry and
  lifecycle）。**会话 = 一个 `Agent` + 一个 `Session`**，可以同时活着多个。
- 并发是常态：本地子代理默认 `maxActiveSubagents: 8`（`dsh-subagent/README.md`，Activation
  capacity），每个子代理是**同进程**的独立 Agent/Session；Web 端两个对话也可以同时跑。
- 结论：`session` 绝不能用模块级变量。状态按 **`agent.session.id`** 分桶（`Map<sessionId, …>`）；
  同一 session 共享 daemon 侧召回去重状态，两个对话共用一个 id 会把正文吞成 `#id 同上文`。

## 2. 回合边界：`session/event` 的 `turn/*` 与 `compaction/*`，没有撤销

- 持久边界事件通过 `ctx.on('session/event', (session, event) => …)` 广播
  （`dsh-session/src/index.ts:79`）。与 adapter 有关的是：
  - `turn/start`（`{turn}`）/ `turn/end`（`{turn, reason}`）——`dsh-agent-loop/src/agent.ts:342,428`；
  - `compaction/start`（`{compactionId, sourceCommandId?, turn}`）/ `compaction/end`
    （`dsh-compaction/src/types.ts:24`；两者都是 session event，仅写日志）。
- `agent/created` 带 `source: 'startup' | 'resume' | 'clear' | 'compact'`
  （`dsh-agent/src/runtime-types.ts:152,291`）。注意 `clear`/`compact` 是**保留值、暂无发射方**
  （`dsh-agent/README.md`，Dev Note）——别指望它来通知"清空/压缩"。
- 这个版本**没有**消息级撤销/回退：`dsh-session`、`dsh-api-session-controller` 里搜不到
  rewind/truncate 事件；"新开对话"= 新 `session.id`，不是原 id 清空。
- 结论：`reset: true` 发在四处——①每个 agent 首次 prefetch（覆盖 resume/新进程）；
  ②`compaction/end` 之后；③会话 id 变化（自然就是新桶）；④（无撤销，故无第四处）。
  回合内的普通轮次**不**发 reset，否则去重指针永远不会出现。

## 3. 辅助模型：有，且就是本会话的模型路由

- `ctx.llm.stream({provider, model, messages, system, temperature, maxTokens, stop, signal,
  purpose})`（`dsh-llm/README.md`，Minimal composition；`dsh-llm/src/types.ts` 的
  `GenerateOptions`）。`temperature`、`maxTokens`、`signal` 都收——0 / 500 token / 60 s
  `AbortController` 能直接表达。
- 路由从 `agent.model.current()` 取（`dsh-agent/src/model-selection.ts:105`，返回
  `{provider, model, reasoningEffort?}`）。这就是本回合正在用的路由，因此**不需要**另配密钥，
  也不会引入第二个 provider。
- 流一定以 `finish` 收尾：`reason.kind` ∈ `stop | tool-calls | max-tokens | aborted | error`
  （`dsh-llm/src/types.ts:163`）。文本在 `text-delta` / `block-end` 里。会话标题、压缩摘要
  都是这条路的现成用户，说明辅助调用在本版本是常规操作。
- 结论：回合末抽取用 `agent.model.current()` 的 route、`temperature: 0`、`maxTokens: 500`、
  `AbortSignal` 60 s；失败只留本地日志，**不写兜底事件**。

## 4. 工具协议：`ctx.tools.register()`，回调是 `tools/result` 与 `tools/post-execute`

- 注册：`ctx.tools.register(definition)`，definition = `{name, description, parameters(JSON Schema),
  output: {schema, render}, execute(args, exec), …}`（`dsh-tools/src/contracts.ts:27`、
  `src/index.ts:128`）；注册是 effect，插件卸载即摘除。`inject = ['tools']`。
- `execute(args, exec)` 里 `exec.agent` 是调用方 Agent，`exec.signal` 是取消信号
  （`dsh-tools/src/contracts.ts:186,202`）。
- 结果回调有两个层次（`dsh-tools/src/events.ts:29-49`）：
  - `tools/post-execute`：**waterfall**，可以 `{kind:'accept', content:[…]}` 改写/追加模型看到的
    结果内容，也可以挂 `additionalContexts`；
  - `tools/result`：**emit**，只观察冻结后的最终结果，改不了。
  另有 `tools/pre-execute`（policy）与 `ctx.tools.guard()`（单调拒绝）。
- 结论：中间件用 `tools/post-execute`——成功且改了状态时，把提醒行追加到 `result.content` 尾部；
  插件自己的 `evt_add` 不提醒，本回合已经写过也不再提醒。

## 额外约束：bundle 不能 `import '@deepseek-ai/*'`

profile 里的本地插件是**链接目录**，Node 从插件的真实路径解析裸导入，而它不在 dsh 安装的
`node_modules` 链上。现存三份本地插件（`~/.dsh/local-plugins/dsh-better`、`dsh-title-guard`）都只
`import 'node:*'`。所以本 adapter 只用 `ctx` API + Node 内置模块：工具定义用原始 JSON Schema
`ToolDefinition`，注入消息用普通对象 `{id, role:'user', content, source:{kind, form}}`
（`dsh-session/src/index.ts:355` 只要求 `source.kind` 是非空字符串；官方 `dsh-time-context`
也是往 `agent/pre-step` 的 `messages` 尾部追加一条 user message）。
