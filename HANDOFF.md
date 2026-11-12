# HANDOFF — eventmem（core / adapter 拆分）

给下一个接手的人（或下一个会话的我自己）。状态：**2026-11-09，拆分完成并已上线；0.2.1 把图谱并回 adapter、重写了双语 README。**

## 一句话现状

- 仓库：Mint `~/eventmem` ↔ Windows `D:\eventmem`（单向同步，见文末）。
- 运行时：`~/.hermes/plugins/eventmem` → 软链到本仓库 `adapters/hermes/eventmem`。
- 图谱：`~/.hermes/plugins/eventmem-graph` 与 `~/.hermes/desktop-plugins/eventmem-graph` 也指向本仓库
  `adapters/hermes/` 下的对应目录（`install.sh` 一次建三条软链）。
- 生产库：`~/.hermes/eventmem/events.db`（schema **v8**，2026-11-12 迁移；迁移时 282 事件 / 1642 边，
  备份 `events.db.bak-v7-*`。`edges.backend` 为空串 = v8 之前的 StartLux 边，不是 NULL）。
- 连边：`eventmem-worker.service` 跑的是 **daemon**（`run.py daemon`，单元名保留），管理 API + GUI 在
  `127.0.0.1:9123`；一轮探活一次 → 整轮粘住，日志里应出现 `eventmem decision: using startlux`。
- 生产链路（2026-11-12 切换）：`~/.hermes/eventmem/settings.yaml` 里 `decision.backends: [startlux]`，
  pack 在 `backends/startlux/config.yaml`（`external: true`，`model: jev` 以保持请求体不变）。
  **进程归 `startlux.service` 管**（active + enabled）：链路模式下 worker 不再跑 `decision.start_script`，
  所以那个 unit 是唯一的启动者。边会记 `backend='startlux'`（切换前的是 `'default'`）。
- **因此丢了一个兜底**：unit 是 `Type=oneshot` + `RemainAfterExit`，进程挂了 systemd 不会自己拉。
  18090 掉线时 pass 记 `jev-unavailable`、游标不动（不丢数据），恢复要手动
  `systemctl --user restart startlux`（start.sh 幂等，重启无副作用）。
- 运行时：本机已装 llama.cpp **b12284**（`~/.hermes/eventmem/runtimes/llama.cpp/`，23 MB，
  `run.py models list` 可见）；**没装 GGUF**——模型按需 `models install lev`（2.4 GB）。
- 测试：14 个套件 **343 项**（packs 27 / decision 21 / daemon 16 / install 19 / ui 15 / gui_proxy 7），双解释器全绿。

## 这次改了什么

1. **core / adapter 拆开**：`core/eventmem_core` 不再 import 任何宿主；
   `adapters/hermes/eventmem` 只做接线。`__init__.py` 从 1721 行拆成
   `provider / tools / middleware / extract / host / settings / cli`。
2. **参数接口**：`CoreSettings`（frozen） + `apply()`；Hermes 侧读
   `plugins.entries.eventmem.settings.*`；worker/nightly 每进程读一次；
   `--print-settings` 可打印生效值。默认值 = 拆分前常量，有回归测试钉住。
3. **i18n 去宿主**：core 不再 `import agent.i18n`；adapter 在 initialize 时
   注册语言 provider。
4. **编码器目录**：`EVENTMEM_MODELS_ROOT` / `CoreSettings.models_root`，
   默认仍是 `<hermes_home>/localmem/models`。

## 没改的东西（故意的）

- schema、store 的 SQL、recall 的融合与渲染、夜间两问与 margin 语义。
- 召回块格式（用户定的契约）。
- 工具名与参数（模型已经学会的那一套）。

## 怎么验证

```bash
cd ~/eventmem
for t in store recall nightly worker i18n plugin settings hermes_settings packs; do
  python tests/test_$t.py | tail -1
done
# 宿主加载路径（真 Hermes）
~/.hermes/installs/*/environments/*/venv/bin/python - <<'PY'
import sys; sys.path.insert(0, "/home/wacky_ju2/.hermes/hermes-agent")
from plugins import memory
p = memory.load_memory_provider("eventmem", register_skills=False)
print(p.name, p.is_available(), len(p.get_tool_schemas()))
PY
```

## GUI 怎么开（含前后端分离）

三种入口，指向同一份页面：

| 场景 | 地址 |
|---|---|
| 在跑 daemon 的那台机器上 | `http://127.0.0.1:9123/`（完整五页） |
| 桌面端连**远端** backend（我们的常态） | app 状态栏的 **记忆后台** 按钮 → `/eventmem-gui`：**完整控制台**，六页（概览/后端/模型/调度/日志/图谱） |
| 手机 / 任意浏览器 | `<dashboard>/api/plugins/eventmem-gui/`（走 dashboard 登录，完整五页） |

实现：`adapters/hermes/eventmem-gui/dashboard/plugin_api.py` 是纯代理，把 daemon 的
**整张 HTTP 面**（静态页 + `/api/*`）原样转出去；`desktop-plugins/eventmem-gui/plugin.js` 是
**原生 React 版控制台**，数据走 `ctx.rest('/api/*')`（落到 `/api/plugins/eventmem-gui/api/*`，即代理）。
daemon 在别的机器上就 `EVENTMEM_DAEMON_URL=http://host:9123`。

**不要用 iframe**：桌面端 renderer 是 `file://` 加载的，`<iframe src="/api/…">` 会解析成
`file:///api/…`，什么都画不出来 —— 2026-11-12 就是踩了这个，表现为纯黑/纯白。插件页唯一能到
backend 的门是 `ctx.rest`（IPC → 主进程 → 带凭据的 HTTP）。

**图谱为什么能合在一个页里**：运行时插件是从 `blob:` URL 求值的，import map 只放行
`@hermes/plugin-sdk` 和 `react*`，所以插件之间不能互相 import。`eventmem-graph` 在
`register()` 里把 `{ GraphPage, ctx }` 挂到 `globalThis.__eventmemGraph`，控制台的「图谱」页用
**图谱自己的 ctx** 渲染它 —— 一套实现、一条数据路径（`/api/plugins/eventmem-graph/*`）。
graph 插件没启用时这页显示提示，不会崩。

**graph 插件是 headless 的**（2026-11-12 用户要求去掉重复入口）：它不再 `ctx.register` 任何路由或
状态栏按钮，只挂上面那个全局 + 注入 CSS。所以 `plugins.enabled` 里它必须留着，否则「图谱」页空。

改完插件怎么验：`./tools/plugin-smoke/run.sh` —— 用 Hermes 自带的 react + 一个 SDK 桩，把六个页
在 `react-dom/server` 下各渲染两遍（空数据与假数据），再单独渲染图谱组件（0 原子 / 2 原子），
要 `SMOKE PASS` + `GRAPH SMOKE PASS`。它验不了 IPC、真实主题和点击。

两个前提（改完要做的）：

1. **页面用相对路径取数据**（`ui/app.js` 的 `api()` 会去掉开头的 `/`）。绝对路径在代理前缀下会
   打到 dashboard 自己身上——`test_gui_proxy` 钉住了这条。
2. **dashboard 插件是启动时挂载的**：加/改这个插件后要重启服务它的进程（web/手机是
   `systemctl --user restart hermes-dashboard`；桌面端是 app 里 Cmd/Ctrl-K → Reload desktop plugins）。
   **插件还必须在 `plugins.enabled` 里**——用户插件的 `api` 文件只有在白名单里才会被 import
   （防代码执行的闸门）；没开白名单时路由根本不存在，iframe 打开就是一片空白（2026-11-12 踩过）。
   开完不用重启：没挂载的路由第一次被请求时会 hot-mount。`hermes plugins enable eventmem-gui` 即可。
3. **插件目录里不能留同名副本**：宿主按目录扫，`eventmem-graph.bak-*` 会以同一个 `id` 再加载一次，
   启动就报 `duplicate id`。旧目录现在挪到 `<hermes_home>/plugin-backups/`（扫描根之外），
   2026-11-12 已把两台机器上的存量 `.bak-*` 清出去。

### 主题与语言（2026-11-12）

**深色模式的坑**：宿主的 `--ui-bg-input` 在 `styles.css:314` 被写死成 `#fcfcfc`，深色主题从不覆盖它，
所以任何直接用它当底色的输入框/下拉在深色下是**白底**（截图里那一排文本框）。正确写法：

```css
background: var(--ui-bg-tertiary);   /* 或 --ui-bg-editor（代码框），都是 color-mix 出来的主题色 */
appearance: none; -webkit-appearance: none;   /* 不写这行，UA 控件会自己画白底，忽略 background */
```

外加 `color-scheme: dark` 让滚动条、勾选框、下拉弹层跟着变：控制台在挂载时读
`getComputedStyle(document.body).backgroundColor` 算亮度，`MutationObserver` 盯着 `<html>` 的
class/style 变化，切主题即时跟。（`plugins/.../plugin.js` 的 `probeDark` / `useDarkTheme`）

**语言**：一本字典 `core/eventmem_core/ui_strings.py` 的 `EN`，**以中文原文为 key**
（`EN["跑一轮"] = "Run a pass"`），因为两个 UI 都是无构建的纯 JS，key 化只会多一层对不上号的映射。
daemon 在 `GET /api/i18n` 回 `{lang, map}`，`lang` 来自 `CoreSettings.language`
（`EVENTMEM_LANG` → 宿主 `display.language` → `en`）。

- 桌面控制台：每条中文都包在 `t('…')` / `fmt('…{0}…')` 里，`Console` 在首屏渲染前拉 `/api/i18n`，
  拿不到就等（`i18n.error` 时按中文继续，不会白屏）。状态栏那颗 chip 在 `register()` 里预热字典。
- **新增 `t('…')` 必须往 `EN` 加条目**，`tests/test_ui.py::test_every_translated_literal_has_a_dictionary_entry`
  会把没条目的抓出来。
- 配置模板（新建后端那三段 YAML）不共用字典：`TEMPLATES` / `TEMPLATES_EN` 两份，`templates()` 按语言选。
- **浏览器版控制台（`http://127.0.0.1:9123/`，即 `core/eventmem_core/ui/app.js`）还没接**——它仍是纯中文。

验证：`./tools/plugin-smoke/run.sh` 会用 `ui_strings.EN` 渲一遍英文，任何残留中文直接 FAIL
（kind/关系名是入库数据，按设计不翻译，检查前已排除）。

### 实时面板与两个按钮（2026-11-12）

概览页上方常驻一条**运行条**（任何页签都在）：`● 正在使用 <backend> [kind]  [12 对] [9 边] +1  19s  [停止] [切换并重跑]`。

数据来自新的 `GET /api/status.live`：一轮开始时由 daemon 建 `{backend, pairs, edges, since, updated}`，
nightly 每判定完**一对**回调一次 `on_progress` 更新它（`+1` 就是 `pairs` 的增量动画），pass 结束时清空。
同时 `status` 多了 `busy` / `stopping`。空闲时那条显示「上次 startlux · 时间 · N 对 / M 边」。

| 路由 | 作用 |
|---|---|
| `POST /api/stop` | 协作式取消：手上这一对做完就停，**游标不动**（下一轮重做，`add_edge` 去重，代价是时间不是正确性） |
| `POST /api/switch` | 同上 + 叫醒轮询：下一轮立刻从链首重新探活，所以「LM Studio 刚就绪」不用等慢后端跑完半小时 |

实现要点：`nightly.build_edges(..., on_progress=, should_stop=)`；`should_stop` 在**每对之前**问一次；
结果里带 `skipped: "stopped"` / `stopped: true`，`worker.run_once` 把 `"stopped"` 加进「不推进游标」的名单。
daemon 里 `_abort`（停本轮）与 `_stop`（关进程）是两个不同事件，别混用。

### 当前生产链路（2026-11-12）

`~/.hermes/eventmem/settings.yaml` → `decision.backends: [win-autothink, startlux]`

| # | pack | 在哪 | 形态 | 说明 |
|---|---|---|---|---|
| 1 | `win-autothink` | Windows `10.42.0.51:1234`（LM Studio 0.4.28，模型 `vllm-d3-mini`，`type: decision`） | external + `check_model: true` | 首选；实测 **189 对 / 331 边 / 101 s ≈ 0.53 s/对**（Mint 约 4–5 s/对） |
| 2 | `startlux` | Mint `127.0.0.1:18090`（`startlux.service`） | external | StartLux-Decision-**v1.1**-4B-Instant |

一轮 pass 只探活一次，**第一个活着的负责整轮**；中途失败中止本轮、游标不推进（不丢数据）。
所以 Windows 关机不会丢数据，只是那一轮之后才回退。


Windows 这一格的两个前提（2026-11-12 踩过）：

- LM Studio 的 `/v1/systemone`、`/v1/decisions` **只接受 `type: decision` 的模型**。本地导入的 GGUF 会被它定性成
  `llm`（带 chat template / tool_use），`chat/completions` 能跑但决策端点回 `invalidModelIdentifier`，
  而且**没有界面开关能改**。判断方法是 `GET /api/v0/models` 看 `type`。
- 决策引擎有 **30 s 硬上限**（`Engine protocol systemOne request timed out after 30000ms`），而 JIT 冷加载
  的时间也算在里面，所以「闲一分钟 → TTL 卸载 → 下一轮冷加载超时 → 3 对失败 → 冷却 5 分钟」会成循环。
  稳的做法是手动加载并保持：`POST /api/v1/models/load {"model":"vllm-d3-mini","context_length":8192}`
  （手动加载的模型不受「自动卸载未使用的即时加载模型」影响），之后单次判定稳定 2.8 s。

Windows 侧三件事缺一不可：① LM Studio「在网络中提供服务」打开（默认关；关着时 Mint 连 1234
直接超时，`probe → unreachable`）；② 防火墙放行 1234 入站；③ `model:` 与 LM Studio 里的模型 id
逐字一致 —— `check_model: true` 会在探活阶段就发现不一致并回退，而不是每轮卡在同一处。

## Windows 形态（已实测 2026-11-12）

daemon 在 Windows 上用**计划任务**跑，不用服务。实测通过的做法：

```bat
:: D:\eventmem\.smoke\daemon.cmd（ASCII，CRLF）
@echo off
cd /d D:\eventmem
py -3.12 adapters\hermes\run.py daemon --db D:\eventmem\.smoke\events.db --port 9125 >> D:\eventmem\.smoke\daemon.log 2>&1
```

```
schtasks /create /tn eventmem-daemon /tr "D:\eventmem\.smoke\daemon.cmd" /sc once /st 23:59 /ru <你> /it /f
schtasks /run    /tn eventmem-daemon
```

实测：`/api/health` 回 `{"status":"ok","schema":8,...}`，日志 `api on http://127.0.0.1:9125  (GUI at /)`。

两个 Windows 专属坑：

- **没有 `fcntl` ≠ 有锁**。`acquire_lock` 在 Windows 返回 `NO_GUARD`（不是 `None`）；早先两者混为一谈，
  daemon 一启动就 `another daemon already owns …` 然后 exit 1。
- **`schtasks /end` 只杀 .cmd 外壳**，python 子进程还活着，端口和日志文件都被占住；要 `taskkill /pid <pid> /f`。
- 计划任务里没有 `HERMES_HOME`，`packs_dir` 会落到 `%LOCALAPPDATA%\hermes\eventmem\backends`；
  要指到别处就在 .cmd 里 `set HERMES_HOME=…`。


## 已知 / 下一步

- **P0–P5（v3 后端平台化）已落地**：`providers.py`（两套线格式）、`packs.py`（一个文件夹 =
  一个后端）、`decision.py`（`UrlProvider` + `Chain`：一轮探活一次、整轮粘住、401=needs_key）、
  `runtime.py`（自托管 pack 的进程/端口/僵尸回收）、`daemon.py`（60 s 轮询 + 管理 API）、
  `install.py`（llama.cpp 发行包匹配 + GGUF 下载/续传/校验 + 生成 pack）、`edges.backend`（v8）、
  `ui/`（GUI：概览/后端/模型/调度/日志）与 `settings.yaml`（GUI 与手写同一份，daemon 重读即生效）。
  自托管 pack 现在真能用：`models install lev` → 把 `lev` 加进 `decision.backends` → 重启 daemon，
  它会在有活时把 llama-server 拉起来（端口写进 `<pack>/.runtime.json`）。
  P5 收尾也做完了：README 中英双版、`docs/backends.md`（键表/链路/排错）、`examples/backends/` 四个
  可直接拷的样例（并有测试盯着它们能加载），Windows 计划任务形态实测通过（见上一节）。
  **剩下**：无。StartLux 已降级成兼容 pack（`backends/startlux`，链路 `[startlux]`，边记 `backend='startlux'`）。
- **桌面会话仍在用旧模块**：本会话进程启动早于改动，`prefetch` 还是旧的。
  换新代码要重启 `hermes-dashboard` / `hermes-gateway`（或开新会话）。
- `eventmem-graph`（3D 图谱）已并入：`adapters/hermes/eventmem-graph`（dashboard 后端）+
  `adapters/hermes/desktop-plugins/eventmem-graph`（前端）。它直连 events.db、不走 `EventMem`，
  所以不算 provider，也不注册任何东西。
- DeepSeek Harness adapter：照 `docs/architecture.md` 的表格重写
  `provider/tools/middleware/settings/host` 五件套，core 一行不用动。
- 旧的 `hermes-eventmem` 仓库：内容已过时（仍是拆分前的插件），待决定归档还是改名。

## 同步到 Windows

```bash
cd ~/eventmem
tar czf - --exclude=.git --exclude=__pycache__ . | \
  ssh rubp@10.42.0.51 "tar xzf - -C D:\\eventmem"
```

推送仍然只能由 Windows 的 GitHub Desktop 做（两边 git CLI 都没有 GitHub 凭据）。

## 图谱渲染与概览布局（2026-11-12 晚）

- **边不再是 SVG。** 每条边原来有 2 个 `<path>` + 1 个 `<text>`，真库 3652 组 = 7304 个 path；
  相机一动就逐条重写 `d`/`stroke`，同样的场景实测 **26.6 fps（中位 33ms，p95 50ms）**。
  现在边画在两块 canvas 上（底层在卡片下、前层放选中边和标签），按 `(颜色, 透明度, 线宽)`
  合并成 Path2D 一次 `stroke()`，同场景 **59.7 fps（中位 16.7ms，p95 16.7ms）**。
  边的点击本来就是舞台上的几何命中测试，不依赖 DOM，所以交互没退。
- **LOD 按缩放而不是按深度：** `cam.dist / cam.fit > 1.15` 每两条画一条，`> 1.9` 每四条画一条，
  同时抬高透明度下限、线宽最小 1px —— 缩远看到的是稀疏网而不是一堆亚像素雾。
- **人工改过的边**原来是 SVG 的 `linearGradient`，canvas 里换成纯色 `#c58bff`。
- **概览两栏等高：** `.emg-card + .emg-card{margin-top:12px}` 在 grid 里会把第二张卡压矮 12px
  且下移 12px（复现：top 26 vs 14、height 225 vs 237）。`.emg-grid` 现在 `align-items:stretch`
  且 `.emg-grid > .emg-card{margin-top:0}`。
- **量这个的工具**在 `tools/plugin-smoke/bench/`（真 Chromium + 真场景 + esm.sh 的 React），
  改图谱时先跑一次 `await __bench(3000)` 再下结论。
