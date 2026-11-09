#!/usr/bin/env python3
"""eventmem unit tests — middleware + tool surface, no store/recall/embed needed.

Run directly:  <python> tests/test_plugin.py      (prints PASS n / FAIL m, exit 1 on failure)

Everything here uses fakes: a FakeStore for the write path and a stub recall module, so
the suite runs before store.py/recall.py/embed.py exist and never touches ~/.hermes data.
"""

from __future__ import annotations

import os
import sys

os.environ["EVENTMEM_LANG"] = "zh"
import threading
import time
import traceback
import types
from pathlib import Path

_HERE = Path(__file__).resolve()
_REPO = _HERE.parents[1]                  # tests -> repo
_CORE = _REPO / "core"
_ADAPTER = _REPO / "adapters" / "hermes"
_HERMES_AGENT = Path.home() / ".hermes" / "hermes-agent"
for _p in (str(_CORE), str(_ADAPTER), str(_HERMES_AGENT), str(_HERE.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# CI 上没有 Hermes 宿主的 agent 包：用最小桩顶上（真装了就照用真的）。
import _host_stub  # noqa: E402

_host_stub.install()

import eventmem  # noqa: E402
from eventmem import (  # noqa: E402
    EventMemProvider, MEMORY_HINT_ZH, is_state_changing, memory_hint, on_tool_result, register,
)
from eventmem import extract as extract_mod  # noqa: E402
from eventmem_core import EventMem  # noqa: E402
from eventmem_core.settings import CoreSettings  # noqa: E402

CASES: list = []


def case(fn):
    CASES.append(fn)
    return fn


def check(cond, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def check_eq(got, want, msg: str) -> None:
    if got != want:
        raise AssertionError(f"{msg}: got {got!r}, want {want!r}")


# --------------------------------------------------------------------------- fakes

class FakeStore:
    """Minimal stand-in for store.Store: just the calls __init__.py makes."""

    def __init__(self) -> None:
        self.projects: dict[str, dict] = {"小主机": {"id": 1, "name": "小主机", "status": "active"}}
        self.events: dict[int, dict] = {}
        self.artifacts: dict[int, dict] = {}
        self.edges: list[dict] = []
        self.removed: list[int] = []
        self._event_id = 100
        self._art_id = 500
        self._project_id = 10

    def add_project(self, name: str) -> int:
        pid = self._project_id
        self._project_id += 1
        self.projects[name] = {"id": pid, "name": name, "status": "active"}
        return pid

    def get_project(self, name: str):
        return self.projects.get(name)

    def list_projects(self, include_archived: bool = False):
        return list(self.projects.values())

    def add_event(self, kind, text, *, ts=None, project=None, session=None,
                  src_msg=None, art_id=None, art_path=None, extra_projects=None,
                  vec=None) -> int:
        if project is not None and project not in self.projects:
            raise AssertionError(f"add_event got unknown project {project!r}")
        eid = self._event_id
        self._event_id += 1
        self.events[eid] = {
            "id": eid, "ts": ts or 0.0, "kind": kind, "text": text,
            "project": project, "project_id": (self.projects.get(project) or {}).get("id"),
            "session": session, "art_id": art_id, "art_path": art_path,
            "src_msg": src_msg, "vec": vec,
        }
        return eid

    def get_event(self, eid: int):
        return self.events.get(int(eid))

    def add_artifact(self, kind: str, text: str, src_msg=None) -> int:
        aid = self._art_id
        self._art_id += 1
        self.artifacts[aid] = {"id": aid, "kind": kind, "text": text}
        return aid

    def get_artifact(self, aid: int):
        return self.artifacts.get(int(aid))

    def add_edge(self, src, dst, rel, conf, decided_by, cross=None) -> None:
        self.edges.append({"src": src, "dst": dst, "rel": rel, "conf": conf,
                           "decided_by": decided_by, "cross": cross})

    # -- 图导航（mem_neighbors 用）
    def get_events(self, ids):
        return [self.events[i] for i in ids if i in self.events]

    def edges_into(self, eid):
        return [dict(e) for e in self.edges if e["dst"] == int(eid)]

    def edges_from(self, eid):
        return [dict(e) for e in self.edges if e["src"] == int(eid)]

    def session_window(self, session, center_id, before=2, after=2):
        rows = sorted((e for e in self.events.values() if e.get("session") == session),
                      key=lambda e: (e["ts"], e["id"]))
        idx = next((i for i, e in enumerate(rows) if e["id"] == int(center_id)), None)
        if idx is None:
            return []
        return [dict(e) for e in rows[max(0, idx - before):idx + after + 1]
                if e["id"] != int(center_id)]

    def project_events(self, pid: int):
        return [e for e in self.events.values() if e.get("project_id") == int(pid)]

    def forget(self, eid: int) -> None:
        self.events.pop(int(eid), None)
        self.removed.append(int(eid))

    def close(self) -> None:
        pass


class FakeCtx:
    def __init__(self) -> None:
        self.provider = None
        self.middleware: list[tuple[str, object]] = []
        self.aux_tasks: list[tuple[str, dict]] = []

    def register_memory_provider(self, provider) -> None:
        self.provider = provider

    def register_middleware(self, kind, callback) -> None:
        self.middleware.append((kind, callback))

    def register_auxiliary_task(self, key, **kwargs) -> None:
        self.aux_tasks.append((key, kwargs))


def make_provider(store=None, session="sess-1", settings=None) -> EventMemProvider:
    """Provider wired to a FakeStore without initialize() (no files, no encoder)."""
    store = store if store is not None else FakeStore()
    st = settings or CoreSettings(db_path=":memory:")
    core = EventMem(st, store=store, encoder=None)
    core._encoder_dead = True              # never import a real encoder in tests
    provider = EventMemProvider()
    provider._core = core
    provider._settings = st
    provider._session_id = session
    provider._primary = True
    return provider


class aux_patch:
    """Replace ``eventmem.extract._aux_llm`` for one block; yields the captured calls."""

    def __init__(self, content: str = "", error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.calls: list[list[dict]] = []

    def __enter__(self):
        self._saved = extract_mod._aux_llm

        def fake(messages, **kwargs):
            self.calls.append(messages)
            if self.error is not None:
                raise self.error
            return types.SimpleNamespace(choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content=self.content))])

        extract_mod._aux_llm = fake
        return self.calls

    def __exit__(self, *exc_info):
        extract_mod._aux_llm = self._saved
        return False


def run_middleware(tool_name, args, result):
    """Invoke on_tool_result exactly the way hermes_cli.middleware does (all kwargs)."""
    calls = []

    def next_call(next_args=None):
        calls.append(next_args)
        return result

    out = on_tool_result(
        tool_name=tool_name, args=args, next_call=next_call, original_args=args,
        session_id="sess-1", task_id="task-1", tool_call_id="call-1",
        telemetry_schema_version="hermes.observer.v1",
        middleware_schema_version="hermes.middleware.v1",
    )
    return out, calls


def last_tool_json(provider, tool_name, args) -> dict:
    import json

    raw = provider.handle_tool_call(tool_name, args)
    check(isinstance(raw, str), "handle_tool_call must return a JSON string")
    return json.loads(raw)


# --------------------------------------------------------------------------- middleware

@case
def test_hint_text_matches_contract():
    check_eq(MEMORY_HINT_ZH,
             "\n[memory] 该动作改变了系统状态；本回合结束前请调用 evt_add 补一条 event",
             "hint text")


@case
def test_write_file_is_state_changing():
    check(is_state_changing("write_file", {"path": "/tmp/a", "content": "x"}) is True,
          "write_file must be state-changing")
    check(is_state_changing("patch", {"path": "/tmp/a", "old_string": "x", "new_string": "y"}) is True,
          "patch must be state-changing")


@case
def test_ls_is_not_state_changing():
    check(is_state_changing("terminal", {"command": "ls -la /tmp"}) is False, "ls must be read-only")
    check(is_state_changing("terminal", {"command": "cat a.txt | head -20"}) is False, "cat|head read-only")
    check(is_state_changing("terminal", {"command": "grep -rn foo ."}) is False, "grep read-only")
    check(is_state_changing("terminal", {"command": "ps aux | grep python"}) is False, "ps read-only")
    check(is_state_changing("terminal", {"command": "ls > /dev/null 2>&1"}) is False,
          "redirect to /dev/null is not a write")


@case
def test_rm_is_state_changing():
    check(is_state_changing("terminal", {"command": "rm -rf /tmp/junk"}) is True, "rm must trigger")
    check(is_state_changing("terminal", {"command": "mkdir -p /tmp/x"}) is True, "mkdir must trigger")
    check(is_state_changing("terminal", {"command": "mv a b"}) is True, "mv must trigger")
    check(is_state_changing("terminal", {"command": "cp a b"}) is True, "cp must trigger")
    check(is_state_changing("terminal", {"command": "sed -i 's/a/b/' f.txt"}) is True,
          "sed -i must trigger")
    check(is_state_changing("terminal", {"command": "echo hi > note.txt"}) is True,
          "a real redirect must trigger")


@case
def test_git_status_is_not_state_changing():
    check(is_state_changing("terminal", {"command": "git status"}) is False, "git status read-only")
    check(is_state_changing("terminal", {"command": "git log --oneline -5"}) is False, "git log read-only")
    check(is_state_changing("terminal", {"command": "git diff HEAD"}) is False, "git diff read-only")
    check(is_state_changing("terminal", {"command": "git commit -m 'x'"}) is True, "git commit must trigger")
    check(is_state_changing("terminal", {"command": "git push origin main"}) is True, "git push must trigger")
    check(is_state_changing("terminal", {"command": "git -C /some/repo status"}) is False,
          "git -C <dir> status is read-only")
    check(is_state_changing("terminal", {"command": "git -C /some/repo commit -m x"}) is True,
          "git -C <dir> commit must trigger")


@case
def test_python_snippets():
    check(is_state_changing("terminal", {"command": "python -c 'print(1)'"}) is False,
          "printing snippet is read-only")
    check(is_state_changing("terminal", {"command": "python -c 'import json;print(json.dumps({}))'"}) is False,
          "json.dumps is not a file write")
    check(is_state_changing("terminal", {"command": "python -c 'import pickle;pickle.dump(x, f)'"}) is True,
          "pickle.dump writes")
    check(is_state_changing("terminal", {"command": "python train.py"}) is True,
          "running a script is an action")


@case
def test_hermes_commands():
    check(is_state_changing("terminal", {"command": "hermes config set memory.provider eventmem"}) is True,
          "hermes config set writes config")
    check(is_state_changing("terminal", {"command": "hermes plugins enable eventmem"}) is True,
          "hermes plugins enable writes config")
    check(is_state_changing("terminal", {"command": "hermes cron add --prompt x"}) is True,
          "hermes cron add writes state")
    check(is_state_changing("terminal", {"command": "hermes status"}) is False,
          "hermes status read-only")
    check(is_state_changing("terminal", {"command": "hermes config get memory.provider"}) is False,
          "hermes config get read-only")
    check(is_state_changing("terminal", {"command": "hermes plugins list"}) is False,
          "hermes plugins list read-only")


@case
def test_heredoc_scripts():
    writing = "python3 - <<'PY'\nfrom pathlib import Path\nPath('/tmp/x').write_text('y')\nPY"
    check(is_state_changing("terminal", {"command": writing}) is True,
          "a here-doc script that writes must trigger")
    quiet = "python3 - <<'PY'\nprint(len([1, 2, 3]))\nPY"
    check(is_state_changing("terminal", {"command": quiet}) is False,
          "a here-doc script that only prints must not trigger")


@case
def test_gh_commands():
    check(is_state_changing("terminal", {"command": "gh pr merge 12 --squash"}) is True, "gh pr merge")
    check(is_state_changing("terminal", {"command": "gh issue create --title x"}) is True, "gh issue create")
    check(is_state_changing("terminal", {"command": "gh pr list"}) is False, "gh pr list read-only")
    check(is_state_changing("terminal", {"command": "gh run list"}) is False, "gh run list read-only")


@case
def test_package_and_service_commands():
    check(is_state_changing("terminal", {"command": "sudo apt-get install -y jq"}) is True,
          "apt-get install must trigger")
    check(is_state_changing("terminal", {"command": "apt list --installed | head"}) is False,
          "apt list read-only")
    check(is_state_changing("terminal", {"command": "pip install requests"}) is True, "pip install must trigger")
    check(is_state_changing("terminal", {"command": "pip list"}) is False, "pip list read-only")
    check(is_state_changing("terminal", {"command": "systemctl restart nginx"}) is True,
          "systemctl restart must trigger")
    check(is_state_changing("terminal", {"command": "systemctl status nginx"}) is False,
          "systemctl status read-only")
    check(is_state_changing("terminal", {"command": "docker ps -a"}) is False, "docker ps read-only")
    check(is_state_changing("terminal", {"command": "docker compose up -d"}) is True,
          "docker compose up must trigger")


@case
def test_compound_and_wrapped_commands():
    check(is_state_changing("terminal", {"command": "cd /srv && git status && ls"}) is False,
          "compound read-only must stay read-only")
    check(is_state_changing("terminal", {"command": "cd /srv && rm -rf build"}) is True,
          "one mutating segment makes the line mutating")
    check(is_state_changing("terminal", {"command": "sudo -u root rm -f /etc/x"}) is True,
          "sudo wrapper must be unwrapped")
    check(is_state_changing("terminal", {"command": "FOO=1 touch /tmp/z"}) is True,
          "env assignment prefix must be skipped")
    check(is_state_changing("terminal", {"command": "echo 'a > b' && pwd"}) is False,
          "quoted redirect is not a write")
    check(is_state_changing("terminal", {"command": "python -c 'print(1)'"}) is False,
          "a pure computation is not a write")
    check(is_state_changing("terminal", {"command": "python train_sft.py --bs 2"}) is True,
          "running a script is an action")


@case
def test_on_tool_result_appends_hint_for_change():
    out, calls = run_middleware("write_file", {"path": "/tmp/a", "content": "x"}, "written: ok")
    check_eq(len(calls), 1, "next_call must be called exactly once")
    check_eq(out, "written: ok" + memory_hint(), "hint appended to a mutating result")


@case
def test_on_tool_result_leaves_readonly_result_untouched():
    out, calls = run_middleware("terminal", {"command": "ls -la"}, "file a\nfile b\n")
    check_eq(len(calls), 1, "next_call must be called exactly once")
    check_eq(out, "file a\nfile b\n", "read-only result must be byte-identical")
    check(memory_hint().strip() not in out, "no hint for read-only tools")


@case
def test_on_tool_result_failed_command_still_hints():
    out, _ = run_middleware("terminal", {"command": "rm -rf /root/x"}, "exit_code: 1\npermission denied")
    check(out.endswith(memory_hint()), "a failed mutating command still counts (design §4.1)")


@case
def test_on_tool_result_structured_result_untouched():
    payload = {"output": "ok", "exit_code": 0}
    out, _ = run_middleware("write_file", {"path": "/tmp/a"}, payload)
    check_eq(out, payload, "non-str results must not be coerced")


@case
def test_on_tool_result_does_not_double_the_hint():
    out, _ = run_middleware("write_file", {"path": "/tmp/a"}, "ok" + memory_hint())
    check_eq(out.count("[memory]"), 1, "hint must not be appended twice")


@case
def test_on_tool_result_propagates_downstream_error():
    def boom(next_args=None):
        raise RuntimeError("downstream failed")

    try:
        on_tool_result(tool_name="write_file", args={}, next_call=boom)
    except RuntimeError as exc:
        check_eq(str(exc), "downstream failed", "error text")
    else:
        raise AssertionError("next_call errors must propagate")


# --------------------------------------------------------------------------- tools

@case
def test_evt_add_rejects_unknown_project():
    provider = make_provider()
    payload = last_tool_json(provider, "evt_add", {"events": [
        {"kind": "变更", "text": "换 2T 系统盘", "project": "不存在的项目"},
    ]})
    check("error" in payload, "unknown project must be an error")
    check("不存在的项目" in payload["error"], "error must name the bad project")
    check_eq(provider._core.store.events, {}, "nothing may be written for a rejected call")
    check("小主机" in payload.get("known_projects", []), "error should list valid projects")


@case
def test_evt_add_writes_known_project_and_links():
    store = FakeStore()
    provider = make_provider(store)
    payload = last_tool_json(provider, "evt_add", {"events": [
        {"kind": "变更", "text": "小主机换 2T NVMe 作系统盘", "project": "小主机"},
        {"kind": "结果", "text": "容量从 256G 变成 2T", "project": "小主机",
         "links": [{"rel": "因为", "index": 1}]},
    ]})
    check_eq(payload.get("count"), 2, "two events written")
    check_eq(len(store.events), 2, "store received both events")
    check_eq(store.edges, [{"src": 101, "dst": 100, "rel": "因为", "conf": 1.0,
                            "decided_by": "llm_batch", "cross": None}], "batch edge")
    check_eq(provider._session_project, "小主机", "project becomes the session default")


@case
def test_evt_add_inherits_session_project_and_accepts_unfiled():
    store = FakeStore()
    provider = make_provider(store)
    provider._session_project = "小主机"
    inherited = last_tool_json(provider, "evt_add", {"events": [
        {"kind": "尝试", "text": "先跑基准"},
    ]})
    check_eq(inherited["events"][0]["project"], "小主机", "inherited project")
    unfiled = last_tool_json(provider, "evt_add", {"events": [
        {"kind": "教训", "text": "别在 /tmp 里训练", "project": "unfiled"},
    ]})
    check_eq(unfiled["events"][0]["project"], "unfiled", "unfiled is accepted")
    check_eq(store.events[101]["project"], None, "unfiled stores NULL project")


@case
def test_evt_add_rejects_bad_shape():
    provider = make_provider()
    too_many = last_tool_json(provider, "evt_add", {"events": [
        {"kind": "尝试", "text": f"e{i}"} for i in range(4)
    ]})
    check("error" in too_many, "more than 3 events must be rejected")
    bad_kind = last_tool_json(provider, "evt_add", {"events": [{"kind": "随便", "text": "x"}]})
    check("error" in bad_kind, "unknown kind must be rejected")
    empty = last_tool_json(provider, "evt_add", {"events": []})
    check_eq(empty.get("count"), 0, "zero events is a legal call")


@case
def test_evt_add_rejects_dangling_and_backwards_links():
    store = FakeStore()
    provider = make_provider(store)
    first = last_tool_json(provider, "evt_add", {"events": [
        {"kind": "决定", "text": "先跑基准", "project": "小主机"}]})
    older = first["events"][0]["id"]

    # 指向不存在的事件：不能写悬挂边，且必须报错（以前会静默 ok）
    dangling = last_tool_json(provider, "evt_add", {"events": [
        {"kind": "结果", "text": "x", "project": "小主机",
         "links": [{"rel": "细化", "event_id": 999999}]}]})
    check_eq(dangling.get("edge_errors"), [f"#{older + 1}: event_id=999999 不存在"], repr(dangling))
    check_eq(store.edges, [], "dangling edge must not be written")

    # 指向更晚的事件：方向反了，拒绝（否则更新的一条会被标成已失效）
    store.events[500] = {"id": 500, "ts": time.time() + 3600, "kind": "结果",
                         "text": "来自未来的事件", "project": "小主机", "project_id": 1,
                         "session": None, "art_id": None, "src_msg": None, "vec": None}
    backwards = last_tool_json(provider, "evt_add", {"events": [
        {"kind": "结果", "text": "y", "project": "小主机",
         "links": [{"rel": "改变", "event_id": 500}]}]})
    check_eq(len(backwards.get("edge_errors") or []), 1, repr(backwards))
    check_eq(store.edges, [], "backwards edge must not be written")

    # 指向更早的已有事件：允许
    ok = last_tool_json(provider, "evt_add", {"events": [
        {"kind": "结果", "text": "z", "project": "小主机",
         "links": [{"rel": "因为", "event_id": older}]}]})
    check("edge_errors" not in ok, repr(ok))
    check_eq([(e["src"], e["dst"], e["rel"]) for e in store.edges],
             [(ok["events"][0]["id"], older, "因为")], repr(store.edges))


@case
def test_evt_add_rejects_archived_project():
    store = FakeStore()
    provider = make_provider(store)
    store.projects["旧项目"] = {"id": 2, "name": "旧项目", "status": "archived"}

    payload = last_tool_json(provider, "evt_add", {"events": [
        {"kind": "结果", "text": "x", "project": "旧项目"}]})
    check("error" in payload, repr(payload))
    check("归档" in payload["error"], payload["error"])
    check_eq(store.events, {}, "nothing may be written to an archived project")

    extra = last_tool_json(provider, "evt_add", {"events": [
        {"kind": "结果", "text": "y", "project": "小主机", "extra_projects": ["旧项目"]}]})
    check("error" in extra, repr(extra))
    check_eq(store.events, {}, "nothing may be written when a secondary project is archived")


@case
def test_expand_returns_artifact_text():
    store = FakeStore()
    provider = make_provider(store)
    payload = last_tool_json(provider, "evt_add", {"events": [{
        "kind": "尝试", "text": "启动第三阶段精修", "project": "小主机",
        "artifact": "python train_sft.py --bs 2 --lr 2e-5", "artifact_kind": "命令",
    }]})
    event_id = payload["events"][0]["id"]
    expanded = last_tool_json(provider, "expand", {"event_id": event_id})
    check_eq(expanded["artifact"]["text"], "python train_sft.py --bs 2 --lr 2e-5",
             "artifact full text")
    check_eq(expanded["kind"], "尝试", "event kind")
    missing = last_tool_json(provider, "expand", {"event_id": 999999})
    check("error" in missing, "unknown event id must error")


@case
def test_project_tools():
    store = FakeStore()
    provider = make_provider(store)
    listed = last_tool_json(provider, "proj_list", {})
    check_eq([p["name"] for p in listed["projects"]], ["小主机"], "project list")
    added = last_tool_json(provider, "proj_add", {"name": "Qwen 训练"})
    check_eq(added["existed"], False, "new project")
    again = last_tool_json(provider, "proj_add", {"name": "Qwen 训练"})
    check_eq(again["existed"], True, "idempotent project add")
    reserved = last_tool_json(provider, "proj_add", {"name": "unfiled"})
    check("error" in reserved, "unfiled is reserved")


@case
def test_evt_forget_removes_event():
    store = FakeStore()
    provider = make_provider(store)
    payload = last_tool_json(provider, "evt_add", {"events": [{"kind": "教训", "text": "x"}]})
    event_id = payload["events"][0]["id"]
    gone = last_tool_json(provider, "evt_forget", {"event_id": event_id})
    check_eq(gone.get("forgotten"), event_id, "forget reports the id")
    check("text" not in gone, "forget must not echo the forgotten text back")
    check_eq(store.removed, [event_id], "store.forget called")
    missing = last_tool_json(provider, "evt_forget", {"event_id": event_id})
    check("error" in missing, "forgetting a missing event must error")


@case
def test_prefetch_returns_none_when_recall_is_empty():
    provider = make_provider()
    provider._core.recall = lambda *a, **k: ""
    provider._core.project_hint = lambda *a, **k: ""
    check_eq(provider.prefetch("2T 系统盘"), None, "empty recall -> None")
    check_eq(provider.recall_status(), None, "no status for an empty recall")
    provider._core.recall = lambda *a, **k: "[2026-11-06] [小主机] 变更 换 2T  #12"
    block = provider.prefetch("2T 系统盘")
    check(block and "#12" in block, "non-empty recall is returned verbatim")
    check_eq(provider.recall_status().count, 1, "recall status counts rendered lines")


@case
def test_prefetch_appends_project_candidates():
    """模型写事件前得知道有哪些项目可选；召回为空时也要单独给这一行。"""
    store = FakeStore()
    provider = make_provider(store=store)
    block = provider.prefetch("继续 eventmem 的改进")
    check(block is not None, "有项目时即使召回为空也要给候选行")
    check("可用项目" in block and "小主机" in block, block)
    check_eq(provider.recall_status().count, 0, "只有候选行时不算命中条数")

    store.projects.clear()                      # 一个项目都没有 → 没有候选行
    check_eq(provider.prefetch("继续"), None, "没有项目就没有可注入的内容")


@case
def test_prefetch_encodes_the_query_once():
    """一次编码两处共用：recall 的向量臂 + 项目候选。各编一次就是 60 ms。"""
    store = FakeStore()
    provider = make_provider(store=store)
    calls = []

    class _Enc:
        def encode(self, text, is_query=False):
            calls.append(text)
            return [1.0, 0.0, 0.0]

    provider._core._encoder = _Enc()
    provider._core._encoder_dead = False
    provider.prefetch("查一下 eventmem 的召回")
    check_eq(calls.count("查一下 eventmem 的召回"), 1, "query 只该编码一次：" + repr(calls))
    check_eq(calls[2:], [], "空项目的名字向量只编一次、之后走缓存：" + repr(calls))


@case
def test_prefetch_puts_the_hint_after_the_block():
    provider = make_provider()
    provider._core.recall = lambda *a, **k: "[2026-11-06] [小主机] #12 变更 x"
    provider._core.project_hint = lambda *a, **k: "[eventmem] 可用项目：小主机"
    block = provider.prefetch("q")
    check_eq(block, "[2026-11-06] [小主机] #12 变更 x\n\n[eventmem] 可用项目：小主机",
             "候选行接在召回块后面")
    check_eq(provider.recall_status().count, 1, "命中条数只数正文行")


@case
def test_recall_state_is_reused_and_reset_at_session_boundaries():
    """跨轮状态：同会话复用；新会话 / 压缩 / 撤销 都要丢弃重来。"""
    provider = make_provider()
    made: list[object] = []

    class _State:
        def __init__(self) -> None:
            made.append(self)

    def fake_recall(text, *, state=None, qvec=None, **kw):
        return "[小主机] %s\n[2026-11-08] #1 结果 x" % ("s" if state is not None else "-")

    provider._core.new_recall_state = lambda: _State()
    provider._core.recall = fake_recall
    provider._core.project_hint = lambda *a, **k: ""
    provider.prefetch("q1")
    provider.prefetch("q2")
    check_eq(len(made), 1, "同一会话应复用同一个 RecallState")

    provider.on_session_switch(provider._session_id, reset=False)   # 同会话刷新
    check(provider._recall_state is not None, "同会话刷新不该清状态")

    provider.on_session_switch("sess-1", reset=False, reason="compression")
    check(provider._recall_state is None, "就地压缩（id 不变）必须清状态")

    provider._recall_state = _State()
    provider.on_session_switch(provider._session_id, reset=False, rewound=True)
    check(provider._recall_state is None, "/undo 必须清状态")

    provider._recall_state = _State()
    provider.on_session_switch("sess-2")
    check(provider._recall_state is None, "新会话必须清状态")

    provider._recall_state = _State()
    provider.on_session_switch("sess-2", reset=True)
    check(provider._recall_state is None, "/new 必须清状态")


@case
def test_prefetch_works_without_recall_state_support():
    """core 不提供 RecallState 时（老形态 / 假对象）退化为无状态，不报错。"""
    provider = make_provider()
    provider._core.new_recall_state = lambda: None
    provider._core.recall = lambda *a, **k: "[eventmem] x\n#1 结果 y"
    provider._core.project_hint = lambda *a, **k: ""
    block = provider.prefetch("q")
    check(block and "#1" in block, "无状态也应返回块")
    check(provider._recall_state is None, "不该凭空造状态")


@case
def test_recall_without_recall_module_degrades():
    # 没有项目时 prefetch 才彻底没有内容可给；有项目时「候选行」本身就是要注入的东西
    # （见 test_prefetch_appends_project_candidates），所以这里先把项目清空。
    provider = make_provider()
    provider._core.store.projects.clear()
    provider._core.project_hint = lambda *a, **k: ""
    provider._core.recall = lambda *a, **k: ""
    check_eq(provider.prefetch("anything"), None, "nothing to recall and no projects -> None")

    def boom(*a, **k):
        raise RuntimeError("recall exploded")

    provider._core.recall = boom
    check_eq(provider.prefetch("anything"), None, "recall errors are swallowed")


@case
def test_sync_turn_extracts_events_with_the_aux_model():
    provider = make_provider()
    payload = ('{"events":[{"kind":"结果","text":"召回用向量+FTS 的 RRF 融合"},'
               '{"kind":"教训","text":"纯向量会漏掉型号和报错码"}]}')
    with aux_patch(payload) as calls:
        provider.on_turn_start(1, "eventmem 的召回是怎么做的？")
        provider.sync_turn("eventmem 的召回是怎么做的？", "向量 + FTS5 → RRF，先装填后渲染。",
                           session_id="sess-1")
    events = list(provider._core.store.events.values())
    check_eq(len(events), 2, "two extracted events")
    check_eq([e["kind"] for e in events], ["结果", "教训"], "kinds kept")
    check_eq(events[0]["text"], "召回用向量+FTS 的 RRF 融合", "text kept")
    check(calls and "向量 + FTS5" in calls[0][1]["content"], "the reply reached the extractor")
    check(calls[0][0]["content"].startswith("你是事件记忆抽取器"), "system prompt used")


@case
def test_sync_turn_writes_nothing_when_extractor_returns_empty():
    provider = make_provider()
    with aux_patch('{"events":[]}'):
        provider.on_turn_start(1, "为什么不用纯向量？")
        provider.sync_turn("为什么不用纯向量？", "因为字面命中。", session_id="sess-1")
    check_eq(provider._core.store.events, {}, "nothing recorded")


@case
def test_sync_turn_falls_back_when_extraction_fails():
    provider = make_provider()
    with aux_patch(error=RuntimeError("no aux route")):
        provider.on_turn_start(1, "eventmem 的召回是怎么做的？")
        provider.sync_turn("eventmem 的召回是怎么做的？", "向量 + FTS5 → RRF。第二句不该进。",
                           session_id="sess-1")
    events = list(provider._core.store.events.values())
    check_eq(len(events), 1, "one fallback event")
    check(events[0]["text"].startswith("用户问「eventmem 的召回是怎么做的？」"), events[0]["text"])


@case
def test_empty_evt_add_still_gets_auto_recorded():
    provider = make_provider()
    with aux_patch('{"events":[{"kind":"结果","text":"召回用 RRF"}]}'):
        provider.on_turn_start(1, "eventmem 的召回是怎么做的？")
        provider.handle_tool_call("evt_add", {"events": []})
        provider.sync_turn("eventmem 的召回是怎么做的？", "向量 + FTS5 → RRF。", session_id="sess-1")
    check_eq(len(provider._core.store.events), 1, "0 events on a substantive turn is auto-recorded")


@case
def test_sync_turn_skips_when_events_written():
    provider = make_provider()
    provider.on_turn_start(1, "问题")
    provider.handle_tool_call("evt_add", {"events": [{"kind": "结果", "text": "记一笔"}]})
    provider.sync_turn("问题", "答案", session_id="sess-1")
    check_eq(len(provider._core.store.events), 1, "no duplicate auto event")


@case
def test_sync_turn_skips_trivial_prompts():
    provider = make_provider()
    provider.on_turn_start(1, "ok")
    provider.sync_turn("ok", "好的", session_id="sess-1")
    check_eq(provider._core.store.events, {}, "a bare acknowledgement is not recorded")


@case
def test_register_wires_provider_and_middleware():
    ctx = FakeCtx()
    register(ctx)
    check(isinstance(ctx.provider, EventMemProvider), "provider registered")
    check_eq(ctx.provider.name, "eventmem", "provider name")
    check_eq(len(ctx.middleware), 1, "one middleware registered")
    kind, callback = ctx.middleware[0]
    check_eq(kind, "tool_execution", "middleware kind must be tool_execution")
    check(callback is on_tool_result, "the middleware is on_tool_result")
    check_eq([key for key, _kw in ctx.aux_tasks], ["eventmem_extract"],
             "the post-turn extraction task is registered")


@case
def test_tool_schemas_are_well_formed():
    provider = make_provider()
    schemas = provider.get_tool_schemas()
    names = [s["name"] for s in schemas]
    check_eq(names, ["evt_add", "expand", "mem_neighbors", "mem_recall", "proj_add",
                     "proj_list", "evt_forget"], "tool names")
    for schema in schemas:
        check(isinstance(schema.get("description"), str) and schema["description"],
              f"{schema['name']} needs a description")
        check_eq(schema.get("parameters", {}).get("type"), "object", f"{schema['name']} params")
    evt = schemas[0]["parameters"]["properties"]["events"]
    check_eq(evt["maxItems"], 3, "evt_add caps a batch at 3")
    check_eq(sorted(evt["items"]["properties"]["kind"]["enum"]),
             sorted(["决定", "尝试", "结果", "变更", "教训"]), "kinds")
    check("path" in evt["items"]["properties"], "evt_add takes an optional art_path")
    neigh = next(s for s in schemas if s["name"] == "mem_neighbors")
    check_eq(neigh["parameters"]["required"], ["event_id"], "mem_neighbors needs an id")
    check_eq(neigh["parameters"]["properties"]["depth"]["maximum"], 3, "depth is bounded")


@case
def test_mem_neighbors_tool() -> None:
    """#62 不完整时，模型能按 id 把邻居拉出来（而不是靠重新措辞去撞召回）。"""
    store = FakeStore()
    provider = make_provider(store=store, session="s")
    old = store.add_event("决定", "没有 7 天窗口", project="小主机", ts=1000.0)
    mid = store.add_event("变更", "改为链头候选", project="小主机", ts=2000.0)
    new = store.add_event("结果", "回放 113 对", project="小主机", ts=3000.0)
    store.add_edge(new, mid, "否定", 0.85, "jev")     # 入边：谁否定了它
    store.add_edge(mid, old, "改变", 0.9, "jev")      # 出边：它取代了谁
    store.add_edge(new, old, "细化", 0.7, "jev")      # 默认不跟的关系

    data = last_tool_json(provider, "mem_neighbors", {"event_id": mid})
    check(data["found"] is True, data)
    ctx = data["context"]
    check("后来的事件" in ctx, ctx)
    check("#%d" % new in ctx, "入边（谁否定了它）必须出现：" + ctx)
    check("更早的事件" in ctx and "#%d" % old in ctx, "出边（它改了谁）必须出现：" + ctx)
    check("细化" not in ctx, "细化默认不跟，否则一跳就一屏：" + ctx)

    only_change = last_tool_json(provider, "mem_neighbors",
                                 {"event_id": mid, "rels": ["改变"]})
    check("#%d" % new not in only_change["context"], "rels 过滤要生效：" + only_change["context"])

    deep = last_tool_json(provider, "mem_neighbors", {"event_id": old, "depth": 2})
    check("#%d" % new in deep["context"], "depth=2 要能走到两跳外：" + deep["context"])

    missing = last_tool_json(provider, "mem_neighbors", {"event_id": 99999})
    check("error" in missing, missing)


@case
def test_no_inline_link_worker() -> None:
    """连边归独立进程（eventmem-worker.service）；宿主 initialize() 不许再起线程。

    背景：Hermes 每个会话宿主都是一个完整后端，插件按进程加载，把轮询放在宿主里
    会变成 dashboard/gateway/会话后端各一份，抢同一个单槽模型。
    """
    import shutil
    import tempfile

    home = tempfile.mkdtemp(prefix="eventmem-home-")
    try:
        provider = EventMemProvider()
        provider.initialize("s-inline", hermes_home=home)
        names = [t.name for t in threading.enumerate()]
        check("eventmem-link" not in names, "initialize() 不能起 eventmem-link 线程")
        provider.shutdown()
    finally:
        shutil.rmtree(home, ignore_errors=True)


def main() -> int:
    passed = failed = 0
    for fn in CASES:
        try:
            fn()
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            traceback.print_exc()
        else:
            passed += 1
            print(f"PASS {fn.__name__}")
    print(f"PASS {passed} / FAIL {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
