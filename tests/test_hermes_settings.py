#!/usr/bin/env python3
"""eventmem Hermes adapter settings tests — config.yaml → CoreSettings.

Run directly:  <python> tests/test_hermes_settings.py

The adapter's only job here is translation.  These cases pin the spellings it accepts,
the fallbacks it promises, and the one thing an operator will actually touch:
``plugins.entries.eventmem.settings.edges.margin``.
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
for _p in (str(REPO / "core"), str(REPO / "adapters" / "hermes"), str(HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import _host_stub  # noqa: E402

_host_stub.install()

import eventmem.settings as S  # noqa: E402
import eventmem_core.i18n as i18n  # noqa: E402
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


def home_with(config: str) -> str:
    """A throwaway HERMES_HOME containing exactly *config* as config.yaml."""
    tmp = tempfile.mkdtemp(prefix="eventmem-settings-")
    Path(tmp, "config.yaml").write_text(config, encoding="utf-8")
    return tmp


# --------------------------------------------------------------------------- defaults

@case
def test_no_config_means_the_core_defaults() -> None:
    home = home_with("")
    try:
        got = S.build(home)
        check_eq(got.edges.margin, 0.15, "margin default")
        check_eq(got.recall.budget_tokens, 600, "recall default")
        check_eq(got.max_events_per_call, 3, "batch default")
        check_eq(Path(got.db_path), Path(home) / "eventmem" / "events.db", "db lives under the home")
        # 模型归 eventmem 自己（不再借 localmem 的目录）；两个目录都不存在时也指向这里，
        # 这样报错说的是对的地方。
        check_eq(Path(got.models_root), Path(home) / "eventmem" / "models", "model dir")
    finally:
        _rm(home)


@case
def test_models_dir_falls_back_to_the_legacy_localmem_path() -> None:
    """老部署的模型还在 localmem/models 下：没有新目录时退回旧的，别静默失去向量臂。"""
    home = home_with("")
    try:
        legacy = Path(home) / "localmem" / "models"
        legacy.mkdir(parents=True)
        got = S.build(home)
        check_eq(Path(got.models_root), legacy, "legacy fallback")
        own = Path(home) / "eventmem" / "models"
        own.mkdir(parents=True)
        check_eq(Path(S.build(home).models_root), own, "新目录一出现就优先用它")
    finally:
        _rm(home)


@case
def test_missing_config_file_is_not_an_error() -> None:
    tmp = tempfile.mkdtemp(prefix="eventmem-settings-")
    try:
        got = S.build(tmp)                      # no config.yaml at all
        check_eq(got.edges.margin, 0.15, "defaults with no file")
    finally:
        _rm(tmp)


# --------------------------------------------------------------------------- overrides

@case
def test_nested_plugin_settings_reach_the_core() -> None:
    home = home_with(
        "plugins:\n"
        "  entries:\n"
        "    eventmem:\n"
        "      settings:\n"
        "        edges:\n"
        "          margin: 0.25\n"
        "          cross_top_k: 5\n"
        "        recall:\n"
        "          budget_tokens: 1200\n"
        "        decision:\n"
        "          url: 'http://127.0.0.1:9999'\n"
    )
    try:
        got = S.build(home)
        check_eq(got.edges.margin, 0.25, "edges.margin — the top1-vs-top2 knob")
        check_eq(got.edges.cross_top_k, 5, "another edge knob")
        check_eq(got.recall.budget_tokens, 1200, "recall budget")
        check_eq(got.decision.url, "http://127.0.0.1:9999", "decision url")
        check_eq(got.edges.heads_cos_min, 0.35, "unmentioned knobs keep their defaults")
    finally:
        _rm(home)


@case
def test_dotted_keys_are_accepted_too() -> None:
    home = home_with(
        "plugins:\n"
        "  entries:\n"
        "    eventmem:\n"
        "      settings:\n"
        "        'edges.margin': 0.3\n"
        "        'recall.max_items': 7\n"
    )
    try:
        got = S.build(home)
        check_eq(got.edges.margin, 0.3, "dotted margin")
        check_eq(got.recall.max_items, 7, "dotted max_items")
    finally:
        _rm(home)


@case
def test_legacy_config_subtree_still_reads() -> None:
    home = home_with(
        "plugins:\n"
        "  entries:\n"
        "    eventmem:\n"
        "      config:\n"
        "        edges:\n"
        "          margin: 0.2\n"
    )
    try:
        check_eq(S.build(home).edges.margin, 0.2, "pre-0.2 spelling")
    finally:
        _rm(home)


@case
def test_db_override_wins_over_the_default_path() -> None:
    home = home_with("")
    try:
        got = S.build(home, db="/tmp/somewhere/events.db")
        check_eq(got.db_path, "/tmp/somewhere/events.db", "explicit db")
    finally:
        _rm(home)


# --------------------------------------------------------------------------- failure modes

@case
def test_a_bad_value_warns_and_keeps_the_default() -> None:
    home = home_with(
        "plugins:\n"
        "  entries:\n"
        "    eventmem:\n"
        "      settings:\n"
        "        edges:\n"
        "          margin: nonsense\n"
    )
    warnings: list[str] = []
    try:
        got = S.build(home, warn=warnings.append)
        check_eq(got.edges.margin, 0.15, "bad margin is ignored, not fatal")
        check(any("margin" in w for w in warnings), "and it is reported: %r" % warnings)
    finally:
        _rm(home)


@case
def test_broken_yaml_falls_back_to_defaults() -> None:
    home = home_with("plugins: [unclosed\n")
    warnings: list[str] = []
    try:
        got = S.build(home, warn=warnings.append)
        check_eq(got.edges.margin, 0.15, "unparseable config is not fatal")
    finally:
        _rm(home)


# --------------------------------------------------------------------------- language

@case
def test_display_language_is_passed_to_the_core() -> None:
    home = home_with("display:\n  language: en\n")
    try:
        prev = os.environ.pop("EVENTMEM_LANG", None)
        try:
            check_eq(S.language(home), "en", "adapter reads display.language")
            s = S.build(home)
            check_eq(s.language, "en", "settings carry it")
            S.install_language_provider(home)
            check_eq(i18n.language(), "en", "core renderer follows via the provider")
        finally:
            i18n.set_language_provider(None)
            if prev is not None:
                os.environ["EVENTMEM_LANG"] = prev
    finally:
        _rm(home)


@case
def test_eventmem_lang_beats_the_host_provider() -> None:
    home = home_with("display:\n  language: en\n")
    try:
        S.install_language_provider(home)
        prev = os.environ.get("EVENTMEM_LANG")
        os.environ["EVENTMEM_LANG"] = "zh"
        try:
            check_eq(i18n.language(), "zh", "EVENTMEM_LANG is the outer override")
        finally:
            if prev is None:
                os.environ.pop("EVENTMEM_LANG", None)
            else:
                os.environ["EVENTMEM_LANG"] = prev
    finally:
        i18n.set_language_provider(None)
        _rm(home)


def _rm(path: str) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)


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
