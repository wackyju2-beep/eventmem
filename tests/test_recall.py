#!/usr/bin/env python3
"""eventmem recall 测试 — 直接运行：``python tests/test_recall.py``。

不依赖 store.py / schema.py / __init__.py（它们由其它任务并行实现）：
本文件自带 FakeStore（实现 recall 用到的那些方法）+ embed.FakeEncoder。

覆盖 design §6 的关键路径，尤其是"装填顺序 ≠ 渲染顺序"：项目骨架里那条最早的
「决定」在预算紧张时必须仍然被保留。
"""

from __future__ import annotations

import datetime as _dt
import re
import struct
import os
import sys

os.environ["EVENTMEM_LANG"] = "zh"   # 断言的是中文文案
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

import eventmem_core.recall as recall_mod  # noqa: E402
from eventmem_core.embed import FakeEncoder  # noqa: E402
from eventmem_core.recall import estimate_tokens, recall  # noqa: E402


# --------------------------------------------------------------------------- 工具


def ts(year, month, day, hour=9, minute=0):
    return _dt.datetime(year, month, day, hour, minute).timestamp()


def vec_bytes(encoder, text):
    """编码器输出 → 落库用的 float32 little-endian bytes。"""
    vec = encoder.encode(text)
    if vec is None:
        return None
    values = [float(x) for x in vec]
    return struct.pack("<%df" % len(values), *values)


def terms(query):
    out = []
    for tok in re.findall(r"[A-Za-z0-9_+#.\-]{2,}|[\u4e00-\u9fff]+", query or ""):
        if re.match(r"[A-Za-z0-9]", tok):
            out.append(tok.lower())
        elif len(tok) <= 2:
            out.append(tok)
        else:
            out.extend(tok[i:i + 2] for i in range(len(tok) - 1))
    return list(dict.fromkeys(out))


def segments(out):
    """按空行切段：会话首轮是「骨架段 + 命中段」，后续只有命中段。"""
    return [seg for seg in (out or "").split("\n\n") if seg.strip()]


def body_lines(out):
    """命中段的正文行（跳过表头）。两段时最后一段没有重复表头。"""
    segs = segments(out)
    if not segs:
        return []
    lines = segs[-1].split("\n")
    return lines[1:] if len(segs) == 1 else lines


# --------------------------------------------------------------------------- FakeStore


class FakeStore:
    """只实现 recall 需要的方法；语义与契约一致，不做持久化。"""

    def __init__(self, fts_override=None):
        self.events = {}
        self.edges = []
        self.projects = {}
        self.fts_override = dict(fts_override or {})

    # -- 构造

    def add(self, eid, ts_value, kind, text, project=None, session=None,
            art_id=None, vec=None):
        pid = None
        if project:
            pid = self.projects.setdefault(project, len(self.projects) + 1)
        self.events[int(eid)] = {
            "id": int(eid), "ts": float(ts_value), "kind": kind, "text": text,
            "project": project, "project_id": pid, "session": session,
            "art_id": art_id, "src_msg": None, "vec": vec,
        }
        return int(eid)

    def add_edge(self, src, dst, rel, conf=1.0):
        self.edges.append({"src": int(src), "dst": int(dst), "rel": rel, "conf": conf})

    # -- 读

    def iter_vecs(self):
        return [(eid, ev["vec"]) for eid, ev in sorted(self.events.items()) if ev.get("vec")]

    def fts_search(self, query, limit=20):
        if query in self.fts_override:
            return list(self.fts_override[query])[:limit]
        wanted = terms(query)
        if not wanted:
            return []
        scored = []
        for eid, ev in self.events.items():
            hay = ("%s %s %s" % (ev.get("project") or "", ev.get("kind") or "",
                                 ev.get("text") or "")).lower()
            hits = sum(1 for term in wanted if term.lower() in hay)
            if hits:
                scored.append((eid, float(hits)))
        scored.sort(key=lambda kv: (-kv[1], kv[0]))
        return scored[:limit]

    def edges_into(self, eid):
        return [dict(e) for e in self.edges if e["dst"] == int(eid)]

    def edges_from(self, eid):
        return [dict(e) for e in self.edges if e["src"] == int(eid)]

    def get_event(self, eid):
        ev = self.events.get(int(eid))
        return dict(ev) if ev else None

    def get_events(self, ids):
        out = []
        for eid in ids:
            ev = self.events.get(int(eid))
            if ev:
                out.append(dict(ev))
        return out

    def project_events(self, project_id):
        return [dict(ev) for ev in self.events.values()
                if ev.get("project_id") == int(project_id)]

    def list_projects(self, include_archived=False):
        return [{"id": pid, "name": name, "status": "active"}
                for name, pid in self.projects.items()]

    def session_window(self, session, center_id, before=2, after=2):
        rows = [ev for ev in self.events.values() if ev.get("session") == session]
        rows.sort(key=lambda ev: (ev["ts"], ev["id"]))
        index = next((i for i, ev in enumerate(rows) if ev["id"] == int(center_id)), None)
        if index is None:
            return []
        lo = max(0, index - int(before))
        hi = index + int(after) + 1
        return [dict(ev) for ev in rows[lo:hi] if ev["id"] != int(center_id)]

    def chain(self, eid, rels=("改变", "否定"), max_depth=20):
        """与真实 Store 同语义：``max_depth`` 是**跳数**，不是节点数。"""
        eid = int(eid)
        if eid not in self.events:
            return []
        adjacency = {}
        for edge in self.edges:
            if edge["rel"] not in rels:
                continue
            if edge["src"] not in self.events or edge["dst"] not in self.events:
                continue
            adjacency.setdefault(edge["src"], set()).add(edge["dst"])
            adjacency.setdefault(edge["dst"], set()).add(edge["src"])
        seen = {eid}
        frontier = {eid}
        for _ in range(max(0, int(max_depth))):
            nxt = set()
            for cur in frontier:
                nxt |= adjacency.get(cur, set())
            nxt -= seen
            if not nxt:
                break
            seen |= nxt
            frontier = nxt
        return [dict(self.events[i]) for i in sorted(seen, key=lambda i: (self.events[i]["ts"], i))]

    def resolve_head(self, eid, max_hops=3, min_cos=0.0, rels=("改变",)):
        """与真实 Store 同语义：只跟 改变、每步唯一后继、跳数上限（fake 不算 cos）。"""
        eid = int(eid)
        cur = self.events.get(eid)
        if cur is None:
            return None
        hops = 0
        seen = {eid}
        while hops < max(0, int(max_hops)):
            nxt = sorted({edge["src"] for edge in self.edges
                          if edge["dst"] == int(cur["id"]) and edge["rel"] in rels}
                         - seen)
            if len(nxt) != 1:
                break
            cand = self.events.get(nxt[0])
            if cand is None or (cand["ts"], cand["id"]) <= (cur["ts"], cur["id"]):
                break
            cur = cand
            seen.add(int(cand["id"]))
            hops += 1
        return {"event": dict(cur), "hops": hops} if hops else None

    def edges_into(self, eid):
        return [dict(edge) for edge in self.edges if edge["dst"] == int(eid)]

    def inbound_degree(self, eid):
        return sum(1 for edge in self.edges if edge["dst"] == int(eid))


# --------------------------------------------------------------------------- 用例


TESTS = []


def test(fn):
    TESTS.append(fn)
    return fn


def _tight_store():
    """项目骨架用例的语料：一条最老的「决定」+ 一堆又新又长的边缘命中。"""
    store = FakeStore()
    project = "小主机"
    store.add(1, ts(2026, 10, 25), "决定", "当时只有 256G，训练数据不能放这儿", project=project)
    store.add(2, ts(2026, 10, 26), "尝试", "把训练数据挂到 USB 移动盘上跑", project=project)
    store.add(3, ts(2026, 10, 26, 13), "结果", "USB 盘 IO 抖动，每 epoch 慢一倍", project=project)
    store.add(4, ts(2026, 10, 27), "教训", "USB 盘不能当训练数据盘", project=project)
    store.add(5, ts(2026, 11, 6), "变更", "小主机换 2T NVMe 作系统盘，原 256G 退成备份盘", project=project)
    filler = "训练数据迁移到新盘之后重新跑了完整的三阶段流程并记录每一步耗时与损失曲线用于横向对比"
    for i in range(6, 13):
        # 用「尝试」：否则按 §6「最新一条结果/变更」取到的会是这些填充事件而不是 #5
        store.add(i, ts(2026, 11, 6, 14) + i, "尝试", "%s（第 %d 次观测）" % (filler, i), project=project)
    return store


@test
def test_empty_store_returns_empty():
    assert recall(FakeStore(), FakeEncoder(64), "任何问题") == ""


@test
def test_blank_query_returns_empty():
    store = FakeStore()
    store.add(1, ts(2026, 10, 25), "决定", "只有 256G", project="小主机")
    assert recall(store, FakeEncoder(64), "   ") == ""
    assert recall(store, FakeEncoder(64), "") == ""


@test
def test_skeleton_old_decision_survives_tight_budget():
    """§6 的核心 bug：预算紧张时最老的「决定」不能被时间排序截掉。"""
    store = _tight_store()
    out = recall(store, None, "训练数据 盘", budget_tokens=120, top_k=20)
    assert out, "应当有输出"
    segs = segments(out)
    assert len(segs) == 2, "首轮应有「骨架段 + 命中段」:\n" + out
    skeleton_seg, body_seg = segs
    assert "#1" in skeleton_seg, "项目骨架里最早的「决定」缺失:\n" + out
    assert "#5" in skeleton_seg, "最新「变更」缺失:\n" + out
    assert "#4" in skeleton_seg, "最新「教训」缺失:\n" + out
    # 骨架段固定按 决定 → 最新结果/变更 → 最新教训，不按时间
    assert (skeleton_seg.index("#1") < skeleton_seg.index("#5") < skeleton_seg.index("#4")), \
        "骨架段顺序应为 决定→结果→教训:\n" + skeleton_seg
    # 又新又长的边缘命中不能靠时间霸占预算
    assert "#12" not in out, "填表事件挤掉了骨架:\n" + out
    assert len(body_seg.split("\n")) < 12, "预算明显超了:\n" + out
    # 命中段仍是组内 ts 倒序；日期只在变化时打印
    dates, last = [], None
    for line in body_seg.split("\n"):
        m = re.match(r"\[(\d{4}-\d{2}-\d{2})\]", line)
        if m:
            last = m.group(1)
        dates.append(last)
    assert dates == sorted(dates, reverse=True), "命中段没有按 ts 倒序渲染:\n" + out
    # 反证：预算足够时它们都是候选
    full = recall(store, None, "训练数据 盘", budget_tokens=3000)
    assert "#12" in full and "#1" in full


@test
def test_budget_is_respected():
    store = _tight_store()
    budget = 120
    out = recall(store, None, "训练数据 盘", budget_tokens=budget)
    segs = segments(out)
    assert len(segs) == 2, "首轮应有骨架段 + 命中段:\n" + out
    used_body = sum(estimate_tokens(line) + 1 for line in segs[-1].split("\n"))
    assert used_body <= budget, "命中段估算 %d 超过预算 %d:\n%s" % (used_body, budget, out)
    used_skel = sum(estimate_tokens(line) + 1 for line in segs[0].split("\n"))
    assert used_skel <= recall_mod.SKELETON_BUDGET, \
        "骨架段估算 %d 超过独立预算 %d:\n%s" % (used_skel, recall_mod.SKELETON_BUDGET, out)


@test
def test_cross_project_render_has_project_names():
    store = FakeStore(fts_override={"q": [(101, 5.0), (102, 4.0), (201, 3.0), (202, 2.0)]})
    store.add(101, ts(2026, 10, 1), "决定", "冻结基座，只训池化头", project="Qwen训练")
    store.add(102, ts(2026, 10, 2), "结果", "两条路线都撞在 0.7301", project="Qwen训练")
    store.add(201, ts(2026, 10, 3), "变更", "小主机换 2T NVMe", project="小主机")
    store.add(202, ts(2026, 10, 4), "教训", "USB 盘不能当训练盘")
    out = recall(store, None, "q", budget_tokens=400)
    assert "[Qwen训练]" in out, out
    assert "[小主机]" in out, out
    assert "[未归类]" in out, out
    assert "[2026-10-02]" in out, out
    # 组内 ts 倒序
    assert out.index("#102") < out.index("#101"), out
    # 每行都能定位到 #id（新格式把 id 放在行首）
    for line in body_lines(out):
        assert re.search(r"(^|\s)#\d+\b", line), line
    assert "expand(#id)" in out.split("\n")[0]


@test
def test_degrade_to_fts_without_encoder():
    store = FakeStore()
    store.add(7, ts(2026, 10, 5), "结果", "两条路线都撞在 0.7301", project="Qwen训练")
    out_none = recall(store, None, "0.7301", budget_tokens=200)
    assert "#7" in out_none, "encoder=None 时应降级纯 FTS:\n" + out_none

    class DeadEncoder:
        dim = 32

        def encode(self, text, is_query=False):
            return None

    out_dead = recall(store, DeadEncoder(), "0.7301", budget_tokens=200)
    assert "#7" in out_dead, "encode() 返回 None 时应降级纯 FTS:\n" + out_dead


@test
def test_no_match_returns_empty_string():
    store = FakeStore()
    store.add(1, ts(2026, 10, 25), "决定", "只有 256G，训练数据不能放这儿", project="小主机")
    assert recall(store, None, "zzzqqq") == ""
    assert recall(store, FakeEncoder(64), "zzzqqq") == ""  # 没有 vec 时向量臂也空


@test
def test_chain_old_event_marked_superseded():
    store = FakeStore()
    store.add(10, ts(2026, 10, 25), "决定", "当时预算只够 256G，训练数据不能放这儿", project="小主机")
    store.add(20, ts(2026, 11, 6), "变更", "小主机换 2T NVMe 作系统盘", project="小主机")
    store.add_edge(20, 10, "改变")
    # 命中旧端：链必须把新值带出来，否则会把三个月前的 256G 当今天的事实
    out_old = recall(store, None, "预算", budget_tokens=400)
    assert "#10" in out_old and "#20" in out_old, "命中旧事件时链没有带出新值:\n" + out_old
    line10 = next(line for line in out_old.split("\n") if "#10" in line)
    assert "已失效·被11-06取代" in line10, line10
    # 命中新端：链必须把旧值带出来，"以前是 256G"才拼得出来
    out_new = recall(store, None, "NVMe", budget_tokens=400)
    assert "#10" in out_new and "#20" in out_new, "命中新事件时链没有带出旧值:\n" + out_new


@test
def test_current_state_resolution_skips_the_middle_hop():
    """256G→1T→2T：命中 256G 时，块里要给 2T（当前），不要中间那个 1T。"""
    store = FakeStore()
    store.add(10, ts(2026, 9, 1), "决定", "系统盘 256G，根分区只用了一半", project="小主机")
    store.add(20, ts(2026, 9, 20), "变更", "把系统盘换成 1T 的 NVMe", project="小主机")
    store.add(30, ts(2026, 10, 30), "变更", "又把系统盘换成 2T，克隆后启动正常", project="小主机")
    store.add_edge(20, 10, "改变")
    store.add_edge(30, 20, "改变")
    store.fts_override["zzzqqq"] = [(10, 5.0)]
    out = recall(store, None, "zzzqqq", budget_tokens=600)
    assert "#10" in out and "#30" in out, "旧值或当前值没进块：\n" + out
    assert "#20" not in out, "中间那一跳不该出现：\n" + out
    line30 = next(line for line in out.split("\n") if "#30" in line)
    assert "当前" in line30, line30
    line10 = next(line for line in out.split("\n") if "#10" in line)
    assert "已失效" in line10, line10


@test
def test_current_state_resolution_stops_on_a_branch():
    """两条后继 = 不知道哪条是当前值：退回只给一跳，不乱猜。"""
    store = FakeStore()
    store.add(10, ts(2026, 9, 1), "决定", "系统盘 256G", project="小主机")
    store.add(20, ts(2026, 9, 20), "变更", "换成 1T", project="小主机")
    store.add(25, ts(2026, 9, 21), "变更", "换成 512G", project="小主机")
    store.add_edge(20, 10, "改变")
    store.add_edge(25, 10, "改变")
    store.fts_override["zzzqqq"] = [(10, 5.0)]
    out = recall(store, None, "zzzqqq", budget_tokens=600)
    assert "当前" not in out, "分叉时不该标当前：\n" + out
    assert "#10" in out and ("#20" in out or "#25" in out), out


@test
def test_chain_negative_relation_marks_old_end():
    store = FakeStore()
    store.add(30, ts(2026, 10, 20), "教训", "以为 USB 盘能当训练盘", project="小主机")
    store.add(40, ts(2026, 11, 2), "结果", "实测 IO 抖动，方案作废", project="小主机")
    store.add_edge(40, 30, "否定")
    out = recall(store, None, "IO 抖动", budget_tokens=400)
    assert "#30" in out and "#40" in out, "否定链没有整条带出:\n" + out
    line30 = next(line for line in out.split("\n") if "#30" in line)
    assert "已失效·被11-02否定" in line30, line30


@test
def test_superseded_by_embedded_in_chain_is_used():
    """真实 Store 的 chain() 自带 superseded_by；此时不应再依赖 edges_into。"""
    store = FakeStore()
    store.add(50, ts(2026, 10, 25), "决定", "只有 256G", project="小主机")
    store.add(60, ts(2026, 11, 6), "变更", "换 2T NVMe", project="小主机")

    def chain(eid, rels=("改变", "否定"), max_depth=20):
        rows = [dict(store.events[50]), dict(store.events[60])]
        for row in rows:
            row["superseded_by"] = []
        rows[0]["superseded_by"] = [{"id": 60, "ts": store.events[60]["ts"], "rel": "改变"}]
        rows[0]["invalidated"] = True
        return rows

    def boom(_eid):
        raise RuntimeError("edges_into must not be needed when chain() embeds the relation")

    store.chain = chain
    store.edges_into = boom
    out = recall(store, None, "256G", budget_tokens=400)
    line50 = next(line for line in out.split("\n") if "#50" in line)
    assert "已失效·被11-06取代" in line50, line50


@test
def test_artifact_marker_only_on_that_line():
    store = FakeStore(fts_override={"q": [(1, 2.0), (2, 1.0)]})
    store.add(1, ts(2026, 10, 29), "尝试", "启动第三阶段精修，long_budget 设 3072",
              project="Qwen训练", art_id=88)
    store.add(2, ts(2026, 10, 28), "结果", "两条路线都撞在 0.7301", project="Qwen训练")
    out = recall(store, None, "q", budget_tokens=400)
    line1 = next(line for line in out.split("\n") if "#1" in line)
    line2 = next(line for line in out.split("\n") if "#2" in line)
    assert line1.rstrip().endswith("📎"), line1
    assert not line2.rstrip().endswith("📎"), line2


@test
def test_session_neighbours_added_last():
    store = FakeStore()
    store.add(1, ts(2026, 10, 5, 9), "决定", "XQZ 方案定为纯本地", project="小主机", session="sess-a")
    store.add(2, ts(2026, 10, 5, 10), "结果", "顺带观察到风扇转速曲线很平", project="小主机", session="sess-a")
    store.add(3, ts(2026, 10, 5, 11), "教训", "另一个会话里的无关条目", project="小主机", session="sess-b")
    out = recall(store, None, "XQZ", budget_tokens=400)
    assert "#1" in out, out
    assert "#2" in out, "同会话邻居没带出:\n" + out
    assert "#3" not in out, "不同会话的条目不该被带出:\n" + out


@test
def test_rrf_merges_vector_and_fts_arms():
    encoder = FakeEncoder(64)
    store = FakeStore()
    # 1 只有向量臂能命中（字面完全不同，但向量就是那句话）
    store.add(1, ts(2026, 10, 2), "结果", "语义相近但一个字都不重样的记录",
              project="Qwen训练", vec=vec_bytes(encoder, "两条路线都撞在 0.7301"))
    # 2 只有 FTS 臂能命中（没有 vec）
    store.add(2, ts(2026, 10, 3), "结果", "两条路线都撞在 0.7301", project="Qwen训练")
    out = recall(store, encoder, "两条路线都撞在 0.7301", budget_tokens=400)
    assert "#1" in out, "向量臂命中丢失:\n" + out
    assert "#2" in out, "FTS 臂命中丢失:\n" + out


@test
def test_vector_path_pure_python_matches_numpy():
    encoder = FakeEncoder(64)
    store = FakeStore()
    store.add(1, ts(2026, 10, 1), "决定", "只有 256G，训练数据不能放这儿",
              project="小主机", vec=vec_bytes(encoder, "只有 256G，训练数据不能放这儿"))
    store.add(2, ts(2026, 10, 2), "结果", "两条路线都撞在 0.7301",
              project="Qwen训练", vec=vec_bytes(encoder, "两条路线都撞在 0.7301"))
    store.add(3, ts(2026, 10, 3), "教训", "USB 盘不能当训练盘",
              project="小主机", vec=vec_bytes(encoder, "USB 盘不能当训练盘"))

    with_np = recall(store, encoder, "训练数据 放哪儿", budget_tokens=400)
    saved = recall_mod._np
    recall_mod._np = None
    try:
        without_np = recall(store, encoder, "训练数据 放哪儿", budget_tokens=400)
    finally:
        recall_mod._np = saved
    ids_np = {int(m) for m in re.findall(r"#(\d+)", with_np)}
    ids_py = {int(m) for m in re.findall(r"#(\d+)", without_np)}
    assert ids_np == ids_py, (with_np, without_np)
    assert 1 in ids_np and ids_np, (with_np, without_np)


@test
def test_skeleton_picks_up_to_five():
    """骨架 ≤5 条：最早决定 / 最新决定 / 最新结果 / 最新变更 / 最新教训。"""
    store = FakeStore()
    for i in range(10, 30):
        store.add(i, ts(2026, 9, 1, 9) + i * 3600, "尝试", "阶段推进记录 %d" % i, project="Qwen训练")
    store.add(5, ts(2026, 9, 3), "决定", "骨架：最早决定", project="Qwen训练")
    store.add(8, ts(2026, 9, 7), "决定", "骨架：最新决定", project="Qwen训练")
    store.add(6, ts(2026, 9, 4), "结果", "骨架：早一点的结果", project="Qwen训练")
    store.add(9, ts(2026, 9, 8), "结果", "骨架：最新结果", project="Qwen训练")
    store.add(10, ts(2026, 9, 9), "变更", "骨架：最新变更", project="Qwen训练")
    store.add(7, ts(2026, 9, 5), "教训", "骨架：早一点的教训", project="Qwen训练")
    store.add(11, ts(2026, 9, 10), "教训", "骨架：最新教训", project="Qwen训练")
    store.add(900, ts(2026, 9, 2), "尝试", "被反复引用的里程碑", project="Qwen训练")
    for src in (12, 13, 14, 15, 16):
        store.add_edge(src, 900, "细化")
    skeleton = recall_mod._skeleton(store, store.projects["Qwen训练"], [])
    ids = [e["id"] for e in skeleton]
    assert ids == [5, 8, 9, 10, 11], \
        "骨架应是 最早决定/最新决定/最新结果/最新变更/最新教训：%r" % ids
    assert 900 not in ids, "里程碑不该再被强塞进骨架"
    assert recall_mod.SKELETON_MAX == 5, recall_mod.SKELETON_MAX


@test
def test_skeleton_skips_missing_roles():
    """角色缺失就跳过：只有一条决定时不重复，不足 5 条是常态。"""
    store = FakeStore()
    store.add(1, ts(2026, 9, 1), "决定", "唯一决定", project="P")
    store.add(2, ts(2026, 9, 2), "结果", "唯一结果", project="P")
    skeleton = recall_mod._skeleton(store, store.projects["P"], [])
    assert [e["id"] for e in skeleton] == [1, 2]


@test
def test_token_estimate_counts_cjk_as_one():
    assert estimate_tokens("中文") == 2
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("") == 0
    assert estimate_tokens("中文abcd") == 3


# --------------------------------------------------------------------------- 跨轮状态（会话内不重复）


@test
def test_skeleton_is_given_once_per_project_per_session():
    """骨架每个项目每会话只给一次；第二轮起命中里没有的骨架条目不再出现。"""
    store = FakeStore(fts_override={"NVMe 系统盘": [(5, 5.0), (6, 4.0), (7, 3.0), (8, 2.0), (9, 1.0)]})
    store.add(1, ts(2026, 10, 1), "决定", "当初只批了 5000 块预算", project="小主机")
    store.add(5, ts(2026, 11, 6, 10), "变更", "换成 2T NVMe 系统盘", project="小主机")
    store.add(6, ts(2026, 11, 6, 11), "结果", "NVMe 装好后开机 8 秒", project="小主机")
    store.add(7, ts(2026, 11, 6, 12), "教训", "NVMe 别用杂牌", project="小主机")
    store.add(8, ts(2026, 11, 6, 13), "尝试", "NVMe 温度压测", project="小主机")
    store.add(9, ts(2026, 11, 6, 14), "结果", "NVMe 待机 41 度", project="小主机")

    state = recall_mod.RecallState()
    first = recall(store, None, "NVMe 系统盘", budget_tokens=400, state=state)
    assert "#1" in first, "首轮应给骨架里最早的「决定」:\n" + first
    second = recall(store, None, "NVMe 系统盘", budget_tokens=400, state=state)
    assert "#1" not in second, "骨架不该在第二轮再次出现:\n" + second
    third = recall(store, None, "NVMe 系统盘", budget_tokens=400, state=state)
    assert "#1" not in third, "骨架第三轮也不该出现:\n" + third


@test
def test_repeat_items_become_same_as_above_pointers():
    """同会话第二次命中同一批条目：只留编号 + 同上文，不再重复正文。"""
    store = FakeStore()
    store.add(1, ts(2026, 11, 8, 10, 1), "结果", "游标改成高水位，先开始的 pass 后结束不会改回去", project="eventmem")
    store.add(2, ts(2026, 11, 8, 10, 2), "结果", "游标只在变大时更新，多进程各跑一份也安全", project="eventmem")
    store.add(3, ts(2026, 11, 8, 10, 3), "结果", "游标落后不等于卡住，一轮 pass 可能跑半小时", project="eventmem")

    state = recall_mod.RecallState()
    first = recall(store, None, "游标", budget_tokens=400, state=state)
    ids_first = {int(m) for m in re.findall(r"#(\d+)", first)}
    assert ids_first, "首轮应有内容:\n" + first

    second = recall(store, None, "游标", budget_tokens=400, state=state)
    assert "同上文" in second, "重复条目必须给出指针:\n" + second
    for eid in ids_first:
        assert ("#%d" % eid) in second, "指针里应保留编号 #%d:\n%s" % (eid, second)
    bodies = [ln for ln in body_lines(second) if ln and "同上文" not in ln]
    assert not bodies, "全部重复时不该再渲染正文:\n" + second


@test
def test_repeat_pointer_does_not_consume_item_slots():
    """指针行不占条目配额：上一轮没给过的条目仍按 max_items 正常装填。"""
    store = FakeStore(fts_override={"盘": [(1, 4.0), (2, 3.0), (3, 2.0), (4, 1.0)]})
    for i in range(1, 5):
        store.add(i, ts(2026, 11, 1, 9) + i * 60, "结果", "盘容量与温度观测 第 %d 次" % i, project="小主机")

    state = recall_mod.RecallState()
    state.skeleton_projects.add("小主机")   # 本用例只看命中段：骨架段按会话记账已给过
    first = recall(store, None, "盘", budget_tokens=600, max_items=2, state=state)
    ids_first = {int(m) for m in re.findall(r"#(\d+)", first)}
    assert len(ids_first) <= 2, first

    second = recall(store, None, "盘", budget_tokens=600, max_items=2, state=state)
    bodies = [ln for ln in body_lines(second) if ln and "同上文" not in ln]
    assert len(bodies) == 2, "重复条目占了条目配额:\n" + second
    for line in bodies:
        eid = int(re.search(r"#(\d+)", line).group(1))
        assert eid not in ids_first, "这一轮装的应该是没给过的条目:\n" + second


@test
def test_skeleton_returns_for_a_new_project_mid_session():
    """长会话中途换项目：新焦点项目仍会拿到自己的骨架。"""
    store = FakeStore(fts_override={
        "NVMe": [(11, 5.0), (12, 4.0), (13, 3.0)],
        "推优": [(21, 5.0), (22, 4.0), (23, 3.0)],
    })
    store.add(3, ts(2026, 10, 1), "决定", "小主机当初决定不扩容", project="小主机")
    store.add(11, ts(2026, 11, 6, 10), "变更", "小主机换 2T NVMe", project="小主机")
    store.add(12, ts(2026, 11, 6, 11), "结果", "小主机 NVMe 装好", project="小主机")
    store.add(13, ts(2026, 11, 6, 12), "教训", "小主机 NVMe 别买杂牌", project="小主机")
    store.add(2, ts(2026, 10, 2), "决定", "今年推优只报本科生", project="团支部")
    store.add(21, ts(2026, 11, 7, 10), "变更", "推优名单确定 3 人", project="团支部")
    store.add(22, ts(2026, 11, 7, 11), "结果", "推优材料收齐", project="团支部")
    store.add(23, ts(2026, 11, 7, 12), "教训", "推优别忘交表", project="团支部")

    state = recall_mod.RecallState()
    first = recall(store, None, "NVMe", budget_tokens=500, state=state)
    assert "#3" in first, "首轮应给「小主机」的骨架:\n" + first
    second = recall(store, None, "推优", budget_tokens=500, state=state)
    assert "#2" in second, "换到新项目后应给它自己的骨架:\n" + second
    assert "#3" not in second, "不该把上一个项目的骨架再拖出来:\n" + second


@test
def test_first_recall_is_two_segments_and_later_is_one():
    """首轮 = 骨架段 + 命中段（空行分开，骨架条目不在命中段重复）；后续只剩命中段。"""
    store = FakeStore(fts_override={"NVMe 系统盘": [(5, 5.0), (6, 4.0), (7, 3.0), (8, 2.0), (9, 1.0)]})
    store.add(1, ts(2026, 10, 1), "决定", "当初只批了 5000 块预算", project="小主机")
    store.add(5, ts(2026, 11, 6, 10), "变更", "换成 2T NVMe 系统盘", project="小主机")
    store.add(6, ts(2026, 11, 6, 11), "结果", "NVMe 装好后开机 8 秒", project="小主机")
    store.add(7, ts(2026, 11, 6, 12), "教训", "NVMe 别用杂牌", project="小主机")
    store.add(8, ts(2026, 11, 6, 13), "尝试", "NVMe 温度压测", project="小主机")
    store.add(9, ts(2026, 11, 6, 14), "结果", "NVMe 待机 41 度", project="小主机")

    state = recall_mod.RecallState()
    first = recall(store, None, "NVMe 系统盘", state=state)
    segs = segments(first)
    assert len(segs) == 2, "首轮应分「骨架段 + 命中段」:\n" + first
    assert "#1" in segs[0] and "#7" in segs[0] and "#9" in segs[0], "骨架段缺条目:\n" + first
    assert segments(first)[0].index("#1") < segs[0].index("#9") < segs[0].index("#7"), \
        "骨架段应为 决定→结果→教训:\n" + segs[0]
    assert "#1" not in segs[1] and "#7" not in segs[1] and "#9" not in segs[1], \
        "骨架条目不该在命中段重复:\n" + first
    used_body = sum(estimate_tokens(line) + 1 for line in segs[1].split("\n"))
    assert used_body <= recall_mod.DEFAULT_BUDGET, "命中段超默认预算:\n" + first

    second = recall(store, None, "NVMe 系统盘", state=state)
    assert len(segments(second)) == 1, "后续不该再有骨架段:\n" + second


@test
def test_skeleton_segment_has_its_own_budget():
    """命中段预算压到 1 也不影响骨架段：两段预算互不占用。"""
    store = _tight_store()
    out = recall(store, None, "训练数据 盘", budget_tokens=1)
    segs = segments(out)
    assert len(segs) == 1, "命中段被压空时只剩骨架段:\n" + out
    assert "#1" in segs[0] and "#5" in segs[0] and "#4" in segs[0], out


@test
def test_default_budgets_are_850_and_600():
    assert recall_mod.SKELETON_BUDGET == 850
    assert recall_mod.DEFAULT_BUDGET == 600


@test
def test_repeat_pointer_stays_within_budget():
    """指针行也要算进 token 预算，块总开销不许超。"""
    store = FakeStore()
    for i in range(1, 4):
        store.add(i, ts(2026, 11, 8, 10, i), "结果",
                  "游标的实现细节与多进程并发时的边界条件第 %d 次记录" % i, project="eventmem")
    state = recall_mod.RecallState()
    for _ in range(3):
        out = recall(store, None, "游标", budget_tokens=200, state=state)
        used = sum(estimate_tokens(line) + 1 for line in out.split("\n"))
        assert used <= 200, "估算 %d 超过 200:\n%s" % (used, out)


@test
def test_repeat_pointer_is_capped_and_never_renders_bodies():
    """重复条目超过 REPEAT_MAX 时：指针只列前 8 个，其余也不再渲染正文。"""
    store = FakeStore()
    for i in range(1, 13):
        store.add(i, ts(2026, 11, 8, 9) + i * 60, "结果", "观测记录 第 %d 条" % i, project="小主机")
    state = recall_mod.RecallState()
    state.seen_ids.update(range(1, 13))
    state.skeleton_projects.add("小主机")   # 只看命中段的指针行为

    out = recall(store, None, "观测记录", budget_tokens=850, state=state)
    pointer = [ln for ln in body_lines(out) if "同上文" in ln]
    assert len(pointer) == 1, "重复条目应合成一行指针:\n" + out
    assert len(re.findall(r"#\d+", pointer[0])) == recall_mod.REPEAT_MAX, pointer[0]
    bodies = [ln for ln in body_lines(out) if ln and "同上文" not in ln]
    assert not bodies, "超出 REPEAT_MAX 的重复条目不该再渲染正文:\n" + out


# --------------------------------------------------------------------------- runner


@test
def test_vector_floor_drops_weak_hits():
    """向量下限：没有它，库里有 vec 时任何 query 都会命中，记忆块永远不为空。"""
    store = FakeStore()
    # 事件向量与查询向量正交 → 余弦 0
    store.add(1, ts(2026, 10, 25), "决定", "完全无关的闲聊内容",
              project="杂项", vec=struct.pack("<3f", 0.0, 1.0, 0.0))

    class FixedEncoder:
        dim = 3

        def encode(self, text, is_query=False):
            return [1.0, 0.0, 0.0]

    assert recall(store, FixedEncoder(), "zzzqqq") == "", "余弦 0 应被 0.35 的下限挡掉"
    assert "#1" in recall(store, FixedEncoder(), "zzzqqq", min_score=0.0), \
        "下限放到 0 后同一条应能进来（证明是下限在起作用）"


@test
def test_max_items_cap():
    """条数上限：600 token 里不该塞进四十行。"""
    store = FakeStore()
    for i in range(1, 31):
        store.add(i, ts(2026, 10, 25) + i, "结果", "训练结果 第%d 次" % i, project="Qwen训练")
    state = recall_mod.RecallState()
    state.skeleton_projects.add("Qwen训练")   # 只看命中段的条数上限
    out = recall(store, None, "训练结果", budget_tokens=3000, max_items=5, state=state)
    lines = body_lines(out)
    assert 0 < len(lines) <= 5, "max_items 没生效：%d 行\n%s" % (len(lines), out)


@test
def test_vectors_of_another_dimension_are_ignored():
    """换 embedding 模型后，旧维度的向量不能按前缀比余弦（那排的是前段能量）。"""
    store = FakeStore(fts_override={"NVMe 系统盘": []})  # 只看向量臂
    enc64 = FakeEncoder(64)
    enc32 = FakeEncoder(32)
    good = store.add(1, ts(2026, 11, 1), "变更", "小主机换 2T NVMe 系统盘",
                     project="小主机", vec=vec_bytes(enc64, "小主机换 2T NVMe 系统盘"))
    stale = store.add(2, ts(2026, 11, 2), "变更", "小主机换 2T NVMe 系统盘 备份盘",
                      project="小主机", vec=vec_bytes(enc32, "小主机换 2T NVMe 系统盘 备份盘"))

    out = recall(store, enc64, "NVMe 系统盘", min_score=0.0)
    ids = [int(m) for m in re.findall(r"#(\d+)", out)]
    assert good in ids, out
    assert stale not in ids, "32 维的旧向量必须被跳过，而不是按前 32 维算余弦\n%s" % out


@test
def test_neighbours_renders_both_directions():
    """mem_neighbors 的渲染：入边/出边/同会话分段，默认不跟细化。"""
    store = FakeStore()
    store.add(1, ts(2026, 11, 1), "决定", "先不做 7 天窗口", project="小主机", session="s1")
    store.add(2, ts(2026, 11, 2), "变更", "候选改为活跃链头", project="小主机", session="s1")
    store.add(3, ts(2026, 11, 3), "结果", "回放只有 113 对", project="小主机", session="s1")
    store.add(4, ts(2026, 11, 4), "教训", "阈值要看了分布再定", project="小主机", session="s1")
    store.add_edge(2, 1, "改变", 0.9)
    store.add_edge(3, 2, "细化", 0.8)
    store.add_edge(4, 2, "否定", 0.7)

    block = recall_mod.neighbours(store, 2)
    assert "#2" in block and "后来的事件" in block and "#4" in block, block
    assert "更早的事件" in block and "#1" in block, block
    assert "细化" not in block, "细化默认不跟：" + block
    assert "同会话" in block and "#3" in block, block

    with_refine = recall_mod.neighbours(store, 2, rels=["细化"])
    assert "#3" in with_refine and "更早的事件" not in with_refine, with_refine

    assert recall_mod.neighbours(store, 999) == "", "不存在的事件返回空串"


@test
def test_project_hint_ranks_by_centroid():
    """项目候选：与本回合最像的项目排前面（模型只看到标题，选或新建）。"""
    class _OneHot:
        def encode(self, text, is_query=False):
            return [1.0, 0.0, 0.0] if ("盘" in text or "主机" in text) else [0.0, 1.0, 0.0]

    store = FakeStore()
    store.add(1, ts(2026, 11, 1), "决定", "系统盘换 2T", project="小主机", vec=[1.0, 0.0, 0.0])
    store.add(2, ts(2026, 11, 2), "结果", "换盘完成", project="小主机", vec=[0.9, 0.1, 0.0])
    store.add(3, ts(2026, 11, 3), "结果", "实验报告交了", project="有机实验", vec=[0.0, 1.0, 0.0])

    line = recall_mod.project_hint(store, _OneHot(), "主机 系统盘")
    assert line.startswith("[eventmem] 可用项目"), line
    assert line.index("小主机") < line.index("有机实验"), "相关项目必须排前面：" + line
    assert "proj_add" in line, "必须告诉模型怎么新建：" + line

    # 编码器不可用：仍然给名字（按活跃度），不报错、不返回空
    plain = recall_mod.project_hint(store, None, "随便什么")
    assert "小主机" in plain and "有机实验" in plain, plain
    assert "相关度" not in plain, "没排序就不要自称相关度：" + plain


@test
def test_project_hint_limit_and_empty_store():
    store = FakeStore()
    assert recall_mod.project_hint(store, None, "q") == "", "没有项目就没有候选行"
    for i in range(7):
        store.add(i + 1, ts(2026, 11, 1, 9, i), "决定", "第 %d 件事" % i, project="项目%d" % i)
    line = recall_mod.project_hint(store, None, "q")
    names = [part.strip() for part in line.split("：", 1)[1].split("；")[0].split("/")]
    assert len(names) == 5, line
    assert "共 7 个" in line, line


@test
def test_qvec_is_reused_and_empty_projects_fall_back_to_their_name():
    class _Stub:
        def __init__(self):
            self.calls = []

        def encode(self, text, is_query=False):
            self.calls.append(text)
            return [1.0, 0.0, 0.0] if "盘" in text else [0.0, 1.0, 0.0]

    recall_mod._NAME_VECS.clear()
    store = FakeStore()
    store.add(1, ts(2026, 11, 1), "决定", "系统盘换 2T", project="小主机", vec=[1.0, 0.0, 0.0])
    store.projects["空项目"] = 99          # 注册了但一条事件都没有
    qv = [0.0, 1.0, 0.0]                   # 「像空项目、不像小主机」

    enc = _Stub()
    line = recall_mod.project_hint(store, enc, "忽略这段文字", qvec=qv)
    assert enc.calls == ["[空项目]"], "只有空项目该去编码名字：" + repr(enc.calls)
    assert line.index("空项目") < line.index("小主机"), "名字向量应该把空项目排上来：" + line

    again = recall_mod.project_hint(store, enc, "忽略这段文字", qvec=qv)
    assert enc.calls == ["[空项目]"], "名字向量要缓存，别每回合重编：" + repr(enc.calls)
    assert again == line


def main() -> int:
    passed = 0
    failed = 0
    failures = []
    for fn in TESTS:
        try:
            fn()
        except Exception:  # noqa: BLE001 - 测试脚本：打印上下文并继续
            failed += 1
            failures.append(fn.__name__)
            print("FAIL %s" % fn.__name__)
            traceback.print_exc()
        else:
            passed += 1
            print("PASS %s" % fn.__name__)
    print("\nPASS %d / FAIL %d" % (passed, failed))
    if failures:
        print("failed: " + ", ".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
