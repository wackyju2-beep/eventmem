"""eventmem / Hermes — the seven tools, their schemas and their dispatch.

Tool text is host-facing (it is what the model reads), so it lives here and goes through
``tr`` like every other model-facing string.  The *logic* below each handler is a thin
translation layer: validate what the model sent, call one :class:`EventMem` method, and
return a JSON string.  No SQL, no encoding, no recall maths.

`evt_add` is the only tool with real rules of its own:

* ``project`` is a closed vocabulary — a name that does not exist is an error, never an
  implicit ``proj_add`` (design §4.3);
* omitting ``project`` inherits the session's sticky project, re-checked against the
  active list so an archived project cannot be reached by silence;
* ``links`` may point at an earlier event of the same batch (``index``) or an existing
  event (``event_id``) — only backwards in time, so the graph stays a DAG.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Optional

from eventmem_core import EventMem, UNFILED
from eventmem_core.i18n import tr
from eventmem_core.settings import KINDS, RELS, CoreSettings

from .host import redact

__all__ = ["ToolContext", "schemas", "handle", "as_int"]


@dataclass
class ToolContext:
    """What a tool needs from the session, without handing it the provider."""

    core: EventMem
    settings: CoreSettings
    session_id: str = ""
    primary: bool = True
    #: Session sticky default project (provider keeps it in sync with the store).
    session_project: str = ""


def as_int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------- schemas

def schemas(settings: CoreSettings) -> list[dict[str, Any]]:
    limit = int(settings.max_events_per_call)
    return [
        {
            "name": "evt_add",
            "description": tr(
                f"记录本回合发生的事件（0–{limit} 条）。只在本回合的最后一个工具调用里写。\n"
                "三条判据：\n"
                "① 持久副作用（文件/配置/硬件/环境/代码库变了）→ 变更；\n"
                "② 假设闭环（多次报错重试折算成一条）→ 尝试+结果，或证伪死路 → 教训；\n"
                "③ 契约拍板（定方向、定约束、排除方案，带理由）→ 决定。\n"
                "问答也要记：把「用户问了什么 + 结论/依据是什么」写成一条结果或教训，"
                "不能因为没动文件就不记。\n"
                "只有寒暄、纯确认、复述已知内容可以传空数组；实质回合没写（或传空数组），"
                "回合结束后插件会用机械摘要自动补一条。",
                f"Record what happened this turn (0–{limit} events). Write them in the last "
                "tool call of the turn.\n"
                "Three tests:\n"
                "(1) lasting side effect (files, config, hardware, environment or code changed) "
                "→ 变更;\n"
                "(2) hypothesis closed (several failed attempts collapse into one) → 尝试 + 结果, "
                "or a dead end → 教训;\n"
                "(3) contract settled (direction, constraints, rejected options, with reasons) "
                "→ 决定.\n"
                "Q&A counts too: write what the user asked plus the conclusion or its basis as a "
                "结果 or a 教训 — don't skip it just because no file changed.\n"
                "An empty array is only for greetings, bare confirmations and restating what is "
                "already known; if a substantive turn writes nothing, the plugin appends a "
                "mechanical summary after the turn.",
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "events": {
                        "type": "array",
                        "minItems": 0,
                        "maxItems": limit,
                        "description": tr("按发生顺序排列；0 条表示本回合无需记录。",
                                          "In the order they happened; 0 means nothing to record."),
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string", "enum": list(KINDS),
                                         "description": tr(
                                             "决定 / 尝试 / 结果 / 变更 / 教训。",
                                             "决定=decision, 尝试=attempt, 结果=result, "
                                             "变更=change, 教训=lesson. Send the Chinese word.")},
                                "text": {"type": "string",
                                         "description": tr(
                                             "一句话，≤120 字，第一人称；有\"为什么\"就带上。",
                                             "One sentence, ≤120 characters, first person; "
                                             "include the why when there is one.")},
                                "project": {
                                    "type": "string",
                                    "description": tr(
                                        "现有项目名，或 \"unfiled\"。不填则继承本会话默认项目。"
                                        "未知名字会被拒绝（不会自动建项目）。",
                                        "An existing project name, or \"unfiled\". Omitted = "
                                        "inherit the session's default project. Unknown names "
                                        "are rejected (no project is ever created implicitly)."),
                                },
                                "artifact": {
                                    "type": "string",
                                    "description": tr(
                                        "大块不可压缩的原文：命令、配置、日志、代码。"
                                        "全文单独存，不进事件正文，用时 expand 取回。",
                                        "Bulk text that cannot be compressed: commands, configs, "
                                        "logs, code. Stored on its own, never in the event body; "
                                        "fetch it back with expand."),
                                },
                                "artifact_kind": {
                                    "type": "string",
                                    "description": tr("命令 / 配置 / 日志 / 代码 / 其他",
                                                      "command / config / log / code / other"),
                                },
                                "path": {
                                    "type": "string",
                                    "description": tr(
                                        "artifact 的源文件路径（可选，写绝对路径）。同一路径的多次改动"
                                        "靠它连成一条链——内容变了 sha 会变，路径不会。",
                                        "Source file of the artifact (optional, absolute). Events "
                                        "touching the same path get linked; the sha changes with "
                                        "the content, the path does not."),
                                },
                                "extra_projects": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": tr("同一事件还属于哪些项目（可选）。",
                                                      "Other projects this event also belongs to (optional)."),
                                },
                                "links": {
                                    "type": "array",
                                    "description": tr(
                                        "与同批前序事件或已有事件的关系；只在确知因果时写。",
                                        "Relations to earlier events in this batch or to existing "
                                        "ones; only when the connection is certain."),
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "rel": {"type": "string", "enum": list(RELS),
                                                    "description": tr(
                                                        "关系种类：改变 / 否定 / 重复 / 因为 / 细化。",
                                                        "改变=changes, 否定=negates, 重复=repeats, "
                                                        "因为=because, 细化=refines. Send the Chinese word.")},
                                            "index": {
                                                "type": "integer", "minimum": 1,
                                                "description": tr(
                                                    "同批事件的序号（1 起），必须早于本条。",
                                                    "1-based position within this batch; must point "
                                                    "to an earlier event."),
                                            },
                                            "event_id": {
                                                "type": "integer",
                                                "description": tr(
                                                    "已有事件的 id（与 index 二选一）。",
                                                    "Id of an existing event (either this or index)."),
                                            },
                                            "conf": {
                                                "type": "number", "minimum": 0, "maximum": 1,
                                                "description": tr("可选，默认 1.0。",
                                                                  "Optional, default 1.0."),
                                            },
                                        },
                                        "required": ["rel"],
                                    },
                                },
                            },
                            "required": ["kind", "text"],
                        },
                    },
                },
                "required": ["events"],
            },
        },
        {
            "name": "expand",
            "description": tr(
                "取回一条事件的 artifact 全文（命令/配置/日志/代码原样返回）。"
                "事件没有 artifact 时只返回事件本身。",
                "Fetch the full artifact text behind one event (commands, configs, logs, code, "
                "verbatim). Without an artifact it just returns the event."),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "integer",
                                 "description": tr("召回行末尾的 #id。",
                                                   "The #id at the end of a recall line.")},
                },
                "required": ["event_id"],
            },
        },
        {
            "name": "mem_neighbors",
            "description": tr(
                "按 #id 展开一条记忆在图上的邻居：谁改了/否定了它（后来的事件指过来）、"
                "它自己取代或解释了谁（它指出去）、同会话前后几条。"
                "召回行看着不完整、或想知道「这条是怎么来的/后来怎么样了」时用它。",
                "Expand one memory by #id: who changed or negated it (later events pointing at it), "
                "what it supersedes or explains, and its same-session neighbours. Use it when a "
                "recall line looks incomplete or you need the story around one event."),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "integer",
                                 "description": tr("行首的 #id。", "The #id from a memory line.")},
                    "rels": {
                        "type": "array",
                        "items": {"type": "string", "enum": list(RELS)},
                        "description": tr(
                            "跟哪几种关系，默认 改变/否定/因为。细化/重复数量大，需要时再加。",
                            "Relations to follow; default change/negate/because. Add refine/repeat "
                            "only when you need them — they are numerous."),
                    },
                    "depth": {"type": "integer", "minimum": 1, "maximum": 3,
                              "description": tr("展开几跳，默认 1。", "Hops to expand, default 1.")},
                },
                "required": ["event_id"],
            },
        },
        {
            "name": "mem_recall",
            "description": tr(
                "按全局相似度检索事件记忆（向量 + FTS5 → RRF），返回带日期、"
                "项目名和 #id 的渲染块。自动注入不够时用它换措辞再查；"
                "要原文时对命中的 #id 调 expand。",
                "Search event memory by global similarity (vector + FTS5 → RRF); returns a "
                "rendered block with dates, project names and #ids. Use it when the automatic "
                "injection was not enough — try different wording — and call expand on a hit "
                "when you need the full text."),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string",
                              "description": tr("自然语言或关键词。",
                                                "Natural language or keywords.")},
                    "budget_tokens": {"type": "integer", "minimum": 100, "maximum": 4000,
                                      "description": tr("注入预算，默认 600。",
                                                        "Injection budget, default 600.")},
                },
                "required": ["query"],
            },
        },
        {
            "name": "proj_add",
            "description": tr(
                "新建一个人工命名的项目。项目名只能由人/模型显式建立，"
                "evt_add 不会自动建项目；同名已存在时返回已有 id。",
                "Create a human-named project. Project names exist only because a person or the "
                "model created them explicitly — evt_add never creates one; an existing name "
                "returns its id."),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": tr("项目名，例如 \"小主机\"。",
                                               "Project name, e.g. \"mini-pc\".")},
                },
                "required": ["name"],
            },
        },
        {
            "name": "proj_list",
            "description": tr(
                "列出所有在用项目名（evt_add 的 project 只能从这里选，或写 \"unfiled\"）。",
                "List the active project names (evt_add's project must come from here, or be "
                "\"unfiled\")."),
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "evt_forget",
            "description": tr(
                "永久删除一条事件：事件行、它的边、项目关联、无人引用的 artifact。"
                "不可撤销，只在用户明确要求遗忘时使用。",
                "Permanently delete one event: the row, its edges, its project links and any "
                "artifact nothing else references. Irreversible — only when the user explicitly "
                "asks to forget something."),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_id": {"type": "integer",
                                 "description": tr("要删除的事件 id。",
                                                   "Id of the event to delete.")},
                },
                "required": ["event_id"],
            },
        },
    ]


# --------------------------------------------------------------------------- dispatch

def handle(tool_name: str, args: dict[str, Any], ctx: ToolContext) -> str:
    try:
        if tool_name == "evt_add":
            return _json(_tool_add(args or {}, ctx))
        if tool_name == "expand":
            return _json(_tool_expand(args or {}, ctx))
        if tool_name == "mem_neighbors":
            return _json(_tool_neighbors(args or {}, ctx))
        if tool_name == "mem_recall":
            return _json(_tool_recall(args or {}, ctx))
        if tool_name == "proj_add":
            return _json(_tool_project_add(args or {}, ctx))
        if tool_name == "proj_list":
            return _json(_tool_project_list(ctx))
        if tool_name == "evt_forget":
            return _json(_tool_forget(args or {}, ctx))
    except Exception as exc:  # noqa: BLE001 - report, never raise into the loop
        return _json({"error": f"{type(exc).__name__}: {exc}"})
    return _json({"error": f"unknown tool {tool_name}"})


def _tool_add(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    core, settings = ctx.core, ctx.settings
    limit = int(settings.max_events_per_call)
    if not ctx.primary:
        return {"error": tr("eventmem: 当前不是主会话（agent_context 非 primary），不写事件",
                            "eventmem: not the primary session (agent_context != primary); "
                            "no events written")}
    items = args.get("events")
    if items is None:
        items = []
    if not isinstance(items, list):
        return {"error": tr("events 必须是数组", "events must be an array")}
    if len(items) > limit:
        return {"error": tr(f"一次最多 {limit} 条事件（收到 {len(items)} 条）",
                            f"at most {limit} events per call (got {len(items)})")}
    if not items:
        # 空数组只表示「模型看过、没写」——不当作已记录：实质回合会在回合末被抽取补写。
        return {"ok": True, "count": 0,
                "note": tr("本回合没有需要记录的事件", "nothing to record this turn")}

    known = core.project_names()
    inherited = ctx.session_project
    prepared: list[dict[str, Any]] = []
    for pos, item in enumerate(items, 1):
        if not isinstance(item, dict):
            return {"error": tr(f"第 {pos} 条不是对象", f"entry {pos} is not an object")}
        kind = str(item.get("kind") or "").strip()
        text = str(item.get("text") or "").strip()
        if kind not in KINDS:
            return {"error": tr(f"第 {pos} 条 kind={kind!r} 非法", f"entry {pos}: invalid kind={kind!r}"),
                    "kinds": list(KINDS)}
        if not text:
            return {"error": tr(f"第 {pos} 条 text 为空", f"entry {pos}: empty text")}
        # 不校验长度：120 字是工具描述里的软约束，超长也整条收——截断就等于丢内容。
        project, project_error = _resolve_project(item, core, known, inherited, pos)
        if project_error:
            return project_error
        extras, extra_error = _resolve_extras(item, core, known, pos)
        if extra_error:
            return extra_error
        prepared.append({
            "kind": kind,
            "text": redact(text),
            "project": project,
            "extras": extras,
            "artifact": str(item.get("artifact") or "") or "",
            "artifact_kind": str(item.get("artifact_kind") or "其他").strip() or "其他",
            "art_path": str(item.get("path") or "").strip() or None,
            "links": item.get("links") if isinstance(item.get("links"), list) else [],
        })

    written: list[dict[str, Any]] = []
    for item in prepared:
        art_id = None
        if item["artifact"]:
            art_id = core.add_artifact(item["artifact_kind"], redact(item["artifact"]))
        text = str(item["text"] or "").strip()
        ts = time.time()
        try:
            event_id = core.add_event(
                item["kind"], text, ts=ts, project=item["project"],
                session=ctx.session_id or None, art_id=art_id,
                art_path=item.get("art_path"), extra_projects=item["extras"] or None,
            )
        except Exception as exc:  # noqa: BLE001
            return {"error": tr(f"写入失败：{type(exc).__name__}: {exc}",
                                f"write failed: {type(exc).__name__}: {exc}"),
                    "written": written, "known_projects": known}
        written.append({
            "id": event_id, "ts": ts, "kind": item["kind"], "text": item["text"],
            "project": item["project"] or UNFILED, "artifact": bool(art_id),
        })

    sticky = next((w["project"] for w in written if w["project"] != UNFILED), "")
    if sticky:
        ctx.session_project = sticky

    edge_errors = _write_links(core, prepared, written)
    result: dict[str, Any] = {"ok": True, "count": len(written), "events": written}
    if sticky:
        result["session_project"] = sticky
    if edge_errors:
        result["edge_errors"] = edge_errors
    return result


def _resolve_project(item: dict[str, Any], core: EventMem, known: list[str],
                     inherited: str, pos: int) -> tuple[Optional[str], Optional[dict[str, Any]]]:
    """``None`` means unfiled; an unknown *name* is an error, never a new project."""
    if "project" not in item or item.get("project") is None:
        # 会话粘性也要过归档检查：项目在会话中途被归档后，不能靠「不写 project」绕过去。
        return (inherited or None) if inherited in known else None, None
    raw = str(item.get("project") or "").strip()
    if not raw or raw == UNFILED:
        return None, None
    try:
        row = core.get_project(raw)
    except Exception as exc:  # noqa: BLE001
        return None, {"error": tr(f"第 {pos} 条项目查询失败：{exc}",
                                  f"entry {pos}: project lookup failed: {exc}")}
    if not row:
        return None, {
            "error": tr(f"第 {pos} 条：未知项目 {raw!r}；不会自动新建",
                        f"entry {pos}: unknown project {raw!r}; nothing is created "
                        "automatically"),
            "known_projects": known,
            "hint": tr("先用 proj_add 建项目，或写 \"unfiled\"/省略 project",
                       "create it with proj_add first, or use \"unfiled\" / omit project"),
        }
    if str(row.get("status") or "active") == "archived":
        return None, {
            "error": tr(f"第 {pos} 条：项目 {raw!r} 已归档，不再接收新事件",
                        f"entry {pos}: project {raw!r} is archived and takes no new events"),
            "known_projects": known,
            "hint": tr("改写一个在用项目名，或写 \"unfiled\"",
                       "use an active project name, or write \"unfiled\""),
        }
    return str(row.get("name") or raw), None


def _resolve_extras(item: dict[str, Any], core: EventMem, known: list[str],
                    pos: int) -> tuple[list[str], Optional[dict[str, Any]]]:
    raw = item.get("extra_projects")
    if not isinstance(raw, list):
        return [], None
    out: list[str] = []
    for name in raw:
        name = str(name or "").strip()
        if not name or name == UNFILED:
            continue
        if name in out:
            continue
        try:
            row = core.get_project(name)
        except Exception as exc:  # noqa: BLE001
            return [], {"error": tr(f"第 {pos} 条次要项目查询失败：{exc}",
                                    f"entry {pos}: secondary project lookup failed: {exc}")}
        if not row:
            return [], {"error": tr(f"第 {pos} 条：未知次要项目 {name!r}",
                                    f"entry {pos}: unknown secondary project {name!r}"),
                        "known_projects": known}
        if str(row.get("status") or "active") == "archived":
            return [], {"error": tr(f"第 {pos} 条：次要项目 {name!r} 已归档，不能挂新事件",
                                    f"entry {pos}: secondary project {name!r} is archived"),
                        "known_projects": known}
        out.append(str(row.get("name") or name))
    return out, None


def _write_links(core: EventMem, prepared: list[dict[str, Any]],
                 written: list[dict[str, Any]]) -> list[str]:
    """Batch-internal edges (§5): the model knows the order, Jev only fills across days."""
    errors: list[str] = []
    for pos, (item, new) in enumerate(zip(prepared, written)):
        for link in item["links"]:
            if not isinstance(link, dict):
                errors.append(tr(f"#{new['id']}: link 不是对象", f"#{new['id']}: link is not an object"))
                continue
            rel = str(link.get("rel") or "").strip()
            if rel not in RELS:
                errors.append(tr(f"#{new['id']}: rel={rel!r} 非法", f"#{new['id']}: invalid rel={rel!r}"))
                continue
            dst: Optional[int] = None
            if link.get("index") is not None:
                index = as_int(link["index"]) or 0
                # 1-based position inside this batch; only earlier events may be targets.
                if not 1 <= index <= len(written) or index - 1 >= pos:
                    errors.append(tr(f"#{new['id']}: index={index} 必须指向本批前序事件",
                                     f"#{new['id']}: index={index} must point to an earlier event in this batch"))
                    continue
                dst = written[index - 1]["id"]
            elif link.get("event_id") is not None:
                dst = as_int(link["event_id"])
                if dst is None:
                    errors.append(tr(f"#{new['id']}: event_id 不是整数", f"#{new['id']}: event_id is not an integer"))
                    continue
            if dst is None:
                errors.append(tr(f"#{new['id']}: link 缺少 index 或 event_id", f"#{new['id']}: link needs index or event_id"))
                continue
            if dst == new["id"]:
                errors.append(tr(f"#{new['id']}: 不能连向自己", f"#{new['id']}: an event cannot link to itself"))
                continue
            try:
                conf = link.get("conf")
                outcome = core.add_link(int(new["id"]), int(dst), rel,
                                        1.0 if conf is None else float(conf))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"#{new['id']} → #{dst} {rel}: {exc}")
                continue
            if not outcome.get("ok"):
                detail = str(outcome.get("error") or "")
                if "is not older" in detail:
                    errors.append(tr(f"#{new['id']}: event_id={dst} 不早于本条，只能连向更早的事件",
                                     f"#{new['id']}: event {dst} is not older than this one; links only go back"))
                elif detail.startswith("no event"):
                    errors.append(tr(f"#{new['id']}: event_id={dst} 不存在", f"#{new['id']}: no event #{dst}"))
                else:
                    errors.append(f"#{new['id']} → #{dst} {rel}: {detail}")
    return errors


def _tool_expand(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    event_id = as_int(args.get("event_id", args.get("id")))
    if event_id is None:
        return {"error": tr("event_id 必须是整数", "event_id must be an integer")}
    out = ctx.core.expand(event_id)
    if out is None:
        return {"error": tr(f"没有事件 #{event_id}", f"no event #{event_id}")}
    return out


def _tool_neighbors(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    event_id = as_int(args.get("event_id", args.get("id")))
    if event_id is None:
        return {"error": tr("event_id 必须是整数", "event_id must be an integer")}
    if not ctx.core.get_event(event_id):
        return {"error": tr(f"没有事件 #{event_id}", f"no event #{event_id}")}
    rels = args.get("rels")
    if isinstance(rels, str):
        rels = [part for part in re.split(r"[,/\s]+", rels) if part]
    if not isinstance(rels, list) or not rels:
        rels = None
    depth = as_int(args.get("depth")) or 1
    block = ctx.core.neighbours(event_id, rels=rels, depth=depth) or ""
    return {"event_id": event_id, "context": block, "found": bool(block.strip())}


def _tool_recall(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"error": tr("query 不能为空", "query must not be empty")}
    budget = args.get("budget_tokens")
    try:
        budget_tokens = int(budget) if budget is not None else ctx.settings.recall.budget_tokens
    except (TypeError, ValueError):
        budget_tokens = ctx.settings.recall.budget_tokens
    block = ctx.core.recall(query, budget_tokens=max(100, min(budget_tokens, 4000)))
    return {"query": query, "context": block or "", "found": bool((block or "").strip())}


def _tool_project_add(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    name = re.sub(r"\s+", " ", str(args.get("name") or "")).strip()
    if not name:
        return {"error": tr("name 不能为空", "name must not be empty")}
    if name == UNFILED:
        return {"error": tr("\"unfiled\" 是未归类的保留名，不能作为项目名",
                            "\"unfiled\" is the reserved name for unclassified events")}
    if len(name) > 60:
        return {"error": tr("项目名过长（上限 60 字）", "project name too long (max 60 characters)")}
    outcome = ctx.core.add_project(name)
    if outcome.get("archived"):
        return {"ok": True, "id": outcome["id"], "name": name, "existed": True,
                "archived": True,
                "note": tr("该项目已归档，不接收新事件，也不作为本会话默认项目",
                           "this project is archived: no new events, and it will not become "
                           "the session default")}
    ctx.session_project = name
    return {"ok": True, "id": outcome["id"], "name": name, "existed": bool(outcome.get("existed")),
            "session_project": name}


def _tool_project_list(ctx: ToolContext) -> dict[str, Any]:
    projects = []
    for row in ctx.core.list_projects() or []:
        projects.append({"id": row.get("id"), "name": row.get("name"),
                         "status": row.get("status") or "active"})
    return {"projects": projects, "count": len(projects),
            "note": tr("evt_add 的 project 只能选这里，或写 \"unfiled\"",
                       "evt_add's project must come from this list, or be \"unfiled\"")}


def _tool_forget(args: dict[str, Any], ctx: ToolContext) -> dict[str, Any]:
    event_id = as_int(args.get("event_id", args.get("id")))
    if event_id is None:
        return {"error": tr("event_id 必须是整数", "event_id must be an integer")}
    if not ctx.core.get_event(event_id):
        return {"error": tr(f"没有事件 #{event_id}", f"no event #{event_id}")}
    ctx.core.forget(event_id)
    return {"ok": True, "forgotten": event_id}
