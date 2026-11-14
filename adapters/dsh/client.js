/**
 * eventmem / dsh — the memory console, rendered natively in the dsh Web client.
 *
 * This is the Hermes 「记忆后台」 ported to dsh: the same five views (概览 / 后端 / 模型 /
 * 调度 / 日志) over the daemon's own `/api/*` surface, reached through the Host route
 * `/api/eventmem` that `lib/console.js` proxies over the adapter's tunnel.
 *
 * It is a page, not an iframe: every control is a dsh control, every color a
 * `--dsw-alias-*` token, so light/dark, locale and type scale follow the host. The
 * sidebar entry registers into `sidebar.panellist` with order 5 — directly under 插件
 * (order 0) and above 自动化任务 (order 10) — and the page itself occupies the `main`
 * keyed slot under the same id.
 *
 * No build step: plain ESM evaluated by the client module loader, `h()` instead of JSX.
 */

window.__ModuleLoader__.load({
  id: 'dsh-eventmem',
  factory(require) {
    const React = require('react')
    const h = React.createElement
    const { useCallback, useEffect, useMemo, useRef, useState } = React

    const NS = 'eventmem'
    const PANEL_ID = 'eventmem'

    /** English UI strings; Chinese keys double as the lookup keys. */
    const EN = {
      '记忆后台': 'Memory console',
      '概览': 'Overview',
      '后端': 'Backends',
      '模型': 'Models',
      '调度': 'Schedule',
      '日志': 'Logs',
      'eventmem daemon 的记忆运维台：概览、后端链路、模型、连边调度与日志。': 'The eventmem daemon backstage: overview, backend chain, models, edge schedule, and logs.',
      'daemon 不可达': 'daemon unreachable',
      '连接中…': 'Connecting…',
      '重启服务': 'Restart service',
      '重启 eventmem 服务？服务将短暂不可用，正在进行的任务会先结束。': 'Restart the eventmem service? It will be temporarily unavailable while ongoing tasks finish.',
      '正在请求重启…': 'Requesting restart…',
      '正在等待服务重启…': 'Waiting for the service to restart…',
      '服务已重启': 'Service restarted',
      '暂未确认服务重启，请检查服务状态。': 'Restart not yet confirmed. Check the service status.',
      '重启请求未完成，请检查服务状态。': 'Restart request did not complete. Check the service status.',
      '连接': 'Connection',
      '开始使用 eventmem': 'Set up eventmem',
      '适配器已加载。下面是本机与 daemon 的依赖状态；有不可用项时，按提示修正后重试。': 'The adapter is loaded. This is the dependency state between this machine and the daemon; fix any unavailable item and retry.',
      '适配器已加载': 'Adapter loaded',
      '记忆服务': 'Memory service',
      '数据库身份': 'Database identity',
      '数据库': 'Database',
      'API token': 'API token',
      '向量检索': 'Vector retrieval',
      '关系后端': 'Relation backend',
      '远程模式': 'Remote mode',
      '本机模式': 'Local mode',
      '连接设置': 'Connection settings',
      '本机记忆': 'Local memory',
      '远程记忆': 'Remote memory',
      '使用本机 127.0.0.1 上的 daemon（推荐）。': 'Use the daemon on this machine at 127.0.0.1 (recommended).',
      '通过 SSH 隧道连接另一台机器上的 daemon。': 'Reach the daemon on another machine through an SSH tunnel.',
      'daemon 地址': 'daemon address',
      'SSH 主机别名': 'SSH host alias',
      '预期数据库 ID（可选）': 'Expected database id (optional)',
      'token 文件（可选）': 'token file (optional)',
      '测试连接': 'Test connection',
      '保存并重新加载': 'Save and reload',
      '重新检查': 'Check again',
      '正在测试…': 'Testing…',
      '正在保存…': 'Saving…',
      '已保存。适配器正在重新加载，稍后会重新连接。': 'Saved. The adapter is reloading and will reconnect shortly.',
      '连接可用。保存后插件会按这份配置重载。': 'Connection works. Saving reloads the adapter with this configuration.',
      '没有 daemon 时，记忆功能会静默降级：召回不注入，回合结束不写事件。': 'Without a daemon, memory degrades silently: no recall injection and no turn-end writes.',
      '安装 daemon：': 'Install the daemon:',
      '安全：daemon 默认只监听 loopback。若开启 token，本页通过 token 文件读取，不会把 token 写进配置。': 'Security: the daemon listens on loopback by default. If a token is enabled, this page reads it from the token file and never writes the token into configuration.',
      '可选：向量检索需要 ONNX 模型；关系连边需要一个 decision 后端。': 'Optional: vector retrieval needs the ONNX model; edge building needs a decision backend.',
      '细节': 'Detail',
      '状态': 'Status',
      '未配置': 'not configured',
      '已配置': 'configured',
      '不可用': 'unavailable',
      '可选': 'optional',
      '说明': 'Notes',
      '暂停全部判断': 'Pause all judging',
      '继续判断': 'Resume judging',
      '已暂停：这一轮停下后不再自动开新的一轮': 'Paused: the current pass stops and no new pass starts',
      '已继续：按调度恢复': 'Resumed on schedule',
      '事件': 'Events',
      '边': 'Edges',
      '待判': 'Pending',
      '最新': 'Newest',
      '空库': 'Empty',
      '对已判': 'evaluated pairs',
      '空闲 · 游标落后': 'idle · cursor behind',
      '个事件': 'events',
      '无游标': 'no cursor',
      '正在准备这一轮…': 'preparing this pass…',
      '第': 'event',
      '个事件 · 已判': '· judged',
      '对（预计）': 'pairs (estimate)',
      '已判': 'judged',
      '空闲': 'idle',
      '回合记忆抽取失败': 'Turn memory extraction failed',
      '这些回合没有写进任何记忆：抽取模型没给出可用的 0–3 条事件，系统不会再用机械摘要兜底。修好模型后点「清除」。': 'Those turns wrote no memory: the extractor produced no usable 0–3 events and there is no fallback. Clear these after fixing the model.',
      '清除': 'Clear',
      '输出无法解析': 'unparsable output',
      '调用失败': 'call failed',
      'kind 分布': 'Kind distribution',
      '项目（按事件数）': 'Projects (by events)',
      '还没有事件': 'No events yet',
      '还没有项目': 'No projects yet',
      '最近几轮 pass': 'Recent passes',
      '完成': 'done',
      '这一进程还没跑过 pass': 'No pass in this process yet',
      '后端链路': 'Backend chain',
      '空（用 decision.url 单端点）': 'empty (single decision.url endpoint)',
      '顺序即回退顺序：一轮开始时逐项探活，第一个活着的负责整轮。': 'Order is fallback order: one probe each at pass start, the first live one owns the pass.',
      '连边数据': 'Edge data',
      '清空会删掉全部边和「判过的对」并把游标归零：下一轮 pass 从最早的事件重新判。': 'Clearing removes every edge and evaluated pair and resets the cursor, so the next pass re-judges from the oldest event.',
      '清空连边并重跑': 'Clear edges and re-run',
      '链路顺序': 'Chain order',
      '上移': 'Move up',
      '下移': 'Move down',
      '移除': 'Remove',
      '加到末尾': 'Append',
      '保存链路': 'Save chain',
      '空链路 = 只用 decision.url 那个单端点（v2.1 行为）。': 'An empty chain means only the single decision.url endpoint (v2.1 behaviour).',
      '新建后端': 'New backend',
      '名字，例如 typesafe / lev': 'name, e.g. typesafe / lev',
      '创建后还要把它加进链路，再重启 daemon 才会自动拉起本地进程。': 'After creating it, add it to the chain; a local process starts on the next daemon pass.',
      '探活': 'Probe',
      '启动': 'Start',
      '停止': 'Stop',
      '删除': 'Delete',
      '在链路': 'in chain',
      '运行中': 'running',
      '未运行': 'stopped',
      '缺key': 'missing key',
      '云端 API（System One 协议）': 'Cloud API (System One protocol)',
      'LM Studio / 已装好的本地服务': 'LM Studio / an already running service',
      '本地 GGUF（自己托管，daemon 负责启动）': 'Local GGUF (daemon launches it)',
      '保存的就是这份文件本身，注释会原样留下。': 'Saves this file verbatim, comments included.',
      '保存': 'Save',
      '创建': 'Create',
      '删除后端': 'Delete backend',
      '运行时': 'Runtime',
      '还没装 llama.cpp（点右边按钮，按硬件自动选包）': 'llama.cpp is not installed (the button picks a build for this machine)',
      '下载/更新 llama.cpp': 'Download / update llama.cpp',
      '已装模型': 'Installed models',
      '没有GGUF': 'No GGUF',
      '安装 GGUF + 生成 pack': 'Install a GGUF and write a pack',
      '后端名': 'backend name',
      '量化': 'quantisation',
      '跳过 llama.cpp（我已经有）': 'Skip llama.cpp (already installed)',
      '装完自动加入链路': 'Add to the chain when done',
      '开始安装': 'Start install',
      '进行中': 'running',
      '失败': 'failed',
      '预设每轮问多少对': 'How many pairs per pass',
      '轻': 'Light',
      '标准': 'Normal',
      '深': 'Deep',
      'Tier1 · 向量近邻 top-k': 'Tier1 · vector neighbours top-k',
      'Tier1 · 余弦下限': 'Tier1 · cosine floor',
      '写入边 margin': 'Edge write margin',
      'Tier2 · 同会话最近 top-k': 'Tier2 · same session recent top-k',
      '同会话近期事件数': 'Same-session recent events',
      'Tier2 · 未闭环教训': 'Tier2 · unclosed lessons',
      'Tier2 · 决策骨架': 'Tier2 · decision skeleton',
      'Tier3 · 跨项目 top-k': 'Tier3 · cross-project top-k',
      'Tier3 · 余弦下限': 'Tier3 · cosine floor',
      'Tier 明细': 'Tier detail',
      '请输入有效数值（置信度与 margin 为 0–1）': 'Enter valid numbers (confidence and margin are 0–1)',
      '已保存；下一轮 pass 用新值': 'Saved; the next pass uses the new values',
      '调度已经在默认值上': 'The schedule already uses the defaults',
      '自动运行': 'Automatic runs',
      '每 N 分钟检查': 'Every N minutes',
      '每天固定时间': 'Daily at',
      '每周固定一天': 'Weekly on',
      '关闭（只用按钮）': 'Off (button only)',
      '时间（HH:MM）': 'Time (HH:MM)',
      '星期': 'Weekday',
      '周一': 'Mon',
      '周二': 'Tue',
      '周三': 'Wed',
      '周四': 'Thu',
      '周五': 'Fri',
      '周六': 'Sat',
      '周日': 'Sun',
      '轮询间隔（秒）': 'Poll interval (seconds)',
      '仅保存未来设置': 'Saves future settings only',
      '全部恢复默认': 'Reset to defaults',
      '本进程日志最近条': 'This process log (last entries)',
      '自动刷新': 'Auto refresh',
      '跟随最新': 'Follow latest',
      '先起个名字': 'Give it a name first',
      '刷新': 'Refresh',
      '（还没有日志）': '(no log entries)',
      '保存成功': 'Saved',
      '已恢复默认': 'Restored defaults',
      '已清除 {n} 条抽取失败警告': 'Cleared {n} extraction-failure notice(s)',
      '已清空 {edges} 条边、{pairs} 对判定记录，游标归零': 'Cleared {edges} edges and {pairs} pair records; cursor reset',
      '确认操作': 'Confirm action',
      '取消': 'Cancel',
      '确认': 'Confirm',
      '删除后不可恢复。': 'This cannot be undone.',
      '确认删除该后端？': 'Delete this backend?',
      '确认清空全部边和判过的对？': 'Clear every edge and evaluated pair?',
      '确认恢复调度默认值？': 'Restore the schedule defaults?',
      '可用': 'available',
      '不可用': 'unavailable',
      '已删除': 'Deleted',
      '已创建': 'Created',
      '安装完成': 'Install finished',
      '安装失败': 'Install failed',
      '开始下载 llama.cpp': 'Downloading llama.cpp',
      '安装任务已开始': 'Install started',
      '暂无': 'none',
      // ---- graph page ----
      '图谱': 'Graph',
      '记忆图谱': 'Memory graph',
      '筛选': 'Filter',
      '筛选：项目 / 会话 / 日期 / 类型 / 序号 / 关系 / 语义': 'Filter: project / session / date / kind / ids / relation / semantic',
      '项目': 'Project',
      '项目：不限': 'Project: any',
      '会话': 'Session',
      '会话：不限': 'Session: any',
      '日期': 'Date',
      '日期：不限': 'Date: any',
      '关系': 'Relation',
      '序号': 'IDs',
      '语义': 'Semantic',
      '语义检索': 'Semantic search',
      '语义检索：描述你要找的记忆…': 'Semantic search: describe the memory…',
      '语义检索：库里还没有事件向量': 'Semantic search: no event vectors yet',
      '语义命中数': 'Semantic hits',
      '检索': 'Search',
      '检索中…': 'Searching…',
      '语义不可用': 'semantic unavailable',
      '清除语义筛选': 'Clear the semantic filter',
      '清除语义': 'Clear semantic',
      '看不懂：': 'Unrecognized: ',
      '{n} 个编号不在库里': '{n} ids are not in the database',
      '条': 'items',
      '扫描 ': 'scanned ',
      '跳过': 'skipped',
      '在上面输入一句描述，回车检索': 'Type a description above, then press Enter',
      '库里还没有事件向量': 'No event vectors yet',
      '清空筛选': 'Clear filters',
      '焦点范围': 'Focus',
      '仅命中': 'Matches only',
      '含一跳邻居': '+ one hop',
      '关系名': 'Relation labels',
      '原子': 'atoms',
      '边': 'edges',
      '灰边': 'grey edges',
      '命中': 'matched',
      '重置视角': 'Reset view',
      '复原布局': 'Reset layout',
      '载入记忆图谱…': 'Loading the graph…',
      '图谱渲染失败': 'The graph failed to render',
      '读不到 eventmem 图谱': 'Cannot read the eventmem graph',
      '确认 daemon 在跑（/api/health 通）再重试。': 'Check that the daemon is up (GET /api/health) and retry.',
      '重试': 'Retry',
      '还没有记忆原子': 'No memory atoms yet',
      'eventmem 里写过事件之后这里才有东西。': 'Once eventmem has written an event, it shows up here.',
      '（这一问没有留档）': '(nothing recorded for this question)',
      '关闭': 'Close',
      '会话：': 'Session: ',
      '✎ 人工修改于 ': '✎ edited ',
      '（彩虹描边）': ' (rainbow rim)',
      '类型': 'Kind',
      '所属项目': 'Project',
      '正文': 'Text',
      '保存中…': 'Saving…',
      '保存并更新向量': 'Save and re-embed',
      '已保存，Embedding 向量已重算（{n} 维）': 'Saved; embedding re-embedded ({n} dims)',
      '已保存；向量没有更新：': 'Saved; the vector was not updated: ',
      '未知原因': 'unknown reason',
      '。项目/类型变了：下一轮连边会从这条之前重选候选，判过的对不再问，已有的边一条不删': '. Project/kind changed: the next link pass re-selects candidates from before this event, already-judged pairs are not asked again, and no edge is deleted.',
      '保存会重算这条的 Embedding 向量（彩虹描边）': 'Saving re-embeds this event (rainbow rim)',
      '未改动': 'Unchanged',
      '连着的边': 'Connected edges',
      '没有边': 'No edges',
      '无': 'none',
      '— 不改项目 —': '— keep project —',
      '未归类（清空项目）': 'Unfiled (clear project)',
      '— 不改类型 —': '— keep kind —',
      '批量编辑': 'Batch edit',
      ' 个原子': ' atoms',
      '取消选择': 'Clear selection',
      '应用中…': 'Applying…',
      '应用': 'Apply',
      '改项目会重算向量与跨项目标记；已有的边一条不删': 'Changing a project re-embeds and re-marks cross-project edges; no edge is deleted',
      '选项目或类型后应用': 'Pick a project or kind, then apply',
      '编号：': 'IDs: ',
      '已改 {n} 条': 'Updated {n}',
      '向量 {a} 条重算 / {b} 条跳过（': 'Re-embedded {a} / skipped {b} (',
      '向量 {n} 条已重算': 'Re-embedded {n}',
      '跨项目标记 {n} 条已修正': 'Fixed {n} cross-project mark(s)',
      '已标记重算候选边：下一轮连边会从最早的这条之前开始，判过的对不再问': 'Candidate reseat marked: the next link pass starts before the oldest of these; already-judged pairs are not asked again',
      '候选边不用重算：游标已经在这批事件之前': 'No reseat needed: the cursor is already before these events',
      '{n} 条不存在，已跳过': '{n} missing, skipped',
      '判定：无关系': 'Judged: no relation',
      '置信 ': 'Confidence ',
      '人工': 'human',
      '模型判定': 'model',
      '这一对问过模型，但没有过 margin —— 没有连边': 'The model judged this pair but it did not clear the margin — no edge',
      '✎ 人工改过（彩虹描边）': '✎ edited by hand (rainbow rim)',
      '后': 'later',
      '前': 'earlier',
      '问题一 · 后一条相对前一条': 'Question 1 · later vs earlier',
      '问题二 · 补充关系': 'Question 2 · detail relation',
      '改这条边的关系': 'Edit this relation',
      '人工连一条边': 'Draw a relation by hand',
      '— 选一个关系 —': '— pick a relation —',
      '保存关系': 'Save relation',
      '连上': 'Connect',
      '断开': 'Disconnect',
      '人工改过的边会蒙上彩虹光': 'Hand-edited edges get a rainbow glow',
      // ---- 概览的待判 / 直播条 ------------------------------------------------
      '第 {0}/{1} 个事件 · 正在数这一轮还要问多少对…': 'event {0}/{1} · counting the pairs in this pass…',
      '第 {0}/{1} 个事件 · 已判 {2} 对（预计）': 'event {0}/{1} · {2} pairs judged (estimate)',
      '空闲 · 游标落后 {0} 个事件': 'idle · cursor behind {0} events',
      '正在使用': 'Using',
      '已暂停': 'paused',
      '正在暂停…': 'pausing…',
      '暂停中': 'paused',
      '上次 {0} · {1} · {2} 对 / {3} 边': 'last {0} · {1} · {2} pairs / {3} edges',
      '还没跑过': 'no pass yet',
      '切换并重跑': 'Switch & rerun',
      '已请求切换：本轮停下后立刻重跑，重新探活链路': 'Switch requested: the pass stops, then the chain is probed again immediately',
      '对': 'pairs',
      // ---- 调度页 ------------------------------------------------------------
      '已暂停（不开新的一轮）': 'paused (no new round)',
      '什么时候算': 'When',
      '每 N 分钟检查一次（有新事件才跑）': 'on an interval (only when there is new work)',
      '关闭自动运行（只用按钮）': 'off (buttons only)',
      '间隔（秒）': 'Interval (seconds)',
      '时间': 'Time',
      '保存自动运行设置': 'Save schedule',
      '暂停后任何模式都不再开新的一轮：正在跑的那一轮会在当前这一对结束后停下，暂停状态存在数据库里，重启 daemon 也不会自己恢复，直到点「继续判断」。': 'Pausing vetoes every mode: the running round stops after the pair in flight, and the flag is stored in the database — restarting the daemon will not resume it until you press Continue.',
      '预设（每轮问多少对）': 'Presets (pairs asked per event)',
      '标准（默认）': 'Standard (default)',
      '候选来源会去重，实际数量未知；每对 8 秒仅供参考，当前模型速度未知。': 'Candidate sources overlap; actual volume is unknown. Eight seconds per pair is only an illustration; the current model speed is unknown.',
      '候选选择与成本': 'Candidate selection and cost',
      '保存未来设置会同时保存候选数量和关系规则；历史操作不会保存未来设置。': 'Saving future settings saves both candidate selection and relationship rules; historical actions do not save future settings.',
      '仅保存未来设置': 'Save for future only',
      '恢复调度默认值': 'Reset schedule defaults',
      '（无配置文件）': '(no config file)',
      '确认恢复调度默认值？': 'Restore the schedule defaults?',
      '关系接受条件': 'Relationship acceptance',
      '全局 margin': 'Global margin',
      '改变最低置信度': 'Change minimum confidence',
      '否定最低置信度': 'Negate minimum confidence',
      '重复最低置信度': 'Repeat minimum confidence',
      '因为最低置信度': 'Because minimum confidence',
      '细化最低置信度': 'Refine minimum confidence',
      '有未保存的改动': 'Unsaved changes',
      '已保存': 'Saved',
      '保存接受条件': 'Save acceptance rules',
      '接受条件已保存：之后的连边按该标准判定': 'Acceptance rules saved: later edges are judged by them',
      '保存后，从下一轮 pass 起的连边一律按这里的 margin 与最低置信度判定；已有边不动（要改历史就用下面的重校准预览）。': 'After saving, every edge from the next pass on is judged by this margin and these minimum confidences. Existing edges are untouched; use the recalibration preview below to change history.',
      '历史关系重校准': 'Historical relationship recalibration',
      '只重判已有概率的关系；无法使用的记录会跳过。预览不会修改历史。': 'Re-evaluate stored probabilities only; unavailable records are skipped. Preview does not change history.',
      '历史概率格式有歧义，模型来源未知。StartLux 输出条件概率，但不能假设其他后端相同。默认跳过条件形式；仅在确认后勾选，选择只影响条件形式的记录。': 'Legacy probability formats are ambiguous and model provenance is unknown. StartLux reports conditional probabilities, but other backends cannot be assumed to do so. Conditional-shaped records are skipped by default; opt in only if confirmed. This choice affects only conditional-shaped records.',
      '将条件形式的历史概率按条件概率处理': 'Treat conditional-shaped historical probabilities as conditional',
      '历史候选补算': 'Historical candidate backfill',
      '重新选择历史候选需要 Jev 模型判定；耗时和成本不确定。预览不会启动任务。': 'Re-selecting historical candidates requires Jev model decisions; time and cost are uncertain. Preview does not start a job.',
      '补算使用已保存的关系阈值，不使用未保存的阈值编辑；候选参数使用当前表单。': 'Backfill uses saved relationship thresholds, not unsaved threshold edits; candidate parameters come from this form.',
      '预览历史': 'Preview history',
      '应用历史重校准': 'Apply historical recalibration',
      '启动历史候选补算': 'Start historical candidate backfill',
      '停止补算': 'Stop backfill',
      '已请求停止：本轮做完手上这一对就停': 'Stop requested: this pass ends after the pair in flight',
      '确认应用历史操作？此操作不会保存未来设置。': 'Apply this historical operation? It will not save future settings.',
      '请求已提交': 'Request submitted',
      '参数已改变，请重新预览': 'Settings changed; preview again',
      '预览未返回令牌，无法应用': 'Preview did not return a token; cannot apply',
      '运行模式无效': 'invalid schedule mode',
      '时间要写成 HH:MM': 'time must be HH:MM',
      '星期要在 0–6': 'weekday must be 0–6',
      '已恢复默认（清掉 settings.yaml 里的覆盖）': 'Defaults restored (overrides removed from settings.yaml)',
      '配置尚未加载': 'Configuration has not loaded',
      'Tier2 · 同会话近期事件数': 'Tier2 · recent events in session',
      'Tier1 = 全局向量近邻；Tier2 = 同会话近期 + 未闭环教训 + 决策骨架；Tier3 = 跨项目。改的是 daemon 的 settings.yaml，下一轮 pass 生效。': 'Tier1 = global vector neighbours; Tier2 = recent in this session + unclosed lessons + decision skeleton; Tier3 = cross-project. This edits the daemon\'s settings.yaml and applies to the next pass.',
      // ---- 历史重校准 / 补算的预览文本 -----------------------------------------
      '{0}：新增 {1}，删除 {2}': '{0}: {1} added, {2} removed',
      '置信度更新的边：{0}': 'Edges with confidence updates: {0}',
      '跳过的事件对：{0}': 'Skipped pairs: {0}',
      '未覆盖的 Jev 边：{0}': 'Uncovered Jev edges: {0}',
      '记录了人工删除的事件对：{0}': 'Pairs with recorded manual deletions: {0}',
      '旧版人工删除记录无法确认。': 'Older manual deletions cannot be identified reliably.',
      '历史事件 {0} 条；当前图观察到 {1} 个候选对。该观察值不是最终数量，也不是上限或下限；新边可能改变后续候选。': '{0} historical events; {1} candidate pairs observed on the current graph. This is neither the final count nor an upper or lower bound; new edges may change later candidates.',
      '补算状态：{0}；已判 {1} 对，新增 {2} 条边': 'Backfill: {0}; {1} pairs judged, {2} edges added',
      '尚无补算任务': 'No backfill job',
      '等待中': 'Pending',
      '运行中': 'Running',
      '已结束': 'Finished',
      '已完成': 'Completed',
      '已停止': 'Stopped',
      '模型不可用': 'Model unavailable',
      '模型判定失败': 'Model decision failed',
      '改变': 'Change',
      '否定': 'Negate',
      '重复': 'Repeat',
      '因为': 'Because',
      '细化': 'Refine',
    }
    const ZH = Object.fromEntries(Object.keys(EN).map((key) => [key, key]))

    // ---------------------------------------------------------------- data

    const api = async (path, body) => {
      const url = 'api/eventmem?path=' + encodeURIComponent(path)
      const init = body === undefined
        ? {}
        : { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) }
      const response = await fetch(url, init)
      let data = {}
      try { data = await response.json() } catch { data = {} }
      if (!response.ok) {
        const error = new Error((data && data.error) || ('HTTP ' + response.status))
        error.status = response.status
        throw error
      }
      return data
    }

    const when = (ts) => (ts ? new Date(ts * 1000).toLocaleString(undefined, { hour12: false }) : '—')
    const num = (value) => Number(value || 0).toLocaleString()
    /** Number-or-dash for a value that may legitimately be absent (a tile before data). */
    const NI = (value) => (value === null || value === undefined || value === '' ? '—' : String(value))
    /** `{0}`-style interpolation on a translated string (the daemon's own texts use it). */
    const fill = (tr, text, ...args) => String(tr(text)).replace(/\{(\d+)\}/g,
      (whole, index) => (args[Number(index)] === undefined ? whole : String(args[Number(index)])))
    const dur = (seconds) => {
      if (seconds === null || seconds === undefined) return '—'
      if (seconds < 90) return Math.round(seconds) + 's'
      if (seconds < 5400) return Math.round(seconds / 60) + 'min'
      return (seconds / 3600).toFixed(1) + 'h'
    }
    const gb = (bytes) => (Number(bytes || 0) / 1e9).toFixed(2) + ' GB'
    const mb = (bytes) => (Number(bytes || 0) / 1e6).toFixed(Number(bytes) > 1e8 ? 0 : 1) + ' MB'

    // ---------------------------------------------------------------- style

    const CSS = `
.emc{height:100%;min-height:0;display:flex;flex-direction:column;box-sizing:border-box;
  background:var(--dsw-alias-bg-base);color:var(--dsw-alias-label-primary);font-size:13.5px}
.emc-head{flex:none;padding:28px clamp(24px,4vw,48px) 0}
[data-platform=darwin] .emc-head{padding-top:calc(28px + var(--dsh-frame-top-clearance,0px))}
.emc-head-in{max-width:none;margin:0;display:flex;align-items:flex-start;gap:16px;flex-wrap:wrap;justify-content:space-between}
.emc-title{margin:0;font-size:20px;font-weight:500;line-height:28px}
.emc-intro{margin:4px 0 0;color:var(--dsw-alias-label-secondary);font-size:13px;line-height:20px}
.emc-head-side{display:flex;flex-direction:column;align-items:flex-end;gap:8px;flex:none;padding-top:2px}
.emc-restart-status{max-width:260px;text-align:right;color:var(--dsw-alias-label-secondary);font-size:12px}
.emc-tabs{max-width:none;margin:16px 0 0;display:flex;gap:22px;border-bottom:.5px solid var(--dsw-alias-border-l3)}
.emc-tab{appearance:none;border:0;background:transparent;color:var(--dsw-alias-label-secondary);font:inherit;
  font-size:13.5px;line-height:20px;padding:8px 0 10px;cursor:pointer;position:relative;white-space:nowrap}
.emc-tab:hover{color:var(--dsw-alias-label-primary)}
.emc-tab.on{color:var(--dsw-alias-label-primary);font-weight:500}
.emc-tab:focus-visible{outline:2px solid var(--dsw-focus-ring-color,var(--dsw-alias-state-business-primary));
  outline-offset:2px;border-radius:4px}
.emc-tab.on:after{content:"";position:absolute;left:0;right:0;bottom:-.5px;height:2px;border-radius:2px;
  background:var(--dsw-alias-state-business-primary)}
.emc-body{flex:1;min-height:0;overflow:hidden auto;scrollbar-gutter:stable;
  padding:20px clamp(24px,4vw,48px) 56px}
.emc-body-in{max-width:none;margin:0;display:flex;flex-direction:column;gap:16px}
.emc-card{border:.5px solid var(--dsw-alias-settings-card-stroke);background:var(--dsw-alias-settings-card-fill);
  border-radius:var(--dsw-radius-lg);padding:14px 16px;box-sizing:border-box}
.emc-card-head{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:10px}
.emc-card-head h3{margin:0;font-size:13px;font-weight:500;color:var(--dsw-alias-label-secondary)}
.emc-tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px}
@media (max-width:640px){.emc-tiles{grid-template-columns:1fr}}
.emc-tile{border:.5px solid var(--dsw-alias-settings-card-stroke);background:var(--dsw-alias-settings-card-fill);
  border-radius:var(--dsw-radius-lg);padding:14px 16px}
.emc-tile .k{font-size:12px;color:var(--dsw-alias-label-secondary);line-height:18px}
.emc-tile .v{font-size:26px;line-height:34px;font-weight:500;font-variant-numeric:tabular-nums}
.emc-tile .s{font-size:11.5px;color:var(--dsw-alias-label-tertiary);line-height:16px;margin-top:2px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.emc-table{width:100%;border-collapse:collapse;font-size:13px}
.emc-table th{font-weight:400;color:var(--dsw-alias-label-tertiary);text-align:left;padding:0 10px 6px 0;font-size:12px}
.emc-table td{padding:7px 10px 7px 0;border-top:.5px solid var(--dsw-alias-border-l2);vertical-align:middle;
  overflow-wrap:anywhere}
.emc-table tr:first-child td{border-top:0}
.emc-table .n{text-align:right;font-variant-numeric:tabular-nums}
.emc-table .sub{color:var(--dsw-alias-label-tertiary)}
.emc-bar{height:6px;border-radius:3px;background:var(--dsw-alias-bg-layer-2);overflow:hidden}
.emc-bar > i{display:block;height:100%;border-radius:3px;background:var(--dsw-alias-brand-primary)}
.emc-btn{appearance:none;display:inline-flex;align-items:center;justify-content:center;gap:5px;height:30px;
  padding:0 12px;border-radius:var(--dsw-radius-sm);border:.5px solid var(--dsw-alias-border-l3);
  background:transparent;color:var(--dsw-alias-label-primary);font:inherit;font-size:13px;cursor:pointer;
  white-space:nowrap}
.emc-btn:hover{background:var(--dsw-alias-interactive-bg-hover)}
.emc-btn:disabled{opacity:.5;cursor:default;background:transparent}
.emc-btn.primary{background:var(--dsw-alias-button-primary-fill);color:var(--dsw-alias-label-primary-foreground);
  border-color:transparent}
.emc-btn.primary:hover{background:var(--dsw-alias-button-primary-hover)}
.emc-btn.danger{color:var(--dsw-alias-state-error-primary)}
.emc-btn.danger:hover{background:var(--dsw-alias-interactive-bg-hover-danger)}
.emc-btn.ghost{border-color:transparent;color:var(--dsw-alias-label-secondary)}
.emc-btn.sm{height:26px;padding:0 9px;font-size:12.5px}
.emc-input,.emc-select,.emc-area{box-sizing:border-box;width:100%;height:32px;padding:0 9px;font:inherit;
  font-size:13px;color:var(--dsw-alias-label-primary);background:var(--dsw-alias-bg-layer-1);
  border:.5px solid var(--dsw-alias-border-l3);border-radius:var(--dsw-radius-sm);outline:none}
.emc-input:focus,.emc-select:focus,.emc-area:focus{border-color:var(--dsw-focus-ring-color,var(--dsw-alias-state-business-primary))}
.emc-area{height:auto;min-height:120px;padding:8px 9px;resize:vertical;
  font-family:var(--dsw-font-mono,var(--ds-font-family-code,ui-monospace,SFMono-Regular,Menlo,Consolas,monospace));
  font-size:12px;line-height:1.6}
.emc-field{display:flex;flex-direction:column;gap:5px;min-width:0}
.emc-field > span{font-size:11.5px;color:var(--dsw-alias-label-secondary)}
.emc-grid{display:grid;gap:12px 14px}
.emc-grid.c2{grid-template-columns:repeat(2,minmax(0,1fr))}
.emc-grid.c3{grid-template-columns:repeat(3,minmax(0,1fr))}
@media (max-width:720px){.emc-grid.c2,.emc-grid.c3{grid-template-columns:1fr}}
.emc-row{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.emc-row .emc-input,.emc-row .emc-select{width:auto;min-width:160px;flex:1}
.emc-hint{color:var(--dsw-alias-label-tertiary);font-size:12px;line-height:18px;margin:8px 0 0}
.emc-pill{display:inline-flex;align-items:center;gap:6px;height:24px;padding:0 9px;border-radius:999px;
  border:.5px solid var(--dsw-alias-border-l3);color:var(--dsw-alias-label-secondary);font-size:11.5px;
  font-variant-numeric:tabular-nums;white-space:nowrap}
.emc-pill.ok{color:var(--dsw-alias-state-success-primary)}
.emc-pill.bad{color:var(--dsw-alias-state-error-primary)}
.emc-pill i{width:6px;height:6px;border-radius:50%;background:currentColor;display:inline-block}
.emc-chain{display:flex;flex-wrap:wrap;gap:6px}
.emc-chip{display:inline-flex;align-items:center;gap:4px;height:26px;padding:0 4px 0 9px;border-radius:var(--dsw-radius-sm);
  border:.5px solid var(--dsw-alias-border-l3);font-size:12px;background:var(--dsw-alias-bg-layer-1)}
.emc-chip .n{color:var(--dsw-alias-label-tertiary);font-variant-numeric:tabular-nums}
.emc-chip button{appearance:none;border:0;background:transparent;color:var(--dsw-alias-label-tertiary);
  cursor:pointer;font:inherit;line-height:1;padding:3px 4px;border-radius:4px}
.emc-chip button:hover{background:var(--dsw-alias-interactive-bg-hover);color:var(--dsw-alias-label-primary)}
.emc-pack{border:.5px solid var(--dsw-alias-border-l2);border-radius:var(--dsw-radius-md);padding:10px 12px;margin-top:10px}
.emc-pack:first-of-type{margin-top:0}
.emc-pack-head{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.emc-pack-name{font-weight:500}
.emc-tag{font-size:11px;color:var(--dsw-alias-label-tertiary);border:.5px solid var(--dsw-alias-border-l2);
  border-radius:4px;padding:1px 5px;line-height:15px}
.emc-tag.up{color:var(--dsw-alias-state-success-primary)}
.emc-tag.down{color:var(--dsw-alias-state-warn-primary)}
.emc-meta{color:var(--dsw-alias-label-tertiary);font-size:11.5px;line-height:17px;margin-top:5px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.emc-pack details{margin-top:8px}
.emc-pack summary{cursor:pointer;color:var(--dsw-alias-label-secondary);font-size:12px}
.emc-log{margin:0;padding:10px 12px;border-radius:var(--dsw-radius-md);background:var(--dsw-alias-bg-layer-1);
  border:.5px solid var(--dsw-alias-border-l2);
  font-family:var(--dsw-font-mono,var(--ds-font-family-code,ui-monospace,SFMono-Regular,Menlo,Consolas,monospace));
  font-size:12.5px;line-height:1.7;white-space:pre-wrap;word-break:break-word;
  max-height:calc(100vh - 300px);overflow:auto;scrollbar-gutter:stable}
.emc-log .w{color:var(--dsw-alias-state-warn-primary)}
.emc-log .e{color:var(--dsw-alias-state-error-primary)}
.emc-log .d{color:var(--dsw-alias-label-tertiary)}
.emc-empty{color:var(--dsw-alias-label-tertiary);font-size:12.5px;padding:4px 0}
.emc-banner{display:flex;align-items:flex-start;gap:10px;border:.5px solid var(--dsw-alias-state-warn-primary);
  border-radius:var(--dsw-radius-md);padding:10px 12px;font-size:12.5px;line-height:18px;
  color:var(--dsw-alias-label-primary);background:color-mix(in srgb,var(--dsw-alias-state-warn-primary) 6%,transparent)}
.emc-banner.err{border-color:var(--dsw-alias-state-error-primary);
  background:color-mix(in srgb,var(--dsw-alias-state-error-primary) 6%,transparent)}
.emc-banner .grow{flex:1;min-width:0}
.emc-banner b{font-weight:500}
.emc-modal-backdrop{position:fixed;inset:0;z-index:70;display:flex;align-items:center;justify-content:center;
  padding:24px;background:color-mix(in srgb,#000 32%,transparent)}
.emc-modal{width:min(420px,100%);box-sizing:border-box;border-radius:var(--dsw-radius-lg);
  padding:18px 20px 16px;background:var(--dsw-alias-bg-layer-3);
  border:.5px solid var(--dsw-alias-border-l3);box-shadow:var(--dsw-elevation-prominent)}
.emc-modal p{margin:0 0 16px;font-size:13.5px;line-height:21px;white-space:pre-wrap}
.emc-modal .acts{display:flex;justify-content:flex-end;gap:8px}
.emc-toast{position:fixed;left:50%;bottom:24px;transform:translateX(-50%);z-index:60;max-width:min(560px,80vw);
  padding:8px 14px;border-radius:999px;font-size:12.5px;line-height:18px;color:var(--dsw-alias-label-primary);
  background:var(--dsw-alias-bg-layer-3);border:.5px solid var(--dsw-alias-border-l3);
  box-shadow:var(--dsw-elevation-prominent)}
.emc-toast.err{border-color:var(--dsw-alias-state-error-primary)}
.emc-progress{display:flex;align-items:center;gap:10px;margin-top:8px}
.emc-progress .emc-bar{flex:1}
.emc-progress .n{font-variant-numeric:tabular-nums;color:var(--dsw-alias-label-secondary);font-size:11.5px;white-space:nowrap}
.emc-check{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--dsw-alias-label-secondary);cursor:pointer}
.emc-check input{accent-color:var(--dsw-alias-brand-primary);margin:0}
/* ---------------------------------------------------------- digits + live bar */
/* 数字翻页：每一位一扇小窗，值变了就由旧数字翻出去、新数字翻进来；千分位逗号不动。 */
.emc-flip{display:inline-flex;align-items:baseline;font-variant-numeric:tabular-nums}
.emc-flip-cell{position:relative;display:inline-block;min-width:.56em;text-align:center}
.emc-flip-cell .out{position:absolute;inset:0;transform-origin:50% 100%;
  animation:emc-flip-out .18s ease-in forwards}
.emc-flip-cell .to{display:inline-block;backface-visibility:hidden;transform-origin:50% 0;
  animation:emc-flip-in .2s .09s ease-out backwards}
@keyframes emc-flip-out{from{transform:rotateX(0);opacity:1}to{transform:rotateX(-92deg);opacity:0}}
@keyframes emc-flip-in{from{transform:rotateX(92deg);opacity:0}to{transform:rotateX(0);opacity:1}}
@media (prefers-reduced-motion:reduce){.emc-flip-cell .out{animation:none;opacity:0}
  .emc-flip-cell .to{animation:none}}
.emc-live{flex:none;display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  margin:14px clamp(24px,4vw,48px) 0;padding:7px 12px;box-sizing:border-box;font-size:12px;
  border:.5px solid var(--dsw-alias-border-l2);border-radius:var(--dsw-radius-md);
  background:color-mix(in srgb,var(--dsw-alias-brand-primary) 4%,transparent)}
.emc-live.on{background:color-mix(in srgb,var(--dsw-alias-brand-primary) 9%,transparent)}
.emc-live .dot{width:7px;height:7px;border-radius:50%;flex:none;
  background:var(--dsw-alias-label-tertiary)}
.emc-live .dot.on{background:var(--dsw-alias-state-success-primary);animation:emc-pulse 1.9s ease-out infinite}
@keyframes emc-pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--dsw-alias-state-success-primary) 55%,transparent)}
  70%{box-shadow:0 0 0 7px transparent}100%{box-shadow:0 0 0 0 transparent}}
.emc-live .state{color:var(--dsw-alias-label-secondary)}
.emc-live .backend{font-weight:600;
  font-family:var(--dsw-font-mono,var(--ds-font-family-code,ui-monospace,SFMono-Regular,Menlo,Consolas,monospace))}
.emc-live .grow{flex:1;min-width:8px}
.emc-live .counts{display:flex;align-items:center;gap:8px;font-variant-numeric:tabular-nums}
.emc-live .count{display:inline-flex;align-items:baseline;gap:3px;padding:1px 9px;border-radius:999px;
  border:.5px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-1)}
.emc-live .count .emc-flip{font-weight:600;font-size:13px;
  font-family:var(--dsw-font-mono,var(--ds-font-family-code,ui-monospace,SFMono-Regular,Menlo,Consolas,monospace))}
.emc-live .flash{width:40px;flex:none;text-align:right;font-weight:600;font-size:12px;
  color:var(--dsw-alias-state-success-primary);
  font-family:var(--dsw-font-mono,var(--ds-font-family-code,ui-monospace,SFMono-Regular,Menlo,Consolas,monospace))}
.emc-live .flash span{display:inline-block;animation:emc-float 1s ease-out forwards}
@keyframes emc-float{0%{opacity:0;transform:translateY(5px)}25%{opacity:1;transform:translateY(0)}
  100%{opacity:0;transform:translateY(-10px)}}
@media (prefers-reduced-motion:reduce){.emc-live .dot.on{animation:none}.emc-live .flash span{animation:none}}
/* --------------------------------------------------------------- graph page */
.emc-body-in.wide{max-width:none}
.emc-body.bare{padding:0;overflow:auto;display:block;min-height:0}
.emc-body.bare .emc-g{height:calc(100vh - 140px);min-height:380px;border:0;border-radius:0}
.emc-g-atom[data-hit="0"] .emc-g-body{pointer-events:none}
.emc-g{position:relative;height:calc(100vh - 262px);min-height:460px;overflow:hidden;
  border:.5px solid var(--dsw-alias-settings-card-stroke);border-radius:var(--dsw-radius-lg);
  background:var(--dsw-alias-bg-base);user-select:none;touch-action:none;cursor:grab}
.emc-g:active{cursor:grabbing}
.emc-g-stage{position:absolute;inset:0;overflow:hidden;
  background:radial-gradient(circle at 50% 42%,color-mix(in srgb,var(--dsw-alias-brand-primary) 7%,transparent),transparent 64%),
             radial-gradient(circle at 50% 50%,transparent 38%,color-mix(in srgb,var(--dsw-alias-bg-base) 62%,transparent) 100%)}
.emc-g-canvas{position:absolute;inset:0;width:100%;height:100%;pointer-events:none}
.emc-g-back{z-index:1}
.emc-g-front{z-index:25000}
.emc-g-atoms{position:absolute;inset:0;pointer-events:none}
.emc-g-atom{position:absolute;left:0;top:0;width:168px;height:54px;pointer-events:none;
  contain:layout style paint}
.emc-g-body{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);overflow:hidden;box-sizing:border-box;
  pointer-events:auto;cursor:pointer;
  width:calc((18px - 12px * var(--f,0)) + 150px * var(--m,0));
  height:calc((18px - 12px * var(--f,0)) + 36px * var(--m,0));
  border-radius:calc(9px - var(--m,0) * 1px);
  background:color-mix(in srgb,var(--em-kind,var(--dsw-alias-label-secondary)) calc(var(--f,0) * 100%),var(--dsw-alias-bg-layer-1));
  border-style:solid;border-width:1px;
  border-color:color-mix(in srgb,var(--em-kind,var(--dsw-alias-label-secondary)) calc((1 - var(--m,0)) * 100%),var(--dsw-alias-border-l3));
  border-left-width:calc(1px + 2px * var(--m,0));
  border-left-color:var(--em-kind,var(--dsw-alias-label-secondary))}
.emc-g-atom:hover .emc-g-body{box-shadow:0 6px 18px color-mix(in srgb,#000 26%,transparent)}
.emc-g-atom[data-sel="1"] .emc-g-body{box-shadow:0 0 0 1.5px var(--dsw-alias-state-business-primary),
  0 10px 26px color-mix(in srgb,#000 32%,transparent)}
.emc-g-atom[data-multi="1"] .emc-g-body{outline:2px dashed var(--dsw-alias-state-business-primary);
  outline-offset:2px}
.emc-g-atom[data-edited="1"] .emc-g-body::after{content:'';position:absolute;inset:-1px;border-radius:inherit;
  padding:2px;pointer-events:none;
  background:conic-gradient(from 0deg,#ff5f6d,#ffc371,#a8e063,#56ccf2,#9b73ff,#ff5f6d);
  -webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);
  -webkit-mask-composite:xor;mask-composite:exclude;animation:emc-g-hue 7s linear infinite}
@keyframes emc-g-hue{to{filter:hue-rotate(360deg)}}
.emc-g-num{position:absolute;left:0;right:0;top:50%;transform:translateY(-50%);text-align:center;
  font:700 8px/1 var(--dsw-font-mono,var(--ds-font-family-code,ui-monospace,monospace));
  color:var(--dsw-alias-label-secondary);
  opacity:calc((1 - var(--m,0)) * (1 - var(--f,0)));pointer-events:none}
.emc-g-card{position:absolute;left:50%;top:50%;width:164px;height:52px;box-sizing:border-box;padding:5px 8px;
  pointer-events:none;transform:translate(-50%,-50%) scale(var(--sc,1));transform-origin:50% 50%;
  opacity:clamp(0,calc((var(--m,0) - 0.34) * 1.8),1)}
/* The card scales with the box, so its text is scaled too. Without its own layer Chrome
   re-rasterizes that text on every frame of the animation; on its own layer the scale is
   a compositor transform. Promoted only while the card actually shows (m > 0.3): promoting
   all 500+ cards would cost more memory than it saves. */
.emc-g-atom[data-card="1"] .emc-g-card{will-change:transform;backface-visibility:hidden}
.emc-g-chead{display:flex;align-items:center;gap:5px;font-size:9px;line-height:1.25;letter-spacing:.01em}
.emc-g-cid{font-family:var(--dsw-font-mono,var(--ds-font-family-code,ui-monospace,monospace));
  font-weight:700;color:var(--dsw-alias-label-primary)}
.emc-g-ckind{font-weight:600;color:var(--em-kind,var(--dsw-alias-label-secondary))}
.emc-g-ctime{margin-left:auto;color:var(--dsw-alias-label-tertiary);font-variant-numeric:tabular-nums;white-space:nowrap}
.emc-g-ctext{margin-top:3px;font-size:9.5px;line-height:1.4;color:var(--dsw-alias-label-secondary);
  word-break:break-word;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.emc-g-lasso{position:absolute;display:none;z-index:30000;pointer-events:none;
  border:1px dashed var(--dsw-alias-state-business-primary);
  background:color-mix(in srgb,var(--dsw-alias-state-business-primary) 10%,transparent)}
.emc-g-top{position:absolute;inset:0;z-index:40000;pointer-events:none}
.emc-g-top > *{pointer-events:auto}
.emc-g-bar{position:absolute;left:10px;right:10px;top:10px;z-index:40000;display:flex;flex-wrap:wrap;
  align-items:center;gap:6px;padding:6px 8px;border-radius:var(--dsw-radius-md);pointer-events:auto;
  background:color-mix(in srgb,var(--dsw-alias-bg-layer-1) 86%,transparent);
  border:.5px solid var(--dsw-alias-border-l3);backdrop-filter:blur(12px)}
.emc-g-brand{font-weight:600;letter-spacing:.02em;margin-right:2px}
.emc-g-seg{display:inline-flex;border:.5px solid var(--dsw-alias-border-l3);border-radius:var(--dsw-radius-sm);overflow:hidden}
.emc-g-seg-btn{font-size:11px;padding:3px 8px;line-height:1.35;color:var(--dsw-alias-label-secondary);
  background:transparent;border:0;cursor:pointer;font-family:inherit}
.emc-g-seg-btn:hover{background:var(--dsw-alias-interactive-bg-hover);color:var(--dsw-alias-label-primary)}
.emc-g-seg-btn[data-on="1"]{background:color-mix(in srgb,var(--dsw-alias-state-business-primary) 20%,transparent);
  color:var(--dsw-alias-label-primary)}
.emc-g-input{height:24px;box-sizing:border-box;font:inherit;font-size:11.5px;padding:0 7px;
  border-radius:var(--dsw-radius-sm);background:var(--dsw-alias-bg-layer-2);
  color:var(--dsw-alias-label-primary);border:.5px solid var(--dsw-alias-border-l3);outline:none}
.emc-g-input:focus{border-color:var(--dsw-focus-ring-color,var(--dsw-alias-state-business-primary))}
.emc-g-sem{display:flex;align-items:center;gap:4px}
.emc-g-seminput{width:190px}
.emc-g-semtag{display:inline-flex;align-items:center;gap:3px;font-size:11px;color:var(--dsw-alias-label-secondary);
  border:.5px solid var(--dsw-alias-border-l2);border-radius:999px;padding:1px 4px 1px 7px;max-width:200px;
  overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.emc-g-semtag button{appearance:none;border:0;background:transparent;color:var(--dsw-alias-label-tertiary);
  cursor:pointer;font:inherit;line-height:1;padding:2px 3px;border-radius:4px}
.emc-g-semtag button:hover{background:var(--dsw-alias-interactive-bg-hover);color:var(--dsw-alias-label-primary)}
.emc-g-count{font-size:11.5px;color:var(--dsw-alias-label-tertiary);font-variant-numeric:tabular-nums;white-space:nowrap}
.emc-g-spacer{flex:1 1 auto}
.emc-g-fbtn{appearance:none;display:inline-flex;align-items:center;gap:5px;height:24px;padding:0 9px;
  font:inherit;font-size:11.5px;border-radius:var(--dsw-radius-sm);
  border:.5px solid var(--dsw-alias-border-l3);background:transparent;color:var(--dsw-alias-label-secondary);
  cursor:pointer;white-space:nowrap}
.emc-g-fbtn:hover{background:var(--dsw-alias-interactive-bg-hover);color:var(--dsw-alias-label-primary)}
.emc-g-fbtn[data-on="1"]{background:color-mix(in srgb,var(--dsw-alias-state-business-primary) 20%,transparent);
  color:var(--dsw-alias-label-primary)}
.emc-g-fbadge{display:inline-flex;align-items:center;justify-content:center;min-width:14px;height:14px;
  padding:0 4px;border-radius:999px;font-size:10px;line-height:1;
  background:var(--dsw-alias-state-business-primary);color:var(--dsw-alias-label-primary-foreground)}
.emc-g-panel{position:absolute;left:10px;top:52px;z-index:40000;width:min(380px,calc(100% - 20px));
  max-height:calc(100% - 62px);overflow:auto;overscroll-behavior:contain;box-sizing:border-box;padding:12px 14px;
  border-radius:var(--dsw-radius-md);background:var(--dsw-alias-bg-layer-1);
  border:.5px solid var(--dsw-alias-border-l3);box-shadow:var(--dsw-elevation-prominent);
  display:flex;flex-direction:column;gap:10px}
.emc-g-panel h4{margin:0;font-size:11.5px;font-weight:500;color:var(--dsw-alias-label-secondary)}
.emc-g-chips{display:flex;flex-wrap:wrap;gap:5px}
.emc-g-chip{display:inline-flex;align-items:center;gap:5px;height:22px;padding:0 8px;font:inherit;font-size:11.5px;
  border-radius:999px;border:.5px solid var(--dsw-alias-border-l3);background:transparent;
  color:var(--dsw-alias-label-secondary);cursor:pointer;white-space:nowrap}
.emc-g-chip:hover{background:var(--dsw-alias-interactive-bg-hover)}
.emc-g-chip[data-on="1"]{background:color-mix(in srgb,var(--dsw-alias-label-primary) 10%,transparent);
  color:var(--dsw-alias-label-primary);border-color:var(--dsw-alias-label-tertiary)}
.emc-g-chip .dot{width:7px;height:7px;border-radius:50%;flex:none}
.emc-g-chip .line{width:12px;height:2px;border-radius:1px;flex:none}
.emc-g-legend{position:absolute;left:10px;bottom:10px;z-index:40000;display:flex;flex-wrap:wrap;align-items:center;
  gap:10px;padding:5px 9px;border-radius:var(--dsw-radius-md);pointer-events:none;
  background:color-mix(in srgb,var(--dsw-alias-bg-layer-1) 80%,transparent);
  border:.5px solid var(--dsw-alias-border-l2);font-size:11px;color:var(--dsw-alias-label-secondary)}
.emc-g-legend span.item{display:inline-flex;align-items:center;gap:4px;white-space:nowrap}
.emc-g-legend .dot{width:7px;height:7px;border-radius:50%}
.emc-g-legend .line{width:12px;height:2px;border-radius:1px}
.emc-g-insp{position:absolute;right:10px;top:52px;bottom:10px;z-index:40000;width:min(344px,calc(100% - 20px));
  box-sizing:border-box;display:flex;flex-direction:column;gap:10px;padding:12px 14px;overflow:auto;
  border-radius:var(--dsw-radius-md);background:color-mix(in srgb,var(--dsw-alias-bg-layer-1) 96%,transparent);
  border:.5px solid var(--dsw-alias-border-l3);box-shadow:var(--dsw-elevation-prominent)}
.emc-g-insp-head{display:flex;align-items:center;gap:8px}
.emc-g-insp-title{font-size:14px;font-weight:500}
.emc-g-insp-sub{font-size:11.5px;color:var(--dsw-alias-label-tertiary)}
.emc-g-x{appearance:none;margin-left:auto;border:0;background:transparent;color:var(--dsw-alias-label-tertiary);
  font:inherit;font-size:13px;line-height:1;padding:3px 5px;border-radius:4px;cursor:pointer}
.emc-g-x:hover{background:var(--dsw-alias-interactive-bg-hover);color:var(--dsw-alias-label-primary)}
.emc-g-meta{display:flex;flex-direction:column;gap:3px;font-size:11.5px;line-height:17px;
  color:var(--dsw-alias-label-tertiary)}
.emc-g-edited{color:var(--dsw-alias-state-business-primary)}
.emc-g-field{display:flex;flex-direction:column;gap:4px;font-size:11.5px}
.emc-g-field > span{color:var(--dsw-alias-label-secondary)}
.emc-g-textarea{box-sizing:border-box;width:100%;min-height:96px;padding:7px 8px;resize:vertical;font:inherit;
  font-size:12.5px;line-height:1.55;color:var(--dsw-alias-label-primary);
  background:var(--dsw-alias-bg-layer-2);border:.5px solid var(--dsw-alias-border-l3);
  border-radius:var(--dsw-radius-sm);outline:none}
.emc-g-textarea:focus{border-color:var(--dsw-focus-ring-color,var(--dsw-alias-state-business-primary))}
.emc-g-actions{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.emc-g-note{font-size:11.5px;line-height:17px;color:var(--dsw-alias-label-secondary);
  background:var(--dsw-alias-bg-layer-2);border-radius:var(--dsw-radius-sm);padding:6px 8px}
.emc-g-err{font-size:11.5px;line-height:17px;color:var(--dsw-alias-state-error-primary)}
.emc-g-section{display:flex;flex-direction:column;gap:6px;border-top:.5px solid var(--dsw-alias-border-l2);
  padding-top:8px;margin-top:2px}
.emc-g-section-title{font-size:11.5px;color:var(--dsw-alias-label-secondary)}
.emc-g-edges{display:flex;flex-direction:column;gap:2px}
.emc-g-edge-row{appearance:none;display:flex;align-items:center;gap:6px;width:100%;box-sizing:border-box;
  padding:4px 5px;border:0;border-radius:4px;background:transparent;color:var(--dsw-alias-label-primary);
  font:inherit;font-size:11.5px;text-align:left;cursor:pointer}
.emc-g-edge-row:hover{background:var(--dsw-alias-interactive-bg-hover)}
.emc-g-edge-row .dot{width:7px;height:7px;border-radius:50%;flex:none}
.emc-g-edge-row .rel{font-weight:500}
.emc-g-edge-row .id{margin-left:auto;color:var(--dsw-alias-label-tertiary);
  font-family:var(--dsw-font-mono,var(--ds-font-family-code,ui-monospace,monospace))}
.emc-g-endpoint{appearance:none;display:flex;flex-direction:column;gap:2px;width:100%;box-sizing:border-box;
  text-align:left;padding:6px 8px;border-radius:var(--dsw-radius-sm);cursor:pointer;font:inherit;
  border:.5px solid var(--dsw-alias-border-l2);background:var(--dsw-alias-bg-layer-2);
  color:var(--dsw-alias-label-primary)}
.emc-g-endpoint:hover{border-color:var(--dsw-alias-border-l3)}
.emc-g-endpoint .head{display:flex;align-items:center;gap:6px;font-size:11px}
.emc-g-endpoint .id{font-family:var(--dsw-font-mono,var(--ds-font-family-code,ui-monospace,monospace);
  font-weight:600}
.emc-g-endpoint .role{margin-left:auto;color:var(--dsw-alias-label-tertiary)}
.emc-g-endpoint .text{font-size:11.5px;line-height:16px;color:var(--dsw-alias-label-secondary);
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.emc-g-prob{display:flex;flex-direction:column;gap:3px;font-size:11px}
.emc-g-prob-title{color:var(--dsw-alias-label-secondary)}
.emc-g-prob-row{display:grid;grid-template-columns:52px 1fr 42px;align-items:center;gap:6px}
.emc-g-prob-row[data-hi="1"]{color:var(--dsw-alias-label-primary);font-weight:500}
.emc-g-prob-row .track{height:4px;border-radius:2px;background:var(--dsw-alias-bg-layer-2);overflow:hidden}
.emc-g-prob-row .fill{display:block;height:100%;border-radius:2px}
.emc-g-prob-row .num{text-align:right;font-variant-numeric:tabular-nums;color:var(--dsw-alias-label-tertiary)}
.emc-g-center{position:absolute;inset:0;display:grid;place-items:center;padding:24px;text-align:center}
.emc-g-msg{display:flex;flex-direction:column;align-items:center;gap:10px;max-width:420px}
.emc-g-msg .t{font-size:14px;font-weight:500}
.emc-g-msg .b{font-size:12px;line-height:18px;color:var(--dsw-alias-label-secondary);word-break:break-word}
.emc-btn.on{border-color:var(--dsw-alias-state-business-primary);background:var(--dsw-alias-interactive-bg-hover);color:var(--dsw-alias-label-primary)}
.emc-setup{display:flex;flex-direction:column;gap:12px;max-width:760px}
.emc-setup-checks{display:flex;flex-direction:column;gap:1px;border:.5px solid var(--dsw-alias-border-l2);
  border-radius:var(--dsw-radius-sm);overflow:hidden;background:var(--dsw-alias-border-l2)}
.emc-setup-check{display:grid;grid-template-columns:8px 116px 1fr;align-items:start;gap:8px;
  padding:7px 9px;background:var(--dsw-alias-bg-layer-2);font-size:11.5px;line-height:17px}
.emc-setup-check .dot{width:7px;height:7px;border-radius:50%;margin-top:5px;background:var(--dsw-alias-label-tertiary)}
.emc-setup-check.ok .dot{background:var(--dsw-alias-state-success-primary,var(--dsw-alias-state-business-primary))}
.emc-setup-check.warn .dot{background:var(--dsw-alias-state-warning-primary,var(--dsw-alias-label-tertiary))}
.emc-setup-check.bad .dot{background:var(--dsw-alias-state-error-primary)}
.emc-setup-check .label{font-weight:500}
.emc-setup-check .detail{color:var(--dsw-alias-label-secondary);word-break:break-word}
.emc-setup-form{display:flex;flex-direction:column;gap:10px}
.emc-setup-seg{display:flex;gap:6px;flex-wrap:wrap}
.emc-setup-code{font-family:var(--dsw-font-mono,var(--ds-font-family-code,ui-monospace,monospace));
  font-size:11.5px;line-height:17px;white-space:pre-wrap;word-break:break-word;color:var(--dsw-alias-label-secondary)}
`

    // ------------------------------------------------------------- widgets

    function Btn({ children, onClick, kind, disabled, title, style }) {
      const cls = 'emc-btn' + (kind ? ' ' + kind : '')
      return h('button', { type: 'button', className: cls, onClick, disabled, title, style }, children)
    }

    function Pill({ ok, children }) {
      return h('span', { className: 'emc-pill ' + (ok ? 'ok' : 'bad') }, h('i'), children)
    }

    function Field({ label, children }) {
      return h('label', { className: 'emc-field' }, h('span', null, label), children)
    }

    function Card({ title, actions, children }) {
      return h('section', { className: 'emc-card' },
        title || actions
          ? h('div', { className: 'emc-card-head' }, h('h3', null, title), actions || null)
          : null,
        children)
    }

    function Bar({ ratio }) {
      const width = Math.max(0, Math.min(1, Number(ratio) || 0)) * 100
      return h('div', { className: 'emc-bar' }, h('i', { style: { width: width + '%' } }))
    }

    /**
     * 数字翻页 (ported from the Hermes console). Only the cells whose glyph changed flip:
     * digits are compared right-to-left, so `1,299 → 1,300` flips three cells and leaves
     * the comma alone. The tile stays one inline run, so baseline and font-size are the
     * host's.
     */
    function FlipDigits({ value }) {
      const text = typeof value === 'number' ? value.toLocaleString('en-US') : NI(value)
      const shown = useRef(text)
      const [flip, setFlip] = useState(null)

      useEffect(() => {
        const before = shown.current
        shown.current = text
        if (before === text) return undefined
        const width = Math.max(before.length, text.length)
        const a = before.padStart(width, ' ')
        const b = text.padStart(width, ' ')
        const cells = []
        for (let index = 0; index < width; index += 1) {
          if (b[index] === ' ') continue
          cells.push({ to: b[index], from: a[index] === b[index] ? '' : (a[index] === ' ' ? '' : a[index]) })
        }
        setFlip({ cells, at: Date.now() })
        const timer = window.setTimeout(() => setFlip(null), 460)
        return () => window.clearTimeout(timer)
      }, [text])

      const cells = flip ? flip.cells : [...text].map((character) => ({ to: character, from: '' }))
      return h('span', { className: 'emc-flip' },
        cells.map((cell, index) => h('span', { key: index, className: 'emc-flip-cell' },
          cell.from ? h('span', { className: 'out' }, cell.from) : null,
          h('span', { className: 'to' }, cell.to))))
    }

    function Empty({ children }) {
      return h('div', { className: 'emc-empty' }, children)
    }

    function Notice({ text, kind }) {
      if (!text) return null
      return h('div', { className: 'emc-banner' + (kind === 'err' ? ' err' : ''), role: 'alert' },
        h('span', { className: 'grow' }, text))
    }

    function useToast(t) {
      const [toast, setToast] = useState(null)
      const timer = useRef(null)
      const notify = useCallback((text, kind) => {
        setToast({ text: String(text), kind: kind || 'ok' })
        if (timer.current) clearTimeout(timer.current)
        timer.current = setTimeout(() => setToast(null), kind === 'err' ? 7000 : 3200)
      }, [])
      useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])
      return [toast, notify, t]
    }

    /**
     * Host-styled confirmation. A native `window.confirm` freezes the renderer and ignores
     * the theme, so destructive actions use this modal (Escape cancels, Enter confirms).
     */
    function useConfirm(t) {
      const [ask, setAsk] = useState(null)
      const confirm = useCallback((message) => new Promise((resolve) => {
        setAsk({ message, resolve })
      }), [])
      const settle = useCallback((answer) => {
        setAsk((current) => {
          if (current) current.resolve(answer)
          return null
        })
      }, [])
      useEffect(() => {
        if (!ask) return undefined
        const onKey = (event) => {
          if (event.key === 'Escape') { event.preventDefault(); settle(false) }
          if (event.key === 'Enter') { event.preventDefault(); settle(true) }
        }
        window.addEventListener('keydown', onKey, true)
        return () => window.removeEventListener('keydown', onKey, true)
      }, [ask, settle])
      const dialog = ask
        ? h('div', { className: 'emc-modal-backdrop', onClick: () => settle(false) },
            h('div', {
              className: 'emc-modal', role: 'dialog', 'aria-modal': true, 'aria-label': t('确认操作'),
              onClick: (event) => event.stopPropagation(),
            },
            h('p', null, ask.message),
            h('div', { className: 'acts' },
              h(Btn, { onClick: () => settle(false), autoFocus: true }, t('取消')),
              h(Btn, { kind: 'danger', onClick: () => settle(true) }, t('确认')))))
        : null
      return [confirm, dialog]
    }

    // ------------------------------------------------------------- live strip

    /**
     * The pass's own ticker, on every tab: which backend is answering, how many pairs it
     * has judged in *this* pass, and how long it has been at it. Each answered pair
     * flashes `+N` — N is the increment since the previous poll, so a slow poll shows
     * `+2` instead of lying about `+1`.
     */
    function LiveBar({ t, status, notify }) {
      const live = (status && status.live) || null
      const busy = Boolean(live) || Boolean(status && status.busy)
      const paused = Boolean(status && status.paused)
      const pairs = live ? live.pairs || 0 : 0
      const edges = live ? live.edges || 0 : 0
      const pairNow = live ? live.pairs || 0 : null
      const previous = useRef(null)
      const [flash, setFlash] = useState(null)

      useEffect(() => {
        const before = previous.current
        previous.current = pairNow
        if (before === null || pairNow === null || pairNow <= before) return undefined
        setFlash({ n: pairNow - before, at: Date.now() })
        const timer = window.setTimeout(() => setFlash(null), 1000)
        return () => window.clearTimeout(timer)
      }, [pairNow])

      const packs = (status && status.backends) || []
      const active = packs.find((pack) => pack.name === ((live && live.backend) || '')) || null
      const runs = (status && status.runs) || []
      const last = runs[runs.length - 1] || null
      const elapsed = live && live.since ? Math.max(0, Date.now() / 1000 - live.since) : 0

      const switchBackend = async () => {
        try {
          await api('switch', {})
          notify(t('已请求切换：本轮停下后立刻重跑，重新探活链路'))
        } catch (error) {
          notify(error.message, 'err')
        }
      }

      return h('div', { className: 'emc-live' + (busy ? ' on' : '') },
        h('span', { className: 'dot' + (busy ? ' on' : '') }),
        h('span', { className: 'state' },
          paused ? (busy ? t('正在暂停…') : t('已暂停')) : busy ? t('正在使用') : t('空闲')),
        h('span', { className: 'backend' }, (live && live.backend) || (last && last.backend) || '—'),
        active ? h('span', { className: 'emc-tag' }, active.kind) : null,
        paused ? h('span', { className: 'emc-tag down' }, t('暂停中')) : null,
        h('span', { className: 'grow' }),
        busy
          ? h('span', { className: 'counts' },
              // A fixed slot: the flash comes and goes, the numbers must not move.
              h('span', { className: 'flash' }, flash ? h('span', { key: flash.at }, '+' + flash.n) : null),
              h('span', { className: 'count' }, h(FlipDigits, { value: pairs }), ' ' + t('对')),
              h('span', { className: 'count' }, h(FlipDigits, { value: edges }), ' ' + t('边')),
              h('span', { className: 'emc-hint', style: { margin: 0 } }, dur(elapsed)))
          : h('span', { className: 'emc-hint', style: { margin: 0 } },
              last
                ? fill(t, '上次 {0} · {1} · {2} 对 / {3} 边', last.backend || '—', when(last.ts), num(last.pairs), num(last.edges))
                : t('还没跑过')),
        h(Btn, { kind: 'sm', disabled: !status, onClick: switchBackend }, t('切换并重跑')))
    }

    // ------------------------------------------------------------- overview

    function Overview({ t, status, notify }) {
      const [stats, setStats] = useState(null)
      const [config, setConfig] = useState(null)
      const [error, setError] = useState('')
      const [confirm, confirmDialog] = useConfirm(t)
      const live = Boolean(status && status.live)

      const load = useCallback(async () => {
        try {
          const [nextStats, nextConfig] = await Promise.all([api('stats'), api('config')])
          setStats(nextStats)
          setConfig(nextConfig)
          setError('')
        } catch (e) {
          setError(e.message)
        }
      }, [])

      useEffect(() => {
        let alive = true
        let timer = null
        const tick = async () => {
          await load()
          if (alive) timer = setTimeout(tick, live ? 2000 : 5000)
        }
        tick()
        return () => { alive = false; if (timer) clearTimeout(timer) }
      }, [load, live])

      const clearNotices = async () => {
        try {
          const r = await api('notices/clear', {})
          notify(t('已清除 {n} 条抽取失败警告', { n: r.cleared || 0 }))
          load()
        } catch (e) { notify(e.message, 'err') }
      }
      const clearEdges = async () => {
        if (!(await confirm(t('确认清空全部边和判过的对？') + '\n' + t('删除后不可恢复。')))) return
        try {
          const r = await api('edges/clear', {})
          notify(t('已清空 {edges} 条边、{pairs} 对判定记录，游标归零', {
            edges: num(r.edges), pairs: num(r.pairs),
          }))
          load()
        } catch (e) { notify(e.message, 'err') }
      }

      if (!stats) {
        return h('div', { className: 'emc-body-in' },
          error ? h(Notice, { text: error, kind: 'err' }) : h(Empty, null, '…'))
      }

      const kinds = stats.kinds || {}
      const kindEntries = Object.entries(kinds)
      const kindMax = Math.max(1, ...kindEntries.map(([, n]) => n))
      const kindTotal = kindEntries.reduce((sum, [, n]) => sum + n, 0) || 1
      const failures = stats.failures || { count: 0, last: [] }
      const cursor = (status && status.cursor) || {}
      const runs = ((status && status.runs) || []).slice().reverse()
      const chain = (config && config.effective && config.effective.decision && config.effective.decision.backends) || []

      // 待判 = 本轮预计要问的对数 − 已问。planned 在这一轮开始时只读算出来（见 nightly.plan），
      // 写边会退休链头，真实数量只会更少，所以标「预计」。没有 pass 在跑时退回游标口径。
      const run = (status && status.live) || null
      const planned = run && typeof run.pairs_planned === 'number' ? run.pairs_planned : null
      const pendingLeft = run ? (planned === null ? null : Math.max(0, planned - (run.pairs || 0))) : null
      const walked = run ? run.event_index || 0 : 0
      const walkTotal = run ? run.events_total || 0 : 0
      const pendingSub = run
        ? planned === null
          ? (walkTotal
              ? fill(t, '第 {0}/{1} 个事件 · 正在数这一轮还要问多少对…', walked, walkTotal)
              : t('正在准备这一轮…'))
          : fill(t, '第 {0}/{1} 个事件 · 已判 {2} 对（预计）', walked, walkTotal, num(run.pairs))
        : (cursor.behind === null || cursor.behind === undefined)
          ? t('无游标')
          : fill(t, '空闲 · 游标落后 {0} 个事件', num(cursor.events))

      return h('div', { className: 'emc-body-in' },
        error ? h(Notice, { text: error, kind: 'err' }) : null,

        h('div', { className: 'emc-tiles' },
          h('div', { className: 'emc-tile' },
            h('div', { className: 'k' }, t('事件')),
            h('div', { className: 'v' }, h(FlipDigits, { value: stats.events })),
            h('div', { className: 's' }, stats.newest ? `${t('最新')} ${when(stats.newest)}` : t('空库'))),
          h('div', { className: 'emc-tile' },
            h('div', { className: 'k' }, `${t('边')} / ${t('对已判')}`),
            h('div', { className: 'v' }, h(FlipDigits, { value: stats.edges })),
            h('div', { className: 's' }, `${num(stats.pairs)} ${t('对已判')}`)),
          h('div', { className: 'emc-tile' },
            h('div', { className: 'k' }, t('待判')),
            h('div', { className: 'v' }, h(FlipDigits, { value: pendingLeft })),
            h('div', { className: 's' }, pendingSub))),

        failures.count
          ? h(Card, {
              title: `${t('回合记忆抽取失败')} (${failures.count})`,
              actions: h(Btn, { kind: 'sm', onClick: clearNotices }, t('清除')),
            },
            h('p', { className: 'emc-hint', style: { marginTop: 0 } },
              t('这些回合没有写进任何记忆：抽取模型没给出可用的 0–3 条事件，系统不会再用机械摘要兜底。修好模型后点「清除」。')),
            h('table', { className: 'emc-table' },
              h('tbody', null, (failures.last || []).map((row, index) =>
                h('tr', { key: index },
                  h('td', { className: 'sub' }, when(row.ts)),
                  h('td', null, row.reason === 'unparsable' ? t('输出无法解析') : t('调用失败')),
                  h('td', { className: 'sub' }, row.detail || '—'),
                  h('td', { className: 'sub' }, String(row.session || '—').slice(0, 12)))))))
          : null,

        h('div', { className: 'emc-grid c2', style: { alignItems: 'start' } },
          h(Card, { title: t('kind 分布') },
            kindEntries.length
              ? h('table', { className: 'emc-table' }, h('tbody', null, kindEntries.map(([kind, n]) =>
                h('tr', { key: kind },
                  h('td', { style: { width: '38%' } }, kind),
                  h('td', null, h(Bar, { ratio: n / kindMax })),
                  h('td', { className: 'n' }, num(n)),
                  h('td', { className: 'n sub' }, Math.round((n / kindTotal) * 100) + '%')))))
              : h(Empty, null, t('还没有事件'))),
          h(Card, { title: t('项目（按事件数）') },
            (stats.projects || []).length
              ? h('table', { className: 'emc-table' }, h('tbody', null, (stats.projects || []).map((project) =>
                h('tr', { key: project.name },
                  h('td', null, project.name),
                  h('td', { className: 'n' }, num(project.events)),
                  h('td', { className: 'n sub' }, when(project.last))))))
              : h(Empty, null, t('还没有项目')))),

        h(Card, { title: t('最近几轮 pass') },
          runs.length
            ? h('table', { className: 'emc-table' },
                h('thead', null, h('tr', null,
                  h('th', null, ''),
                  h('th', null, ''),
                  h('th', { className: 'n' }, t('已判')),
                  h('th', { className: 'n' }, t('边')),
                  h('th', null, t('后端')))),
                h('tbody', null, runs.map((run, index) =>
                  h('tr', { key: index },
                    h('td', { className: 'sub' }, when(run.ts)),
                    h('td', null, run.skipped ? String(run.skipped) : t('完成')),
                    h('td', { className: 'n' }, run.pairs === undefined ? '—' : num(run.pairs)),
                    h('td', { className: 'n' }, run.edges === undefined ? '—' : num(run.edges)),
                    h('td', { className: 'sub' }, run.backend || '')))))
            : h(Empty, null, t('这一进程还没跑过 pass'))),

        h(Card, { title: t('后端链路') },
          h('div', { className: 'emc-chain' },
            chain.length
              ? chain.map((name, index) => h('span', { className: 'emc-chip', key: name },
                  h('span', { className: 'n' }, index + 1 + '.'), name))
              : h('span', { className: 'emc-hint', style: { margin: 0 } }, t('空（用 decision.url 单端点）'))),
          h('p', { className: 'emc-hint' }, t('顺序即回退顺序：一轮开始时逐项探活，第一个活着的负责整轮。'))),

        h(Card, { title: t('连边数据') },
          h('p', { className: 'emc-hint', style: { marginTop: 0 } },
            t('清空会删掉全部边和「判过的对」并把游标归零：下一轮 pass 从最早的事件重新判。')),
          h('div', { className: 'emc-row' }, h(Btn, { kind: 'danger', onClick: clearEdges }, t('清空连边并重跑')))),
        confirmDialog)
    }

    // --------------------------------------------------------------- setup

    /**
     * The first-run page: what works, what is optional, and the two fields that actually
     * differ between a single-machine install and a remote one. It writes through the
     * adapter's `setup/save` route, which uses the profile config editor — no YAML editing.
     */
    function Setup({ t, setup, testResult, busy, message, onTest, onSave, onRefresh }) {
      const config = (setup && setup.config) || {}
      const [draft, setDraft] = useState({
        mode: config.mode || 'local',
        baseUrl: config.baseUrl || 'http://127.0.0.1:9123',
        tunnelHost: config.tunnelHost || '',
        expectDbId: config.expectDbId || '',
        tokenFile: config.tokenFile || '',
      })
      const synced = useRef('')

      useEffect(() => {
        if (!setup || !setup.config) return
        // Do not overwrite what the user is typing on every health poll; only resync when
        // the saved endpoint changes (a save, or a profile edit made elsewhere).
        const key = [setup.config.mode, setup.config.baseUrl, setup.config.tunnelHost,
          setup.config.expectDbId, setup.config.tokenFile].join('|')
        if (synced.current === key) return
        synced.current = key
        setDraft({
          mode: setup.config.mode || 'local',
          baseUrl: setup.config.baseUrl || 'http://127.0.0.1:9123',
          tunnelHost: setup.config.tunnelHost || '',
          expectDbId: setup.config.expectDbId || '',
          tokenFile: setup.config.tokenFile || '',
        })
      }, [setup])

      const checks = (testResult && testResult.checks && testResult.checks.length)
        ? testResult.checks
        : ((setup && setup.checks) || [])
      const change = (key) => (event) => setDraft((prev) => ({ ...prev, [key]: event.target.value }))
      const remote = draft.mode === 'remote'

      return h('div', { className: 'emc-setup' },
        h(Card, { title: t('开始使用 eventmem'),
          actions: h(Btn, { kind: 'sm', onClick: onRefresh, disabled: busy !== '' }, t('重新检查')) },
        h('p', { className: 'emc-hint', style: { marginTop: 0 } },
          t('适配器已加载。下面是本机与 daemon 的依赖状态；有不可用项时，按提示修正后重试。')),
        h('div', { className: 'emc-setup-checks' }, checks.map((item) =>
          h('div', { key: item.id, className: 'emc-setup-check ' + (item.state || 'warn') },
            h('span', { className: 'dot' }),
            h('span', { className: 'label' }, t(item.label)),
            h('span', { className: 'detail' }, item.detail || '')))),
        setup && setup.daemon
          ? h('p', { className: 'emc-hint', style: { margin: 0 } },
              [setup.daemon.db ? t('数据库') + ': ' + setup.daemon.db : '',
                setup.daemon.dbId ? 'id ' + setup.daemon.dbId : '',
                setup.daemon.schema !== undefined ? 'schema ' + setup.daemon.schema : ''].filter(Boolean).join(' · '))
          : null,
        setup && setup.error ? h(Notice, { text: setup.error, kind: 'err' }) : null,
        testResult && testResult.error ? h(Notice, { text: testResult.error, kind: 'err' }) : null,
        testResult && testResult.ok ? h(Notice, { text: t('连接可用。保存后插件会按这份配置重载。') }) : null,
        message ? h(Notice, { text: message }) : null),
        h('div', { className: 'emc-setup-form' },
          h('div', { className: 'emc-setup-seg' },
            h('button', { type: 'button', className: 'emc-btn' + (!remote ? ' on' : ''),
              onClick: () => setDraft((prev) => ({ ...prev, mode: 'local' })) }, t('本机记忆')),
            h('button', { type: 'button', className: 'emc-btn' + (remote ? ' on' : ''),
              onClick: () => setDraft((prev) => ({ ...prev, mode: 'remote' })) }, t('远程记忆'))),
          h('p', { className: 'emc-hint', style: { margin: 0 } },
            remote ? t('通过 SSH 隧道连接另一台机器上的 daemon。') : t('使用本机 127.0.0.1 上的 daemon（推荐）。')),
          h(Field, { label: t('daemon 地址') },
            h('input', { className: 'emc-input', value: draft.baseUrl, onChange: change('baseUrl'),
              spellCheck: false, placeholder: 'http://127.0.0.1:9123' })),
          remote
            ? h(Field, { label: t('SSH 主机别名') },
                h('input', { className: 'emc-input', value: draft.tunnelHost, onChange: change('tunnelHost'),
                  spellCheck: false, placeholder: 'my-server' }))
            : null,
          h(Field, { label: t('预期数据库 ID（可选）') },
            h('input', { className: 'emc-input', value: draft.expectDbId, onChange: change('expectDbId'),
              spellCheck: false, placeholder: '8–64 hex characters' })),
          h(Field, { label: t('token 文件（可选）') },
            h('input', { className: 'emc-input', value: draft.tokenFile, onChange: change('tokenFile'),
              spellCheck: false, placeholder: 'C:\\Users\\me\\.hermes\\eventmem\\daemon.token' })),
          h('div', { className: 'emc-row' },
            h(Btn, { onClick: () => onTest(draft), disabled: busy !== '' },
              busy === 'test' ? t('正在测试…') : t('测试连接')),
            h(Btn, { kind: 'primary', onClick: () => onSave(draft), disabled: busy !== '' },
              busy === 'save' ? t('正在保存…') : t('保存并重新加载')))),
        h(Card, { title: t('安装 daemon：') },
          h('div', { className: 'emc-setup-code' },
            'git clone https://github.com/wackyju2-beep/eventmem.git D:\\eventmem\ncd D:\\eventmem\n.\\install.cmd'),
          h('p', { className: 'emc-hint' },
            t('没有 daemon 时，记忆功能会静默降级：召回不注入，回合结束不写事件。') +
            ' ' + t('可选：向量检索需要 ONNX 模型；关系连边需要一个 decision 后端。'))),
        h('p', { className: 'emc-hint' },
          t('安全：daemon 默认只监听 loopback。若开启 token，本页通过 token 文件读取，不会把 token 写进配置。')))
    }

    // ------------------------------------------------------------- backends

    const TEMPLATES = {
      'cloud': '# 云端 Jev / System One 端点\nprotocol: systemone\nmodel: jev-latest\nbase_url: https://api.typesafe.ai\napi_key_env: TYPESAFE_API_KEY\nlicense: proprietary\nnote: 官方托管；没 key 时探活记 needs_key\n',
      'lmstudio': '# LM Studio（或任何已装好的本地服务）：我们不启动它\nprotocol: systemone\nmodel: lev\nexternal: true\nbase_url: http://127.0.0.1:1234\nnote: LM Studio 里要先加载一个 decision model\n',
      'local': '# 自己托管：daemon 有活时才拉起 llama-server\nprotocol: systemone\nmodel: lev\nweights: ../../models/lev/lev-Q4_K_M.gguf\nlaunch: auto\nport: 0\nlicense: apache-2.0\n',
    }

    function PackCard({ t, pack, notify, onChanged }) {
      const [text, setText] = useState(pack.config || '')
      const [dirty, setDirty] = useState(false)
      const [busy, setBusy] = useState(false)
      const [confirm, confirmDialog] = useConfirm(t)

      useEffect(() => {
        if (!dirty) setText(pack.config || '')
      }, [pack.config, dirty])

      const act = async (action) => {
        setBusy(true)
        try {
          const data = await api(`backends/${encodeURIComponent(pack.name)}/${action}`, {})
          if (action === 'probe') {
            notify(`${pack.name} ${data.ok ? t('可用') : t('不可用')}${data.reason ? '：' + data.reason : ''}`,
              data.ok ? 'ok' : 'err')
          } else {
            notify(`${pack.name} ${action}: ${data.error || 'ok'}`, data.error ? 'err' : 'ok')
            onChanged()
          }
        } catch (e) {
          notify(e.message, 'err')
        } finally {
          setBusy(false)
        }
      }

      const save = async () => {
        setBusy(true)
        try {
          const data = await api(`packs/${encodeURIComponent(pack.name)}/config`, { text })
          setDirty(false)
          notify(`${t('保存成功')} ${pack.name}` + (data.warnings && data.warnings.length ? '\n' + data.warnings.join('\n') : ''))
          onChanged()
        } catch (e) {
          notify(e.message, 'err')
        } finally {
          setBusy(false)
        }
      }

      const remove = async () => {
        if (!(await confirm(`${t('确认删除该后端？')} ${pack.name}`))) return
        setBusy(true)
        try {
          await api(`packs/${encodeURIComponent(pack.name)}/delete`, {})
          notify(`${t('已删除')} ${pack.name}`)
          onChanged()
        } catch (e) {
          if (!String(e.message).includes('链路')) {
            notify(e.message, 'err')
            setBusy(false)
            return
          }
          if (!(await confirm(e.message + '\n' + t('删除后不可恢复。')))) { setBusy(false); return }
          try {
            await api(`packs/${encodeURIComponent(pack.name)}/delete`, { force: true })
            notify(`${t('已删除')} ${pack.name}`)
            onChanged()
          } catch (inner) {
            notify(inner.message, 'err')
          }
        } finally {
          setBusy(false)
        }
      }

      const state = pack.running === true
        ? h('span', { className: 'emc-tag up' }, `${t('运行中')} :${pack.port === undefined ? '?' : pack.port}`)
        : pack.kind === 'self'
          ? h('span', { className: 'emc-tag down' }, t('未运行'))
          : pack.needs_key
            ? h('span', { className: 'emc-tag down' }, t('缺key'))
            : null

      return h('div', { className: 'emc-pack' },
        h('div', { className: 'emc-pack-head' },
          h('span', { className: 'emc-pack-name' }, pack.name),
          h('span', { className: 'emc-tag' }, pack.kind),
          pack.in_chain ? h('span', { className: 'emc-tag' }, t('在链路')) : null,
          state,
          h('span', { style: { flex: 1 } }),
          h(Btn, { kind: 'ghost sm', disabled: busy, onClick: () => act('probe') }, t('探活')),
          pack.kind === 'self' ? h(Btn, { kind: 'sm', disabled: busy, onClick: () => act('start') }, t('启动')) : null,
          pack.kind === 'self' ? h(Btn, { kind: 'sm', disabled: busy, onClick: () => act('stop') }, t('停止')) : null,
          h(Btn, { kind: 'danger sm', disabled: busy, onClick: remove }, t('删除'))),
        h('div', { className: 'emc-meta' },
          [pack.protocol, pack.model || '?', pack.url || pack.weights || '', pack.license].filter(Boolean).join(' · ')),
        h('details', null,
          h('summary', null, 'config.yaml'),
          h('textarea', {
            className: 'emc-area',
            spellCheck: false,
            value: text,
            onChange: (event) => { setText(event.target.value); setDirty(true) },
            style: { marginTop: 8 },
          }),
          h('div', { className: 'emc-row', style: { marginTop: 8 } },
            h(Btn, { kind: 'primary sm', disabled: busy, onClick: save }, t('保存')),
            h('span', { className: 'emc-hint', style: { margin: 0 } }, t('保存的就是这份文件本身，注释会原样留下。')))),
        confirmDialog)
    }

    function Backends({ t, notify }) {
      const [packs, setPacks] = useState(null)
      const [chain, setChain] = useState([])
      const [error, setError] = useState('')
      const [template, setTemplate] = useState('cloud')
      const [name, setName] = useState('')
      const [text, setText] = useState(TEMPLATES.cloud)
      const [busy, setBusy] = useState(false)
      const [addPick, setAddPick] = useState('')
      const dirty = useRef(false)
      const problems = useRef('')

      const load = useCallback(async () => {
        try {
          const [packData, config] = await Promise.all([api('packs'), api('config')])
          setPacks(packData)
          if (!dirty.current) {
            setChain([...((config.effective && config.effective.decision && config.effective.decision.backends) || [])])
          }
          setError('')
          const signature = (packData.problems || []).join('\n')
          if (signature !== problems.current) {
            problems.current = signature
            if (signature) notify(signature, 'err')
          }
        } catch (e) {
          setError(e.message)
        }
      }, [notify])

      useEffect(() => { load() }, [load])

      const updateChain = (next) => { dirty.current = true; setChain(next) }

      const saveChain = async () => {
        setBusy(true)
        try {
          await api('config', { decision: { backends: chain } })
          dirty.current = false
          notify(t('保存成功'))
          load()
        } catch (e) {
          notify(e.message, 'err')
        } finally {
          setBusy(false)
        }
      }

      const available = ((packs && packs.packs) || []).map((pack) => pack.name).filter((n) => !chain.includes(n))

      const create = async () => {
        const trimmed = name.trim()
        if (!trimmed) { notify(t('先起个名字'), 'err'); return }
        setBusy(true)
        try {
          await api('packs', { name: trimmed, text })
          notify(`${t('已创建')} ${trimmed}`)
          setName('')
          load()
        } catch (e) {
          notify(e.message, 'err')
        } finally {
          setBusy(false)
        }
      }

      return h('div', { className: 'emc-body-in' },
        error ? h(Notice, { text: error, kind: 'err' }) : null,

        h(Card, { title: t('链路顺序') },
          h('div', { className: 'emc-chain' },
            chain.length
              ? chain.map((packName, index) => h('span', { className: 'emc-chip', key: packName },
                  h('span', { className: 'n' }, index + 1 + '.'),
                  packName,
                  h('button', {
                    type: 'button', title: t('上移'),
                    onClick: () => {
                      if (index === 0) return
                      const next = chain.slice()
                      ;[next[index - 1], next[index]] = [next[index], next[index - 1]]
                      updateChain(next)
                    },
                  }, '↑'),
                  h('button', {
                    type: 'button', title: t('下移'),
                    onClick: () => {
                      if (index === chain.length - 1) return
                      const next = chain.slice()
                      ;[next[index + 1], next[index]] = [next[index], next[index + 1]]
                      updateChain(next)
                    },
                  }, '↓'),
                  h('button', {
                    type: 'button', title: t('移除'),
                    onClick: () => updateChain(chain.filter((_, at) => at !== index)),
                  }, '✕')))
              : h('span', { className: 'emc-hint', style: { margin: 0 } }, t('空（用 decision.url 单端点）'))),
          h('div', { className: 'emc-row', style: { marginTop: 10 } },
            h('select', {
              className: 'emc-select',
              value: addPick,
              onChange: (event) => setAddPick(event.target.value),
            }, h('option', { value: '' }, available.length ? '—' : t('暂无')),
              available.map((packName) => h('option', { key: packName, value: packName }, packName))),
            h(Btn, {
              onClick: () => {
                if (addPick && !chain.includes(addPick)) { updateChain([...chain, addPick]); setAddPick('') }
              },
            }, t('加到末尾')),
            h(Btn, { kind: 'primary', disabled: busy, onClick: saveChain }, t('保存链路'))),
          h('p', { className: 'emc-hint' }, t('空链路 = 只用 decision.url 那个单端点（v2.1 行为）。'))),

        h(Card, { title: t('新建后端') },
          h('div', { className: 'emc-row' },
            h('select', {
              className: 'emc-select',
              value: template,
              onChange: (event) => { setTemplate(event.target.value); setText(TEMPLATES[event.target.value] || '') },
            },
              h('option', { value: 'cloud' }, t('云端 API（System One 协议）')),
              h('option', { value: 'lmstudio' }, t('LM Studio / 已装好的本地服务')),
              h('option', { value: 'local' }, t('本地 GGUF（自己托管，daemon 负责启动）'))),
            h('input', {
              className: 'emc-input', value: name, placeholder: t('名字，例如 typesafe / lev'),
              onChange: (event) => setName(event.target.value),
            })),
          h('textarea', {
            className: 'emc-area', spellCheck: false, value: text,
            onChange: (event) => setText(event.target.value),
            style: { marginTop: 10 },
          }),
          h('div', { className: 'emc-row', style: { marginTop: 10 } },
            h(Btn, { kind: 'primary', disabled: busy, onClick: create }, t('创建')),
            h('span', { className: 'emc-hint', style: { margin: 0 } }, t('创建后还要把它加进链路，再重启 daemon 才会自动拉起本地进程。')))),

        h(Card, { title: `${t('后端')} · ${packs ? (packs.dir || '') : ''}` },
          !packs || !(packs.packs || []).length
            ? h(Empty, null, `${(packs && packs.dir) || ''} — ${t('暂无')}`)
            : packs.packs.map((pack) => h(PackCard, {
                key: pack.name, t, pack, notify, onChanged: load,
              }))))
    }

    // --------------------------------------------------------------- models

    function Models({ t, notify }) {
      const [models, setModels] = useState(null)
      const [job, setJob] = useState(null)
      const [error, setError] = useState('')
      const [name, setName] = useState('lev')
      const [repo, setRepo] = useState('ggml-org/lev-GGUF')
      const [quant, setQuant] = useState('Q4_K_M')
      const [skipLlama, setSkipLlama] = useState(false)
      const [addChain, setAddChain] = useState(false)
      const [busy, setBusy] = useState(false)
      const alive = useRef(true)

      useEffect(() => () => { alive.current = false }, [])

      const load = useCallback(async () => {
        try {
          const [modelData, jobData] = await Promise.all([api('models'), api('job')])
          setModels(modelData)
          setJob(jobData.job)
          setError('')
        } catch (e) {
          setError(e.message)
        }
      }, [])

      useEffect(() => { load() }, [load])

      const running = Boolean(job && job.state === 'running')
      useEffect(() => {
        if (!running) return undefined
        const timer = setInterval(async () => {
          try {
            const data = await api('job')
            setJob(data.job)
            if (!data.job || data.job.state !== 'running') {
              clearInterval(timer)
              if (data.job && data.job.state === 'done') notify(t('安装完成'))
              if (data.job && data.job.state === 'error') notify(`${t('安装失败')}：${data.job.error || ''}`, 'err')
              load()
            }
          } catch {
            clearInterval(timer)
          }
        }, 1200)
        return () => clearInterval(timer)
      }, [running, notify, load])

      const install = async (body) => {
        setBusy(true)
        try {
          await api('install', body)
          notify(t('安装任务已开始'))
          const data = await api('job')
          setJob(data.job)
          if (!data.job || data.job.state !== 'running') {
            setTimeout(async () => {
              try {
                const later = await api('job')
                if (alive.current) setJob(later.job)
              } catch { /* ignore */ }
            }, 800)
          }
        } catch (e) {
          notify(e.message, 'err')
        } finally {
          setBusy(false)
        }
      }

      const startGguf = async () => {
        const trimmed = name.trim() || 'lev'
        await install({ name: trimmed, repo: repo.trim(), quant: quant.trim(), skip_llama: skipLlama, force: false })
        if (addChain) {
          try {
            const config = await api('config')
            const chain = [...((config.effective && config.effective.decision && config.effective.decision.backends) || [])]
            if (!chain.includes(trimmed)) {
              chain.push(trimmed)
              await api('config', { decision: { backends: chain } })
              notify(t('装完自动加入链路'))
            }
          } catch (e) {
            notify(e.message, 'err')
          }
        }
      }

      const llama = (models && models.llama) || {}
      const jobLabel = job
        ? ({ idle: t('空闲'), running: t('进行中'), done: t('完成'), error: t('失败') }[job.state] || job.state)
        : ''
      const pct = job && job.total ? Math.min(100, Math.round((job.done / job.total) * 100)) : (job && job.state === 'done' ? 100 : 0)

      return h('div', { className: 'emc-body-in' },
        error ? h(Notice, { text: error, kind: 'err' }) : null,

        h('div', { className: 'emc-grid c2' },
          h(Card, { title: t('运行时') },
            llama.build
              ? h('div', null,
                  h('div', { className: 'emc-meta' }, 'llama.cpp ', h('b', null, llama.build), ' · ', llama.asset || ''),
                  h('div', { className: 'emc-meta' }, llama.binary || ''))
              : h(Empty, null, t('还没装 llama.cpp（点右边按钮，按硬件自动选包）')),
            h('div', { className: 'emc-row', style: { marginTop: 10 } },
              h(Btn, { disabled: busy || running, onClick: () => install({ runtime_only: true }) },
                t('下载/更新 llama.cpp')))),
          h(Card, { title: t('已装模型') },
            (models && models.models || []).length
              ? h('table', { className: 'emc-table' }, h('tbody', null, models.models.map((model) =>
                  h('tr', { key: model.repo + model.file },
                    h('td', null, `${model.repo}/${model.file}`),
                    h('td', { className: 'n' }, gb(model.size))))))
              : h(Empty, null, t('没有GGUF')))),

        h(Card, { title: t('安装 GGUF + 生成 pack') },
          h('div', { className: 'emc-grid c3' },
            h(Field, { label: t('后端名') },
              h('input', { className: 'emc-input', value: name, onChange: (event) => setName(event.target.value) })),
            h(Field, { label: 'HuggingFace repo' },
              h('input', { className: 'emc-input', value: repo, onChange: (event) => setRepo(event.target.value) })),
            h(Field, { label: t('量化') },
              h('input', { className: 'emc-input', value: quant, onChange: (event) => setQuant(event.target.value) }))),
          h('div', { className: 'emc-row', style: { marginTop: 10 } },
            h('label', { className: 'emc-check' },
              h('input', { type: 'checkbox', checked: skipLlama, onChange: (event) => setSkipLlama(event.target.checked) }),
              t('跳过 llama.cpp（我已经有）')),
            h('label', { className: 'emc-check' },
              h('input', { type: 'checkbox', checked: addChain, onChange: (event) => setAddChain(event.target.checked) }),
              t('装完自动加入链路')),
            h('span', { style: { flex: 1 } }),
            h(Btn, { kind: 'primary', disabled: busy || running, onClick: startGguf }, t('开始安装'))),
          job && (job.state !== 'idle' || job.phase)
            ? h('div', null,
                h('div', { className: 'emc-row', style: { marginTop: 10 } },
                  h('span', { className: 'emc-hint', style: { margin: 0 } },
                    `${jobLabel}${job.phase ? ' · ' + job.phase : ''}${job.error ? ' · ' + job.error : ''}`),
                  h('span', { style: { flex: 1 } }),
                  h('span', { className: 'emc-hint', style: { margin: 0 } },
                    job.total ? `${mb(job.done)} / ${mb(job.total)}` : (job.elapsed ? job.elapsed + 's' : ''))),
                h('div', { className: 'emc-progress', style: { marginTop: 6 } },
                  h(Bar, { ratio: pct / 100 }),
                  h('span', { className: 'n' }, pct + '%')))
            : null))
    }

    // ------------------------------------------------------------- schedule
    // The daemon's schedule page, not a settings form: the same seven blocks Hermes has
    // (自动运行 / 预设 / 候选选择与成本 / 关系接受条件 / 历史关系重校准 / 历史候选补算 /
    // 说明). Saving future settings and touching history stay separate actions — a history
    // operation never writes settings, and saving never starts work.

    /** 候选选择与成本：一轮问多少对、从哪儿找候选。 */
    const CANDIDATE_FIELDS = [
      ['heads_vec_top_k', 'Tier1 · 向量近邻 top-k', 1],
      ['heads_cos_min', 'Tier1 · 余弦下限', 0.05],
      ['session_heads_top_k', 'Tier2 · 同会话最近 top-k', 1],
      ['session_recent', 'Tier2 · 同会话近期事件数', 1],
      ['unclosed_lessons', 'Tier2 · 未闭环教训', 1],
      ['decision_skeleton', 'Tier2 · 决策骨架', 1],
      ['cross_top_k', 'Tier3 · 跨项目 top-k', 1],
      ['cross_cos_min', 'Tier3 · 余弦下限', 0.05],
    ]

    /** 关系接受条件：之后每条新边都按这里的 margin / 最低置信度判定。 */
    const ACCEPT_FIELDS = [
      ['margin', '全局 margin'],
      ['min_conf_change', '改变最低置信度'],
      ['min_conf_negate', '否定最低置信度'],
      ['min_conf_repeat', '重复最低置信度'],
      ['min_conf_because', '因为最低置信度'],
      ['min_conf_refine', '细化最低置信度'],
    ]
    const ACCEPT_KEYS = ACCEPT_FIELDS.map(([key]) => key)

    const SCHEDULE_DEFAULTS = {
      heads_vec_top_k: 10,
      heads_cos_min: 0.35,
      margin: 0.15,
      min_conf_change: 0,
      min_conf_negate: 0,
      min_conf_repeat: 0,
      min_conf_because: 0,
      min_conf_refine: 0,
      session_heads_top_k: 10,
      session_recent: 3,
      unclosed_lessons: 2,
      decision_skeleton: 5,
      cross_top_k: 3,
      cross_cos_min: 0.7,
      poll_seconds: 60,
      schedule: 'interval',
      schedule_at: '03:30',
      schedule_weekday: 0,
    }
    const TEXT_KEYS = ['schedule', 'schedule_at']
    const MODES = ['interval', 'daily', 'weekly', 'off']
    const WEEKDAYS = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']

    const PRESETS = {
      light: { heads_vec_top_k: 5, session_heads_top_k: 3, unclosed_lessons: 1, decision_skeleton: 3, cross_top_k: 1 },
      normal: { heads_vec_top_k: 10, session_heads_top_k: 10, unclosed_lessons: 2, decision_skeleton: 5, cross_top_k: 3 },
      deep: { heads_vec_top_k: 20, session_heads_top_k: 15, unclosed_lessons: 3, decision_skeleton: 8, cross_top_k: 5 },
    }

    /** Every numeric field is held as a string so an empty box stays empty while typing. */
    function scheduleForm(edges) {
      const form = {}
      for (const [key, fallback] of Object.entries(SCHEDULE_DEFAULTS)) {
        const source = edges || {}
        const value = source[key] === undefined || source[key] === null ? fallback : source[key]
        form[key] = String(value)
      }
      return form
    }

    /** Historical-recalibration preview, one line per relation. */
    function recalText(result, tr) {
      const line = (text, ...args) => fill(tr, text, ...args)
      const rows = ['改变', '否定', '重复', '因为', '细化'].map((rel) =>
        line('{0}：新增 {1}，删除 {2}', tr(rel), NI((result.added || {})[rel]), NI((result.removed || {})[rel])))
      rows.push(
        line('置信度更新的边：{0}', NI(result.confidence_updates)),
        line('跳过的事件对：{0}', NI(result.skipped)),
        line('未覆盖的 Jev 边：{0}', NI(result.uncovered_jev_edges === undefined ? result.uncovered_edges : result.uncovered_jev_edges)),
        line('记录了人工删除的事件对：{0}', NI(result.manual_deletion_pairs)),
      )
      if (result.legacy_deletions_unknown) rows.push(tr('旧版人工删除记录无法确认。'))
      return rows.join('\n')
    }

    /** Historical-candidate preview: what the pass would walk, not a prediction. */
    function backfillText(result, tr) {
      return fill(tr, '历史事件 {0} 条；当前图观察到 {1} 个候选对。该观察值不是最终数量，也不是上限或下限；新边可能改变后续候选。',
        NI(result.event_count), NI(result.observed_pairs))
    }

    /** Backfill progress line, from `status.backfill`. */
    function progressText(progress, tr) {
      const states = {
        idle: '尚无补算任务', pending: '等待中', running: '运行中', done: '已结束',
        error: '失败', completed: '已完成', stopped: '已停止',
        'jev-unavailable': '模型不可用', 'jev-error': '模型判定失败',
      }
      const state = (progress && progress.result && progress.result.status) || (progress && progress.state) || 'idle'
      const label = states[state] ? tr(states[state]) : state
      const text = fill(tr, '补算状态：{0}；已判 {1} 对，新增 {2} 条边', label,
        progress ? progress.pairs || 0 : 0, progress ? progress.edges || 0 : 0)
      return progress && progress.error ? text + '\n' + progress.error : text
    }

    function Schedule({ t, notify, status, setStatus }) {
      const [config, setConfig] = useState(null)
      const [form, setForm] = useState(null)
      const [error, setError] = useState('')
      const [busy, setBusy] = useState(false)
      const [previews, setPreviews] = useState({})
      const [conditional, setConditional] = useState(false)
      const [confirm, confirmDialog] = useConfirm(t)
      const seeded = useRef(false)
      const formRef = useRef(form)
      formRef.current = form

      const load = useCallback(async () => {
        try {
          const data = await api('config')
          setConfig(data)
          setError('')
        } catch (e) {
          setError(e.message)
        }
      }, [])

      useEffect(() => { load() }, [load])

      // Seed the form once (a reload after saving must not clobber what is being typed).
      useEffect(() => {
        if (!config || seeded.current) return
        setForm(scheduleForm(config.effective && config.effective.edges))
        seeded.current = true
      }, [config])

      const paused = Boolean(status && status.paused)
      const backfill = (status && status.backfill) || null

      const edit = (key, value) => {
        setForm((current) => ({ ...current, [key]: value }))
        setPreviews({})
      }
      const setField = (key) => (event) => edit(key, event.target.value)
      const preset = (values) => {
        setForm((current) => ({ ...current, ...Object.fromEntries(Object.entries(values).map(([k, v]) => [k, String(v)])) }))
        setPreviews({})
      }

      /** The form as the daemon wants it: numbers where numbers, strings where strings. */
      const edgeValues = (source = form) => {
        const edges = {}
        for (const [key, fallback] of Object.entries(SCHEDULE_DEFAULTS)) {
          if (TEXT_KEYS.includes(key)) { edges[key] = String(source[key] || fallback); continue }
          const raw = String(source[key]).trim()
          const value = Number(raw)
          if (raw === '' || !Number.isFinite(value) || value < 0 ||
              (ACCEPT_KEYS.includes(key) && value > 1)) {
            throw new Error(t('请输入有效数值（置信度与 margin 为 0–1）'))
          }
          edges[key] = value
        }
        if (!MODES.includes(edges.schedule)) throw new Error(t('运行模式无效'))
        if (!/^\d{1,2}:\d{2}$/.test(edges.schedule_at)) throw new Error(t('时间要写成 HH:MM'))
        if (!(edges.schedule_weekday >= 0 && edges.schedule_weekday <= 6)) throw new Error(t('星期要在 0–6'))
        return edges
      }
      const candidateEdges = (edges) => Object.fromEntries(Object.entries(edges)
        .filter(([key]) => !ACCEPT_KEYS.includes(key) && !['poll_seconds', 'schedule', 'schedule_at', 'schedule_weekday'].includes(key)))
      const acceptanceEdges = (edges) => Object.fromEntries(ACCEPT_FIELDS.map(([key]) => [key, edges[key]]))
      const sameEdges = (left, right) => JSON.stringify(left) === JSON.stringify(right)

      const togglePause = async () => {
        setBusy(true)
        try {
          const want = !paused
          const data = await api('pause', { paused: want })
          setStatus((current) => current ? { ...current, paused: Boolean(data.paused) } : current)
          notify(want ? t('已暂停：这一轮停下后不再自动开新的一轮') : t('已继续：按调度恢复'))
        } catch (e) {
          notify(e.message, 'err')
        } finally {
          setBusy(false)
        }
      }

      const save = async (keys) => {
        setBusy(true)
        try {
          const values = edgeValues()
          const edges = keys ? Object.fromEntries(keys.map((key) => [key, values[key]])) : values
          await api('config', { edges })
          setPreviews({})
          notify(keys ? t('接受条件已保存：之后的连边按该标准判定') : t('已保存；下一轮 pass 用新值'))
          seeded.current = false
          load()
        } catch (e) {
          notify(e.message, 'err')
        } finally {
          setBusy(false)
        }
      }

      const reset = async () => {
        if (!(await confirm(t('确认恢复调度默认值？')))) return
        setBusy(true)
        try {
          if (!((config && config.overrides) || {}).edges) {
            notify(t('调度已经在默认值上'))
            seeded.current = false
            load()
            return
          }
          // `null` = drop this key's override, which is how the daemon spells "default".
          const edges = Object.fromEntries(Object.keys(SCHEDULE_DEFAULTS).map((key) => [key, null]))
          await api('config', { edges })
          setPreviews({})
          notify(t('已恢复默认（清掉 settings.yaml 里的覆盖）'))
          seeded.current = false
          load()
        } catch (e) {
          notify(e.message, 'err')
        } finally {
          setBusy(false)
        }
      }

      const preview = async (kind) => {
        setBusy(true)
        setPreviews((current) => ({ ...current, [kind]: null }))
        try {
          const values = edgeValues()
          const edges = kind === 'recalibrate' ? acceptanceEdges(values) : candidateEdges(values)
          const historical = kind === 'recalibrate' && conditional ? 'conditional' : null
          const path = kind === 'recalibrate' ? 'recalibrate/preview' : 'backfill/preview'
          const result = await api(path, kind === 'recalibrate'
            ? { edges, historical_scale: historical }
            : { edges })
          if (!result || !result.token) throw new Error(t('预览未返回令牌，无法应用'))
          // The preview is bound to these exact parameters; if the form moved on, drop it.
          const current = edgeValues(formRef.current)
          const want = kind === 'recalibrate' ? acceptanceEdges(current) : candidateEdges(current)
          if (!sameEdges(edges, want)) throw new Error(t('参数已改变，请重新预览'))
          if (kind === 'recalibrate' && historical !== (conditional ? 'conditional' : null)) {
            throw new Error(t('参数已改变，请重新预览'))
          }
          setPreviews((current2) => ({ ...current2, [kind]: { result, edges, historical } }))
        } catch (e) {
          notify(e.message, 'err')
        } finally {
          setBusy(false)
        }
      }

      const apply = async (kind) => {
        const previewed = previews[kind]
        if (!previewed || !(await confirm(t('确认应用历史操作？此操作不会保存未来设置。')))) return
        setBusy(true)
        try {
          const values = edgeValues()
          const want = kind === 'recalibrate' ? acceptanceEdges(values) : candidateEdges(values)
          if (!sameEdges(previewed.edges, want)) throw new Error(t('参数已改变，请重新预览'))
          if (kind === 'recalibrate' && previewed.historical !== (conditional ? 'conditional' : null)) {
            throw new Error(t('参数已改变，请重新预览'))
          }
          const path = kind === 'recalibrate' ? 'recalibrate/apply' : 'backfill/start'
          const result = await api(path, kind === 'recalibrate'
            ? { edges: previewed.edges, token: previewed.result.token, historical_scale: previewed.historical }
            : { edges: previewed.edges, token: previewed.result.token })
          if (kind === 'backfill') {
            setPreviews({})
            setStatus((current) => current ? { ...current, backfill: result.backfill || { state: 'running' } } : current)
          } else {
            setPreviews((current) => ({ ...current, recalibrate: { result, edges: previewed.edges, applied: true } }))
          }
          notify(t('请求已提交'))
        } catch (e) {
          notify(e.message, 'err')
        } finally {
          setBusy(false)
        }
      }

      const stopBackfill = async () => {
        setBusy(true)
        try {
          await api('stop', {})
          notify(t('已请求停止：本轮做完手上这一对就停'))
        } catch (e) {
          notify(e.message, 'err')
        } finally {
          setBusy(false)
        }
      }

      if (!form) return h('div', { className: 'emc-body-in' }, error ? h(Notice, { text: error, kind: 'err' }) : h(Empty, null, '…'))

      const savedEdges = (config && config.effective && config.effective.edges) || {}
      const acceptanceDirty = ACCEPT_FIELDS.some(([key]) =>
        Number(form[key]) !== Number(savedEdges[key] === undefined ? SCHEDULE_DEFAULTS[key] : savedEdges[key]))

      /** One numeric input; the unit lives in the label, the validation in `edgeValues`. */
      const numberField = (key, label, step, max) => h(Field, { label: t(label) },
        h('input', {
          className: 'emc-input', type: 'number', step, min: 0, max,
          value: form[key], onChange: setField(key),
        }))

      return h('div', { className: 'emc-body-in' },
        error ? h(Notice, { text: error, kind: 'err' }) : null,

        h(Card, {
          title: t('自动运行'),
          actions: h('span', { className: 'emc-hint', style: { margin: 0 } },
            paused ? t('已暂停（不开新的一轮）') : t('运行中')),
        },
        h('div', { className: 'emc-grid c3' },
          h(Field, { label: t('什么时候算') },
            h('select', { className: 'emc-select', value: form.schedule, onChange: setField('schedule') },
              h('option', { value: 'interval' }, t('每 N 分钟检查一次（有新事件才跑）')),
              h('option', { value: 'daily' }, t('每天固定时间')),
              h('option', { value: 'weekly' }, t('每周固定一天')),
              h('option', { value: 'off' }, t('关闭自动运行（只用按钮）')))),
          form.schedule === 'interval'
            ? h(Field, { label: t('间隔（秒）') },
                h('input', {
                  className: 'emc-input', type: 'number', min: 5, value: form.poll_seconds,
                  onChange: setField('poll_seconds'),
                }))
            : h(Field, { label: t('时间') },
                h('input', {
                  className: 'emc-input', type: 'time', value: form.schedule_at,
                  onChange: setField('schedule_at'),
                })),
          form.schedule === 'weekly'
            ? h(Field, { label: t('星期') },
                h('select', {
                  className: 'emc-select', value: String(form.schedule_weekday),
                  onChange: setField('schedule_weekday'),
                }, WEEKDAYS.map((label, index) =>
                  h('option', { key: label, value: String(index) }, t(label)))))
            : null),
        h('p', { className: 'emc-hint' },
          t('暂停后任何模式都不再开新的一轮：正在跑的那一轮会在当前这一对结束后停下，暂停状态存在数据库里，重启 daemon 也不会自己恢复，直到点「继续判断」。')),
        h('div', { className: 'emc-row', style: { marginTop: 10 } },
          h(Btn, { kind: 'primary', disabled: busy, onClick: () => save(null) }, t('保存自动运行设置')),
          h(Btn, { kind: paused ? 'primary' : 'danger', disabled: busy || !status, onClick: togglePause },
            paused ? t('继续判断') : t('暂停全部判断')))),

        h(Card, { title: t('预设（每轮问多少对）') },
          h('div', { className: 'emc-row' },
            h(Btn, { onClick: () => preset(PRESETS.light) }, t('轻')),
            h(Btn, { onClick: () => preset(PRESETS.normal) }, t('标准（默认）')),
            h(Btn, { onClick: () => preset(PRESETS.deep) }, t('深')),
            h('span', { style: { flex: 1 } }),
            h('span', { className: 'emc-hint', style: { margin: 0 } },
              t('候选来源会去重，实际数量未知；每对 8 秒仅供参考，当前模型速度未知。')))),

        h(Card, { title: t('候选选择与成本') },
          h('div', { className: 'emc-grid c3' },
            CANDIDATE_FIELDS.map(([key, label, step]) =>
              h('div', { key }, numberField(key, label, step, 1)))),
          h('p', { className: 'emc-hint' },
            t('保存未来设置会同时保存候选数量和关系规则；历史操作不会保存未来设置。')),
          h('div', { className: 'emc-row', style: { marginTop: 10 } },
            h(Btn, { kind: 'primary', disabled: busy, onClick: () => save(null) }, t('仅保存未来设置')),
            h(Btn, { disabled: busy, onClick: reset }, t('恢复调度默认值')),
            h('span', { className: 'emc-hint', style: { margin: 0 } },
              config && config.file ? config.file : t('（无配置文件）')))),

        h(Card, {
          title: t('关系接受条件'),
          actions: h('span', { className: 'emc-hint', style: { margin: 0 } },
            acceptanceDirty ? t('有未保存的改动') : t('已保存')),
        },
        h('div', { className: 'emc-grid c3' },
          ACCEPT_FIELDS.map(([key, label]) => h('div', { key }, numberField(key, label, 0.01, 1)))),
        h('div', { className: 'emc-row', style: { marginTop: 10 } },
          h(Btn, { kind: 'primary', disabled: busy, onClick: () => save(ACCEPT_KEYS) }, t('保存接受条件')),
          h('span', { className: 'emc-hint', style: { flex: 1, margin: 0 } },
            t('保存后，从下一轮 pass 起的连边一律按这里的 margin 与最低置信度判定；已有边不动（要改历史就用下面的重校准预览）。')))),

        h(Card, { title: t('历史关系重校准') },
          h('p', { className: 'emc-hint', style: { marginTop: 0 } },
            t('只重判已有概率的关系；无法使用的记录会跳过。预览不会修改历史。')),
          h('p', { className: 'emc-hint', style: { marginTop: 6 } },
            t('历史概率格式有歧义，模型来源未知。StartLux 输出条件概率，但不能假设其他后端相同。默认跳过条件形式；仅在确认后勾选，选择只影响条件形式的记录。')),
          h('label', { className: 'emc-check', style: { marginTop: 8 } },
            h('input', {
              type: 'checkbox', checked: conditional,
              onChange: (event) => {
                setConditional(event.target.checked)
                setPreviews((current) => ({ ...current, recalibrate: null }))
              },
            }),
            t('将条件形式的历史概率按条件概率处理')),
          h('div', { className: 'emc-row', style: { marginTop: 10 } },
            h(Btn, { disabled: busy, onClick: () => preview('recalibrate') }, t('预览历史')),
            h(Btn, {
              disabled: busy || !previews.recalibrate || previews.recalibrate.applied,
              onClick: () => apply('recalibrate'),
            }, t('应用历史重校准'))),
          previews.recalibrate
            ? h('pre', { className: 'emc-log', style: { maxHeight: 260, marginTop: 10 } },
                recalText(previews.recalibrate.result, t))
            : null),

        h(Card, { title: t('历史候选补算') },
          h('p', { className: 'emc-hint', style: { marginTop: 0 } },
            t('重新选择历史候选需要 Jev 模型判定；耗时和成本不确定。预览不会启动任务。')),
          h('p', { className: 'emc-hint', style: { marginTop: 6 } },
            t('补算使用已保存的关系阈值，不使用未保存的阈值编辑；候选参数使用当前表单。')),
          h('div', { className: 'emc-row', style: { marginTop: 10 } },
            h(Btn, { disabled: busy, onClick: () => preview('backfill') }, t('预览历史')),
            h(Btn, { disabled: busy || !previews.backfill, onClick: () => apply('backfill') },
              t('启动历史候选补算'))),
          previews.backfill
            ? h('pre', { className: 'emc-log', style: { maxHeight: 200, marginTop: 10 } },
                backfillText(previews.backfill.result, t))
            : null,
          h('div', { className: 'emc-row', style: { marginTop: 10, alignItems: 'flex-start' } },
            h('span', { className: 'emc-hint', style: { flex: 1, margin: 0, whiteSpace: 'pre-wrap' } },
              progressText(backfill, t)),
            backfill && ['running', 'pending'].includes(backfill.state)
              ? h(Btn, { disabled: busy, onClick: stopBackfill }, t('停止补算'))
              : null)),

        h(Card, { title: t('说明') },
          h('p', { className: 'emc-hint', style: { margin: 0 } },
            t('Tier1 = 全局向量近邻；Tier2 = 同会话近期 + 未闭环教训 + 决策骨架；Tier3 = 跨项目。改的是 daemon 的 settings.yaml，下一轮 pass 生效。'))),
        confirmDialog)
    }

    // ----------------------------------------------------------------- logs

    function Logs({ t }) {
      const [logs, setLogs] = useState(null)
      const [error, setError] = useState('')
      const [follow, setFollow] = useState(true)
      const preRef = useRef(null)

      const load = useCallback(async () => {
        try {
          const data = await api('logs')
          setLogs(data.logs || [])
          setError('')
        } catch (e) {
          setError(e.message)
        }
      }, [])

      useEffect(() => {
        load()
        const timer = setInterval(load, 3000)
        return () => clearInterval(timer)
      }, [load])

      useEffect(() => {
        const node = preRef.current
        if (!node || !follow) return
        node.scrollTop = node.scrollHeight
      }, [logs, follow])

      const entries = logs || []
      return h('div', { className: 'emc-body-in' },
        error ? h(Notice, { text: error, kind: 'err' }) : null,
        h(Card, {
          title: t('本进程日志最近条'),
          actions: h('div', { className: 'emc-row', style: { gap: 10 } },
            h('label', { className: 'emc-check' },
              h('input', { type: 'checkbox', checked: follow, onChange: (event) => setFollow(event.target.checked) }),
              t('跟随最新')),
            h(Btn, { kind: 'ghost sm', onClick: load }, t('刷新'))),
        },
        h('pre', { className: 'emc-log', ref: preRef },
          entries.length
            ? entries.map((entry, index) => {
                const level = String(entry.level || '')
                const cls = level === 'ERROR' ? 'e' : level === 'WARNING' ? 'w' : level === 'DEBUG' ? 'd' : ''
                return h('span', { key: index, className: cls },
                  `${when(entry.ts)} ${level.slice(0, 1)} ${String(entry.name || '').split('.').pop()}  ${entry.message}\n`)
              })
            : h('span', { className: 'd' }, t('（还没有日志）')))))
    }

    // ------------------------------------------------------------- graph tab
    // Ported from the Hermes 记忆图谱: the lattice layout, camera basis and projection math
    // are unchanged (that is what makes it feel the same); every frame element (bar, panel,
    // inspector) is a dsh control and every chrome color a --dsw token. The kind/relation
    // palette stays hex because it is data encoding, not chrome.

    const G_KINDS = ['决定', '尝试', '结果', '变更', '教训']
    const G_RELS = ['改变', '否定', '重复', '因为', '细化']
    const KIND_COLORS = {
      决定: '#7ea9e0', 尝试: '#68c3ba', 结果: '#79c08c', 变更: '#d7a35f', 教训: '#d97c92',
    }
    const REL_COLORS = {
      改变: '#d9a05b', 否定: '#d0707f', 重复: '#6fae9c', 因为: '#a08ad8', 细化: '#6f9fd8',
    }
    const NONE_COLOR = '#5f6672'
    const EDITED_COLOR = '#c58bff'
    const AXIS_LABELS = ['重复', '改变', '否定', '都不是']
    const DETAIL_LABELS = ['因为', '细化', '都不是']
    const LASSO_HOLD_MS = 320
    const START_DIST = 3.5
    const FOCUS_DIST = 2.4
    const SCALE_REF = 2.4
    const PULL_IN = 0.7
    const FOV_DEG = 26
    const FOV = (FOV_DEG * Math.PI) / 180
    const CARD_BUDGET = 46

    const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v))
    const smoothstep = (e0, e1, x) => {
      const t = clamp((x - e0) / (e1 - e0), 0, 1)
      return t * t * (3 - 2 * t)
    }
    // 远景收缩：eye 到原子的距离 / 参考视距。rel 小 = 很远。0 → 彩色点，1 → 正常小球。
    const farFactor = (rel) => 1 - smoothstep(0.12, 0.28, rel)
    const easeInOut = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2)

    /** 屏幕矩形框选：只挑当前真的看得见的原子（相机前方、没被筛选压掉）。 */
    const lassoPick = (positions, rect) => {
      const ids = []
      for (const [id, p] of positions) {
        if (!p || !p.el) continue
        if (!(p.visibility > 0.5) || !(p.depth > 0.35) || !p.clickable) continue
        if (p.x < rect.x0 || p.x > rect.x1 || p.y < rect.y0 || p.y > rect.y1) continue
        ids.push(id)
      }
      return ids.sort((a, b) => a - b)
    }

    const pad2 = (n) => String(n).padStart(2, '0')
    const gTime = (ts) => {
      const d = new Date(ts * 1000)
      return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`
    }
    const gFull = (ts) => {
      const d = new Date(ts * 1000)
      const date = `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
      return `${date} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`
    }
    const dayKey = (ts) => {
      const d = new Date(ts * 1000)
      return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`
    }
    const shortSession = (sid) => {
      if (!sid) return '无会话'
      const m = /^(\d{8})_(\d{6})/.exec(sid)
      if (!m) return sid
      return `${m[1].slice(4, 6)}-${m[1].slice(6, 8)} ${m[2].slice(0, 2)}:${m[2].slice(2, 4)}:${m[2].slice(4, 6)}`
    }

    /** 序号输入：12 / #12 / 30-42 / 12, 30-42, #56。范围上限 2000，手滑打 1-999999 不会卡死。 */
    function parseIdSpec(text) {
      const ids = new Set()
      const bad = []
      for (const raw of String(text || '').split(/[\s,，、;；]+/)) {
        if (!raw) continue
        const token = raw.replace(/^#/, '')
        if (!token) { bad.push(raw); continue }
        const range = /^(\d+)\s*[-~–—]\s*(\d+)$/.exec(token)
        if (range) {
          let a = Number(range[1])
          let b = Number(range[2])
          if (a > b) { const t = a; a = b; b = t }
          if (b - a > 2000) { bad.push(raw); continue }
          for (let i = a; i <= b; i += 1) ids.add(i)
          continue
        }
        if (/^\d+$/.test(token)) { ids.add(Number(token)); continue }
        bad.push(raw)
      }
      return { ids, bad }
    }

    function atomPasses(ev, filters, idSet, semIds, semActive) {
      if (filters.project && (ev.project || '未归类') !== filters.project) return false
      if (filters.session && ev.session !== filters.session) return false
      if (filters.date && dayKey(ev.ts) !== filters.date) return false
      if (filters.kinds.length && !filters.kinds.includes(ev.kind)) return false
      if (idSet && idSet.size && !idSet.has(ev.id)) return false
      if (semActive && !(semIds && semIds.has(ev.id))) return false
      return true
    }

    const edgeRelKey = (e) => (e.kind === 'none' ? 'none' : e.rel)
    const FACET_VALUE = {
      project: (ev) => ev.project || '未归类',
      session: (ev) => ev.session || '',
      date: (ev) => dayKey(ev.ts),
    }

    /**
     * 分面计数：每个下拉只列「其它条件都满足的事件里真的存在」的值。
     * 算某个维度时把它自己排除，否则选完会话，别的会话就全从列表里消失了。
     */
    function facetCounts(events, filters, idSet, semIds, semActive, pool) {
      const out = { project: new Map(), session: new Map(), date: new Map() }
      for (const facet of ['project', 'session', 'date']) {
        const scoped = { ...filters, [facet]: '' }
        const pick = FACET_VALUE[facet]
        const counts = out[facet]
        for (const ev of events) {
          if (!atomPasses(ev, scoped, idSet, semIds, semActive)) continue
          if (pool && !pool.has(ev.id)) continue
          const value = pick(ev)
          if (!value) continue
          const row = counts.get(value) || { n: 0, last: 0 }
          row.n += 1
          if (ev.ts > row.last) row.last = ev.ts
          counts.set(value, row)
        }
      }
      return out
    }

    /** 所有筛选条件同时生效（AND）：原子维度决定命中集，关系维度决定哪些边参与。 */
    function computeFilterInfo({ events, edges, filters, idSet, semIds, semActive, mode, pulled }) {
      const relSet = filters.rels.length ? new Set(filters.rels) : null
      const atomActive = Boolean(
        filters.project || filters.session || filters.date || filters.kinds.length
        || (idSet && idSet.size) || semActive,
      )
      if (!atomActive && !relSet) {
        return { active: false, match: new Set(), focus: new Set(), promo: new Set(),
          count: 0, edgeCount: 0, atomActive: false, relActive: false }
      }
      const pool = new Set()
      if (relSet) {
        for (const e of edges) {
          if (!relSet.has(edgeRelKey(e))) continue
          pool.add(e.src)
          pool.add(e.dst)
        }
      }
      const match = new Set()
      for (const ev of events) {
        if (!atomPasses(ev, filters, idSet, semIds, semActive)) continue
        if (relSet && !pool.has(ev.id)) continue
        match.add(ev.id)
      }
      const focus = new Set(match)
      if (mode === 'neighbors') {
        for (const e of edges) {
          if (relSet && !relSet.has(edgeRelKey(e))) continue
          if (match.has(e.src) || match.has(e.dst)) {
            focus.add(e.src)
            focus.add(e.dst)
          }
        }
      }
      if (pulled) for (const id of pulled) focus.add(id)
      let edgeCount = 0
      for (const e of edges) {
        if (relSet && !relSet.has(edgeRelKey(e))) continue
        if (focus.has(e.src) && focus.has(e.dst)) edgeCount += 1
      }
      return {
        active: true,
        match,
        focus,
        promo: new Set([...focus].filter((id) => !match.has(id))),
        count: match.size,
        edgeCount,
        atomActive,
        relActive: Boolean(relSet),
      }
    }

    const toggleValue = (list, v) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v])

    /**
     * 选中一颗原子后的「焦点邻域」：它自己、全部一跳邻居，以及按舞台预算挑出的最多
     * 16 颗二跳原子（和舞台上排位置的那批一致）。一跳全部点亮；舞台上被摆到 2.8 半径
     * 的是前 16 颗一跳，被摆到 5 的是那批二跳。
     *
     * 这份结果同时决定三件事，所以只能有一处算法：
     *   - 画面上哪些原子被点亮（ring），其余原子淡出；
     *   - 选中后哪些边被高亮（edges）：只有与中心相连的关系边；
     *   - 高亮的东西哪些可以直接点中（ring / edges 的成员）——不必先退出再点。
     * 灰色「判过、无关系」的对不是关系边，永远不进这里。
     */
    function atomFocusRing(scene, id) {
      const center = scene.byId.get(id)
      if (!center) return null
      const adjacency = new Map()
      for (const e of scene.edges) {
        if (e.kind !== 'rel') continue
        if (!adjacency.has(e.src)) adjacency.set(e.src, new Set())
        if (!adjacency.has(e.dst)) adjacency.set(e.dst, new Set())
        adjacency.get(e.src).add(e.dst)
        adjacency.get(e.dst).add(e.src)
      }
      const direct = [...(adjacency.get(id) || [])]
        .filter((other) => scene.byId.has(other)).sort((x, y) => x - y)
      const directSet = new Set(direct)
      const first = direct.slice(0, 16)
      const second = new Set()
      for (const other of first) {
        for (const mate of adjacency.get(other) || []) {
          if (mate !== id && !directSet.has(mate) && scene.byId.has(mate)) second.add(mate)
        }
      }
      const secondList = [...second].sort((x, y) => x - y).slice(0, 16)
      const ring = new Set([id, ...direct, ...secondList])
      const edges = new Set()
      for (const e of scene.edges) {
        if (e.kind !== 'rel') continue
        if (e.src === id || e.dst === id) edges.add(e.key)
      }
      return { center, direct, first, second: secondList, ring, edges }
    }

    const IN_GRAPH_STAGE = (target) => Boolean(
      target && target.closest && target.closest('.emc-g-stage'),
    )

    /**
     * Atoms live on a 3D lattice: the newest memory sits at the centre and older ones fill
     * outward shells, so the cloud "grows" from the middle and looks the same from any angle.
     * `pos` (id → {x,y,z}) carries positions the user has dragged and that we saved.
     */
    function buildScene(data, pos) {
      const events = data.events || []
      const N = Math.max(1, events.length)
      const side = Math.max(1, Math.ceil(Math.cbrt(N)))
      const nx = side
      const nz = side
      const ny = Math.max(1, Math.ceil(N / (nx * nz)))
      const cells = []
      for (let i = 0; i < nx; i += 1) {
        for (let j = 0; j < ny; j += 1) {
          for (let k = 0; k < nz; k += 1) {
            const x = i - (nx - 1) / 2
            const y = j - (ny - 1) / 2
            const z = k - (nz - 1) / 2
            cells.push({ x, y, z, d: x * x + y * y * 1.2 + z * z, tie: ((i * 73 + j * 131 + k * 197) % 977) / 977 })
          }
        }
      }
      // 从中心向外一层层铺，最新的原子占最中间的格子。
      cells.sort((a, b) => a.d - b.d || a.tie - b.tie)

      const order = [...events].sort((a, b) => b.ts - a.ts || b.id - a.id)
      const atoms = []
      order.forEach((ev, idx) => {
        const c = cells[idx % cells.length]
        const j1 = (((ev.id * 2654435761) % 997) / 997 - 0.5) * 0.1
        const j2 = (((ev.id * 40503 + 17) % 977) / 977 - 0.5) * 0.1
        const j3 = (((ev.id * 7919 + 41) % 983) / 983 - 0.5) * 0.1
        const home = { x: c.x + j1, y: c.y + j2, z: c.z + j3 }
        const saved = pos && pos.get(ev.id)
        atoms.push({
          ...ev,
          project: ev.project || '未归类',
          home,
          x: saved ? saved.x : home.x,
          y: saved ? saved.y : home.y,
          z: saved ? saved.z : home.z,
        })
      })

      const byId = new Map(atoms.map((a) => [a.id, a]))
      const pairKeys = new Set((data.edges || []).map((e) => `${e.src}-${e.dst}`))
      const edges = []
      for (const e of data.edges || []) {
        if (!byId.has(e.src) || !byId.has(e.dst)) continue
        edges.push({
          key: `r|${e.src}|${e.dst}|${e.rel}`,
          kind: 'rel',
          src: e.src,
          dst: e.dst,
          rel: e.rel,
          conf: e.conf,
          cross: Boolean(e.cross),
          probs: e.probs || null,
          edited: Boolean(e.edited_ts),
          manual: e.decided_by === 'human',
          decidedBy: e.decided_by,
        })
      }
      for (const p of data.pairs || []) {
        if (!byId.has(p.src) || !byId.has(p.dst)) continue
        if (pairKeys.has(`${p.src}-${p.dst}`)) continue
        edges.push({
          key: `p|${p.src}|${p.dst}`,
          kind: 'none',
          src: p.src,
          dst: p.dst,
          rel: null,
          conf: 0,
          cross: false,
          probs: null,
          pair: p,
          edited: Boolean(p.edited_ts),
          manual: false,
        })
      }

      // 每条边都给一段可见的弧（同一对之间的多条边左右错开），不要画成直线。
      const byPair = new Map()
      for (const e of edges) {
        const key = `${Math.min(e.src, e.dst)}-${Math.max(e.src, e.dst)}`
        if (!byPair.has(key)) byPair.set(key, [])
        byPair.get(key).push(e)
      }
      for (const list of byPair.values()) {
        list.forEach((e, i) => {
          let h = 0
          for (let j = 0; j < e.key.length; j += 1) h = (h * 31 + e.key.charCodeAt(j)) % 997
          const amp = 0.14 + ((h % 9) / 9) * 0.14
          const sign = i % 2 === 0 ? 1 : -1
          e.bow = clamp(sign * (amp + Math.floor(i / 2) * 0.16), -0.62, 0.62)
        })
      }

      let radius = 1.4
      for (const a of atoms) radius = Math.max(radius, Math.hypot(a.x, a.y, a.z))

      const seed = order[0]
      return {
        atoms,
        byId,
        edges,
        radius,
        newestId: seed ? seed.id : null,
        projects: [...new Set(atoms.map((a) => a.project))].sort(),
      }
    }

    function placeEdgeLabels(labels) {
      const kept = []
      const sorted = [...labels].sort(
        (a, b) => Number(Boolean(b.selected)) - Number(Boolean(a.selected)),
      )
      for (const label of sorted) {
        const width = Math.max(18, label.text.length * label.size) + 8
        const overlap = kept.some((other) => Math.abs(other.x - label.x) < (other.width + width) / 2
          && Math.abs(other.y - label.y) < (other.size + label.size) / 2 + 5)
        if (!overlap || label.selected) kept.push({ ...label, width })
      }
      return kept
    }

    function fitDistance(radius, aspect, fovDeg = FOV_DEG) {
      const half = ((fovDeg / 2) * Math.PI) / 180
      const horizontal = Math.atan(Math.tan(half) * Math.max(0.45, aspect))
      return (radius * 1.12) / Math.sin(Math.min(half, horizontal))
    }

    function animateCamera(cam, patch, ms = 520, delay = 0) {
      const to = {
        yaw: cam.yaw, pitch: cam.pitch, dist: cam.dist,
        fx: cam.fx, fy: cam.fy, fz: cam.fz,
        panX: cam.panX || 0, panY: cam.panY || 0,
        ...patch,
      }
      cam.anim = {
        t0: performance.now() + delay,
        ms,
        from: {
          yaw: cam.yaw, pitch: cam.pitch, dist: cam.dist,
          fx: cam.fx, fy: cam.fy, fz: cam.fz,
          panX: cam.panX || 0, panY: cam.panY || 0,
        },
        to,
      }
      cam.dirty = true
    }

    /** Eye position + orthonormal camera basis (forward / right / up) for a camera state. */
    function cameraBasis(cam) {
      const cp = Math.cos(cam.pitch)
      const sp = Math.sin(cam.pitch)
      const cy = Math.cos(cam.yaw)
      const sy = Math.sin(cam.yaw)
      const ex = cam.fx + cam.dist * cp * sy
      const ey = cam.fy + cam.dist * sp
      const ez = cam.fz + cam.dist * cp * cy
      let fx = cam.fx - ex
      let fy = cam.fy - ey
      let fz = cam.fz - ez
      const fl = Math.hypot(fx, fy, fz) || 1
      fx /= fl
      fy /= fl
      fz /= fl
      let rx = -fz
      let rz = fx
      const rl = Math.hypot(rx, rz) || 1
      rx /= rl
      rz /= rl
      return {
        ex, ey, ez, fx, fy, fz, rx, ry: 0, rz,
        ux: -rz * fy, uy: rz * fx - rx * fz, uz: rx * fy,
      }
    }


    // ----------------------------------------------------------------- stage

    function GraphStage({
      scene, focusInfo, sel, multi, focusReq, showLabels, camRef, posRef, onMoved, focusRing,
      onSelectAtom, onSelectEdge, onBackground, onLasso,
    }) {
      const stageRef = useRef(null)
      const sizeRef = useRef({ w: 0, h: 0 })
      const atomEls = useRef(new Map())
      const backRef = useRef(null)
      const frontRef = useRef(null)
      const lassoElRef = useRef(null)
      // 上一帧每颗原子的屏幕位置：框选就用它做命中，和画出来的东西是同一份数据。
      const screenRef = useRef(new Map())
      const haloRef = useRef('#111315')
      const visRef = useRef({ key: '', fi: null, scene: null, set: null })
      // 选中的原子 → 它的两层邻域。父组件用 sel 算好传下来，舞台这一帧就能直接用，
      // 不必等 focusReq 的副作用跑完（否则换选时会有几帧亮错人）。
      const focusRingRef = useRef(focusRing)
      focusRingRef.current = focusRing
      const retreatEndRef = useRef(0)
      const fadeRef = useRef({ key: null, scene: null, values: new Map(), from: new Map(), t: 0 })
      const avoidRef = useRef(null)
      const hitRef = useRef([])
      const dragRef = useRef(null)
      const movedRef = useRef(0)
      const atomAnims = useRef(new Map())
      const flightRef = useRef({ velocity: 0, last: null })
      // Idle bookkeeping: the drift is a 0.025-unit float, so a full-scene repaint for it
      // at 60fps buys nothing. While nothing but drift is moving we draw at ~30fps and skip
      // the morph pass entirely (the camera did not move, so --m/--f cannot have changed).
      const lastInputRef = useRef(0)
      const lastDrawRef = useRef(0)
      const fadeBusyRef = useRef(false)
      // How many edges were on screen last frame: drives the adaptive edge stride. The
      // scene has ~5k edges but at most a couple of thousand are visible at once, and the
      // stroke cost (not the JS loop) is what makes the canvas expensive.
      const edgeLoadRef = useRef(0)
      const frontDirtyRef = useRef(false)
      // The morph pass must run at least once with the scene it belongs to, even if the
      // camera is parked (otherwise the first paint has no --m/--f and everything is a dot).
      const morphReadyRef = useRef(false)
      const morphSceneRef = useRef(null)
      const lastReqRef = useRef(0)
      const sceneRef = useRef(scene)
      sceneRef.current = scene
      const handlersRef = useRef({})
      handlersRef.current = { onSelectAtom, onSelectEdge, onBackground, onLasso, onMoved }
      const multiRef = useRef(multi)
      multiRef.current = multi
      const lassoRef = useRef({ timer: 0, armed: false, x0: 0, y0: 0, pointerId: 0, box: null })
      const focusRef = useRef(focusInfo)
      focusRef.current = focusInfo
      const selRef = useRef(sel)
      selRef.current = sel
      const labelsRef = useRef(showLabels)
      labelsRef.current = showLabels

      // Bench-only introspection: the harness needs to tell "atom is outside the frustum"
      // apart from "the focus fade never reached it". Off unless the page opts in.
      if (typeof window !== 'undefined' && window.__EM_GRAPH_DEBUG) {
        window.__emGraphState = () => {
          const fade = fadeRef.current
          return {
            fadeKey: fade.key,
            fadeShown: fade.shown ? fade.shown.size : null,
            fadeT: Math.round(fade.t),
            visKey: visRef.current.key,
            visSet: visRef.current.set ? visRef.current.set.size : null,
            fiFocus: focusRef.current && focusRef.current.focus ? focusRef.current.focus.size : null,
            els: atomEls.current.size,
            screen: screenRef.current.size,
            camDist: +camRef.current.dist.toFixed(2),
            camFit: +camRef.current.fit.toFixed(2),
            now: Math.round(performance.now()),
            // 边命中表只会在「选中原子」时非空；这组字段同时用于回归验收。
            hitEdges: hitRef.current.length,
            hitKeys: hitRef.current.map((seg) => seg.key),
            ring: focusRingRef.current ? [...focusRingRef.current.ring] : null,
          }
        }
        // 高亮边的几何：验收脚本要按真实曲线点中一条边。
        window.__emEdgeHits = () => hitRef.current.map((seg) => ({
          key: seg.key, src: seg.edge.src, dst: seg.edge.dst,
          rel: seg.edge.rel, kind: seg.edge.kind,
          x1: seg.x1, y1: seg.y1, cx: seg.cx, cy: seg.cy, x2: seg.x2, y2: seg.y2,
        }))
        // 焦点邻域的层次：第一层（全部一跳）与第二层（舞台上挨着的那 16 颗）分开给出。
        window.__emRingLayers = () => {
          const ring = focusRingRef.current
          if (!ring) return null
          return {
            id: ring.center.id,
            direct: [...ring.direct],
            first: [...ring.first],
            second: [...ring.second],
          }
        }
        window.__emAtomState = (ids) => ids.map((id) => {
          const p = screenRef.current.get(id)
          const alpha = fadeRef.current.values.get(id)
          const el = atomEls.current.get(id)
          const dom = document.querySelector('[data-id="' + id + '"]')
          const base = {
            id, alpha: alpha === undefined ? null : +alpha.toFixed(3),
            same: el === dom,
            disp: el ? el.style.display : null,
            domDisp: dom ? dom.style.display : null,
            connected: el ? el.isConnected : null,
          }
          if (!p) return { ...base, culled: true }
          return {
            ...base,
            x: Math.round(p.x), y: Math.round(p.y),
            depth: +p.depth.toFixed(2), visibility: +p.visibility.toFixed(3),
          }
        })
      }

      useEffect(() => {
        camRef.current.dirty = true
      }, [focusInfo, sel, camRef])

      // Closing an active inspector relinquishes its scheduled pull, without cancelling
      // unrelated reset-view camera animations when no inspector was open.
      const previousSelRef = useRef(sel)
      useEffect(() => {
        const previous = previousSelRef.current
        previousSelRef.current = sel
        if (sel || !previous) return
        const ids = [...atomAnims.current].filter(([, anim]) => anim.changed).map(([id]) => id)
        atomAnims.current.clear()
        camRef.current.anim = null
        if (ids.length) handlersRef.current.onMoved(ids)
      }, [sel, camRef])

      const setAtomEl = useCallback((id, el) => {
        if (el) atomEls.current.set(id, el)
        else atomEls.current.delete(id)
      }, [])

      useEffect(() => {
        const live = new Set(scene.atoms.map((a) => a.id))
        for (const id of [...atomEls.current.keys()]) if (!live.has(id)) atomEls.current.delete(id)
        camRef.current.dirty = true
      }, [scene, camRef])

      // Size tracking + opening camera: sit a few cells away from the newest atom.
      useEffect(() => {
        const el = stageRef.current
        if (!el) return undefined
        const measure = () => {
          const rect = el.getBoundingClientRect()
          const w = Math.max(1, Math.round(rect.width))
          const h = Math.max(1, Math.round(rect.height))
          if (w === sizeRef.current.w && h === sizeRef.current.h) return
          sizeRef.current = { w, h }
          const cam = camRef.current
          cam.fit = fitDistance(scene.radius, w / h)
          cam.min = Math.max(0.8, cam.fit * 0.16)
          cam.max = cam.fit * 2.8
          if (!cam.ready) {
            cam.ready = true
            cam.startDist = START_DIST
            cam.dist = START_DIST
            const seed = scene.byId.get(scene.newestId)
            const b = cameraBasis(cam)
            cam.fx = (seed ? seed.x : 0) + b.fx * cam.dist
            cam.fy = (seed ? seed.y : 0) + b.fy * cam.dist
            cam.fz = (seed ? seed.z : 0) + b.fz * cam.dist
          }
          const cs = getComputedStyle(document.documentElement)
          haloRef.current = cs.getPropertyValue('--dsw-alias-bg-base').trim() || '#111315'
          cam.dirty = true
        }
        measure()
        const ro = new ResizeObserver(measure)
        ro.observe(el)
        return () => ro.disconnect()
      }, [scene, camRef])

      // One rAF loop; React never re-renders for camera/atom motion.
      useEffect(() => {
        let raf = 0
        const tick = (now) => {
          raf = requestAnimationFrame(tick)
          const cam = camRef.current
          const flight = flightRef.current
          const dt = flight.last === null ? 0.016 : clamp((now - flight.last) / 1000, 0, 0.05)
          flight.last = now
          if (Math.abs(flight.velocity) > 0.0001) {
            const b = cameraBasis(cam)
            const decay = Math.exp(-dt / 0.18)
            const distance = flight.velocity * 0.18 * (1 - decay)
            cam.fx += b.fx * distance
            cam.fy += b.fy * distance
            cam.fz += b.fz * distance
            flight.velocity *= decay
            cam.dirty = true
          } else flight.velocity = 0
          if (cam.anim) {
            const { t0, ms, from, to } = cam.anim
            const t = clamp((now - t0) / ms, 0, 1)
            const e = easeInOut(t)
            for (const k of ['yaw', 'pitch', 'dist', 'fx', 'fy', 'fz', 'panX', 'panY']) {
              if (to[k] !== undefined && from[k] !== undefined) cam[k] = from[k] + (to[k] - from[k]) * e
            }
            if (t >= 1) cam.anim = null
            cam.dirty = true
          }

          const anims = atomAnims.current
          if (anims.size) {
            const t = performance.now()
            const done = []
            for (const [id, a] of anims) {
              const atom = scene.byId.get(id)
              if (!atom) { anims.delete(id); continue }
              const p = clamp((t - a.t0) / a.ms, 0, 1)
              const e = easeInOut(p)
              atom.x = a.from.x + (a.to.x - a.from.x) * e
              atom.y = a.from.y + (a.to.y - a.from.y) * e
              atom.z = a.from.z + (a.to.z - a.from.z) * e
              if (p > 0 && Math.hypot(atom.x - a.from.x, atom.y - a.from.y, atom.z - a.from.z) > 1e-8) {
                a.changed = true
                posRef.current.set(id, { x: atom.x, y: atom.y, z: atom.z })
              }
              if (p >= 1) {
                if (a.changed) done.push(id)
                anims.delete(id)
              }
            }
            cam.dirty = true
            if (done.length) handlersRef.current.onMoved(done)
          }

          // Display drift requires continuous redraw, but never changes stored positions.
          const busy = Boolean(cam.anim) || flight.velocity !== 0 || anims.size > 0
            || Boolean(dragRef.current) || fadeBusyRef.current
            || now - lastInputRef.current < 320
          const idle = !busy
          if (idle && now - lastDrawRef.current < 32) return
          cam.dirty = false
          lastDrawRef.current = now
          draw(now, cam, idle)
        }

        const draw = (now, cam, idle) => {
          const { w, h } = sizeRef.current
          if (!w || !h) return
          const b = cameraBasis(cam)
          const focal = h / 2 / Math.tan(FOV / 2)
          const startDist = SCALE_REF
          const fi = focusRef.current
          const active = fi.active
          const selNow = selRef.current
          const panX = cam.panX || 0
          const panY = cam.panY || 0

          const project = (atom) => {
            const vx = atom.x - b.ex
            const vy = atom.y - b.ey
            const vz = atom.z - b.ez
            // Clamp at the near plane instead of returning null: a point that slides behind the
            // camera then projects far off-screen, so the edge to it exits the view smoothly.
            const depth = vx * b.fx + vy * b.fy + vz * b.fz
            const zc = Math.max(depth, 0.35)
            const xc = vx * b.rx + vz * b.rz
            const yc = vx * b.ux + vy * b.uy + vz * b.uz
            const unit = focal / zc
            return {
              x: w / 2 + xc * unit + panX,
              y: h / 2 - yc * unit + panY,
              z: zc,
              depth,
              distance: Math.hypot(vx, vy, vz),
              unit,
              k: clamp(startDist / zc, 0.3, 1.55),
            }
          }

          // ---- which atoms are on stage -----------------------------------------
          const vis = visRef.current
          const wantKey = selNow ? (selNow.type === 'atom' ? `a:${selNow.id}` : `e:${selNow.key}`) : active ? 'filter' : 'all'
          if (vis.key !== wantKey || vis.fi !== fi || vis.scene !== scene) {
            vis.key = wantKey
            vis.fi = fi
            vis.scene = scene
            if (wantKey === 'all') vis.set = null
            else if (wantKey === 'filter') vis.set = new Set(fi.focus)
            else {
              const set = new Set()
              if (selNow.type === 'atom') {
                set.add(selNow.id)
                // 父组件按同一颗原子算好的两层邻域；拿不到时退回一跳邻居。
                const ring = focusRingRef.current
                if (ring && ring.center.id === selNow.id) for (const id of ring.ring) set.add(id)
                else {
                  for (const e of scene.edges) {
                    if (e.kind !== 'rel') continue
                    if (e.src === selNow.id) set.add(e.dst)
                    else if (e.dst === selNow.id) set.add(e.src)
                  }
                }
              } else {
                const found = scene.edges.find((x) => x.key === selNow.key)
                if (found) { set.add(found.src); set.add(found.dst) }
              }
              vis.set = set
            }
          }
          const shown = vis.set
          const fade = fadeRef.current
          const fresh = fade.scene !== scene
          const targetAlpha = (id) => {
            if (!shown || shown.has(id)) return 1
            const target = selNow && !active ? 0.28 : 0
            if (selNow && selNow.type === 'atom') {
              return 1 + (target - 1) * smoothstep(0, 450, now - retreatEndRef.current)
            }
            return target
          }
          if (fresh || fade.key !== wantKey || fade.shown !== shown) {
            const previous = fade.values
            fade.from = new Map(scene.atoms.map((atom) => [
              atom.id, fresh ? targetAlpha(atom.id) : (previous.get(atom.id) ?? 0),
            ]))
            fade.scene = scene
            fade.key = wantKey
            fade.shown = shown
            fade.t = now
          }
          const fadeT = clamp((now - fade.t) / 450, 0, 1)
          fadeBusyRef.current = fadeT < 1
          if (fadeT < 1) cam.dirty = true
          const alphaFor = (id) => {
            const target = targetAlpha(id)
            const from = fade.from.get(id) ?? target
            const alpha = from + (target - from) * smoothstep(0, 1, fadeT)
            fade.values.set(id, alpha)
            return alpha
          }

          const selectedCenter = selNow && selNow.type === 'atom' ? scene.byId.get(selNow.id) : null
          if (selectedCenter) avoidRef.current = { x: selectedCenter.x, y: selectedCenter.y, z: selectedCenter.z }
          const avoidCenter = selectedCenter || (!selNow && !active && fadeT < 1 ? avoidRef.current : null)
          const world = new Map()
          const stableWorld = new Map()
          for (const atom of scene.atoms) {
            let x = atom.x
            let y = atom.y
            let z = atom.z
            if (avoidCenter && (!shown || !shown.has(atom.id))) {
              const dx = atom.x - avoidCenter.x
              const dy = atom.y - avoidCenter.y
              const dz = atom.z - avoidCenter.z
              const d = Math.hypot(dx, dy, dz)
              if (d > 0.001 && d < 7) {
                const amount = (1 - smoothstep(0, 7, d)) * 1.5 * (1 - alphaFor(atom.id)) / 0.72 / d
                x += dx * amount
                y += dy * amount
                z += dz * amount
              }
            }
            stableWorld.set(atom.id, { x, y, z })
            const phase = atom.id * 2.399963
            const seconds = now / 1000
            const distance = Math.hypot(x - b.ex, y - b.ey, z - b.ez)
            const drift = 0.025 * (1 / 3 + 2 / 3 * smoothstep(SCALE_REF / 0.72, 12, distance))
            world.set(atom.id, {
              x: x + drift * Math.sin(seconds * 0.47 + phase),
              y: y + drift * Math.sin(seconds * 0.39 + phase * 1.7),
              z: z + drift * Math.cos(seconds * 0.43 + phase),
            })
          }

          const positions = new Map()
          // Per-atom write cache. It must be the single source of truth for what was last
          // written: hiding an atom through any path has to go through it too, otherwise the
          // next frame computes `display: ''`, sees the cache already saying `''`, skips the
          // write, and the atom stays hidden for good.
          const cacheFor = (el) => el.__em || (el.__em = {
            tx: '', ty: '', op: '', z: null, hit: null, sel: null, multi: null, disp: '',
            m: null, f: null, sc: null, w: 18, h: 18,
          })
          const hide = (el) => {
            if (!el) return
            if (el.style.display !== 'none') el.style.display = 'none'
            cacheFor(el).disp = 'none'
          }
          for (const atom of scene.atoms) {
            const el = atomEls.current.get(atom.id)
            const visibility = alphaFor(atom.id)
            if (visibility <= 0.001) {
              positions.set(atom.id, null)
              hide(el)
              continue
            }
            const p = project(world.get(atom.id))
            const stable = project(stableWorld.get(atom.id))
            if (stable.x < -4000 || stable.x > w + 4000 || stable.y < -4000 || stable.y > h + 4000) {
              positions.set(atom.id, null)
              hide(el)
              continue
            }
            const isSelAtom = Boolean(selNow && selNow.type === 'atom' && selNow.id === atom.id)
            const entry = {
              x: p.x, y: p.y, z: p.z, depth: p.depth, distance: stable.distance,
              stable, k: p.k, el, isSelAtom, visibility, w: 18, h: 18,
            }
            positions.set(atom.id, entry)
            if (!el) continue
            // Per-atom write cache: the loop runs for every visible atom on every frame, and
            // rewriting identical inline styles is pure overhead (and re-invalidates style
            // for the whole 168x54 subtree). Compare first, write only on a real change.
            const cache = cacheFor(el)
            entry.w = cache.w
            entry.h = cache.h
            const nearOpacity = smoothstep(0.05, 0.65, stable.depth)
            const opacity = nearOpacity * visibility
            const disp = opacity > 0.001 ? '' : 'none'
            if (cache.disp !== disp) { el.style.display = disp; cache.disp = disp }
            const opText = opacity.toFixed(2)
            if (cache.op !== opText) { el.style.opacity = opText; cache.op = opText }
            const clickable = opacity > 0.5 && (!shown || shown.has(atom.id)
              || (selNow && selNow.type === 'atom' && visibility > 0.5))
            const hit = clickable ? '1' : '0'
            if (cache.hit !== hit) { el.dataset.hit = hit; cache.hit = hit }
            const tx = p.x.toFixed(1)
            const ty = p.y.toFixed(1)
            if (cache.tx !== tx || cache.ty !== ty) {
              el.style.transform = `translate3d(${tx}px, ${ty}px, 0) translate(-50%, -50%)`
              cache.tx = tx
              cache.ty = ty
            }
            const selFlag = isSelAtom ? '1' : '0'
            if (cache.sel !== selFlag) { el.dataset.sel = selFlag; cache.sel = selFlag }
            const multiFlag = multiRef.current && multiRef.current.has(atom.id) ? '1' : '0'
            if (cache.multi !== multiFlag) { el.dataset.multi = multiFlag; cache.multi = multiFlag }
            const z = isSelAtom ? 34000 : Math.round(20000 - p.z * 100)
            if (cache.z !== z) { el.style.zIndex = String(z); cache.z = z }
            entry.clickable = clickable
          }

          // 卡片预算：钻进云团时把最近的一批变成卡片，更远的继续当小球。
          screenRef.current = positions
          // While the camera is parked, --m/--f/--sc cannot change (only the drift moves,
          // by ~0.02 units), so skip the whole morph pass and reuse the cached geometry.
          const morphDirty = !idle || !morphReadyRef.current || morphSceneRef.current !== scene
          if (morphDirty) {
            morphReadyRef.current = true
            morphSceneRef.current = scene
            const ranked = []
            for (const [, p] of positions) {
              if (!p || !p.el) continue
              if (p.stable.depth > 0.35 && p.stable.x >= -88 && p.stable.x <= w + 88
                && p.stable.y >= -28 && p.stable.y <= h + 28) ranked.push(p)
              else writeMorph(p, 0, farFactor(startDist / Math.max(p.distance, 0.01)))
            }
            ranked.sort((a, b2) => a.distance - b2.distance)
            for (let i = 0; i < ranked.length; i += 1) {
              const p = ranked[i]
              p.far = farFactor(startDist / Math.max(p.distance, 0.01))
              p.baseMorph = smoothstep(0.3, 0.72, clamp(startDist / Math.max(p.distance, 0.01), 0.26, 1.06))
              if (i >= CARD_BUDGET) p.baseMorph *= 1 - smoothstep(1, 1.6, i / CARD_BUDGET)
            }
            // 只有真的会画成卡片的邻居才参与「让位」计算：几百颗远处的点不该每帧比一遍。
            const near = ranked.filter((p) => p.baseMorph >= 0.1)
            for (const p of ranked) {
              let m = p.baseMorph
              for (const q of near) {
                if (q === p) continue
                const separation = Math.max(
                  Math.abs(p.stable.x - q.stable.x) / 168,
                  Math.abs(p.stable.y - q.stable.y) / 54,
                )
                const priority = smoothstep(-0.3, 0.3, p.distance - q.distance)
                m *= 1 - priority * smoothstep(0.1, 0.5, q.baseMorph) * (1 - smoothstep(0.55, 1.08, separation))
              }
              writeMorph(p, m, p.far)
            }
          }

          /**
           * Body geometry for one atom, plus the card's uniform scale. The card keeps its
           * 164x52 layout and is scaled down to the body's content box, so the text scales
           * with the box instead of being clipped by it (the old fixed 9px text in a
           * shrinking box was what looked cut off mid-word).
           */
          function writeMorph(p, m, far) {
            const el = p.el
            const cache = cacheFor(el)
            const base = 18 - 12 * far
            const bodyW = base + 150 * m
            const bodyH = base + 36 * m
            const contentW = bodyW - 2 - 2 * m
            const contentH = bodyH - 2
            const sc = clamp(Math.min(contentW / 164, contentH / 52), 0.04, 1)
            p.w = bodyW
            p.h = bodyH
            p.m = m
            cache.w = bodyW
            cache.h = bodyH
            const mText = m.toFixed(3)
            const fText = far.toFixed(3)
            const scText = sc.toFixed(3)
            if (cache.m !== mText) { el.style.setProperty('--m', mText); cache.m = mText }
            if (cache.f !== fText) { el.style.setProperty('--f', fText); cache.f = fText }
            if (cache.sc !== scText) { el.style.setProperty('--sc', scText); cache.sc = scText }
            // Layer promotion only for cards that are actually on screen; the 0.05 band is
            // hysteresis so a card hovering at the threshold cannot thrash its layer.
            const cardOn = cache.card === '1' ? m > 0.28 : m > 0.33
            const cardFlag = cardOn ? '1' : '0'
            if (cache.card !== cardFlag) { el.dataset.card = cardFlag; cache.card = cardFlag }
          }

          // ---- edges, on canvas -------------------------------------------------
          // One Path2D per (color, opacity, width) bucket: thousands of edges collapse into a
          // few dozen stroke() calls, and nothing in this loop touches the DOM.
          // The canvas is capped at 1.25x even on 2x displays: these are 1px, low-opacity
          // hairlines, and at 2x the stroke fill is the single most expensive thing on the
          // page (hiding the canvas doubles the frame rate at 2560x1440). DOM text stays at
          // the real DPR, so nothing legible loses sharpness.
          const dpr = Math.min(1.25, window.devicePixelRatio || 1)
          const prep = (el) => {
            if (!el) return null
            const pw = Math.round(w * dpr)
            const ph = Math.round(h * dpr)
            if (el.width !== pw || el.height !== ph) {
              el.width = pw
              el.height = ph
            }
            const c = el.getContext('2d')
            c.setTransform(dpr, 0, 0, dpr, 0, 0)
            c.clearRect(0, 0, w, h)
            return c
          }
          const back = prep(backRef.current)
          // The front canvas carries selection/hover highlights and labels. Preparing it
          // clears a full-viewport bitmap; with no selection that is pure waste, so it is
          // prepared lazily below (and cleared once after the last highlight disappears).
          let front = null
          let frontDirty = frontDirtyRef.current

          const anchorOn = (p, dirX, dirY) => {
            const hw = p.w / 2
            const hh = p.h / 2
            const ax = Math.abs(dirX) || 1e-6
            const ay = Math.abs(dirY) || 1e-6
            const t = Math.min(hw / ax, hh / ay)
            return { x: p.x + dirX * t, y: p.y + dirY * t }
          }

          const screenEdges = []
          const selKey = selNow && selNow.type === 'edge' ? selNow.key : null
          const labelsOn = labelsRef.current
          const zoomOut = cam.dist / (cam.fit || cam.dist)
          const coarse = zoomOut > 1.6
          const midCoarse = !coarse && zoomOut > 0.95
          const sampleEdge = (e, n) => (e.key.length + e.key.charCodeAt(0) + e.key.charCodeAt(e.key.length - 1)) % n === 0
          // Edge stride follows how many edges are actually on screen, not the zoom level:
          // the opening camera sits inside the cloud at a "close" zoom and used to stroke all
          // ~5.3k curves. Relation edges keep priority; the grey "judged, no relation" haze
          // (3.7k of them) is thinned twice as hard — it is background texture, not data.
          const load = edgeLoadRef.current
          const baseStride = load > 2400 ? 3 : load > 1400 ? 2 : 1
          let onScreen = 0
          const buckets = new Map()
          const labels = []
          const hotEdges = []
          let selEdge = null

          for (const edge of scene.edges) {
            let a = positions.get(edge.src)
            let c = positions.get(edge.dst)
            if (!a || !c || (a.depth <= 0.35 && c.depth <= 0.35)) continue
            // Clip the world-space segment at the near plane before projecting; clamping a
            // behind-eye endpoint independently invents a false line across the viewport.
            if (a.depth < 0.35 || c.depth < 0.35) {
              const aa = world.get(edge.src)
              const cc = world.get(edge.dst)
              const t = (0.35 - a.depth) / (c.depth - a.depth)
              const clipped = project({
                x: aa.x + (cc.x - aa.x) * t,
                y: aa.y + (cc.y - aa.y) * t,
                z: aa.z + (cc.z - aa.z) * t,
              })
              const point = { ...clipped, w: 0, h: 0, visibility: Math.min(a.visibility, c.visibility) }
              if (a.depth < 0.35) a = point
              else c = point
            }
            const edgeVisibility = Math.min(a.visibility, c.visibility)
            const isSel = selKey === edge.key
            const hot = Boolean(selNow && selNow.type === 'atom' && edge.kind === 'rel'
              && (edge.src === selNow.id || edge.dst === selNow.id))
            const offP = (p) => p.x < -80 || p.x > w + 80 || p.y < -80 || p.y > h + 80
            if (!isSel && offP(a) && offP(c)) continue
            onScreen += 1
            const stride = edge.kind === 'none' ? Math.min(8, baseStride * 2) : baseStride
            if (!isSel && !hot && stride > 1 && !sampleEdge(edge, stride)) continue

            const dx = c.x - a.x
            const dy = c.y - a.y
            const len = Math.hypot(dx, dy) || 1
            const bow = isSel ? 0.1 : edge.bow || 0.16
            const ctlX = (a.x + c.x) / 2 + (-dy / len) * bow * len
            const ctlY = (a.y + c.y) / 2 + (dx / len) * bow * len
            const la = Math.hypot(ctlX - a.x, ctlY - a.y) || 1
            const lc = Math.hypot(ctlX - c.x, ctlY - c.y) || 1
            const A = anchorOn(a, (ctlX - a.x) / la, (ctlY - a.y) / la)
            const C = anchorOn(c, (ctlX - c.x) / lc, (ctlY - c.y) / lc)

            const color = edge.edited ? EDITED_COLOR
              : edge.kind === 'none' ? NONE_COLOR : REL_COLORS[edge.rel] || NONE_COLOR
            const crossFade = edge.cross ? 0.45 : 1
            const depthFade = clamp(1.45 - Math.max(a.z, c.z) / (startDist * 1.85), 0.16, 1)
            const far = coarse || midCoarse
            const k = clamp(startDist / Math.min(a.z, c.z), 0.45, 1.65)
            const width = far ? Math.max(0.9, 1.15 * (edge.cross ? 0.8 : 1) * k)
              : 1.15 * (edge.cross ? 0.8 : 1) * k
            const baseOpacity = far
              ? Math.max(0.16 * crossFade, coarse ? 0.12 : 0.09)
              : 0.17 * crossFade * depthFade

            // 边平常只是背景纹理，不进命中表：必须先点中一颗原子，之后只有与它相连、
            // 画面上被点亮（hot）的那几条边才能点。这样点空白处永远不会“误中”一条边。
            const clickableEndpoint = (p) => p.depth > 0.35 && p.visibility > 0.5 && p.clickable
            if (hot && edgeVisibility > 0.5 && clickableEndpoint(a) && clickableEndpoint(c)) {
              screenEdges.push({ key: edge.key, edge, x1: A.x, y1: A.y, x2: C.x, y2: C.y, cx: ctlX, cy: ctlY })
            }

            if (isSel) {
              selEdge = { color, x1: A.x, y1: A.y, cx: ctlX, cy: ctlY, x2: C.x, y2: C.y, width }
            } else if (hot) {
              hotEdges.push({ color, x1: A.x, y1: A.y, cx: ctlX, cy: ctlY, x2: C.x, y2: C.y,
                width: Math.max(width, 1.4), visibility: edgeVisibility })
            } else if (back) {
              const op = Math.round(baseOpacity * edgeVisibility * 100) / 100
              const wd = Math.round(width * 2) / 2
              const bucketKey = `${color}|${op}|${wd}`
              let bucket = buckets.get(bucketKey)
              if (!bucket) {
                bucket = { color, op, wd, path: new Path2D() }
                buckets.set(bucketKey, bucket)
              }
              bucket.path.moveTo(A.x, A.y)
              bucket.path.quadraticCurveTo(ctlX, ctlY, C.x, C.y)
            }

            const incident = Boolean(selNow && selNow.type === 'atom' && edge.kind === 'rel'
              && (edge.src === selNow.id || edge.dst === selNow.id))
            const visible = isSel || (incident && len < 460) || (labelsOn && len > 120)
            if (!visible) continue
            labels.push({
              text: edge.kind === 'none' ? '无' : edge.rel,
              color,
              x: 0.25 * A.x + 0.5 * ctlX + 0.25 * C.x,
              y: 0.25 * A.y + 0.5 * ctlY + 0.25 * C.y,
              size: (isSel ? 11 : 9.5) * clamp(k, 0.8, 1.2),
              alpha: isSel ? 1 : 0.7,
              selected: isSel,
            })
          }
          // Smoothed so a strafing camera does not flicker between two strides.
          edgeLoadRef.current = Math.round(load * 0.7 + onScreen * 0.3)

          // Only touch the front canvas when it has something to hold (or has to be wiped).
          if (selEdge || hotEdges.length || labels.length || frontDirty) {
            front = prep(frontRef.current)
            frontDirty = Boolean(selEdge || hotEdges.length || labels.length)
            frontDirtyRef.current = frontDirty
          }

          if (back) {
            back.lineCap = 'round'
            for (const bucket of buckets.values()) {
              back.globalAlpha = bucket.op
              back.strokeStyle = bucket.color
              back.lineWidth = bucket.wd
              back.stroke(bucket.path)
            }
            back.globalAlpha = 1
          }

          if (front && hotEdges.length) {
            front.lineCap = 'round'
            for (const hot of hotEdges) {
              const path = new Path2D()
              path.moveTo(hot.x1, hot.y1)
              path.quadraticCurveTo(hot.cx, hot.cy, hot.x2, hot.y2)
              front.strokeStyle = hot.color
              front.globalAlpha = 0.18 * hot.visibility
              front.lineWidth = hot.width * 3.6
              front.stroke(path)
              front.globalAlpha = 0.95 * hot.visibility
              front.lineWidth = hot.width
              front.stroke(path)
            }
            front.globalAlpha = 1
          }

          if (front && selEdge) {
            const path = new Path2D()
            path.moveTo(selEdge.x1, selEdge.y1)
            path.quadraticCurveTo(selEdge.cx, selEdge.cy, selEdge.x2, selEdge.y2)
            front.lineCap = 'round'
            front.strokeStyle = selEdge.color
            front.globalAlpha = 0.34
            front.lineWidth = selEdge.width * 4.4
            front.stroke(path)
            front.globalAlpha = 1
            front.lineWidth = selEdge.width + 2
            front.stroke(path)
          }

          if (front && labels.length) {
            const halo = haloRef.current
            front.textAlign = 'center'
            front.textBaseline = 'alphabetic'
            front.lineJoin = 'round'
            for (const lb of placeEdgeLabels(labels)) {
              front.font = `600 ${lb.size.toFixed(1)}px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`
              front.globalAlpha = 0.9
              front.lineWidth = 3
              front.strokeStyle = halo
              front.strokeText(lb.text, lb.x, lb.y - 6)
              front.fillStyle = lb.color
              front.globalAlpha = lb.alpha
              front.fillText(lb.text, lb.x, lb.y - 6)
            }
            front.globalAlpha = 1
          }

          hitRef.current = screenEdges
        }

        raf = requestAnimationFrame(tick)
        return () => cancelAnimationFrame(raf)
      }, [scene, camRef, posRef])

      // Select an atom: highlight, retreat to fit all direct neighbors, then expand layers.
      useEffect(() => {
        const req = focusReq
        if (!req || !scene) return
        if (req.seq === lastReqRef.current) return
        const { w, h } = sizeRef.current
        if (!w || !h) return
        lastReqRef.current = req.seq
        const cam = camRef.current
        flightRef.current.velocity = 0
        const interrupted = [...atomAnims.current].filter(([, anim]) => anim.changed).map(([id]) => id)
        atomAnims.current.clear()
        if (interrupted.length) handlersRef.current.onMoved(interrupted)
        const b = cameraBasis(cam)
        const focal = h / 2 / Math.tan(FOV / 2)
        const usableW = Math.max(320, w - 364)
        // 检查器占右边：把投影中心左移，焦点才落在「真正可用」的画面中间。
        const panX = usableW / 2 - w / 2
        const D = FOCUS_DIST
        const startAnim = (atom, to, delay = 0) => atomAnims.current.set(atom.id, {
          from: { x: atom.x, y: atom.y, z: atom.z }, to, t0: performance.now() + delay, ms: 720,
        })

        if (req.mode === 'atom') {
          // 舞台排布用这份邻域；点亮/可点集合是父组件用同一函数算好传下来的 focusRing，
          // 两边同源，不会各算一套。
          const ring = atomFocusRing(scene, req.id)
          if (!ring) return
          const a = ring.center
          const to = { x: a.x, y: a.y, z: a.z }
          let retreat = Math.max(FOCUS_DIST, fitDistance(5, usableW / h))
          for (const id of ring.direct) {
            const mate = scene.byId.get(id)
            const dx = mate.x - to.x
            const dy = mate.y - to.y
            const dz = mate.z - to.z
            const forward = dx * b.fx + dy * b.fy + dz * b.fz
            const horizontal = Math.abs(dx * b.rx + dy * b.ry + dz * b.rz)
            const vertical = Math.abs(dx * b.ux + dy * b.uy + dz * b.uz)
            retreat = Math.max(retreat, 0.75 - forward,
              focal * horizontal / Math.max(1, usableW / 2 - 30) - forward,
              focal * vertical / Math.max(1, h / 2 - 30) - forward)
          }
          animateCamera(cam, { fx: to.x, fy: to.y, fz: to.z, dist: retreat, panX, panY: 0 }, 800, 250)
          retreatEndRef.current = performance.now() + 1050
          const levels = new Map([[a.id, 0]])
          for (const id of ring.first) levels.set(id, 1)
          for (const id of ring.second) levels.set(id, 2)
          for (const [id, depth] of levels) {
            if (!depth) continue
            const mate = scene.byId.get(id)
            const layer = depth === 1 ? ring.first : ring.second
            const index = layer.indexOf(id)
            const angle = index * 2.399963229728653 + depth * 0.7
            const z = 1 - 2 * (index + 0.5) / layer.length
            const r = Math.sqrt(1 - z * z)
            const target = depth === 1 ? 2.8 : 5
            const x = r * Math.cos(angle)
            const y = r * Math.sin(angle)
            startAnim(mate, {
              x: to.x + target * (b.rx * x + b.ux * y + b.fx * z),
              y: to.y + target * (b.ry * x + b.uy * y + b.fy * z),
              z: to.z + target * (b.rz * x + b.uz * y + b.fz * z),
            }, depth === 1 ? 1050 : 1450)
          }
        } else {
          const a = scene.byId.get(req.src)
          const c = scene.byId.get(req.dst)
          if (!a || !c) return
          const mid = { x: (a.x + c.x) / 2, y: (a.y + c.y) / 2, z: (a.z + c.z) / 2 }
          const focus = { x: mid.x + b.fx * PULL_IN, y: mid.y + b.fy * PULL_IN, z: mid.z + b.fz * PULL_IN }
          const half = ((usableW * 0.25) * D) / focal
          startAnim(a, { x: focus.x - b.rx * half, y: focus.y - b.ry * half, z: focus.z - b.rz * half })
          startAnim(c, { x: focus.x + b.rx * half, y: focus.y + b.ry * half, z: focus.z + b.rz * half })
          animateCamera(cam, { fx: focus.x, fy: focus.y, fz: focus.z, dist: D, panX, panY: 0 }, 820)
        }
        cam.dirty = true
      }, [focusReq, scene, camRef])

      const stopMotion = useCallback(() => {
        const cam = camRef.current
        cam.anim = null
        flightRef.current.velocity = 0
        const ids = [...atomAnims.current].filter(([, anim]) => anim.changed).map(([id]) => id)
        atomAnims.current.clear()
        if (ids.length) handlersRef.current.onMoved(ids)
      }, [camRef])

      // 框选的矩形只是纯视觉：直接写 style，不走 React state，免得每帧重渲染。
      const paintLasso = (x0, y0, x1, y1) => {
        const el = lassoElRef.current
        if (!el) return
        el.style.display = 'block'
        el.style.left = `${Math.min(x0, x1)}px`
        el.style.top = `${Math.min(y0, y1)}px`
        el.style.width = `${Math.abs(x1 - x0)}px`
        el.style.height = `${Math.abs(y1 - y0)}px`
      }
      const hideLasso = () => {
        const el = lassoElRef.current
        if (el) el.style.display = 'none'
      }

      // 边命中只在选中原子后的那几条高亮边上存在（见绘制循环），所以这里最多几十段，
      // 点击与悬停光标共用同一份几何。
      const pickEdgeAt = useCallback((x, y) => {
        let best = null
        let bestDist = 10
        for (const seg of hitRef.current) {
          let px = seg.x1
          let py = seg.y1
          for (let i = 1; i <= 12; i += 1) {
            const t = i / 12
            const mt = 1 - t
            const qx = mt * mt * seg.x1 + 2 * mt * t * seg.cx + t * t * seg.x2
            const qy = mt * mt * seg.y1 + 2 * mt * t * seg.cy + t * t * seg.y2
            const dx = qx - px
            const dy = qy - py
            const len2 = dx * dx + dy * dy || 1
            const tt = clamp(((x - px) * dx + (y - py) * dy) / len2, 0, 1)
            const d = Math.hypot(x - (px + dx * tt), y - (py + dy * tt))
            if (d < bestDist) { bestDist = d; best = seg }
            px = qx
            py = qy
          }
        }
        return best
      }, [])

      const onPointerDown = useCallback((e) => {
        // 左键拖空白 = 平移相机；中键拖 = 转视角；左键按在原子 = 拖那颗原子；右键长按 = 框选。
        lastInputRef.current = performance.now()
        if (stageRef.current) stageRef.current.style.cursor = ''
        const rotate = e.button === 1
        if (e.button !== 0 && !rotate && e.button !== 2) return
        e.preventDefault()
        const cam = camRef.current
        movedRef.current = 0

        if (e.button === 2) {
          const lasso = lassoRef.current
          clearTimeout(lasso.timer)
          lasso.armed = false
          lasso.box = null
          lasso.x0 = e.clientX
          lasso.y0 = e.clientY
          lasso.pointerId = e.pointerId
          lasso.timer = setTimeout(() => {
            lasso.timer = 0
            const stage = stageRef.current
            if (!stage) return
            const box = stage.getBoundingClientRect()
            lasso.box = box
            lasso.armed = true
            dragRef.current = { mode: 'lasso', x0: lasso.x0 - box.left, y0: lasso.y0 - box.top }
            try { stage.setPointerCapture(lasso.pointerId) } catch { /* ignore */ }
            paintLasso(lasso.x0 - box.left, lasso.y0 - box.top, lasso.x0 - box.left, lasso.y0 - box.top)
          }, LASSO_HOLD_MS)
          return
        }

        const card = e.button === 0 && e.target && e.target.closest ? e.target.closest('.emc-g-atom') : null
        if (card && card.dataset.id) {
          const id = Number(card.dataset.id)
          const atom = sceneRef.current.byId.get(id)
          if (atom) {
            const b = cameraBasis(cam)
            const h = sizeRef.current.h || 1
            const focal = h / 2 / Math.tan(FOV / 2)
            const zc = Math.max(0.15,
              (atom.x - b.ex) * b.fx + (atom.y - b.ey) * b.fy + (atom.z - b.ez) * b.fz)
            dragRef.current = {
              mode: 'atom', id, pointerId: e.pointerId, x: e.clientX, y: e.clientY,
              base: { x: atom.x, y: atom.y, z: atom.z }, b, scale: zc / focal, captured: false,
            }
            return
          }
        }
        dragRef.current = {
          mode: rotate ? 'rotate' : 'pan', x: e.clientX, y: e.clientY,
          yaw: cam.yaw, pitch: cam.pitch, panX: cam.panX || 0, panY: cam.panY || 0,
        }
        // Capture retargets the following `click` at the stage, which would swallow every card
        // click. Background drags capture; a drag that starts on a card keeps the card's click.
        if (!rotate) {
          try { e.currentTarget.setPointerCapture(e.pointerId) } catch { /* ignore */ }
        }
      }, [camRef])

      const onPointerMove = useCallback((e) => {
        const lasso = lassoRef.current
        if (!dragRef.current && !lasso.timer) {
          // 悬停在可点的高亮边上时给手型光标；没有选中原子时命中表是空的，这段几乎零成本。
          const stage = stageRef.current
          if (!stage) return
          if (hitRef.current.length) {
            const box = stage.getBoundingClientRect()
            const over = pickEdgeAt(e.clientX - box.left, e.clientY - box.top)
            const want = over ? 'pointer' : ''
            if (stage.style.cursor !== want) stage.style.cursor = want
          } else if (stage.style.cursor) stage.style.cursor = ''
          return
        }
        lastInputRef.current = performance.now()
        if (lasso.timer && !lasso.armed
          && Math.hypot(e.clientX - lasso.x0, e.clientY - lasso.y0) > 4) {
          clearTimeout(lasso.timer)
          lasso.timer = 0
        }
        const drag = dragRef.current
        if (!drag) return
        if (drag.mode === 'lasso') {
          if (!lasso.box) return
          paintLasso(drag.x0, drag.y0, e.clientX - lasso.box.left, e.clientY - lasso.box.top)
          return
        }
        const dx = e.clientX - drag.x
        const dy = e.clientY - drag.y
        movedRef.current = Math.max(movedRef.current, Math.hypot(dx, dy))
        const cam = camRef.current
        if (!drag.interrupted && movedRef.current > 4) {
          drag.interrupted = true
          stopMotion()
          if (drag.mode === 'atom') {
            const atom = sceneRef.current.byId.get(drag.id)
            if (atom) drag.base = { x: atom.x, y: atom.y, z: atom.z }
          } else if (drag.mode === 'pan') {
            drag.base = { x: cam.fx, y: cam.fy, z: cam.fz }
            drag.b = cameraBasis(cam)
            drag.scale = cam.dist / ((sizeRef.current.h || 1) / 2 / Math.tan(FOV / 2))
          }
        }
        if (drag.mode === 'atom') {
          if (movedRef.current <= 4) return
          if (!drag.captured) {
            drag.captured = true
            atomAnims.current.delete(drag.id)
            try { e.currentTarget.setPointerCapture(drag.pointerId) } catch { /* ignore */ }
          }
          const atom = sceneRef.current.byId.get(drag.id)
          if (atom) {
            atom.x = drag.base.x + (drag.b.rx * dx - drag.b.ux * dy) * drag.scale
            atom.y = drag.base.y + (drag.b.ry * dx - drag.b.uy * dy) * drag.scale
            atom.z = drag.base.z + (drag.b.rz * dx - drag.b.uz * dy) * drag.scale
            posRef.current.set(drag.id, { x: atom.x, y: atom.y, z: atom.z })
            cam.dirty = true
          }
        } else if (drag.mode === 'pan') {
          if (!drag.base) return
          cam.fx = drag.base.x + (-drag.b.rx * dx + drag.b.ux * dy) * drag.scale
          cam.fy = drag.base.y + (-drag.b.ry * dx + drag.b.uy * dy) * drag.scale
          cam.fz = drag.base.z + (-drag.b.rz * dx + drag.b.uz * dy) * drag.scale
          cam.dirty = true
        } else {
          cam.yaw = drag.yaw - dx * 0.006
          cam.pitch = clamp(drag.pitch + dy * 0.006, -1.36, 1.36)
          cam.dirty = true
        }
      }, [camRef, posRef, stopMotion, pickEdgeAt])

      const onPointerUp = useCallback((e) => {
        const lasso = lassoRef.current
        if (lasso.timer) {
          clearTimeout(lasso.timer)
          lasso.timer = 0
        }
        const drag = dragRef.current
        dragRef.current = null
        if (drag && drag.mode === 'lasso') {
          hideLasso()
          const box = lasso.box
          if (box && lasso.armed) {
            const x = e.clientX - box.left
            const y = e.clientY - box.top
            const rect = {
              x0: Math.min(drag.x0, x), x1: Math.max(drag.x0, x),
              y0: Math.min(drag.y0, y), y1: Math.max(drag.y0, y),
            }
            // 太小的框是误触：不当成「全不选」，直接不动选区。
            if (rect.x1 - rect.x0 > 3 && rect.y1 - rect.y0 > 3) {
              handlersRef.current.onLasso(lassoPick(screenRef.current, rect))
            }
          }
          lasso.armed = false
          lasso.box = null
          try { e.currentTarget.releasePointerCapture(e.pointerId) } catch { /* ignore */ }
          return
        }
        if (drag && drag.mode === 'atom' && movedRef.current > 4) handlersRef.current.onMoved([drag.id])
        try { e.currentTarget.releasePointerCapture(e.pointerId) } catch { /* ignore */ }
      }, [])

      const onWheel = useCallback((e) => {
        e.preventDefault()
        lastInputRef.current = performance.now()
        const cam = camRef.current
        if (cam.anim || atomAnims.current.size) stopMotion()
        const b = cameraBasis(cam)
        let nearest = Infinity
        for (const atom of sceneRef.current.atoms) {
          nearest = Math.min(nearest, Math.hypot(atom.x - b.ex, atom.y - b.ey, atom.z - b.ez))
        }
        const scale = clamp(Number.isFinite(nearest) ? nearest / 4 : 3, 0.35, 40)
        const pixels = e.deltaY * (e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? (sizeRef.current.h || 800) : 1)
        flightRef.current.velocity = clamp(
          flightRef.current.velocity - clamp(pixels, -1600, 1600) * 0.025 * scale, -480, 480,
        )
        cam.dirty = true
      }, [camRef, stopMotion])

      useEffect(() => {
        const el = stageRef.current
        if (!el) return undefined
        const handler = (e) => onWheel(e)
        el.addEventListener('wheel', handler, { passive: false })
        return () => el.removeEventListener('wheel', handler)
      }, [onWheel])

      const atomsTree = useMemo(() => scene.atoms.map((a) => h('div', {
        key: a.id,
        ref: (el) => setAtomEl(a.id, el),
        className: 'emc-g-atom',
        'data-id': a.id,
        'data-kind': a.kind,
        'data-sel': '0',
        'data-multi': '0',
        'data-hit': '0',
        'data-edited': a.edited_ts ? '1' : '0',
        title: `${a.text}\n\n${gFull(a.ts)} · ${a.project || '未归类'}`,
        onClick: (e) => {
          e.stopPropagation()
          if (movedRef.current > 5) return
          handlersRef.current.onSelectAtom(a.id)
        },
      }, h('div', { className: 'emc-g-body', style: { '--em-kind': KIND_COLORS[a.kind] || NONE_COLOR } },
        h('span', { className: 'emc-g-num' }, String(a.id)),
        h('div', { className: 'emc-g-card' },
          h('div', { className: 'emc-g-chead' },
            h('span', { className: 'emc-g-cid' }, '#' + a.id),
            h('span', { className: 'emc-g-ckind', style: { color: KIND_COLORS[a.kind] || 'inherit' } }, a.kind),
            h('span', { className: 'emc-g-ctime' }, gTime(a.ts))),
          h('div', { className: 'emc-g-ctext' }, a.text),
          a.edited_ts ? h('span', { className: 'emc-g-edited', title: '人工改过' }, '✎') : null)))), [scene, setAtomEl])

      return h('div', {
        ref: stageRef,
        className: 'emc-g-stage',
        onPointerDown,
        onPointerMove,
        onPointerUp,
        onPointerCancel: onPointerUp,
        onMouseDown: (e) => { if (e.button === 1) e.preventDefault() },
        onAuxClick: (e) => e.preventDefault(),
        // 右键是框选手势，不能弹出上下文菜单。
        onContextMenu: (e) => e.preventDefault(),
        onClick: (e) => {
          if (movedRef.current > 5) return
          if (e.target !== e.currentTarget) return
          const rect = stageRef.current.getBoundingClientRect()
          const best = pickEdgeAt(e.clientX - rect.left, e.clientY - rect.top)
          if (best) handlersRef.current.onSelectEdge(best.edge)
          else handlersRef.current.onBackground()
        },
        onPointerLeave: () => {
          const stage = stageRef.current
          if (stage && stage.style.cursor) stage.style.cursor = ''
        },
      },
      h('div', { className: 'emc-g-atoms' }, atomsTree),
      h('canvas', { ref: backRef, className: 'emc-g-canvas emc-g-back' }),
      h('canvas', { ref: frontRef, className: 'emc-g-canvas emc-g-front' }),
      h('div', { ref: lassoElRef, className: 'emc-g-lasso' }))
    }

    // ------------------------------------------------------------- inspectors

    function ProbBlock({ title, probs, chosen, t }) {
      if (!probs || !Object.keys(probs).length) {
        return h('div', { className: 'emc-hint' }, t('（这一问没有留档）'))
      }
      const entries = Object.entries(probs).sort((a, b) => Number(b[1]) - Number(a[1]))
      const max = Math.max(...entries.map((e) => Number(e[1])), 0.0001)
      return h('div', { className: 'emc-g-prob' },
        h('div', { className: 'emc-g-prob-title' }, title),
        entries.map(([label, p]) => h('div', {
          key: label, className: 'emc-g-prob-row', 'data-hi': label === chosen ? '1' : '0',
        },
        h('span', null, label),
        h('span', { className: 'track' }, h('span', {
          className: 'fill',
          style: {
            width: Math.max(1, Math.round((Number(p) / max) * 100)) + '%',
            background: REL_COLORS[label] || (label === '都不是' ? NONE_COLOR : '#8a8f99'),
          },
        })),
        h('span', { className: 'num' }, Number(p).toFixed(3)))))
    }

    function EndpointCard({ atom, onSelect, role }) {
      if (!atom) return null
      return h('button', {
        type: 'button', className: 'emc-g-endpoint', onClick: () => onSelect(atom.id),
      },
      h('span', { className: 'head' },
        h('span', { className: 'id' }, '#' + atom.id),
        h('span', { style: { color: KIND_COLORS[atom.kind] || 'inherit' } }, atom.kind),
        h('span', { className: 'role' }, role)),
      h('span', { className: 'text' }, atom.text))
    }

    function AtomInspector({ atom, scene, projects, t, onSaved, onSelectEdge, onClose }) {
      const [text, setText] = useState(atom.text)
      const [kind, setKind] = useState(atom.kind)
      const [project, setProject] = useState(atom.project || '未归类')
      const [busy, setBusy] = useState(false)
      const [err, setErr] = useState('')
      const [note, setNote] = useState('')

      useEffect(() => {
        setText(atom.text)
        setKind(atom.kind)
        setProject(atom.project || '未归类')
        setErr('')
        setNote('')
      }, [atom.id, atom.text, atom.kind, atom.project])

      const projectOptions = [
        { value: '未归类', label: '未归类' },
        ...(projects || []).filter((p) => p.status !== 'archived').map((p) => ({ value: p.name, label: p.name })),
      ]
      const dirty = text.trim() !== atom.text || kind !== atom.kind || project !== (atom.project || '未归类')
      const mine = scene.edges.filter((e) => e.src === atom.id || e.dst === atom.id)

      const save = async () => {
        setBusy(true)
        setErr('')
        setNote('')
        try {
          // 只发真的改了的字段：后端按「出现即改」处理，发全量会让一次改正文也触发候选重选。
          const body = { id: atom.id }
          if (text.trim() !== atom.text) body.text = text
          if (kind !== atom.kind) body.kind = kind
          if (project !== (atom.project || '未归类')) body.project = project === '未归类' ? null : project
          const res = await api('mem/graph/event', body)
          // 后端会连同 Embedding 向量一起重算；编码器不可用时只有正文落下，必须说出来。
          const base = res && res.vector === 'updated'
            ? t('已保存，Embedding 向量已重算（{n} 维）').replace('{n}', String(res.dim))
            : t('已保存；向量没有更新：') + ((res && res.note) || t('未知原因'))
          setNote(base + (res && res.reseat ? t('。项目/类型变了：下一轮连边会从这条之前重选候选，判过的对不再问，已有的边一条不删') : ''))
          await onSaved()
        } catch (e) {
          setErr(e && e.message ? e.message : String(e))
        } finally {
          setBusy(false)
        }
      }

      return h('div', { className: 'emc-g-insp', role: 'dialog', 'aria-label': '#' + atom.id },
        h('div', { className: 'emc-g-insp-head' },
          h('span', { className: 'emc-g-insp-title' }, '#' + atom.id),
          h('span', { className: 'emc-g-insp-sub' }, atom.kind),
          h('button', { type: 'button', className: 'emc-g-x', onClick: onClose, title: t('关闭') }, '✕')),
        h('div', { className: 'emc-g-meta' },
          h('div', null, gFull(atom.ts)),
          h('div', null, t('会话：') + shortSession(atom.session)),
          atom.edited_ts
            ? h('div', { className: 'emc-g-edited' }, t('✎ 人工修改于 ') + gTime(atom.edited_ts) + t('（彩虹描边）'))
            : null),
        h('label', { className: 'emc-g-field' },
          h('span', null, t('类型')),
          h('select', {
            className: 'emc-select', value: kind, onChange: (e) => setKind(e.target.value),
          }, G_KINDS.map((k) => h('option', { key: k, value: k }, k)))),
        h('label', { className: 'emc-g-field' },
          h('span', null, t('所属项目')),
          h('select', {
            className: 'emc-select', value: project, onChange: (e) => setProject(e.target.value),
          }, projectOptions.map((o) => h('option', { key: o.value, value: o.value }, o.label)))),
        h('label', { className: 'emc-g-field' },
          h('span', null, t('正文')),
          h('textarea', {
            className: 'emc-g-textarea', rows: 5, value: text,
            onChange: (e) => setText(e.target.value),
          })),
        err ? h('div', { className: 'emc-g-err' }, err) : null,
        note ? h('div', { className: 'emc-g-note' }, note) : null,
        h('div', { className: 'emc-g-actions' },
          h(Btn, { kind: 'primary sm', disabled: busy || !dirty || !text.trim(), onClick: save },
            busy ? t('保存中…') : t('保存并更新向量')),
          h('span', { className: 'emc-hint', style: { margin: 0 } },
            dirty ? t('保存会重算这条的 Embedding 向量（彩虹描边）') : t('未改动'))),
        h('div', { className: 'emc-g-section' },
          h('div', { className: 'emc-g-section-title' }, t('连着的边') + `（${mine.length}）`),
          mine.length === 0
            ? h('div', { className: 'emc-hint', style: { margin: 0 } }, t('没有边'))
            : h('div', { className: 'emc-g-edges' }, mine.map((e) => {
              const other = e.src === atom.id ? e.dst : e.src
              const dir = e.src === atom.id ? '←' : '→'
              return h('button', {
                key: e.key, type: 'button', className: 'emc-g-edge-row',
                onClick: () => onSelectEdge(e),
              },
              h('span', {
                className: 'dot',
                style: { background: e.kind === 'none' ? NONE_COLOR : REL_COLORS[e.rel] || NONE_COLOR },
              }),
              h('span', { className: 'rel' }, e.kind === 'none' ? t('无') : e.rel),
              h('span', { className: 'id' }, `${dir} #${other}`))
            }))))
    }

    function BatchPanel({ ids, projects, t, onDone, onClose }) {
      const [project, setProject] = useState('')
      const [kind, setKind] = useState('')
      const [busy, setBusy] = useState(false)
      const [err, setErr] = useState('')
      const [note, setNote] = useState('')
      const options = [
        { value: '', label: t('— 不改项目 —') },
        { value: '__none__', label: t('未归类（清空项目）') },
        ...(projects || []).filter((p) => p.status !== 'archived').map((p) => ({ value: p.name, label: p.name })),
      ]
      const dirty = Boolean(project || kind)

      const apply = async () => {
        if (!dirty || busy) return
        setBusy(true)
        setErr('')
        setNote('')
        try {
          const body = { ids }
          if (project) body.project = project === '__none__' ? null : project
          if (kind) body.kind = kind
          const res = await api('mem/graph/events', body)
          const v = (res && res.vector) || {}
          const bits = [t('已改 {n} 条').replace('{n}', String((res && res.updated) || ids.length))]
          if (v.updated || v.skipped) {
            bits.push(v.skipped
              ? t('向量 {a} 条重算 / {b} 条跳过（').replace('{a}', String(v.updated)).replace('{b}', String(v.skipped)) + (v.note || t('未知原因')) + '）'
              : t('向量 {n} 条已重算').replace('{n}', String(v.updated)))
          }
          if (res && res.cross) bits.push(t('跨项目标记 {n} 条已修正').replace('{n}', String(res.cross)))
          bits.push(res && res.reseat
            ? t('已标记重算候选边：下一轮连边会从最早的这条之前开始，判过的对不再问')
            : t('候选边不用重算：游标已经在这批事件之前'))
          if (res && res.missing && res.missing.length) {
            bits.push(t('{n} 条不存在，已跳过').replace('{n}', String(res.missing.length)))
          }
          setNote(bits.join(' · '))
          setProject('')
          setKind('')
          await onDone()
        } catch (e) {
          setErr(e && e.message ? e.message : String(e))
        } finally {
          setBusy(false)
        }
      }

      return h('div', { className: 'emc-g-insp', role: 'dialog', 'aria-label': t('批量编辑') },
        h('div', { className: 'emc-g-insp-head' },
          h('span', { className: 'emc-g-insp-title' }, t('批量编辑')),
          h('span', { className: 'emc-g-insp-sub' }, ids.length + t(' 个原子')),
          h('button', { type: 'button', className: 'emc-g-x', onClick: onClose, title: t('取消选择') }, '✕')),
        h('label', { className: 'emc-g-field' },
          h('span', null, t('所属项目')),
          h('select', {
            className: 'emc-select', value: project, onChange: (e) => setProject(e.target.value),
          }, options.map((o) => h('option', { key: o.value, value: o.value }, o.label)))),
        h('label', { className: 'emc-g-field' },
          h('span', null, t('类型')),
          h('select', {
            className: 'emc-select', value: kind, onChange: (e) => setKind(e.target.value),
          }, [{ value: '', label: t('— 不改类型 —') }]
            .concat(G_KINDS.map((k) => ({ value: k, label: k })))
            .map((o) => h('option', { key: o.value, value: o.value }, o.label)))),
        err ? h('div', { className: 'emc-g-err' }, err) : null,
        note ? h('div', { className: 'emc-g-note' }, note) : null,
        h('div', { className: 'emc-g-actions' },
          h(Btn, { kind: 'primary sm', disabled: busy || !dirty, onClick: apply },
            busy ? t('应用中…') : t('应用')),
          h('span', { className: 'emc-hint', style: { margin: 0 } },
            dirty ? t('改项目会重算向量与跨项目标记；已有的边一条不删') : t('选项目或类型后应用'))),
        h('div', { className: 'emc-hint', style: { margin: 0 } },
          t('编号：') + ids.slice(0, 12).join('、') + (ids.length > 12 ? ' …' : '')))
    }

    function EdgeInspector({ edge, scene, data, t, onSaved, onSelectAtom, onClose }) {
      const [rel, setRel] = useState(edge.rel || '')
      const [busy, setBusy] = useState(false)
      const [err, setErr] = useState('')

      useEffect(() => {
        setRel(edge.rel || '')
        setErr('')
      }, [edge.key, edge.rel])

      const src = scene.byId.get(edge.src)
      const dst = scene.byId.get(edge.dst)
      const pair = edge.pair
        || (data.pairs || []).find((p) => p.src === edge.src && p.dst === edge.dst)
        || null
      const ownAxis = edge.probs && Object.keys(edge.probs).length >= 4 ? edge.probs : null
      const ownDetail = edge.probs && Object.keys(edge.probs).length === 3 ? edge.probs : null
      const axisProbs = (pair && pair.axis_probs) || ownAxis
      const detailProbs = (pair && pair.detail_probs) || ownDetail

      const save = async () => {
        if (!rel) return
        setBusy(true)
        setErr('')
        try {
          await api('mem/graph/edge', {
            src: edge.src, dst: edge.dst, rel,
            old_rel: edge.kind === 'rel' ? edge.rel : null,
            conf: edge.conf,
          })
          await onSaved()
        } catch (e) {
          setErr(e && e.message ? e.message : String(e))
        } finally {
          setBusy(false)
        }
      }

      const remove = async () => {
        if (edge.kind !== 'rel') return
        setBusy(true)
        setErr('')
        try {
          await api('mem/graph/edge/delete', { src: edge.src, dst: edge.dst, rel: edge.rel })
          await onSaved()
        } catch (e) {
          setErr(e && e.message ? e.message : String(e))
        } finally {
          setBusy(false)
        }
      }

      return h('div', { className: 'emc-g-insp', role: 'dialog', 'aria-label': t('关系') },
        h('div', { className: 'emc-g-insp-head' },
          h('span', {
            className: 'emc-g-insp-title',
            style: { color: edge.kind === 'none' ? 'var(--dsw-alias-label-secondary)' : REL_COLORS[edge.rel] },
          }, edge.kind === 'none' ? t('判定：无关系') : edge.rel),
          h('span', { className: 'emc-g-insp-sub' }, `${edge.src} → ${edge.dst}`),
          h('button', { type: 'button', className: 'emc-g-x', onClick: onClose, title: t('关闭') }, '✕')),
        h('div', { className: 'emc-g-meta' },
          h('div', null, `#${edge.src}（后） → #${edge.dst}（前）`),
          edge.kind === 'rel'
            ? h('div', null, t('置信 ') + Number(edge.conf).toFixed(3) + ' · '
              + (edge.decidedBy === 'human' ? t('人工') : edge.decidedBy === 'jev' ? t('模型判定') : edge.decidedBy))
            : h('div', null, t('这一对问过模型，但没有过 margin —— 没有连边')),
          edge.edited ? h('div', { className: 'emc-g-edited' }, t('✎ 人工改过（彩虹描边）')) : null),
        h('div', { style: { display: 'flex', flexDirection: 'column', gap: '6px' } },
          h(EndpointCard, { atom: src, role: t('后'), onSelect: onSelectAtom }),
          h(EndpointCard, { atom: dst, role: t('前'), onSelect: onSelectAtom })),
        h(ProbBlock, { title: t('问题一 · 后一条相对前一条'), probs: axisProbs,
          chosen: (pair && pair.axis_label) || edge.rel, t }),
        h(ProbBlock, {
          title: t('问题二 · 补充关系'),
          probs: detailProbs,
          chosen: (pair && pair.detail_label) || (detailProbs === edge.probs ? edge.rel : null),
          t,
        }),
        h('label', { className: 'emc-g-field' },
          h('span', null, edge.kind === 'rel' ? t('改这条边的关系') : t('人工连一条边')),
          h('select', {
            className: 'emc-select', value: rel, onChange: (e) => setRel(e.target.value),
          }, [{ value: '', label: t('— 选一个关系 —') }]
            .concat(G_RELS.map((r) => ({ value: r, label: r })))
            .map((o) => h('option', { key: o.value, value: o.value }, o.label)))),
        err ? h('div', { className: 'emc-g-err' }, err) : null,
        h('div', { className: 'emc-g-actions' },
          h(Btn, { kind: 'primary sm', disabled: busy || !rel || rel === edge.rel, onClick: save },
            busy ? t('保存中…') : edge.kind === 'rel' ? t('保存关系') : t('连上')),
          edge.kind === 'rel'
            ? h(Btn, { kind: 'sm danger', disabled: busy, onClick: remove }, t('断开'))
            : null,
          h('span', { className: 'emc-hint', style: { margin: 0 } }, t('人工改过的边会蒙上彩虹光'))))
    }

    // ------------------------------------------------------------- filter panel

    function GraphFilters({
      filters, patch, projectOptions, sessionOptions, dateOptions, idSpec, idUnknown,
      sem, semTop, setSemTop, vectorCount, activeCount, onClearAll, onSelectHit, onClearSemantic, t,
    }) {
      const pick = (key, options) => h('select', {
        className: 'emc-select', value: filters[key], onChange: (e) => patch({ [key]: e.target.value }),
      }, options.map((o) => h('option', { key: o.value, value: o.value }, o.label)))

      return h('div', { className: 'emc-g-panel' },
        h('h4', null, t('项目')),
        pick('project', projectOptions),
        h('h4', null, t('会话')),
        pick('session', sessionOptions),
        h('h4', null, t('日期')),
        pick('date', dateOptions),
        h('h4', null, t('类型')),
        h('div', { className: 'emc-g-chips' }, G_KINDS.map((k) => h('button', {
          key: k, type: 'button', className: 'emc-g-chip',
          'data-on': filters.kinds.includes(k) ? '1' : '0',
          onClick: () => patch({ kinds: toggleValue(filters.kinds, k) }),
        }, h('span', { className: 'dot', style: { background: KIND_COLORS[k] } }), k))),
        h('h4', null, t('关系')),
        h('div', { className: 'emc-g-chips' }, G_RELS.map((r) => h('button', {
          key: r, type: 'button', className: 'emc-g-chip',
          'data-on': filters.rels.includes(r) ? '1' : '0',
          onClick: () => patch({ rels: toggleValue(filters.rels, r) }),
        }, h('span', { className: 'line', style: { background: REL_COLORS[r] } }), r)),
        h('button', {
          type: 'button', className: 'emc-g-chip',
          'data-on': filters.rels.includes('none') ? '1' : '0',
          onClick: () => patch({ rels: toggleValue(filters.rels, 'none') }),
        }, h('span', { className: 'line', style: { borderTop: '1px dashed ' + NONE_COLOR } }), t('无'))),
        h('h4', null, t('序号')),
        h('input', {
          className: 'emc-g-input', value: filters.ids, placeholder: '12 / 30-42 / #56',
          'aria-label': t('序号'),
          onChange: (e) => patch({ ids: e.target.value }),
        }),
        idSpec.bad.length
          ? h('div', { className: 'emc-g-err' }, t('看不懂：') + idSpec.bad.join('、'))
          : null,
        idUnknown
          ? h('div', { className: 'emc-hint', style: { margin: 0 } },
            t('{n} 个编号不在库里').replace('{n}', String(idUnknown)))
          : null,
        h('h4', null, t('语义')),
        h('div', { className: 'emc-g-sem' },
          h('input', {
            className: 'emc-g-input', type: 'number', min: 1, max: 200, value: semTop,
            style: { width: '64px' }, 'aria-label': t('语义命中数'),
            onChange: (e) => setSemTop(Number(e.target.value) || 16),
          }),
          h('span', { className: 'emc-g-count' }, t('条')),
          sem && sem.hits && sem.hits.length
            ? h('span', { className: 'emc-g-count' },
              t('扫描 ') + String(sem.scanned ?? '—') + (sem.skipped ? ` · ${t('跳过')} ${sem.skipped}` : ''))
            : null),
        sem && sem.q
          ? h('div', { style: { display: 'flex', flexDirection: 'column', gap: '2px' } },
            sem.hits.slice(0, 12).map((hit) => h('button', {
              key: hit.id, type: 'button', className: 'emc-g-edge-row',
              onClick: () => onSelectHit(hit.id),
            }, h('span', { className: 'rel' }, '#' + hit.id),
            h('span', { className: 'id' }, Number(hit.score).toFixed(3)))),
            sem.error ? h('div', { className: 'emc-g-err' }, sem.error) : null)
          : h('div', { className: 'emc-hint', style: { margin: 0 } },
            vectorCount ? t('在上面输入一句描述，回车检索') : t('库里还没有事件向量')),
        h('div', { className: 'emc-g-actions' },
          activeCount
            ? h(Btn, { kind: 'sm', onClick: onClearAll }, t('清空筛选'))
            : null,
          sem && sem.q ? h(Btn, { kind: 'sm ghost', onClick: onClearSemantic }, t('清除语义')) : null))
    }

    /**
     * A render error inside the graph must not take the whole console down: the daemon is
     * still reachable, and the other five tabs have nothing to do with canvas/geometry.
     */
    const GraphBoundary = React.Component ? class GraphBoundary extends React.Component {
      constructor(props) {
        super(props)
        this.state = { error: null }
      }

      static getDerivedStateFromError(error) {
        return { error }
      }

      render() {
        const t = this.props.t
        if (this.state.error) {
          const message = String((this.state.error && this.state.error.message) || this.state.error)
          return h('div', { className: 'emc-g' },
            h('div', { className: 'emc-g-center' },
              h('div', { className: 'emc-g-msg' },
                h('div', { className: 't' }, t('图谱渲染失败')),
                h('div', { className: 'b' }, message),
                h(Btn, { onClick: () => this.setState({ error: null }) }, t('重试')))))
        }
        return this.props.children
      }
    } : null

    // ---------------------------------------------------------------- graph page

    function Graph({ t, notify }) {
      const [data, setData] = useState(null)
      const [error, setError] = useState('')
      const [loading, setLoading] = useState(true)
      const [filters, setFilters] = useState({ project: '', session: '', date: '', kinds: [], rels: [], ids: '' })
      const [mode, setMode] = useState('only')
      const [showLabels, setShowLabels] = useState(false)
      const [panelOpen, setPanelOpen] = useState(false)
      const [semQ, setSemQ] = useState('')
      const [semTop, setSemTop] = useState(16)
      const [sem, setSem] = useState(null)
      const [semBusy, setSemBusy] = useState(false)
      const [pulled, setPulled] = useState(() => new Set())
      const [sel, setSel] = useState(null)
      const [multi, setMulti] = useState(() => new Set())
      const [focusReq, setFocusReq] = useState(null)
      const [layoutEpoch, setLayoutEpoch] = useState(0)
      const seqRef = useRef(0)
      // 用户拖过/拉过的位置：本地权威，落库前也不回弹。
      const posRef = useRef(new Map())
      const seededRef = useRef(false)
      const pendingRef = useRef(new Set())
      const saveTimer = useRef(0)
      const barRef = useRef(null)
      // 筛选面板是浮在图谱上面的一层：按下它外面时先关面板，这一下不再穿透到画布
      // 去选中原子/边；第二次点才真正生效。这里存的是「这一下要吞掉」的时间戳。
      const panelGuardRef = useRef(0)
      const camRef = useRef({
        yaw: 0.55, pitch: 0.26, dist: START_DIST, fx: 0, fy: 0, fz: 0,
        panX: 0, panY: 0, fit: 40, min: 6, max: 300, anim: null, dirty: true, ready: false,
      })

      const load = useCallback(async () => {
        try {
          const next = await api('mem/graph')
          setData(next)
          setError('')
        } catch (e) {
          setError(e && e.message ? e.message : String(e))
        } finally {
          setLoading(false)
        }
      }, [])

      useEffect(() => { load() }, [load])
      useEffect(() => () => { if (saveTimer.current) clearTimeout(saveTimer.current) }, [])

      const rawScene = useMemo(() => {
        if (!data) return null
        if (!seededRef.current) {
          seededRef.current = true
          for (const p of data.positions || []) posRef.current.set(p.id, { x: p.x, y: p.y, z: p.z })
        }
        return buildScene(data, posRef.current)
      }, [data, layoutEpoch])

      // 关系筛选直接裁剪边表：被筛掉的边既不画、也进不了命中表。
      const scene = useMemo(() => {
        if (!rawScene) return null
        if (!filters.rels.length) return rawScene
        const keep = new Set(filters.rels)
        return { ...rawScene, edges: rawScene.edges.filter((e) => keep.has(edgeRelKey(e))) }
      }, [rawScene, filters.rels])

      const totals = useMemo(() => {
        if (!rawScene) return { atoms: 0, rel: 0, none: 0 }
        let rel = 0
        let none = 0
        for (const e of rawScene.edges) {
          if (e.kind === 'none') none += 1
          else rel += 1
        }
        return { atoms: rawScene.atoms.length, rel, none }
      }, [rawScene])

      const idSpec = useMemo(() => parseIdSpec(filters.ids), [filters.ids])
      const idUnknown = useMemo(() => {
        if (!data || !idSpec.ids.size) return 0
        const known = new Set(data.events.map((ev) => ev.id))
        let missing = 0
        for (const id of idSpec.ids) if (!known.has(id)) missing += 1
        return missing
      }, [data, idSpec])

      const semActive = Boolean(sem && sem.q && !sem.error)
      const semIds = useMemo(() => (semActive ? new Set(sem.hits.map((h) => h.id)) : null), [semActive, sem])
      const relPool = useMemo(() => {
        if (!filters.rels.length) return null
        const ids = new Set()
        for (const e of (scene && scene.edges) || []) {
          ids.add(e.src)
          ids.add(e.dst)
        }
        return ids
      }, [filters.rels, scene])

      const facets = useMemo(
        () => facetCounts((data && data.events) || [], filters, idSpec.ids, semIds, semActive, relPool),
        [data, filters, idSpec, semIds, semActive, relPool],
      )

      const optionList = (map, emptyLabel, format, selected, sort) => {
        const list = [...map.entries()].sort(sort).map(([value, row]) => ({
          value, label: format(value, row),
        }))
        if (selected && !map.has(selected)) {
          list.unshift({ value: selected, label: format(selected, { n: 0 }) })
        }
        return [{ value: '', label: emptyLabel }, ...list]
      }
      const projectOptions = optionList(facets.project, t('项目：不限'),
        (v, r) => `${v} · ${r.n} ${t('条')}`, filters.project,
        (a, b) => String(a[0]).localeCompare(String(b[0]), 'zh'))
      const sessionOptions = optionList(facets.session, t('会话：不限'),
        (v, r) => `${shortSession(v)} · ${r.n} ${t('条')}`, filters.session,
        (a, b) => b[1].last - a[1].last)
      const dateOptions = optionList(facets.date, t('日期：不限'),
        (v, r) => `${v} · ${r.n} ${t('条')}`, filters.date,
        (a, b) => (a[0] < b[0] ? 1 : -1))

      const activeCount = [
        filters.project, filters.session, filters.date,
        filters.kinds.length ? 1 : 0, filters.rels.length ? 1 : 0,
        idSpec.ids.size ? 1 : 0, semActive ? 1 : 0,
      ].filter(Boolean).length

      const filterInfo = useMemo(() => {
        if (!scene) {
          return { active: false, match: new Set(), focus: new Set(), promo: new Set(),
            count: 0, edgeCount: 0, atomActive: false, relActive: false }
        }
        return computeFilterInfo({
          events: (data && data.events) || [],
          edges: scene.edges,
          filters,
          idSet: idSpec.ids,
          semIds: semActive ? new Set(sem.hits.map((h) => h.id)) : null,
          semActive,
          mode,
          pulled,
        })
      }, [data, scene, filters, idSpec, sem, semActive, mode, pulled])

      const filterKey = JSON.stringify([
        filters.project, filters.session, filters.date, filters.kinds, filters.rels, filters.ids,
        (sem && sem.q) || '', (sem && sem.error) || '', ((sem && sem.hits) || []).map((h) => h.id), mode,
      ])
      useEffect(() => { setPulled(new Set()) }, [filterKey])

      // 筛完之后命中的原子可能全在镜头外（默认镜头贴着最新原子），命中集变化时把镜头收拢。
      const fittedRef = useRef('')
      useEffect(() => {
        if (!filterInfo.active) {
          fittedRef.current = ''
          return undefined
        }
        if (!scene || sel || !filterInfo.focus.size) return undefined
        if (fittedRef.current === filterKey) return undefined
        const timer = setTimeout(() => {
          fittedRef.current = filterKey
          const atoms = []
          let n = 0
          let cx = 0
          let cy = 0
          let cz = 0
          for (const id of filterInfo.focus) {
            const a = scene.byId.get(id)
            if (!a) continue
            atoms.push(a)
            cx += a.x
            cy += a.y
            cz += a.z
            n += 1
          }
          if (!n) return
          cx /= n
          cy /= n
          cz /= n
          let radius = 1.5
          for (const a of atoms) radius = Math.max(radius, Math.hypot(a.x - cx, a.y - cy, a.z - cz))
          const cam = camRef.current
          const sinHalf = Math.min(0.95, Math.max(0.06, ((scene.radius || 1) * 1.12) / Math.max(1e-6, cam.fit)))
          const dist = Math.max(cam.min, Math.min(cam.max, (radius * 1.12) / sinHalf))
          animateCamera(cam, { dist, fx: cx, fy: cy, fz: cz, panX: 0, panY: 0 }, 620)
        }, 260)
        return () => clearTimeout(timer)
      }, [filterInfo, filterKey, scene, sel, camRef])

      useEffect(() => {
        if (!panelOpen) return undefined
        const outside = (e) => {
          if (barRef.current && barRef.current.contains(e.target)) return
          // 点在图谱舞台上：这次按下只负责关掉面板，同一个点击不能穿透去选中东西。
          if (IN_GRAPH_STAGE(e.target)) panelGuardRef.current = performance.now()
          setPanelOpen(false)
        }
        const escape = (e) => {
          if (e.key === 'Escape') {
            setPanelOpen(false)
            setSel(null)
          }
        }
        document.addEventListener('mousedown', outside)
        document.addEventListener('keydown', escape)
        return () => {
          document.removeEventListener('mousedown', outside)
          document.removeEventListener('keydown', escape)
        }
      }, [panelOpen])

      // 常驻的捕获阶段监听：门卫只挡「关面板的那一次点击」。每次新的按下先清门卫，
      // 这样按下后拖开（不产生 click）不会把门卫留到下一次点击；时间戳再兜一层底。
      useEffect(() => {
        const press = () => { panelGuardRef.current = 0 }
        const swallow = (e) => {
          if (!panelGuardRef.current) return
          const age = performance.now() - panelGuardRef.current
          panelGuardRef.current = 0
          if (age > 900 || !IN_GRAPH_STAGE(e.target)) return
          e.stopPropagation()
          e.preventDefault()
        }
        document.addEventListener('pointerdown', press, true)
        document.addEventListener('click', swallow, true)
        return () => {
          document.removeEventListener('pointerdown', press, true)
          document.removeEventListener('click', swallow, true)
        }
      }, [])

      const patch = useCallback((update) => {
        setFilters((prev) => ({ ...prev, ...update }))
        setSel(null)
        setMulti(new Set())
      }, [])

      const clearAll = useCallback(() => {
        setFilters({ project: '', session: '', date: '', kinds: [], rels: [], ids: '' })
        setSem(null)
        setSemQ('')
        setMode('only')
        setPulled(new Set())
        setSel(null)
        setMulti(new Set())
      }, [])

      const clearSemantic = useCallback(() => {
        setSem(null)
        setSemQ('')
      }, [])

      const runSemantic = useCallback(async () => {
        const q = semQ.trim()
        if (!q) {
          setSem(null)
          return
        }
        setSemBusy(true)
        setPanelOpen(true)
        try {
          const res = await api('mem/graph/semantic', { q, top: semTop })
          const hits = Array.isArray(res && res.hits)
            ? res.hits.map((x) => ({ id: Number(x.id), score: Number(x.score) }))
              .filter((x) => Number.isFinite(x.id) && Number.isFinite(x.score))
            : []
          setSem({
            q, hits,
            scanned: Number.isFinite(Number(res && res.scanned)) ? Number(res.scanned) : null,
            skipped: Number(res && res.skipped) || 0,
            error: '',
          })
        } catch (e) {
          setSem({ q, hits: [], scanned: null, skipped: 0, error: e && e.message ? e.message : String(e) })
        } finally {
          setSemBusy(false)
        }
      }, [semQ, semTop])

      const sceneRef = useRef(scene)
      sceneRef.current = scene

      // 与舞台高亮同一份邻域数据：哪两颗原子/哪几条边被点亮，就归哪一批可以一次点中。
      const selRing = useMemo(
        () => (scene && sel && sel.type === 'atom' ? atomFocusRing(scene, sel.id) : null),
        [scene, sel],
      )
      // 选中一条边时，亮着的是它的两端：点这两颗也是直接换选，不必先退出。
      const selEdgeEnds = useMemo(() => {
        if (!scene || !sel || sel.type !== 'edge') return null
        const edge = scene.edges.find((e) => e.key === sel.key)
        return edge ? new Set([edge.src, edge.dst]) : null
      }, [scene, sel])

      const selectAtom = useCallback((id, force) => {
        // 有焦点时：点自己 = 退出；点被点亮的两层邻域（或选中边的两端）= 直接换选；
        // 点别处仍然只退出（第二下才选中），保留原来防误点的规矩。
        if (sel && !force) {
          if (sel.type === 'atom' && sel.id === id) {
            setSel(null)
            return
          }
          const lit = (selRing && selRing.ring.has(id)) || (selEdgeEnds && selEdgeEnds.has(id))
          if (!lit) {
            setSel(null)
            return
          }
        }
        setSel({ type: 'atom', id })
        seqRef.current += 1
        setFocusReq({ seq: seqRef.current, mode: 'atom', id })
      }, [sel, selRing, selEdgeEnds])

      const selectEdge = useCallback((edge, force) => {
        // 平常边不可点（舞台命中表是空的）；能走到这里的边来自「选中原子」后的高亮集合，
        // 或者检查器里的明确点名。再点一次当前选中的边 = 退出。
        if (sel && !force) {
          if (sel.type === 'edge' && sel.key === edge.key) {
            setSel(null)
            return
          }
          if (!(selRing && selRing.edges.has(edge.key))) {
            setSel(null)
            return
          }
        }
        if (filterInfo.active) {
          setPulled((prev) => {
            const next = new Set(prev)
            next.add(edge.src)
            next.add(edge.dst)
            return next
          })
        }
        setSel({ type: 'edge', key: edge.key })
        seqRef.current += 1
        setFocusReq({ seq: seqRef.current, mode: 'edge', src: edge.src, dst: edge.dst })
      }, [sel, selRing, filterInfo])

      const resetView = useCallback(() => {
        const cam = camRef.current
        const seed = rawScene && rawScene.newestId != null ? rawScene.byId.get(rawScene.newestId) : null
        const direction = cameraBasis({ ...cam, yaw: 0.55, pitch: 0.26 })
        animateCamera(cam, {
          yaw: 0.55,
          pitch: 0.26,
          dist: START_DIST,
          fx: (seed ? seed.x : 0) + direction.fx * START_DIST,
          fy: (seed ? seed.y : 0) + direction.fy * START_DIST,
          fz: (seed ? seed.z : 0) + direction.fz * START_DIST,
          panX: 0,
          panY: 0,
        })
      }, [rawScene])

      const flushPositions = useCallback(() => {
        const ids = [...pendingRef.current]
        pendingRef.current.clear()
        if (!ids.length) return
        const positions = ids.map((id) => {
          const p = posRef.current.get(id)
          return p ? { id, x: p.x, y: p.y, z: p.z } : null
        }).filter(Boolean)
        if (!positions.length) return
        api('mem/graph/positions', { positions }).catch(() => {})
      }, [])

      // 位置落库是防抖的：连续拖动/连点只在停下来 450ms 后写一次。
      const onMoved = useCallback((ids) => {
        for (const id of ids) pendingRef.current.add(id)
        if (saveTimer.current) clearTimeout(saveTimer.current)
        saveTimer.current = setTimeout(flushPositions, 450)
      }, [flushPositions])

      const resetLayout = useCallback(async () => {
        try {
          await api('mem/graph/positions/clear', {})
        } catch {
          /* 后端不在也照样回到默认网格 */
        }
        posRef.current = new Map()
        pendingRef.current.clear()
        setSel(null)
        setMulti(new Set())
        setLayoutEpoch((e) => e + 1)
        camRef.current.ready = false
      }, [])

      const selectedAtom = sel && sel.type === 'atom' && scene ? scene.byId.get(sel.id) : null
      const selectedEdge = sel && sel.type === 'edge' && scene
        ? scene.edges.find((e) => e.key === sel.key)
          || ((rawScene && rawScene.edges.find((e) => e.key === sel.key)) || null)
        : null
      const multiIds = [...multi].filter((id) => scene && scene.byId.has(id)).sort((a, b) => a - b)
      const vectorCount = Number((data && data.vectors && data.vectors.events) || 0)

      if (!data && loading) {
        return h('div', { className: 'emc-g' },
          h('div', { className: 'emc-g-center' },
            h('div', { className: 'emc-g-msg' }, h('div', { className: 'b' }, t('载入记忆图谱…')))))
      }
      if (error && !data) {
        return h('div', { className: 'emc-g' },
          h('div', { className: 'emc-g-center' },
            h('div', { className: 'emc-g-msg' },
              h('div', { className: 't' }, t('读不到 eventmem 图谱')),
              h('div', { className: 'b' }, error),
              h('div', { className: 'b' }, t('确认 daemon 在跑（/api/health 通）再重试。')),
              h(Btn, { onClick: load }, t('重试')))))
      }
      if (!scene || scene.atoms.length === 0) {
        return h('div', { className: 'emc-g' },
          h('div', { className: 'emc-g-center' },
            h('div', { className: 'emc-g-msg' },
              h('div', { className: 't' }, t('还没有记忆原子')),
              h('div', { className: 'b' }, t('eventmem 里写过事件之后这里才有东西。')))))
      }

      return h('div', { className: 'emc-g' },
        h(GraphStage, {
          scene,
          focusInfo: filterInfo,
          sel,
          multi,
          focusReq,
          showLabels,
          camRef,
          posRef,
          onMoved,
          focusRing: selRing,
          onSelectAtom: selectAtom,
          onSelectEdge: selectEdge,
          onLasso: (ids) => {
            setSel(null)
            setMulti(new Set(ids))
          },
          onBackground: () => {
            setSel(null)
            setMulti(new Set())
          },
        }),

        // The bar and the filter panel must not live inside one another: `backdrop-filter`
        // on the bar makes it the containing block for absolute children, which shrank the
        // panel to the bar's height. The overlay wrapper keeps both anchored to `.emc-g`.
        h('div', { className: 'emc-g-top', ref: barRef },
          h('div', { className: 'emc-g-bar' },
          h('span', { className: 'emc-g-brand' }, t('记忆图谱')),
          h('button', {
            type: 'button', className: 'emc-g-fbtn', 'data-on': panelOpen ? '1' : '0',
            onClick: () => setPanelOpen((v) => !v),
            title: t('筛选：项目 / 会话 / 日期 / 类型 / 序号 / 关系 / 语义'),
          }, t('筛选'), activeCount ? h('span', { className: 'emc-g-fbadge' }, String(activeCount)) : null),
          h('div', { className: 'emc-g-sem' },
            h('input', {
              className: 'emc-g-input emc-g-seminput',
              value: semQ,
              placeholder: vectorCount ? t('语义检索：描述你要找的记忆…') : t('语义检索：库里还没有事件向量'),
              'aria-label': t('语义检索'),
              onChange: (e) => setSemQ(e.target.value),
              onKeyDown: (e) => {
                if (e.key === 'Enter') {
                  e.preventDefault()
                  runSemantic()
                }
              },
            }),
            h(Btn, { kind: 'sm ghost', disabled: semBusy || !vectorCount, onClick: runSemantic },
              semBusy ? t('检索中…') : t('检索')),
            sem ? h('span', {
              className: 'emc-g-semtag',
              title: sem.error || `${sem.q} · Top ${semTop}`,
            }, sem.error ? t('语义不可用') : `${t('语义')} ${sem.hits.length}`,
            h('button', {
              type: 'button', title: t('清除语义筛选'), onClick: clearSemantic,
            }, '✕')) : null),
          filterInfo.active
            ? h('div', { className: 'emc-g-seg', title: t('焦点范围') },
              [['only', t('仅命中')], ['neighbors', t('含一跳邻居')]].map(([k, label]) => h('button', {
                key: k, type: 'button', className: 'emc-g-seg-btn',
                'data-on': mode === k ? '1' : '0', onClick: () => setMode(k),
              }, label)))
            : null,
          h('label', { className: 'emc-check' },
            h('input', {
              type: 'checkbox', checked: showLabels,
              onChange: (e) => setShowLabels(e.target.checked),
            }),
            '  ' + t('关系名')),
          h('span', { className: 'emc-g-count' },
            `${totals.atoms} ${t('原子')} · ${totals.rel} ${t('边')}`
            + (totals.none ? ` · ${totals.none} ${t('灰边')}` : '')
            + (filterInfo.active ? ` · ${t('命中')} ${filterInfo.count} / ${filterInfo.edgeCount}` : '')),
          h('span', { className: 'emc-g-spacer' }),
          h(Btn, { kind: 'sm ghost', onClick: resetView }, t('重置视角')),
          h(Btn, { kind: 'sm ghost', onClick: resetLayout }, t('复原布局')),
          h(Btn, { kind: 'sm ghost', onClick: () => load() }, t('刷新'))),
          panelOpen
            ? h(GraphFilters, {
              filters, patch, projectOptions, sessionOptions, dateOptions,
              idSpec, idUnknown, sem, semTop, setSemTop, vectorCount, activeCount,
              onClearAll: clearAll, onClearSemantic: clearSemantic,
              onSelectHit: (id) => { if (scene.byId.has(id)) selectAtom(id, true) },
              t,
            })
            : null),

        h('div', { className: 'emc-g-legend' },
          G_KINDS.map((k) => h('span', { key: k, className: 'item' },
            h('span', { className: 'dot', style: { background: KIND_COLORS[k] } }), k)),
          G_RELS.map((r) => h('span', { key: r, className: 'item' },
            h('span', { className: 'line', style: { background: REL_COLORS[r] } }), r)),
          h('span', { className: 'item' },
            h('span', { className: 'line', style: { borderTop: '1px dashed ' + NONE_COLOR } }), t('无'))),

        selectedAtom
          ? h(AtomInspector, {
            atom: selectedAtom,
            scene,
            projects: data.projects || [],
            t,
            onSaved: load,
            onSelectEdge: (e) => selectEdge(e, true),
            onClose: () => setSel(null),
          })
          : null,
        multiIds.length
          ? h(BatchPanel, {
            ids: multiIds,
            projects: data.projects || [],
            t,
            onDone: load,
            onClose: () => setMulti(new Set()),
          })
          : null,
        selectedEdge
          ? h(EdgeInspector, {
            edge: selectedEdge,
            scene,
            data,
            t,
            onSaved: load,
            onSelectAtom: (id) => selectAtom(id, true),
            onClose: () => setSel(null),
          })
          : null)
    }


    function Console({ tr }) {
      const t = tr
      const [tab, setTab] = useState('overview')
      const [health, setHealth] = useState(null)
      const [status, setStatus] = useState(null)
      const [offline, setOffline] = useState(false)
      const [restartState, setRestartState] = useState('')
      const [restartPending, setRestartPending] = useState(false)
      const [confirmRestart, setConfirmRestart] = useState(false)
      const restartRef = useRef(null)
      const [setup, setSetup] = useState(null)
      const [setupBusy, setSetupBusy] = useState('')
      const [setupMessage, setSetupMessage] = useState('')
      const [testResult, setTestResult] = useState(null)
      const startedOnSetup = useRef(false)
      const [toast, notify] = useToast(t)

      const loadSetup = useCallback(async () => {
        try {
          setSetup(await api('setup'))
        } catch (error) {
          setSetup({ config: null, checks: [], error: error.message })
        }
      }, [])

      useEffect(() => { loadSetup() }, [loadSetup])
      useEffect(() => {
        // A fresh plugin activation with no daemon (or a blocking identity/token problem)
        // should land on the explanation, not on an empty overview. Only do it once; later
        // failures leave the operator's tab alone.
        const blocked = Boolean(setup && Array.isArray(setup.checks)
          && setup.checks.some((item) => item.state === 'bad'))
        if (!startedOnSetup.current && (offline || blocked)) {
          startedOnSetup.current = true
          setTab('setup')
        }
      }, [offline, setup])

      const testSetup = async (draft) => {
        setSetupBusy('test')
        setTestResult(null)
        setSetupMessage('')
        try {
          setTestResult(await api('setup/test', draft))
        } catch (error) {
          setTestResult({ ok: false, error: error.message, checks: [] })
        } finally {
          setSetupBusy('')
        }
      }

      const saveSetupDraft = async (draft) => {
        setSetupBusy('save')
        setTestResult(null)
        setSetupMessage('')
        try {
          await api('setup/save', draft)
          setSetupMessage(t('已保存。适配器正在重新加载，稍后会重新连接。'))
          setTimeout(() => { loadSetup() }, 1200)
        } catch (error) {
          setTestResult({ ok: false, error: error.message, checks: [] })
        } finally {
          setSetupBusy('')
        }
      }

      useEffect(() => {
        let alive = true
        let timer = null
        const tick = async () => {
          try {
            const nextHealth = await api('health')
            if (!alive) return
            setHealth(nextHealth)
            setOffline(false)
            const pending = restartRef.current
            if (pending && nextHealth.instance_id && nextHealth.instance_id !== pending.instanceId) {
              restartRef.current = null
              setRestartPending(false)
              setRestartState(t('服务已重启'))
              setStatus(null)
            }
          } catch {
            if (!alive) return
            setHealth(null)
            setOffline(true)
          }
          // Status is optional: a single failed read must not claim the daemon is down.
          let snapshot = null
          try {
            snapshot = await api('status')
            if (alive) setStatus(snapshot)
          } catch { /* keep the last snapshot */ }
          if (!alive) return
          // While a pass (or a backfill) is running the live strip ticks per answered pair,
          // so the poll tightens; idle pages do not need the traffic.
          const backfilling = Boolean(snapshot && snapshot.backfill &&
            ['running', 'pending'].includes(snapshot.backfill.state))
          const fast = Boolean(snapshot && (snapshot.live || snapshot.busy || backfilling))
          timer = setTimeout(tick, fast ? 1500 : 5000)
        }
        tick()
        return () => { alive = false; if (timer) clearTimeout(timer) }
      }, [])

      const requestRestart = async () => {
        setConfirmRestart(false)
        if (restartPending || !health || !health.instance_id) return
        const pending = { instanceId: health.instance_id }
        restartRef.current = pending
        setRestartPending(true)
        setRestartState(t('正在请求重启…'))
        try {
          await api('restart', {})
          if (restartRef.current === pending) setRestartState(t('正在等待服务重启…'))
        } catch (error) {
          if (restartRef.current !== pending) return
          if (error.status && error.status < 500) {
            restartRef.current = null
            setRestartPending(false)
            setRestartState(String(error.message))
            return
          }
          // A dropped connection may mean the restart started before the response arrived.
          setRestartState(t('重启请求未完成，请检查服务状态。') + (error.message ? ' ' + error.message : ''))
        }
        if (restartRef.current !== pending) return
        setTimeout(() => {
          if (restartRef.current !== pending) return
          restartRef.current = null
          setRestartPending(false)
          setRestartState(t('暂未确认服务重启，请检查服务状态。'))
        }, 120000)
      }
      useEffect(() => () => { restartRef.current = null }, [])

      const tabs = [
        ['overview', t('概览')],
        ['setup', t('连接')],
        ['backends', t('后端')],
        ['models', t('模型')],
        ['schedule', t('调度')],
        ['logs', t('日志')],
        ['graph', t('图谱')],
      ]

      const setupPage = h(Setup, {
        t,
        setup,
        testResult,
        busy: setupBusy,
        message: setupMessage,
        onTest: testSetup,
        onSave: saveSetupDraft,
        onRefresh: loadSetup,
      })

      return h('div', { className: 'emc' },
        h('style', null, CSS),
        h('div', { className: 'emc-head' },
          h('div', { className: 'emc-head-in' },
            h('div', null,
              h('h1', { className: 'emc-title' }, t('记忆后台')),
              h('p', { className: 'emc-intro' }, t('eventmem daemon 的记忆运维台：概览、后端链路、模型、连边调度与日志。'))),
            h('div', { className: 'emc-head-side' },
              h(Pill, { ok: !!health && !offline },
                offline ? t('daemon 不可达') : !health ? t('连接中…')
                  : `ok${health.schema !== undefined ? ' · schema ' + health.schema : ''}${health.pid ? ' · pid ' + health.pid : ''}`),
              h(Btn, { disabled: offline || !health || !health.instance_id || restartPending,
                onClick: () => setConfirmRestart(true) }, t('重启服务')),
              restartState ? h('div', { className: 'emc-restart-status', role: 'status', 'aria-live': 'polite' }, restartState) : null)),
          confirmRestart ? h('div', { className: 'emc-modal-backdrop', onClick: () => setConfirmRestart(false) },
            h('div', { className: 'emc-modal', role: 'dialog', 'aria-modal': true,
              'aria-label': t('重启服务'), onClick: (event) => event.stopPropagation(),
              onKeyDown: (event) => { if (event.key === 'Escape') setConfirmRestart(false) } },
              h('p', null, t('重启 eventmem 服务？服务将短暂不可用，正在进行的任务会先结束。')),
              h('div', { className: 'acts' },
                h('button', { type: 'button', className: 'emc-btn', autoFocus: true,
                  onClick: () => setConfirmRestart(false) }, t('取消')),
                h('button', { type: 'button', className: 'emc-btn danger', onClick: requestRestart }, t('重启服务'))))) : null,
          h('div', { className: 'emc-tabs', role: 'tablist', 'aria-label': t('记忆后台') },
            tabs.map(([key, label]) =>
              h('button', {
                key, type: 'button', role: 'tab', 'aria-selected': tab === key,
                className: 'emc-tab' + (tab === key ? ' on' : ''),
                onClick: () => setTab(key),
              }, label))),
        ),
        tab === 'graph'
          ? h('div', { className: 'emc-body bare' },
            GraphBoundary
              ? h(GraphBoundary, { t }, h(Graph, { t, notify }))
              : h(Graph, { t, notify }))
          : h('div', { style: { display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 } },
            h(LiveBar, { t, status, notify }),
            h('div', { className: 'emc-body' },
              h('div', { className: 'emc-body-in' },
                tab === 'overview' ? (health ? h(Overview, { t, status, notify }) : setupPage) : null,
                tab === 'setup' ? setupPage : null,
                tab === 'backends' ? h(Backends, { t, notify }) : null,
                tab === 'models' ? h(Models, { t, notify }) : null,
                tab === 'schedule' ? h(Schedule, { t, notify, status, setStatus }) : null,
                tab === 'logs' ? h(Logs, { t }) : null))),
        toast ? h('div', { className: 'emc-toast' + (toast.kind === 'err' ? ' err' : ''), role: 'status', 'aria-live': 'polite' }, toast.text) : null)
    }

    // ----------------------------------------------------------- sidebar icon

    /**
     * The host's own `database-outline` glyph (16px, 1px stroke), copied from the client
     * icon set so the entry sits beside 插件 / 自动化任务 without a foreign shape.
     */
    const DATABASE_PATHS = [
      'M13.1967 5.1869C13.7232 4.77378 14.0003 4.30517 14.0001 3.82819C14.0003 3.3512 13.7232 2.88259 13.1967 2.46947C12.6702 2.05635 11.9128 1.71328 11.0006 1.47475C10.0885 1.23621 9.05371 1.11062 8.00039 1.1106C6.94707 1.11057 5.9123 1.23612 5.00009 1.47461C4.08742 1.71301 3.32948 2.05604 2.80249 2.46919C2.2755 2.88235 1.99805 3.35106 1.99805 3.82819C1.99805 4.30531 2.2755 4.77402 2.80249 5.18718C3.32948 5.60033 4.08742 5.94336 5.00009 6.18176C5.9123 6.42025 6.94707 6.5458 8.00039 6.54578C9.05371 6.54575 10.0885 6.42016 11.0006 6.18163C11.9128 5.94309 12.6702 5.60002 13.1967 5.1869Z',
      'M2 3.80371V11.7848',
      'M14 3.80371V11.7848',
      'M2 7.81396C2 8.60524 2.63214 9.36411 3.75736 9.92363C4.88258 10.4832 6.4087 10.7975 8 10.7975C9.5913 10.7975 11.1174 10.4832 12.2426 9.92363C13.3679 9.36411 14 8.60524 14 7.81396',
      'M2 11.7847C2 12.6081 2.63214 13.3977 3.75736 13.98C4.88258 14.5622 6.4087 14.8893 8 14.8893C9.5913 14.8893 11.1174 14.5622 12.2426 13.98C13.3679 13.3977 14 12.6081 14 11.7847',
    ]

    function PanelIcon({ size }) {
      const side = size || 16
      return h('svg', {
        width: side, height: side, viewBox: '0 0 16 16', fill: 'none',
        'aria-hidden': true, focusable: false, strokeWidth: 1,
      }, DATABASE_PATHS.map((d, index) => h('path', { key: index, d, stroke: 'currentColor' })))
    }

    // ---------------------------------------------------------------- plugin

    return {
      inject: ['slots', 'locale'],
      apply(ctx) {
        ctx.effect(() => ctx.locale.register(NS, { zh: ZH, en: EN }), 'eventmem: locale')
        const t = ctx.locale.bind(NS)

        // The page first: the nav row must never point at a key that is not registered.
        ctx.slots.inject('main', () => ctx.slots.register({
          name: 'main',
          key: PANEL_ID,
          locale: NS,
          inject: () => ({ tr: t }),
        }, Console))

        ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
          name: 'sidebar.panellist',
          id: PANEL_ID,
          order: 5,
          label: () => t('记忆后台'),
          locale: NS,
        }, PanelIcon))
      },
    }
  },
})
