"""eventmem.recall — 检索 → RRF 融合 → 按优先级装填 → 按项目分组渲染。

设计：``notes/memory-arch-v2.1.md`` §6。三条铁律：

1. **装填顺序 ≠ 渲染顺序。** 先按优先级（①项目骨架 ②RRF 命中+一跳邻居 ③同会话邻居）
   在 token 预算内装填并整条裁剪，最后才按 ``[项目]`` 分组、组内 ts 倒序渲染。
   如果先按时间排序再切预算，项目骨架里那条最早的「决定」时间戳最老，必然被截掉——
   正好丢掉「为什么」这一角；同时一条昨天发生的边缘命中会靠时间霸占预算。
2. **链要双向取全。** 命中「改变/否定」链上任意一条，就带出整条连通分量。只向前追
   前驱会漏掉新值（命中旧事件时把三个月的 256G 当今天的事实），只向后追新会丢掉
   旧值（"以前是 256G"拼不出来）。
3. **同一会话不重复给正文。** 传 ``state`` 时，已经渲染过的 id 只留 ``#id … 同上文``
   指针；骨架按项目记账，每个项目每会话只给一次（换项目时新项目仍会拿到骨架）。

输出为空串表示"没有可注入的记忆"，调用方据此不注入。
"""

from __future__ import annotations

import logging
import math
import struct
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from .i18n import kind_label, tr  # noqa: E402
from .settings import RecallPolicy  # noqa: E402

try:  # numpy 可选：没有它就纯 Python 点积，结果一致
    import numpy as _np
except Exception:  # noqa: BLE001
    _np = None

logger = logging.getLogger(__name__)

#: Everything the knobs below used to be.  The names stay importable (tests and older
#: adapters read them) but the single source is :class:`eventmem_core.settings.RecallPolicy`.
_POLICY = RecallPolicy()

RRF_K = _POLICY.rrf_k
CHAIN_RELS = ("改变", "否定")
# 只带一跳：命中旧值→拖出新值，命中新值→拖出被取代的旧值。整链会退化成整库
# （实测 56 条事件在一个 改变/否定 连通分量里），把预算和槽位吃光。
CHAIN_MAX_DEPTH = _POLICY.chain_max_depth
#: 骨架最多几条：最早决定 / 最新决定 / 最新结果 / 最新变更 / 最新教训
SKELETON_MAX = _POLICY.skeleton_max
#: 骨架段的预算（每会话/压缩后首轮给一次；骨架只有 ≤5 条，850 是上限不是实际用量）
SKELETON_BUDGET = _POLICY.skeleton_budget
#: 每轮「骨架以外」段的预算（保守估算：CJK 1 字≈1 token）
DEFAULT_BUDGET = _POLICY.budget_tokens
UNFILED = "未归类"
HEADER = "需要原文用 expand(#id)"
#: 同会话内重复命中的条目：只留编号（不占条目配额，也不占正文预算），最多记几个
REPEAT_MAX = _POLICY.repeat_max
REPEAT_NOTE = "同上文"



def unfiled() -> str:
    """Display name for an event with no project."""
    return tr(UNFILED, "unfiled")


def header_text() -> str:
    """One-line reminder printed with the block."""
    return tr(HEADER, "full text: expand(#id)")


def repeat_note() -> str:
    """Pointer line for items already rendered earlier in the session."""
    return tr(REPEAT_NOTE, "see above")


def invalid_mark(stamp: str, rel: str) -> str:
    """``已失效·被11-06取代`` — the mark put on an event that a newer one replaced."""
    if rel == "改变":  # noqa: SIM108 - one comparison, two languages
        return tr("已失效·被%s取代" % stamp, "superseded by %s" % stamp)
    return tr("已失效·被%s否定" % stamp, "negated by %s" % stamp)


@dataclass
class RecallState:
    """跨轮召回状态，每会话一份。

    - ``seen_ids``：本会话已经渲染过正文的事件 id；再次命中时只输出 ``#id`` 指针。
    - ``skeleton_projects``：本会话已经给过骨架的项目名。骨架按项目计数，长会话中途
      换项目时新焦点项目仍会拿到自己的「为什么 / 到哪了」。

    不传 state 的调用是无状态的（每轮都给骨架、不去重），老行为不变。
    """

    seen_ids: set[int] = field(default_factory=set)
    skeleton_projects: set[str] = field(default_factory=set)


@dataclass
class RenderResult:
    """``_render`` 的返回值：块文本 + 这一轮真正渲染了正文 / 只给了指针的 id + 项目集合。"""

    text: str = ""
    new_ids: set[int] = field(default_factory=set)
    repeat_ids: set[int] = field(default_factory=set)
    projects: set[str] = field(default_factory=set)

# 换过 embedding 模型之后，库里两种维度的向量不能按前缀比余弦（那排的是前段能量，
# 不是语义）。只告警一次，避免每轮刷屏。
_DIM_WARNED = False


def _warn_dim_mismatch(dim: int, skipped: int) -> None:
    global _DIM_WARNED
    if _DIM_WARNED or not skipped:
        return
    _DIM_WARNED = True
    logger.warning(
        "eventmem: %d stored vectors are not %d-dim and were ignored; re-embed the library "
        "after changing the embedding model", skipped, dim,
    )


# --------------------------------------------------------------------------- token 估算


def estimate_tokens(text: str) -> int:
    """保守估算：宽字符（CJK/全角/假名/谚文）1 字≈1 token，其他 4 字符≈1 token，向上取整。"""
    if not text:
        return 0
    wide = sum(1 for ch in text if ord(ch) >= 0x2E80)
    narrow = len(text) - wide
    return wide + (narrow + 3) // 4


# --------------------------------------------------------------------------- 召回主入口


def recall(store, encoder, query: str, *, policy: "RecallPolicy | None" = None,
           budget_tokens: "int | None" = None,
           skeleton_budget: "int | None" = None, top_k: "int | None" = None,
           qvec: "list[float] | None" = None,
           now: float | None = None,
           min_score: "float | None" = None, max_items: "int | None" = None,
           state: "RecallState | None" = None) -> str:
    """检索并渲染记忆块。

    输出最多两段，空行分隔，表头只打一次：

    1. **骨架段**（每项目每会话一次，压缩/重置/撤销后重置）：最早决定 / 最新决定 /
       最新结果 / 最新变更 / 最新教训（≤5 条，缺角色跳过），独立预算
       ``skeleton_budget``（默认 850），按 决定→结果→变更→教训 排；
    2. **命中段**（每轮）：RRF 命中 + 一跳 改变/否定 邻居 + 同会话邻居，预算
       ``budget_tokens``（默认 600）。

    ``state`` 是跨轮状态（每会话一份）：骨架按项目记账；本会话已经给过正文的 id
    再命中时只输出一行 ``#id … 同上文`` 指针。``None`` = 无状态（每轮都给骨架、不去重）。
    """
    del now  # 显式说明：渲染只依赖事件 ts，不依赖调用时刻
    pol = policy or _POLICY
    budget_tokens = pol.budget_tokens if budget_tokens is None else int(budget_tokens)
    skeleton_budget = pol.skeleton_budget if skeleton_budget is None else int(skeleton_budget)
    top_k = pol.top_k if top_k is None else int(top_k)
    min_score = pol.min_score if min_score is None else float(min_score)
    max_items = pol.max_items if max_items is None else int(max_items)
    q = query.strip() if isinstance(query, str) else ""
    if not q or store is None or int(top_k) <= 0:
        return ""

    hits = _fuse(store, encoder, q, int(top_k), float(min_score), qvec=qvec,
                 rrf_k=pol.rrf_k)
    if not hits:
        return ""

    hit_ids = [eid for eid, _score in hits]
    events: dict[int, dict] = {}
    for ev in _get_events(store, hit_ids):
        if isinstance(ev, dict) and ev.get("id") is not None:
            try:
                events[int(ev["id"])] = ev
            except (TypeError, ValueError):
                continue

    order = [eid for eid in hit_ids if eid in events]
    if not order:
        return ""

    # 本会话之前已经给过正文的 id（跨轮）；在 basket 里去重是另一码事。
    shown = set(state.seen_ids) if state is not None else set()

    marks: dict[int, str] = {}
    chain_cache: dict[int, list[dict]] = {}

    def pull_chain(seed_id: int) -> list[dict]:
        if seed_id in chain_cache:
            return chain_cache[seed_id]
        chain = _chain(store, seed_id, pol.chain_max_depth)
        chain_cache[seed_id] = chain
        if chain:
            _mark_superseded(store, chain, marks)
        return chain

    # ① 骨架段（独立预算）：最早决定 / 最新决定 / 最新结果 / 最新变更 / 最新教训，最多 5 条。
    #    骨架只回答「为什么 / 到哪了」，**不展开链**：成熟项目里一条骨架的 改变/否定 链
    #    等于整库，会把槽位和预算全吃光。每个项目每会话只给一次。
    skeleton: list[dict] = []
    skeleton_owner: dict[int, str] = {}
    skeleton_ids: set[int] = set()
    for name, pid, _count in _focus_projects(events, order):
        if state is not None and name in state.skeleton_projects:
            continue
        fallback = [events[eid] for eid in order if events[eid].get("project") == name]
        for ev in _skeleton(store, pid, fallback, pol.skeleton_max):
            try:
                eid = int(ev["id"])
            except (KeyError, TypeError, ValueError):
                continue
            if eid in skeleton_ids:
                continue
            skeleton_ids.add(eid)
            skeleton.append(ev)
            skeleton_owner[eid] = name
            pull_chain(eid)       # 只为算失效标记，链成员不进骨架段

    # ② 命中段：按 RRF 从高到低遍历，每条带出**一跳**的 改变/否定 邻居
    #    （命中旧值→拖出新值，命中新值→拖出被取代的旧值）。骨架段已给的 id 不重复。
    basket: list[dict] = []
    seen: set[int] = set(skeleton_ids)

    def want(ev: Any) -> bool:
        if not isinstance(ev, dict) or ev.get("id") is None:
            return False
        try:
            eid = int(ev["id"])
        except (TypeError, ValueError):
            return False
        if eid in seen:
            return False
        seen.add(eid)
        basket.append(ev)
        return True

    new_hits = [eid for eid in order if eid not in shown and eid not in skeleton_ids]
    for eid in order:
        if eid in skeleton_ids:
            continue
        if want(events[eid]):
            if not new_hits:
                continue
            for member in pull_chain(eid):
                want(member)

    # ③ 第三优先：预算还有剩，再放**新命中**事件的同会话前后各 2 条。
    #    整批命中都在之前给过时不补：那种回合的块只该剩指针，不该拿相邻事件凑数。
    for eid in new_hits:
        session = events[eid].get("session")
        if not session:
            continue
        for neighbour in _session_window(store, session, eid, 2, 2):
            want(neighbour)

    body_result = RenderResult()
    skel_result = RenderResult()
    if not skeleton and not basket:
        return ""
    # 先按「多项目」成本装填（行首带 [项目] 是上界），渲染后再按真正留下的项目决定表头。
    if skeleton:
        skel_result = _render(skeleton, marks, {}, budget_tokens=skeleton_budget,
                              max_items=max_items, shown=shown, sort_rows=False,
                              single=False, header=False, repeat_max=pol.repeat_max)
    if basket:
        body_result = _render(basket, marks, dict(hits),
                              budget_tokens=budget_tokens, max_items=max_items,
                              shown=shown, single=False, header=False, repeat_max=pol.repeat_max)

    seg_texts = [r.text for r in (skel_result, body_result) if r.text]
    if not seg_texts:
        return ""
    projects = skel_result.projects | body_result.projects
    if not projects:      # 整轮只有「同上文」指针：项目从候选集合里取
        projects = {ev.get("project") or unfiled() for ev in (skeleton + basket)}
    if len(projects) == 1:
        name = next(iter(projects))
        prefix = "[%s] " % name
        seg_texts = ["\n".join(line[len(prefix):] if line.startswith(prefix) else line
                               for line in seg.split("\n")) for seg in seg_texts]
        header = prefix + header_text()
    else:
        header = header_text()
    text = header + "\n" + "\n\n".join(seg_texts)
    if not text:
        return ""

    if state is not None:
        state.seen_ids.update(skel_result.new_ids)
        state.seen_ids.update(body_result.new_ids)
        # 骨架按项目记账：只有这一轮真的把它给出去了（正文或指针）才算给过。
        for eid in skel_result.new_ids | skel_result.repeat_ids:
            name = skeleton_owner.get(eid)
            if name:
                state.skeleton_projects.add(name)
    return text


# --------------------------------------------------------------------------- 融合


def _fuse(store, encoder, query: str, top_k: int,
          min_score: float = 0.35, qvec: "list[float] | None" = None,
          rrf_k: int = RRF_K) -> list[tuple[int, float]]:
    """向量 top-k + FTS top-k → RRF（k=60）。任一路为空也能工作。

    ``qvec`` 是调用方已经算好的 query 向量（prefetch 一次编码、recall 与 project_hint 共用）：
    一次 encode 实测 25–30 ms，是这条热路径上最贵的一步。
    """
    rankings: list[list[tuple[int, float]]] = []
    if qvec is None:
        qvec = _encode_query(encoder, query)
    if qvec is not None:
        rankings.append(_vector_search(store, qvec, top_k, min_score))
    rankings.append(_fts_search(store, query, top_k))

    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            try:
                eid = int(item[0])
            except (TypeError, ValueError, IndexError):
                continue
            fused[eid] = fused.get(eid, 0.0) + 1.0 / (rrf_k + rank + 1)
    return sorted(fused.items(), key=lambda kv: (-kv[1], kv[0]))


def query_vector(encoder, query: str):
    """对外入口：把 query 编成向量（prefetch 只编一次，recall 与 project_hint 共用）。"""
    return _encode_query(encoder, query)


def _encode_query(encoder, query: str):
    if encoder is None:
        return None
    fn = getattr(encoder, "encode", None)
    if fn is None:
        return None
    try:
        vec = fn(query, is_query=True)
    except TypeError:
        try:
            vec = fn(query)
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001
        return None
    if vec is None:
        return None
    values = _to_floats(vec)
    return values or None


def _fts_search(store, query: str, top_k: int) -> list[tuple[int, float]]:
    fn = getattr(store, "fts_search", None)
    if fn is None:
        return []
    try:
        rows = fn(query, limit=top_k) or []
    except TypeError:
        try:
            rows = fn(query, top_k) or []
        except Exception:  # noqa: BLE001
            return []
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[int, float]] = []
    for row in rows:
        try:
            out.append((int(row[0]), float(row[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _vector_search(store, qvec: list[float], top_k: int,
                   min_score: float = 0.35) -> list[tuple[int, float]]:
    try:
        rows = store.iter_vecs() or []
    except Exception:  # noqa: BLE001
        return []
    if not rows:
        return []
    if _np is not None:
        try:
            return _np_vector_search(rows, qvec, top_k, min_score)
        except Exception:  # noqa: BLE001 - 形状异常时退回纯 Python
            pass
    return _py_vector_search(rows, qvec, top_k, min_score)


def _py_vector_search(rows: Iterable, qvec: list[float], top_k: int,
                      min_score: float = 0.35) -> list[tuple[int, float]]:
    scored: list[tuple[float, int]] = []
    dim = len(qvec)
    mismatched = 0
    for row in rows:
        try:
            eid = int(row[0])
        except (TypeError, ValueError, IndexError):
            continue
        vec = _blob_to_floats(row[1] if len(row) > 1 else None)
        if not vec:
            continue
        if len(vec) != dim:
            mismatched += 1
            continue
        d = dim
        qn = _norm(qvec, d)
        vn = _norm(vec, d)
        if not qn or not vn:
            continue
        dot = 0.0
        for i in range(d):
            dot += qvec[i] * vec[i]
        sim = dot / (qn * vn)
        if sim >= min_score:
            scored.append((sim, eid))
    _warn_dim_mismatch(dim, mismatched)
    scored.sort(key=lambda kv: (-kv[0], kv[1]))
    return [(eid, sim) for sim, eid in scored[:top_k]]


def _np_vector_search(rows: Iterable, qvec: list[float], top_k: int,
                      min_score: float = 0.35) -> list[tuple[int, float]]:
    pairs: list[tuple[int, Any]] = []
    dim = len(qvec)
    mismatched = 0
    for row in rows:
        try:
            eid = int(row[0])
            raw = row[1]
        except (TypeError, ValueError, IndexError):
            continue
        try:
            if isinstance(raw, (bytes, bytearray, memoryview)):
                vec = _np.frombuffer(bytes(raw), dtype="<f4")
            else:
                vec = _np.asarray(raw, dtype=_np.float32)
        except (TypeError, ValueError):
            continue
        if not vec.size:
            continue
        if vec.size != dim:
            mismatched += 1
            continue
        pairs.append((eid, vec))
    _warn_dim_mismatch(dim, mismatched)
    if not pairs:
        return []
    d = dim
    if d <= 0:
        return []
    mat = _np.vstack([v[:d] for _eid, v in pairs]).astype(_np.float32, copy=False)
    qv = _np.asarray(qvec[:d], dtype=_np.float32)
    qn = float(_np.linalg.norm(qv))
    if not qn:
        return []
    norms = _np.linalg.norm(mat, axis=1)
    norms[norms == 0] = 1.0
    sims = (mat @ qv) / (norms * qn)
    ids = [eid for eid, _v in pairs]
    out: list[tuple[int, float]] = []
    for i in _np.argsort(-sims, kind="stable"):
        sim = float(sims[int(i)])
        if sim < min_score:
            continue
        out.append((int(ids[int(i)]), sim))
        if len(out) >= top_k:
            break
    return out


def _to_floats(vec: Any) -> list[float]:
    if vec is None:
        return []
    if isinstance(vec, (bytes, bytearray, memoryview)):
        return _blob_to_floats(vec)
    try:
        return [float(x) for x in vec]
    except (TypeError, ValueError):
        return []


def _blob_to_floats(raw: Any) -> list[float]:
    if raw is None:
        return []
    if isinstance(raw, (bytes, bytearray, memoryview)):
        blob = bytes(raw)
        n = len(blob) // 4
        if n <= 0:
            return []
        return list(struct.unpack("<%df" % n, blob[: n * 4]))
    try:
        return [float(x) for x in raw]
    except (TypeError, ValueError):
        return []


def _norm(vec: list[float], d: int) -> float:
    return math.sqrt(sum(vec[i] * vec[i] for i in range(min(d, len(vec)))))


# --------------------------------------------------------------------------- 装填材料


def _get_events(store, ids: list[int]) -> list[dict]:
    fn = getattr(store, "get_events", None)
    if fn is not None:
        try:
            rows = fn([int(i) for i in ids]) or []
            return [r for r in rows if isinstance(r, dict)]
        except Exception:  # noqa: BLE001
            pass
    fn = getattr(store, "get_event", None)
    if fn is None:
        return []
    out = []
    for eid in ids:
        try:
            ev = fn(int(eid))
        except Exception:  # noqa: BLE001
            ev = None
        if isinstance(ev, dict):
            out.append(ev)
    return out


def _focus_projects(events: dict[int, dict], order: list[int]) -> list[tuple[str, Any, int]]:
    """命中集中到某项目：≥3 条且占命中数 ≥1/3。"""
    counts: dict[str, int] = {}
    pid_by_name: dict[str, Any] = {}
    for eid in order:
        ev = events.get(eid) or {}
        name = ev.get("project")
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
        pid_by_name.setdefault(name, ev.get("project_id"))
    total = len(order)
    focus = [(name, pid_by_name.get(name), n) for name, n in counts.items()
             if n >= 3 and n * 3 >= total]
    focus.sort(key=lambda row: (-row[2], str(row[0])))
    return focus


def _skeleton(store, pid: Any, fallback: list[dict], limit: int = SKELETON_MAX) -> list[dict]:
    """项目骨架：为什么干 / 现在到哪 / 卡在哪，最多 ``SKELETON_MAX`` 条（5）。

    五角：**最早决定**（为什么干）、**最新决定**（现在的方向）、**最新结果**、
    **最新变更**、**最新教训**（卡在哪）。同一角色没有就跳过，所以不足 5 条是常态。

    「否定」在 schema 里是**关系**不是类型；没有「教训」时，用"最新的被否定事件"
    兜底这最后一角。
    """
    events = _project_events(store, pid)
    if not events:
        events = list(fallback or [])
    if not events:
        return []

    picked: list[dict] = []
    picked_ids: set[int] = set()

    def take(ev: Any) -> None:
        if not isinstance(ev, dict) or ev.get("id") is None:
            return
        try:
            eid = int(ev["id"])
        except (TypeError, ValueError):
            return
        if eid in picked_ids:
            return
        picked_ids.add(eid)
        picked.append(ev)

    def key(ev: dict) -> tuple[float, int]:
        return (float(ev.get("ts") or 0.0), int(ev.get("id") or 0))

    decisions = [e for e in events if e.get("kind") == "决定"]
    if decisions:
        take(min(decisions, key=key))       # 为什么干
        take(max(decisions, key=key))       # 现在的方向（只有一条时去重）
    results = [e for e in events if e.get("kind") == "结果"]
    if results:
        take(max(results, key=key))         # 最新结果
    changes = [e for e in events if e.get("kind") == "变更"]
    if changes:
        take(max(changes, key=key))         # 最新变更
    lessons = [e for e in events if e.get("kind") == "教训"]
    if lessons:
        take(max(lessons, key=key))
    else:
        # 项目里没有「教训」时，退而取"最新的被否定事件"。按 ts 从新到旧扫，命中即停，
        # 免得为一个几乎不出现的骨架角把整个项目的事件都查一遍边。
        for ev in sorted(events, key=key, reverse=True):
            if ev.get("id") is None:
                continue
            if any(edge.get("rel") == "否定" for edge in _edges_into(store, ev.get("id"))):
                take(ev)
                break

    return picked[:limit]


def _project_events(store, pid: Any) -> list[dict]:
    if pid is None:
        return []
    fn = getattr(store, "project_events", None)
    if fn is None:
        return []
    try:
        rows = fn(int(pid)) or []
    except Exception:  # noqa: BLE001
        return []
    return [r for r in rows if isinstance(r, dict)]


def _inbound_degree(store, eid: Any) -> int:
    if eid is None:
        return 0
    fn = getattr(store, "inbound_degree", None)
    if fn is None:
        return 0
    try:
        return int(fn(int(eid)) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _chain(store, eid: int, max_depth: int = CHAIN_MAX_DEPTH) -> list[dict]:
    fn = getattr(store, "chain", None)
    if fn is None:
        return []
    for kwargs in ({"rels": CHAIN_RELS, "max_depth": max_depth}, {}):
        try:
            rows = fn(int(eid), **kwargs) or []
        except TypeError:
            continue
        except Exception:  # noqa: BLE001
            return []
        # 防御：不同 Store 实现可能返回 (events, edges) 或 {"events": [...], ...}
        if isinstance(rows, tuple) and rows and isinstance(rows[0], (list, tuple)):
            rows = rows[0]
        elif isinstance(rows, dict):
            rows = rows.get("events") or []
        return [r for r in rows if isinstance(r, dict) and r.get("id") is not None]
    return []


def _edges_into(store, eid: Any) -> list[dict]:
    if eid is None:
        return []
    fn = getattr(store, "edges_into", None)
    if fn is None:
        return []
    try:
        rows = fn(int(eid)) or []
    except Exception:  # noqa: BLE001
        return []
    return [r for r in rows if isinstance(r, dict)]


def _session_window(store, session: str, center_id: int, before: int, after: int) -> list[dict]:
    fn = getattr(store, "session_window", None)
    if fn is None:
        return []
    try:
        rows = fn(session, int(center_id), before, after) or []
    except TypeError:
        try:
            rows = fn(session, int(center_id), before=before, after=after) or []
        except Exception:  # noqa: BLE001
            return []
    except Exception:  # noqa: BLE001
        return []
    return [r for r in rows if isinstance(r, dict)]


# --------------------------------------------------------------------------- 失效标记


def _mark_superseded(store, chain: list[dict], marks: dict[int, str]) -> None:
    """链内"新 → 旧"的 改变/否定 边把旧端标成 [已失效·被MM-DD取代]。

    优先读 ``chain()`` 自带的 ``superseded_by``（真实 Store 会算好，零额外查询）；
    没有这个字段的 Store 实现就逐条问 ``edges_into``。只认链内、且时间更新的来源；
    同刻取「改变」优先。两样都拿不到就只是不标，不影响其它逻辑。
    """
    by_id: dict[int, dict] = {}
    for ev in chain:
        try:
            by_id[int(ev["id"])] = ev
        except (KeyError, TypeError, ValueError):
            continue

    for eid, ev in by_id.items():
        if eid in marks:
            continue
        ts = float(ev.get("ts") or 0.0)
        best: tuple[tuple[float, int], str, float] | None = None

        embedded = ev.get("superseded_by")
        if isinstance(embedded, (list, tuple)):
            # 字段存在（哪怕是空列表）就以它为准，不再回查 edges_into
            for item in embedded:
                if not isinstance(item, dict):
                    continue
                rel = item.get("rel")
                if rel not in CHAIN_RELS:
                    continue
                try:
                    src_ts = float(item.get("ts"))
                except (TypeError, ValueError):
                    continue
                if src_ts <= ts:
                    continue
                rank = (src_ts, 0 if rel == "改变" else 1)
                if best is None or rank > best[0]:
                    best = (rank, str(rel), src_ts)
        else:
            for edge in _edges_into(store, eid):
                rel = edge.get("rel")
                if rel not in CHAIN_RELS:
                    continue
                try:
                    src = int(edge.get("src"))
                except (TypeError, ValueError):
                    continue
                source = by_id.get(src)
                if source is None:
                    continue
                src_ts = float(source.get("ts") or 0.0)
                if src_ts <= ts:
                    continue
                rank = (src_ts, 0 if rel == "改变" else 1)
                if best is None or rank > best[0]:
                    best = (rank, str(rel), src_ts)

        if best is None:
            continue
        _rank, rel, src_ts = best
        stamp = time.strftime("%m-%d", time.localtime(src_ts))
        marks[eid] = invalid_mark(stamp, rel)


# --------------------------------------------------------------------------- 渲染


def _render_body(ev: dict, mark: str | None = None) -> str:
    """一行正文：``#id [失效标记] 类型 正文``；带 artifact 的事件尾随 📎。"""
    kind = str(ev.get("kind") or "")
    text = " ".join(str(ev.get("text") or "").split())
    parts = ["#%d" % int(ev["id"])]
    if mark:
        parts.append("[%s]" % mark)
    if kind:
        parts.append(kind_label(kind))
    if text:
        parts.append(text)
    line = " ".join(parts)
    if ev.get("art_id"):
        line += " 📎"
    return line


def _render_line(ev: dict, mark: str | None = None) -> str:
    """带日期和项目名的单行（估算成本 / 调试用；正式渲染见 ``_render``）。"""
    date = time.strftime("%Y-%m-%d", time.localtime(float(ev.get("ts") or 0.0)))
    project = ev.get("project") or unfiled()
    return "[%s] [%s] %s" % (date, project, _render_body(ev, mark))


def _render(basket: list[dict], marks: dict[int, str] | None = None,
            scores: dict[int, float] | None = None, *,
            budget_tokens: int = DEFAULT_BUDGET, max_items: int = 12,
            shown: set[int] | None = None, sort_rows: bool = True,
            single: bool | None = None, header: bool = True,
            repeat_max: int = REPEAT_MAX) -> RenderResult:
    """按项目分组渲染；项目名只出现一次，日期只在变化时打印。

    单项目时项目名进表头：``[eventmem] 需要原文用 expand(#id)``，每行
    ``[2026-11-08] #58 结果 …``，同一天的后续行省略日期（``#53 …``）。
    多项目时表头不带项目名，行首在项目切换处补 ``[项目]``。
    组序 = 该组最强命中的 RRF 分，组内 ts 倒序；整条装、放不下整条丢，绝不截断正文。

    ``shown`` 是本会话之前已经给过正文的 id：这些条目不再占条目配额与正文预算，
    只在块尾合成一行 ``#id … 同上文`` 指针（统一计一次 token）。

    ``sort_rows=False`` 保留 basket 顺序（骨架段用）；``single`` / ``header`` 供两段式
    外层统一拼表头：``header=False`` 时只返回正文行，项目前缀仍按 ``single`` 决定。
    """
    marks = marks or {}
    scores = scores or {}
    shown = set(shown or ())
    groups: list[tuple[str, float, list[dict]]] = []
    index: dict[str, int] = {}
    for ev in basket:
        name = ev.get("project") or unfiled()
        if name not in index:
            index[name] = len(groups)
            groups.append((name, 0.0, []))
        g_name, g_score, g_rows = groups[index[name]]
        try:
            g_score = max(g_score, float(scores.get(int(ev.get("id") or 0), 0.0)))
        except (TypeError, ValueError):
            pass
        groups[index[name]] = (g_name, g_score, g_rows + [ev])
    groups.sort(key=lambda row: (-row[1], str(row[0])))

    single = len(groups) == 1 if single is None else bool(single)
    _header = (("[%s] %s" % (groups[0][0], header_text())) if single else header_text()) if header else ""
    budget = max(1, int(budget_tokens))
    cap = max(1, int(max_items))

    # ⓪ 重复条目：本会话已经给过正文的，只留编号指针（basket 顺序 = 优先级顺序）。
    repeats: list[int] = []
    repeat_set: set[int] = set()
    for ev in basket:
        try:
            eid = int(ev.get("id"))
        except (TypeError, ValueError):
            continue
        if eid in shown and eid not in repeat_set:
            repeat_set.add(eid)
            repeats.append(eid)
            if len(repeats) >= max(1, int(repeat_max)):
                break
    used = estimate_tokens(_header or header_text()) + 1
    pointer = ""
    pointer_cost = 0
    if repeats:
        pointer = "%s %s" % (" ".join("#%d" % eid for eid in repeats), repeat_note())
        pointer_cost = estimate_tokens(pointer) + 1
        if used + pointer_cost > budget:   # 连指针都放不下就别放，反正这些条目先前已给过
            pointer, pointer_cost = "", 0
    body_budget = budget - pointer_cost

    # ① 选：按 basket 顺序（骨架 → 命中 → 会话邻居）装填。成本按"最坏情况"估——
    #    假定每行都带日期（多项目时还带项目）前缀；渲染时同日/同项目的行会去重，
    #    实际占用只少不多。这样骨架不会因为渲染重排被截掉。
    chosen: list[dict] = []
    for ev in basket:
        if len(chosen) >= cap:
            break
        try:
            eid = int(ev.get("id"))
        except (TypeError, ValueError):
            continue
        if eid in shown:
            # 指针行最多 REPEAT_MAX 个（repeat_set 里的进指针）；超出的重复条目直接不再渲染正文
            continue
        date = time.strftime("%Y-%m-%d", time.localtime(float(ev.get("ts") or 0.0)))
        probe = " ".join(([] if single else ["[%s]" % (ev.get("project") or unfiled())])
                         + ["[%s]" % date, _render_body(ev, marks.get(eid))])
        cost = estimate_tokens(probe) + 1
        if used + cost > body_budget:
            continue          # 整条丢弃，但后面更短的条目仍可装
        used += cost
        chosen.append(ev)
    if not chosen and not pointer:
        return RenderResult()
    keep = {int(ev["id"]) for ev in chosen}

    # ② 渲染：组序 = 组内最强 RRF，组内 ts 倒序；项目名进表头、日期只在变化时打印。
    out = [_header] if _header else []
    prev_date: str | None = None
    prev_proj: str | None = None
    for name, _score, rows in groups:
        if sort_rows:
            rows.sort(key=lambda ev: (-float(ev.get("ts") or 0.0), -int(ev.get("id") or 0)))
        for ev in rows:
            eid = int(ev.get("id"))
            if eid not in keep:
                continue
            date = time.strftime("%Y-%m-%d", time.localtime(float(ev.get("ts") or 0.0)))
            prefix: list[str] = []
            if not single and name != prev_proj:
                prefix.append("[%s]" % name)
            if date != prev_date:
                prefix.append("[%s]" % date)
            out.append(" ".join(prefix + [_render_body(ev, marks.get(eid))]))
            prev_date, prev_proj = date, name
    if pointer:
        out.append(pointer)
    return RenderResult("\n".join(out), {int(ev["id"]) for ev in chosen},
                        set(repeats) if pointer else set(),
                        {ev.get("project") or unfiled() for ev in chosen})


# --------------------------------------------------------------------------- 图导航

#: 导航默认跟的关系：改变/否定回答「谁废了它」，因为回答「它为什么存在」。
#: 细化/重复先不给——实测 441 条边里细化 232 条，一跳就能拖出一屏，反而更看不清。
NEIGHBOUR_RELS = _POLICY.neighbour_rels
#: 一次导航最多渲染多少条邻边，超了就截断并明说。
NEIGHBOUR_CAP = _POLICY.neighbour_cap



def _as_id(value: Any) -> "int | None":
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _edge_rows(store, method: str, eid: int) -> list[dict]:
    fn = getattr(store, method, None)
    if not callable(fn):
        return []
    try:
        rows = fn(int(eid)) or []
    except Exception:  # noqa: BLE001 - navigation is best-effort
        return []
    return [r for r in rows if isinstance(r, dict)]


def neighbours(store, eid: Any, *, rels: "Iterable[str] | None" = None, depth: int = 1,
               window: tuple[int, int] = (2, 2),
               cap: "int | None" = None,
               policy: "RecallPolicy | None" = None) -> str:
    """把一条事件在图上的邻居渲染成块（``mem_neighbors`` 工具用）。

    行格式与召回块完全一致（``[日期] [项目] #id [失效] 类型 正文``），分三段：
    后来的事件（指向它的）、更早的事件（它指向的）、同会话前后几条。空串 = 没这条事件。
    """
    eid = _as_id(eid)
    if eid is None:
        return ""
    get_event = getattr(store, "get_event", None)
    anchor = None
    if callable(get_event):
        try:
            anchor = get_event(eid)
        except Exception:  # noqa: BLE001
            anchor = None
    if not isinstance(anchor, dict):
        return ""

    pol = policy or _POLICY
    wanted = tuple(r for r in (rels or pol.neighbour_rels) if r in ("改变", "否定", "重复", "因为", "细化"))
    wanted = wanted or pol.neighbour_rels
    try:
        hops = max(1, min(int(depth), max(1, int(pol.neighbour_depth_max))))
    except (TypeError, ValueError):
        hops = 1
    cap = max(1, int(pol.neighbour_cap if cap is None else cap))

    inbound: dict[tuple[str, int], float] = {}
    outbound: dict[tuple[str, int], float] = {}
    visited = {eid}
    frontier = [eid]
    truncated = False
    for _hop in range(hops):
        nxt: list[int] = []
        for node in frontier:
            for edge in _edge_rows(store, "edges_into", node):
                rel = str(edge.get("rel") or "")
                other = _as_id(edge.get("src"))
                if rel not in wanted or other is None or other == eid:
                    continue
                inbound.setdefault((rel, other), float(edge.get("conf") or 0.0))
                if other not in visited:
                    nxt.append(other)
            for edge in _edge_rows(store, "edges_from", node):
                rel = str(edge.get("rel") or "")
                other = _as_id(edge.get("dst"))
                if rel not in wanted or other is None or other == eid:
                    continue
                outbound.setdefault((rel, other), float(edge.get("conf") or 0.0))
                if other not in visited:
                    nxt.append(other)
        visited |= set(nxt)
        frontier = nxt
        if not frontier:
            break

    if len(inbound) + len(outbound) > cap:
        truncated = True

    ids = sorted({other for _rel, other in list(inbound) + list(outbound)})
    by_id: dict[int, dict] = {}
    get_events = getattr(store, "get_events", None)
    if callable(get_events) and ids:
        try:
            for ev in get_events(ids) or []:
                if isinstance(ev, dict) and ev.get("id") is not None:
                    by_id[int(ev["id"])] = ev
        except Exception:  # noqa: BLE001
            by_id = {}

    def rows(pairs: dict[tuple[str, int], float]) -> list[tuple[float, int, str, float, dict]]:
        out = []
        for (rel, other), conf in pairs.items():
            ev = by_id.get(other)
            if ev is None:
                continue
            out.append((float(ev.get("ts") or 0.0), other, rel, conf, ev))
        out.sort(key=lambda item: (-item[0], -item[1]))
        return out

    late = rows(inbound)
    early = rows(outbound)

    session_name = anchor.get("session")
    around: list[dict] = []
    if session_name and tuple(window) != (0, 0):
        before, after = window
        for ev in _session_window(store, str(session_name), eid, int(before), int(after)):
            if isinstance(ev, dict) and _as_id(ev.get("id")) not in (None, eid):
                around.append(ev)

    # 锚点是否已经失效：取最新一条「改变/否定」入边的日期做标记（与召回块同一套文案）。
    mark = None
    best = None
    for _ts, _oid, rel, _conf, ev in late:
        if rel not in CHAIN_RELS:
            continue
        key = (float(ev.get("ts") or 0.0), 0 if rel == "改变" else 1)
        if best is None or key > best[0]:
            best = (key, rel, float(ev.get("ts") or 0.0))
    if best is not None:
        _key, rel, src_ts = best
        mark = invalid_mark(time.strftime("%m-%d", time.localtime(src_ts)), rel)

    out: list[str] = [
        tr("[eventmem] #%d 的邻居｜%s｜%d 跳", "[eventmem] neighbours of #%d | %s | %d hop(s)")
        % (eid, "/".join(wanted), hops),
        _render_line(anchor, mark),
    ]

    title_late = tr("← 后来的事件（谁指向它）", "← later events (pointing at it)")
    title_early = tr("→ 更早的事件（它指向谁）", "→ earlier events (it points at)")
    if late:
        out.append(title_late)
        for _ts, _oid, rel, conf, ev in late[:cap]:
            out.append("  %s %.2f ← %s" % (rel, conf, _render_line(ev)))
    if early:
        out.append(title_early)
        for _ts, _oid, rel, conf, ev in early[:cap]:
            out.append("  %s %.2f → %s" % (rel, conf, _render_line(ev)))
    if around:
        out.append(tr("· 同会话（前后）", "· same session (around it)"))
        for ev in around:
            out.append("  " + _render_line(ev))
    if not late and not early and not around:
        out.append(tr("（没有连边，也没有同会话邻居）", "(no edges, no same-session neighbours)"))
    elif truncated:
        out.append(tr("… 邻居过多，只列前 %d 条（可用 rels 收窄）",
                      "… too many neighbours, first %d shown (narrow with rels)") % cap)
    return "\n".join(out)


# --------------------------------------------------------------------------- 项目候选

#: prefetch 每回合最多给几个项目候选（模型写事件时只能选不能造）。
PROJECT_HINT_LIMIT = _POLICY.project_hint_limit


def _project_centroid(store, project_id: Any) -> tuple[list[float], int]:
    """该项目所有事件向量的均值（向量本来就按 ``[项目名] 类型 text`` 编码）。"""
    fn = getattr(store, "project_events", None)
    if not callable(fn):
        return [], 0
    try:
        rows = fn(int(project_id)) or []
    except Exception:  # noqa: BLE001 - 候选是锦上添花，绝不拖垮 prefetch
        return [], 0
    acc: "list[float] | None" = None
    n = 0
    for ev in rows:
        if not isinstance(ev, dict):
            continue
        vec = _to_floats(ev.get("vec"))
        if not vec:
            continue
        if acc is None:
            acc = [0.0] * len(vec)
        if len(vec) != len(acc):
            continue
        for i, x in enumerate(vec):
            acc[i] += x
        n += 1
    if acc is None or not n:
        return [], 0
    return [x / n for x in acc], n


#: 空项目的「名字向量」缓存：没有事件就没有质心，但名字至少还能参与排序。
#: 键里带维度——测试会换不同维度的假编码器，别让它们互相串味。
_NAME_VECS: dict[tuple[str, int], list[float]] = {}


def _name_vector(encoder, name: str, dim: int) -> list[float]:
    key = (name, dim)
    if key in _NAME_VECS:
        return _NAME_VECS[key]
    vec = _encode_query(encoder, "[%s]" % name) or []
    if len(vec) != dim:
        vec = []
    _NAME_VECS[key] = vec
    return vec


def project_hint(store, encoder, query: str, *, limit: "int | None" = None,
                 qvec: "list[float] | None" = None,
                 policy: "RecallPolicy | None" = None) -> str:
    """一行项目候选，按本回合 query 与「项目事件质心」的 cos 排序。

    没有项目时返回空串。空项目（还没有事件）退回它的名字向量：没有事件就没有质心，
    但「小主机」这个名字对「这块 4060 显存」仍然是有用的信号，而且空项目恰恰最可能
    承接新工作。编码器不可用时退化成按活跃度列名字，不假装排过序。
    ``qvec`` 允许调用方传入已经算好的 query 向量（prefetch 与 recall 共用一次编码）。
    写新项目必须自己调 ``proj_add``。
    """
    lister = getattr(store, "list_projects", None)
    if not callable(lister):
        return ""
    try:
        rows = lister() or []
    except Exception:  # noqa: BLE001
        return ""
    projects: list[dict] = []
    for p in rows:
        if not isinstance(p, dict) or not p.get("name") or p.get("id") is None:
            continue
        if str(p.get("status") or "active") != "active":
            continue
        projects.append(p)
    if not projects:
        return ""

    if qvec is None:
        qvec = _encode_query(encoder, query) if query else None
    scored: list[tuple[float, int, str]] = []
    for p in projects:
        centroid, n = _project_centroid(store, p["id"])
        if qvec is not None and not centroid:
            # 空项目：没有事件可平均，用名字向量顶上去（算一次、进程内缓存）。
            centroid = _name_vector(encoder, str(p["name"]), len(qvec))
        cos = -2.0
        if qvec is not None and centroid and len(centroid) == len(qvec):
            den = _norm(qvec, len(qvec)) * _norm(centroid, len(centroid))
            if den:
                cos = sum(a * b for a, b in zip(qvec, centroid)) / den
        scored.append((cos, n, str(p["name"])))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2]))

    pol = policy or _POLICY
    try:
        take = max(1, int(pol.project_hint_limit if limit is None else limit))
    except (TypeError, ValueError):
        take = pol.project_hint_limit
    names = [name for _cos, _n, name in scored[:take]]

    if qvec is not None and any(cos > -2.0 for cos, _n, _name in scored):
        line = tr("[eventmem] 可用项目（按本回合相关度）：%s",
                  "[eventmem] projects (by relevance to this turn): %s") % " / ".join(names)
    else:
        line = tr("[eventmem] 可用项目：%s", "[eventmem] projects: %s") % " / ".join(names)
    if len(scored) > len(names):
        line += tr("（共 %d 个）", " (%d total)") % len(scored)
    line += tr("；都不合适就先 proj_add 再 evt_add",
               "; if none fits, call proj_add first")
    return line
