#!/usr/bin/env python3
"""eventmem — the console's English strings.

One dictionary, keyed by the **Chinese source string**, because the two UIs (the desktop
plugin and the page the daemon serves) are plain JS with no build step: a key-free map means
``t('跑一轮')`` reads as the string it replaces, and a missing entry degrades to the Chinese
rather than to a bare key.

Placeholders are ``{0}``/``{1}`` and are filled by ``fmt()`` on the JS side. Keep the Chinese
side of an entry byte-identical to the literal in the JS, or ``test_ui`` (which greps every
``t('…')``/``fmt('…')`` out of both files) will report it as missing.
"""

from __future__ import annotations

EN: dict[str, str] = {
    # ---- chrome ---------------------------------------------------------
    "记忆后台": "Memory console",
    "概览": "Overview",
    "后端": "Backends",
    "模型": "Models",
    "调度": "Schedule",
    "日志": "Logs",
    "图谱": "Graph",
    "跑一轮": "Run a pass",
    "跑着…": "Running…",
    "daemon 不可达": "daemon unreachable",
    "载入中…": "Loading…",
    "eventmem 记忆后台（概览 / 后端 / 模型 / 调度 / 日志 / 图谱）":
        "eventmem console (overview / backends / models / schedule / logs / graph)",
    "eventmem 的一体化后台：概览、后端链路与 config.yaml、模型安装、连边调度、日志、记忆图谱；远端 backend 也能用。":
        "eventmem's all-in-one console: overview, backend chain and config.yaml, model install, "
        "edge schedule, logs and the memory graph; works with a remote backend too.",
    # ---- overview -------------------------------------------------------
    "事件": "Events",
    "空库": "empty DB",
    "边 / 已判对": "Edges / pairs",
    "连边落后": "Backlog",
    "无游标": "no cursor",
    "kind 分布": "Kind distribution",
    "还没有事件": "No events yet",
    "项目（按事件数）": "Projects (by events)",
    "还没有项目": "No projects yet",
    "最近几轮 pass": "Recent passes",
    "刷新": "Refresh",
    "完成": "done",
    "这一进程还没跑过 pass": "No pass has run in this process yet",
    "后端链路（decision.backends）": "Backend chain (decision.backends)",
    "空（用 decision.url 单端点）": "Empty (single endpoint via decision.url)",
    "最新 {0}": "newest {0}",
    "{0} 对已判": "{0} pairs judged",
    "落后 {0}": "behind {0}",
    "{0} 对 / {1} 边": "{0} pairs / {1} edges",
    # ---- live strip -------------------------------------------------------
    "正在使用": "Using",
    "对": "pairs",
    "边": "edges",
    "停止": "Stop",
    "停止中…": "Stopping…",
    "切换并重跑": "Switch & rerun",
    "已请求停止：本轮做完手上这一对就停": "Stop requested: this pass ends after the pair in flight",
    "已请求切换：本轮停下后立刻重跑，重新探活链路":
        "Switch requested: the pass stops, then the chain is probed again immediately",
    "上次 {0} · {1} · {2} 对 / {3} 边": "last {0} · {1} · {2} pairs / {3} edges",
    "还没跑过": "no pass yet",
    # ---- backends -------------------------------------------------------
    "探活": "Probe",
    "启动": "Start",
    "停止": "Stop",
    "删除": "Delete",
    "确认删除": "Confirm delete",
    "保存": "Save",
    "保存的就是这份文件本身，注释原样留下。": "This saves the file itself; comments are kept.",
    "还原": "Revert",
    "在链路": "in chain",
    "未运行": "not running",
    "缺 key": "needs key",
    "外部": "external",
    "失败": "failed",
    "不可用": "unavailable",
    "链路顺序": "Chain order",
    "上移": "Move up",
    "下移": "Move down",
    "移除": "Remove",
    "（没有可加的）": "(nothing to add)",
    "加到末尾": "Add to end",
    "保存链路": "Save chain",
    "顺序即回退顺序：一轮开始时逐项探活，第一个活着的负责整轮。空链路 = 只用 decision.url。":
        "Order is fallback order: each pass probes in turn and the first live backend takes the "
        "whole round. An empty chain means decision.url only.",
    "新建后端": "New backend",
    "云端 API（System One 协议）": "Cloud API (System One protocol)",
    "LM Studio / 已装好的本地服务": "LM Studio / an already-running local server",
    "本地 GGUF（自己托管，daemon 负责启动）": "Local GGUF (we host it; the daemon launches it)",
    "名字，例如 typesafe / lev": "name, e.g. typesafe / lev",
    "config.yaml（可以先改再保存）": "config.yaml (edit before saving)",
    "创建": "Create",
    "创建后还要把它加进链路，再重启 daemon 才会自动拉起本地进程。":
        "After creating it, add it to the chain; a daemon restart is needed before it can launch a "
        "local process.",
    "链路已保存（下一次 pass 生效）": "Chain saved (applies to the next pass)",
    "先起个名字": "Give it a name first",
    "已创建 {0}": "created {0}",
    "运行中 :{0}": "running :{0}",
    "有 pack 加载失败：\n{0}": "some packs failed to load:\n{0}",
    "{0} 里还没有后端": "no backends in {0} yet",
    "{0}：{1}": "{0}: {1}",
    # ---- models ---------------------------------------------------------
    "运行时": "Runtime",
    "还没装 llama.cpp（按硬件自动选包）": "llama.cpp not installed yet (the build is chosen for this hardware)",
    "下载 / 更新 llama.cpp": "Download / update llama.cpp",
    "已装模型": "Installed models",
    "没有 GGUF": "No GGUF yet",
    "安装 GGUF + 生成 pack": "Install a GGUF + create the pack",
    "后端名": "Backend name",
    "量化": "Quantisation",
    "跳过 llama.cpp（我已经有）": "Skip llama.cpp (I already have one)",
    "装完自动加入链路": "Add to the chain when done",
    "开始安装": "Start install",
    "安装中…": "Installing…",
    "空闲": "idle",
    "开始下载 llama.cpp": "Downloading llama.cpp",
    "安装任务已开始（首次要下 2 GB 左右）": "Install started (the first run downloads ~2 GB)",
    "安装完成": "Install finished",
    "安装失败：{0}": "install failed: {0}",
    "装完会自动把 {0} 加进链路": "adds {0} to the chain when the install finishes",
    "候选来源会去重，实际数量未知；每对 8 秒仅供参考，当前模型速度未知。": "Candidate sources overlap; actual volume is unknown. Eight seconds per pair is only an illustration; the current model speed is unknown.",
    "保存未来设置会同时保存候选数量和关系规则；历史操作不会保存未来设置。": "Saving future settings saves both candidate selection and relationship rules; historical actions do not save future settings.",
    "补算使用已保存的关系阈值，不使用未保存的阈值编辑；候选参数使用当前表单。": "Backfill uses saved relationship thresholds, not unsaved threshold edits; candidate parameters come from this form.",
    "Tier2 · 同会话近期事件数": "Tier2 · recent events in session",
    "停止补算": "Stop backfill",
    "确认应用历史操作？此操作不会保存未来设置。": "Apply this historical operation? It will not save future settings.",
    "{0}：新增 {1}，删除 {2}": "{0}: {1} added, {2} removed",
    "跳过的事件对：{0}": "Skipped pairs: {0}",
    "置信度更新的边：{0}": "Edges with confidence updates: {0}",
    "未覆盖的边：{0}": "Uncovered edges: {0}",
    "历史事件 {0} 条；当前图观察到 {1} 个候选对。该观察值不是最终数量，也不是上限或下限；新边可能改变后续候选。": "{0} historical events; {1} candidate pairs observed on the current graph. This is neither the final count nor an upper or lower bound; new edges may change later candidates.",
    "补算状态：{0}；已判 {1} 对，新增 {2} 条边": "Backfill: {0}; {1} pairs judged, {2} edges added",
    "尚无补算任务": "No backfill job",
    "等待中": "Pending",
    "运行中": "Running",
    "已结束": "Finished",
    "已完成": "Completed",
    "已停止": "Stopped",
    "模型不可用": "Model unavailable",
    "模型判定失败": "Model decision failed",
    "改变": "Change",
    "否定": "Negate",
    "重复": "Repeat",
    "因为": "Because",
    "细化": "Refine",
    "未覆盖的 Jev 边：{0}": "Uncovered Jev edges: {0}",
    "记录了人工删除的事件对：{0}": "Pairs with recorded manual deletions: {0}",
    "旧版人工删除记录无法确认。": "Older manual deletions cannot be identified reliably.",
    # ---- schedule -------------------------------------------------------
    "配置尚未加载": "Configuration has not loaded",
    "候选选择与成本": "Candidate selection and cost",
    "Tier1 = 全局向量近邻；Tier2 = 同会话近期 + 未闭环教训 + 决策骨架；Tier3 = 跨项目。": "Tier1 = global vector neighbours; Tier2 = recent session + unclosed lessons + decision skeleton; Tier3 = cross-project.",
    "关系接受条件": "Relationship acceptance",
    "全局 margin": "Global margin",
    "改变最低置信度": "Change minimum confidence",
    "否定最低置信度": "Negate minimum confidence",
    "重复最低置信度": "Repeat minimum confidence",
    "因为最低置信度": "Because minimum confidence",
    "细化最低置信度": "Refine minimum confidence",
    "仅保存未来设置": "Save for future only",
    "保存接受条件": "Save acceptance rules",
    "接受条件已保存：之后的连边按该标准判定": "Acceptance rules saved: later edges are judged by them",
    "保存后，从下一轮 pass 起的连边一律按这里的 margin 与最低置信度判定；已有边不动（要改历史就用下面的重校准预览）。": "After saving, every edge from the next pass on is judged by this margin and these minimum confidences. Existing edges are untouched; use the recalibration preview below to change history.",
    "有未保存的改动": "Unsaved changes",
    "已保存": "Saved",
    "恢复调度默认值": "Reset schedule defaults",
    "仅清除调度字段的覆盖？": "Clear only schedule overrides?",
    "历史关系重校准": "Historical relationship recalibration",
    "历史概率格式有歧义，模型来源未知。StartLux 输出条件概率，但不能假设其他后端相同。默认跳过条件形式；仅在确认后勾选，选择只影响条件形式的记录。": "Legacy probability formats are ambiguous and model provenance is unknown. StartLux reports conditional probabilities, but other backends cannot be assumed to do so. Conditional-shaped records are skipped by default; opt in only if confirmed. This choice affects only conditional-shaped records.",
    "将条件形式的历史概率按条件概率处理": "Treat conditional-shaped historical probabilities as conditional",
    "历史候选补算": "Historical candidate backfill",
    "只重判已有概率的关系；无法使用的记录会跳过。预览不会修改历史。": "Re-evaluate stored probabilities only; unavailable records are skipped. Preview does not change history.",
    "重新选择历史候选需要 Jev 模型判定；耗时和成本不确定。预览不会启动任务。": "Re-selecting historical candidates requires Jev model decisions; time and cost are uncertain. Preview does not start a job.",
    "预览历史": "Preview history",
    "应用历史重校准": "Apply historical recalibration",
    "启动历史候选补算": "Start historical candidate backfill",
    "任务进度（服务状态）": "Job progress (service status)",
    "请输入有效数值（置信度与 margin 为 0–1）": "Enter valid numbers (confidence and margin must be 0–1)",
    "预览未返回令牌，无法应用": "Preview did not return a token; cannot apply",
    "参数已改变，请重新预览": "Settings changed; preview again",
    "请求已提交": "Request submitted",
    "预设（每轮问多少对）": "Presets (pairs asked per event)",
    "轻": "Light",
    "标准（默认）": "Standard (default)",
    "深": "Deep",
    "Tier 明细": "Tier detail",
    "全部恢复默认": "Reset all",
    "（无配置文件）": "(no config file)",
    "说明": "Notes",
    "Tier1 · 向量近邻 top-k": "Tier1 · vector neighbours top-k",
    "Tier1 · 余弦下限": "Tier1 · cosine floor",
    "Tier1 · 写边 margin": "Tier1 · edge margin",
    "Tier2 · 同会话最近 top-k": "Tier2 · same-session recent top-k",
    "Tier2 · 未闭环教训": "Tier2 · unclosed lessons",
    "Tier2 · 决策骨架": "Tier2 · decision skeleton",
    "Tier3 · 跨项目 top-k": "Tier3 · cross-project top-k",
    "Tier3 · 余弦下限": "Tier3 · cosine floor",
    "轮询间隔（秒）": "Poll interval (s)",
    "已保存；下一轮 pass 用新值": "Saved; the next pass uses it",
    "已恢复默认（清掉 settings.yaml 里的覆盖）": "Defaults restored (overrides removed from settings.yaml)",
    "每事件 ≈ {0} 对 ≈ {1}（按 8 s/对估算）": "per event ≈ {0} pairs ≈ {1} (at 8 s/pair)",
    "Tier1 = 全局向量近邻；Tier2 = 同会话近期 + 未闭环教训 + 决策骨架；Tier3 = 跨项目。改的是 ~/.hermes/eventmem/settings.yaml，下一轮 pass 生效。":
        "Tier1 = global vector neighbours; Tier2 = recent in this session + unclosed lessons + "
        "decision skeleton; Tier3 = cross-project. This edits ~/.hermes/eventmem/settings.yaml and "
        "applies to the next pass.",
    # ---- logs -----------------------------------------------------------
    "本进程日志（最近 400 条）": "Log for this process (last 400 lines)",
    "自动刷新": "Auto-refresh",
    "（还没有日志）": "(no log lines yet)",
    # ---- graph ----------------------------------------------------------
    "没有找到 eventmem-graph 插件：它没装、没启用，或者加载顺序早于本页。\n"
    "图谱页现在只有这一个入口，插件要留着（它不再有自己的按钮和页面）。\n"
    "启用办法：hermes plugins enable eventmem-graph，然后在 app 里 Reload desktop plugins。":
        "The eventmem-graph plugin was not found: not installed, not enabled, or it loaded after "
        "this page. The graph has no other entry point, so that plugin has to stay enabled.\n"
        "Enable it with: hermes plugins enable eventmem-graph, then Reload desktop plugins.",
    "重新加载中…": "Reloading…",
    "重新加载这一页": "Reload this page",
    # ---- toasts ---------------------------------------------------------
    "跳过：{0}": "skipped: {0}",
    "跑完：{0} 对，写 {1} 条边{2}": "pass done: {0} pairs, {1} edges{2}",
    "（{0}）": " ({0})",
}


def strings_for(lang: str) -> dict[str, str]:
    """Only the entries the UI needs: English gets the map, Chinese needs none."""
    return dict(EN) if str(lang).lower().startswith("en") else {}
