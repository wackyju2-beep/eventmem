/**
 * eventmem / dsh — every model-facing string, in one place, two languages.
 *
 * The one rule that must never drift: the "Q&A counts too" sentence appears in both
 * `evt_add`'s description and the turn-end extractor prompt, word for word. It is one
 * constant here (`qaRule`) and both callers concatenate it — edit once, both change.
 */

export const KINDS = ['决定', '尝试', '结果', '变更', '教训']
export const RELS = ['改变', '否定', '重复', '因为', '细化']
export const UNFILED = 'unfiled'

export const OUR_SOURCE_KIND = 'eventmem'

/** The shared judgement line: verbatim in evt_add's description and the extractor prompt. */
const QA_RULE = {
  zh: '问答也要记：把「用户问了什么 + 结论/依据是什么」写成一条结果或教训；'
      + '只有寒暄、纯确认、复述已知内容才返回空数组。',
  en: 'Q&A counts too: write what the user asked plus the conclusion or its basis as a '
      + '结果 or 教训; return an empty array only for greetings, bare confirmations and '
      + 'restating what is already known.',
}

const zh = {
  qaRule: QA_RULE.zh,

  evtAdd: (limit) => `记录这一回合发生的事件（0–${limit} 条）。放在本回合最后一个工具调用里写。
三条判据：
① 持久副作用（文件/配置/代码库/环境真的变了）→ 变更；
② 假设闭环（多次失败试错折算成一条）→ 尝试+结果，或证伪死路 → 教训；
③ 契约拍板（定方向、定约束、排除方案，带理由）→ 决定。
${QA_RULE.zh}
项目名只能从 proj_list 里选，或省略（继承本会话默认项目），或写 "unfiled"；未知名字会被拒绝。
如果本回合没写，回合结束后插件会再读一遍这一轮并补记；抽取失败则不写记忆。`,

  expand: '取回一条事件的 artifact 全文（命令/配置/日志/代码原样返回）。事件没有 artifact 时只返回事件本身。',
  neighbors: '按 #id 展开一条记忆在图上的邻居：谁改了/否定了它、它自己取代或解释了谁、同会话前后几条。'
    + '召回行看着不完整、或想知道「这条是怎么来的/后来怎么样了」时用它。',
  recall: '按全局相似度检索事件记忆（向量 + FTS5 → RRF），返回带日期、项目名和 #id 的渲染块。'
    + '自动注入不够时用它换措辞再查；要原文时对命中的 #id 调 expand。',
  projAdd: '新建一个人工命名的项目。项目名只能显式建立，evt_add 不会自动建项目；同名已存在时返回已有 id。',
  projList: '列出所有在用项目名（evt_add 的 project 只能从这里选，或写 "unfiled"）。',
  forget: '永久删除一条事件：事件行、它的边、项目关联、无人引用的 artifact。'
    + '不可撤销，只在用户明确要求遗忘时使用。',

  pEvents: '按发生顺序排列；0 条表示本回合无需记录。',
  pKind: '决定 / 尝试 / 结果 / 变更 / 教训（写中文原词）。',
  pText: '一句话，≤120 字，第一人称；有「为什么」就带上。',
  pProject: '现有项目名，或 "unfiled"。不填则继承本会话默认项目。',
  pArtifact: '大块不可压缩的原文：命令、配置、日志、代码。全文单独存，不进事件正文，用 expand 取回。',
  pArtifactKind: '命令 / 配置 / 日志 / 代码 / 其他',
  pPath: 'artifact 的源文件路径（可选，绝对路径）。同一路径的多次改动靠它连成链。',
  pExtraProjects: '同一事件还属于哪些项目（可选）。',
  pLinks: '与同批前序事件或已有事件的关系。注意：跨机数据面没有连边端点，这里只做校验，不写边。',
  pRel: '关系种类：改变 / 否定 / 重复 / 因为 / 细化（写中文原词）。',
  pIndex: '同批事件的序号（1 起），必须早于本条。',
  pEventId: '召回行末尾的 #id，或已有事件的 id。',
  pConf: '可选，默认 1.0。',
  pQuery: '自然语言或关键词。',
  pBudget: '注入预算，默认 600，范围 100–4000。',
  pName: '项目名，例如 "小主机"。',
  pRels: '跟哪几种关系，默认 改变/否定/因为。',
  pDepth: '展开几跳，默认 1，上限 3。',

  errEventsArray: 'events 必须是数组',
  errTooMany: (limit, got) => `一次最多 ${limit} 条事件（收到 ${got} 条）`,
  errEntryObject: (pos) => `第 ${pos} 条不是对象`,
  errKind: (pos, kind) => `第 ${pos} 条 kind=${JSON.stringify(kind)} 非法`,
  errText: (pos) => `第 ${pos} 条 text 为空`,
  errUnknownProject: (pos, name) => `第 ${pos} 条：未知项目 ${JSON.stringify(name)}；不会自动新建`,
  errArchivedProject: (pos, name) => `第 ${pos} 条：项目 ${JSON.stringify(name)} 已归档，不再接收新事件`,
  errUnknownExtra: (pos, name) => `第 ${pos} 条：未知次要项目 ${JSON.stringify(name)}`,
  errArchivedExtra: (pos, name) => `第 ${pos} 条：次要项目 ${JSON.stringify(name)} 已归档，不能挂新事件`,
  hintProject: '先用 proj_add 建项目，或省略 project / 写 "unfiled"',
  noteNothing: '本回合没有需要记录的事件',
  noteLinksSkipped: (n) => `跨机数据面只镜像 add_event，没有连边端点：${n} 条 links 已校验但未写入（夜间 worker 会按同会话/artifact 补边）`,
  errNameEmpty: 'name 不能为空',
  errNameReserved: '"unfiled" 是未归类的保留名，不能作为项目名',
  errNameLong: '项目名过长（上限 60 字）',
  errEventId: 'event_id 必须是整数',
  errNoEvent: (id) => `没有事件 #${id}`,
  errQueryEmpty: 'query 不能为空',
  noteProjectList: 'evt_add 的 project 只能选这里，或写 "unfiled"',
  noteArchived: '该项目已归档，不接收新事件，也不作为本会话默认项目',

  unavailable: (detail) => `eventmem 不可用（${detail}）。这一回合继续，没有记忆。`,
  reminder: '↳ 你刚改了这个环境的状态。如果本回合还没用 evt_add 记下这次变化，就现在补一条'
    + '（kind 用 变更/结果/教训，一句话）。',

  extractSystem: `你是事件记忆抽取器。读一轮对话，抽出 0–3 条值得长期记住的事件。
每条一句话（第一人称，≤120 字），kind 只能是 决定 / 尝试 / 结果 / 变更 / 教训。
记：结论、决定及理由、持久改变、踩过的坑；不记寒暄、过程性步骤、复述。
${QA_RULE.zh}
只输出严格 JSON，不要代码块、不要解释：{"events":[{"kind":"结果","text":"…"}]}`,
  extractUser: (q, a) => `用户：${q}\n\n助手：${a}`,
}

const en = {
  qaRule: QA_RULE.en,

  evtAdd: (limit) => `Record what happened this turn (0–${limit} events). Write them in the last tool call of the turn.
Three tests:
(1) lasting side effect (files, config, codebase or environment actually changed) → 变更;
(2) hypothesis closed (several failed attempts collapse into one) → 尝试 + 结果, or a dead end → 教训;
(3) contract settled (direction, constraints, rejected options, with reasons) → 决定.
${QA_RULE.en}
The project name must come from proj_list, be omitted (inherit the session default), or be "unfiled"; unknown names are rejected.
If this turn writes nothing, the plugin reads the turn once more after it ends; a failed extraction records no memory at all.`,

  expand: 'Fetch the full artifact text behind one event (commands, configs, logs, code, verbatim). '
    + 'Without an artifact it just returns the event.',
  neighbors: 'Expand one memory by #id: who changed or negated it, what it supersedes or explains, '
    + 'and its same-session neighbours. Use it when a recall line looks incomplete or you need '
    + 'the story around one event.',
  recall: 'Search event memory by global similarity (vector + FTS5 → RRF); returns a rendered block '
    + 'with dates, project names and #ids. Use it when the automatic injection was not enough — try '
    + 'different wording — and call expand on a hit when you need the full text.',
  projAdd: 'Create a human-named project. Project names exist only because someone created them '
    + 'explicitly — evt_add never creates one; an existing name returns its id.',
  projList: 'List the active project names (evt_add\'s project must come from here, or be "unfiled").',
  forget: 'Permanently delete one event: the row, its edges, its project links and any artifact '
    + 'nothing else references. Irreversible — only when the user explicitly asks to forget something.',

  pEvents: 'In the order they happened; 0 means nothing to record.',
  pKind: '决定 / 尝试 / 结果 / 变更 / 教训 (send the Chinese word).',
  pText: 'One sentence, ≤120 characters, first person; include the why when there is one.',
  pProject: 'An existing project name, or "unfiled". Omitted = inherit the session default project.',
  pArtifact: 'Bulk text that cannot be compressed: commands, configs, logs, code. Stored on its own, '
    + 'never in the event body; fetch it back with expand.',
  pArtifactKind: 'command / config / log / code / other (send the Chinese word)',
  pPath: 'Source file of the artifact (optional, absolute). Events touching the same path get linked.',
  pExtraProjects: 'Other projects this event also belongs to (optional).',
  pLinks: 'Relations to earlier events in this batch or to existing ones. Note: the cross-machine '
    + 'data plane has no link endpoint, so these are validated but not written.',
  pRel: '改变=changes, 否定=negates, 重复=repeats, 因为=because, 细化=refines (send the Chinese word).',
  pIndex: '1-based position within this batch; must point to an earlier event.',
  pEventId: 'The #id at the end of a recall line, or an existing event id.',
  pConf: 'Optional, default 1.0.',
  pQuery: 'Natural language or keywords.',
  pBudget: 'Injection budget, default 600, range 100–4000.',
  pName: 'Project name, e.g. "mini-pc".',
  pRels: 'Relations to follow; default 改变/否定/因为.',
  pDepth: 'Hops to expand, default 1, max 3.',

  errEventsArray: 'events must be an array',
  errTooMany: (limit, got) => `at most ${limit} events per call (got ${got})`,
  errEntryObject: (pos) => `entry ${pos} is not an object`,
  errKind: (pos, kind) => `entry ${pos}: invalid kind=${JSON.stringify(kind)}`,
  errText: (pos) => `entry ${pos}: empty text`,
  errUnknownProject: (pos, name) => `entry ${pos}: unknown project ${JSON.stringify(name)}; nothing is created automatically`,
  errArchivedProject: (pos, name) => `entry ${pos}: project ${JSON.stringify(name)} is archived and takes no new events`,
  errUnknownExtra: (pos, name) => `entry ${pos}: unknown secondary project ${JSON.stringify(name)}`,
  errArchivedExtra: (pos, name) => `entry ${pos}: secondary project ${JSON.stringify(name)} is archived`,
  hintProject: 'create it with proj_add first, or omit project / use "unfiled"',
  noteNothing: 'nothing to record this turn',
  noteLinksSkipped: (n) => `the cross-machine data plane mirrors add_event only and has no link endpoint: `
    + `${n} links were validated but not written (the nightly worker links by session/artifact)`,
  errNameEmpty: 'name must not be empty',
  errNameReserved: '"unfiled" is the reserved name for unclassified events',
  errNameLong: 'project name too long (max 60 characters)',
  errEventId: 'event_id must be an integer',
  errNoEvent: (id) => `no event #${id}`,
  errQueryEmpty: 'query must not be empty',
  noteProjectList: 'evt_add\'s project must come from this list, or be "unfiled"',
  noteArchived: 'this project is archived: no new events, and it will not become the session default',

  unavailable: (detail) => `eventmem is unavailable (${detail}). This turn continues without memory.`,
  reminder: '↳ You just changed this environment. If this turn has not recorded that change with '
    + 'evt_add yet, write it now (kind 变更/结果/教训, one sentence).',

  extractSystem: `You extract event memory. Read one conversation turn and pull out 0–3 events worth remembering long-term.
One sentence per event (first person, ≤120 characters); kind is one of 决定 / 尝试 / 结果 / 变更 / 教训.
Record conclusions, decisions and their reasons, lasting changes, dead ends and the traps hit; skip greetings, step-by-step narration and restatements.
${QA_RULE.en}
Output strict JSON only, no code fence, no prose: {"events":[{"kind":"结果","text":"…"}]}`,
  extractUser: (q, a) => `User: ${q}\n\nAssistant: ${a}`,
}

const TABLE = { zh, en }

/** Resolve the string table for one language tag; anything unknown falls back to Chinese. */
export function strings(lang) {
  return TABLE[String(lang || '').toLowerCase().startsWith('en') ? 'en' : 'zh']
}
