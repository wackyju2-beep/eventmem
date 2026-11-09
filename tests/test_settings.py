#!/usr/bin/env python3
"""eventmem core settings tests — defaults, overlays, and the core/adapter border.

Run directly:  <python> tests/test_settings.py

Two things are being defended here:

1. **Parity.**  An adapter that configures nothing must get exactly the values the
   project shipped with before the split.  If someone "tidies" a default, this fails.
2. **The border.**  ``core/eventmem_core`` may not import a host (``agent.*``,
   ``hermes_cli.*``).  The split is only real if it is enforced by a test.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CORE = REPO / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

from eventmem_core import EventMem, CoreSettings, KINDS, RELS, apply, describe  # noqa: E402
from eventmem_core import nightly as N  # noqa: E402
from eventmem_core.settings import DecisionPolicy, EdgePolicy, RecallPolicy  # noqa: E402

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


# --------------------------------------------------------------------------- parity

@case
def test_defaults_are_the_pre_split_values() -> None:
    """The migration contract: no config = the old behaviour, to the last decimal."""
    e, r, d = EdgePolicy(), RecallPolicy(), DecisionPolicy()
    check_eq((e.margin, e.heads_cos_min, e.heads_vec_top_k), (0.15, 0.35, 10), "edges q1")
    check_eq((e.session_heads_top_k, e.cross_cos_min, e.cross_top_k), (10, 0.7, 3), "edges tiers")
    check_eq((e.session_recent, e.unclosed_lessons, e.decision_skeleton), (3, 2, 5), "edges q2")
    check_eq((e.max_fail_streak, e.poll_seconds, e.first_run_hours), (3, 60.0, 24.0), "worker")
    check_eq(e.cross_kinds, ("变更", "决定"), "cross kinds")
    check_eq((r.budget_tokens, r.skeleton_budget, r.skeleton_max), (600, 850, 5), "recall budgets")
    check_eq((r.top_k, r.max_items, r.min_score), (20, 12, 0.35), "recall fusion")
    check_eq((r.rrf_k, r.chain_max_depth, r.repeat_max), (60, 1, 8), "recall knobs")
    check_eq((r.neighbour_cap, r.neighbour_depth_max, r.project_hint_limit), (30, 3, 5), "nav")
    check_eq(r.neighbour_rels, ("改变", "否定", "因为"), "default neighbour rels")
    check_eq((d.url, d.path, d.timeout), ("http://127.0.0.1:18090", "/v1/systemone", 900.0),
             "decision service")
    check_eq(CoreSettings().max_events_per_call, 3, "batch cap")
    check_eq(KINDS, ("决定", "尝试", "结果", "变更", "教训"), "kinds are the schema")
    check_eq(RELS, ("改变", "否定", "重复", "因为", "细化"), "rels are the schema")


@case
def test_module_constants_mirror_the_policy_defaults() -> None:
    """Old importers (tests, adapters, docs) still read ``nightly.MARGIN``."""
    check_eq(N.MARGIN, EdgePolicy().margin, "nightly.MARGIN")
    check_eq(N.HEADS_COS_MIN, EdgePolicy().heads_cos_min, "nightly.HEADS_COS_MIN")
    check_eq(N.MAX_FAIL_STREAK, EdgePolicy().max_fail_streak, "nightly.MAX_FAIL_STREAK")


@case
def test_settings_are_frozen() -> None:
    settings = CoreSettings()
    try:
        settings.db_path = "/tmp/x"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("CoreSettings must be frozen — adapters replace, never mutate")


# --------------------------------------------------------------------------- overlay

@case
def test_nested_and_dotted_spellings_are_equivalent() -> None:
    nested = apply(CoreSettings(), {"edges": {"margin": 0.3}, "recall": {"budget_tokens": 900}})
    dotted = apply(CoreSettings(), {"edges.margin": 0.3, "recall.budget_tokens": 900})
    check_eq(nested, dotted, "same result either way")
    check_eq(nested.edges.margin, 0.3, "margin applied")
    check_eq(nested.recall.budget_tokens, 900, "budget applied")
    check_eq(nested.edges.heads_cos_min, 0.35, "untouched fields keep their value")


@case
def test_top_level_and_decision_keys() -> None:
    s = apply(CoreSettings(), {"language": "en", "max_events_per_call": "5",
                               "decision": {"url": "http://127.0.0.1:1", "timeout": "30"}})
    check_eq(s.language, "en", "language")
    check_eq(s.max_events_per_call, 5, "string int coerced")
    check_eq(s.decision.url, "http://127.0.0.1:1", "decision url")
    check_eq(s.decision.timeout, 30.0, "timeout coerced to float")


@case
def test_tuple_fields_accept_strings_and_lists() -> None:
    a = apply(CoreSettings(), {"edges": {"cross_kinds": "变更,决定"}})
    b = apply(CoreSettings(), {"recall": {"neighbour_rels": ["改变", "因为"]}})
    check_eq(a.edges.cross_kinds, ("变更", "决定"), "comma separated")
    check_eq(b.recall.neighbour_rels, ("改变", "因为"), "list")


@case
def test_bad_values_fall_back_and_are_reported() -> None:
    warnings: list[str] = []
    s = apply(CoreSettings(), {"edges": {"margin": "abc", "heads_vec_top_k": 0},
                               "recall": {"neighbour_rels": ["改变", "nope"]}},
              warn=warnings.append)
    check_eq(s.edges.margin, 0.15, "unparseable margin keeps the default")
    check_eq(s.edges.heads_vec_top_k, 10, "out-of-range top_k keeps the default")
    check_eq(s.recall.neighbour_rels, ("改变", "否定", "因为"), "invalid rel keeps the default")
    check(len(warnings) >= 3, "every bad value is reported: %r" % warnings)


@case
def test_out_of_range_margin_is_refused() -> None:
    warnings: list[str] = []
    s = apply(CoreSettings(), {"edges": {"margin": 1.7}}, warn=warnings.append)
    check_eq(s.edges.margin, 0.15, "margin must stay in 0..1")
    check(any("margin" in w for w in warnings), warnings)


@case
def test_unknown_keys_are_ignored_with_a_warning() -> None:
    warnings: list[str] = []
    s = apply(CoreSettings(), {"edges": {"margins": 0.2}, "nonsense": 1},
              warn=warnings.append)
    check_eq(s, CoreSettings(), "nothing applied")
    check_eq(len(warnings), 2, "one warning per unknown key: %r" % warnings)


@case
def test_apply_on_top_of_a_custom_base() -> None:
    base = apply(CoreSettings(), {"edges": {"margin": 0.4}, "recall": {"top_k": 5}})
    s = apply(base, {"edges": {"cross_top_k": 2}})
    check_eq(s.edges.margin, 0.4, "previous override survives")
    check_eq(s.recall.top_k, 5, "other section survives")
    check_eq(s.edges.cross_top_k, 2, "new override applied")


@case
def test_describe_is_plain_data() -> None:
    data = describe(CoreSettings(db_path="/tmp/e.db"))
    check_eq(data["db_path"], "/tmp/e.db", "db_path")
    check_eq(data["edges"]["margin"], 0.15, "nested policy flattened to dict")
    check_eq(data["recall"]["budget_tokens"], 600, "nested recall")


# --------------------------------------------------------------------------- wiring

@case
def test_edge_policy_reaches_the_decision_rule() -> None:
    """``edges.margin`` is the knob the whole exercise is about: prove it is wired."""
    answer = {"probabilities": {"改变": 0.40, "否定": 0.34, "重复": 0.01},
              "none": 0.25}
    hit_default = N._accepted(answer, N._AXIS_RELS, 0.15)
    hit_strict = N._accepted(answer, N._AXIS_RELS, 0.10)
    hit_loose = N._accepted(answer, N._AXIS_RELS, 0.01)
    check(hit_default is None, "0.40 vs 0.34 does not clear the default 0.15 margin")
    check(hit_strict is None, "nor 0.10")
    check(hit_loose is not None and hit_loose[0] == "改变", "0.01 lets it through")


@case
def test_eventmem_uses_the_settings_it_was_given(tmp_path=None) -> None:
    """The facade must hand the policy down, not read module globals."""
    settings = apply(CoreSettings(db_path=":memory:"), {"recall": {"budget_tokens": 111}})
    mem = EventMem(settings, store=_NullStore(), encoder=None)
    check_eq(mem.settings.recall.budget_tokens, 111, "settings kept on the instance")
    mem.close()


class _NullStore:
    """Just enough store for EventMem construction (no DB is opened)."""

    db_path = ":memory:"

    def list_projects(self, include_archived=False):
        return []

    def close(self):
        pass


# --------------------------------------------------------------------------- the border

@case
def test_core_never_imports_a_host() -> None:
    pattern = re.compile(r"^\s*(?:from|import)\s+(agent|hermes_cli|plugins|tools)\b")
    offenders = []
    for path in sorted((CORE / "eventmem_core").glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.match(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    check_eq(offenders, [], "core/eventmem_core must stay host-agnostic")


def main() -> int:
    passed = failed = 0
    for fn in CASES:
        try:
            fn()
        except Exception:
            failed += 1
            print(f"FAIL {fn.__name__}")
            import traceback

            traceback.print_exc()
        else:
            passed += 1
            print(f"PASS {fn.__name__}")
    print(f"PASS {passed} / FAIL {failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
