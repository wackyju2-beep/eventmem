#!/usr/bin/env python3
"""eventmem i18n tests — the language switch for everything the model reads.

Run directly:  <python> tests/test_i18n.py   (prints PASS n / FAIL m, exit 1 on failure)

The other suites pin ``EVENTMEM_LANG=zh`` because they assert on the Chinese wording; this one owns
both languages: resolution precedence, the strings themselves, and the promise that **stored values
never change** — kinds and relations stay Chinese in the database whichever language the prompt is in.
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CORE = REPO / "core"
ADAPTER = REPO / "adapters" / "hermes"
for p in (str(CORE), str(ADAPTER), str(HERE)):
    if p not in sys.path:
        sys.path.insert(0, p)

import _host_stub  # noqa: E402

_host_stub.install()

import eventmem  # noqa: E402
from eventmem import extract as extract  # noqa: E402
import eventmem_core.i18n as i18n  # noqa: E402
import eventmem_core.nightly as nightly  # noqa: E402
import eventmem_core.recall as recall  # noqa: E402
from eventmem_core.settings import KINDS  # noqa: E402

CASES: list = []
PASSED: list = []
FAILED: list = []


def case(fn):
    CASES.append(fn)
    return fn


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def check_eq(got, want, msg):
    if got != want:
        raise AssertionError(f"{msg}: got {got!r}, want {want!r}")


class lang_of:
    """Context manager: run a block under one language setting, then restore the environment."""

    def __init__(self, value):
        self.value = value
        self.prev = os.environ.get("EVENTMEM_LANG")

    def __enter__(self):
        if self.value is None:
            os.environ.pop("EVENTMEM_LANG", None)
        else:
            os.environ["EVENTMEM_LANG"] = self.value
        return self

    def __exit__(self, *exc):
        if self.prev is None:
            os.environ.pop("EVENTMEM_LANG", None)
        else:
            os.environ["EVENTMEM_LANG"] = self.prev
        return False


# --------------------------------------------------------------------------- resolution


@case
def test_explicit_env_wins_for_both_languages():
    with lang_of("zh"):
        check_eq(i18n.language(), "zh", "zh is zh")
        check_eq(i18n.tr("中文", "English"), "中文", "tr picks zh")
        check(i18n.is_zh(), "is_zh true under zh")
    with lang_of("en"):
        check_eq(i18n.language(), "en", "en is en")
        check_eq(i18n.tr("中文", "English"), "English", "tr picks en")
        check(not i18n.is_zh(), "is_zh false under en")


@case
def test_aliases_and_unknown_languages():
    for value, want in (("zh-CN", "zh"), ("zh_Hant", "zh"), ("Chinese", "zh"), ("中文", "zh"),
                        ("en-US", "en"), ("English", "en"), ("fr", "en")):
        with lang_of(value):
            check_eq(i18n.language(), want, f"EVENTMEM_LANG={value!r}")


@case
def test_config_language_is_used_when_the_env_is_unset():
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "config.yaml").write_text("display:\n  language: zh\n", encoding="utf-8")
        prev_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = tmp
        try:
            with lang_of(None):
                check_eq(i18n.language(), "zh", "display.language: zh")
        finally:
            if prev_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = prev_home
    # 显式环境变量优先于配置文件
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "config.yaml").write_text("display:\n  language: zh\n", encoding="utf-8")
        prev_home = os.environ.get("HERMES_HOME")
        os.environ["HERMES_HOME"] = tmp
        try:
            with lang_of("en"):
                check_eq(i18n.language(), "en", "env beats config")
        finally:
            if prev_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = prev_home


# --------------------------------------------------------------------------- stored values


@case
def test_display_labels_never_touch_stored_values():
    with lang_of("zh"):
        check_eq(i18n.kind_label("变更"), "变更", "zh kind unchanged")
        check_eq(i18n.rel_label("改变"), "改变", "zh rel unchanged")
    with lang_of("en"):
        check_eq(i18n.kind_label("变更"), "change", "en kind label")
        check_eq(i18n.kind_label("教训"), "lesson", "en kind label 2")
        check_eq(i18n.rel_label("否定"), "negates", "en rel label")
        check_eq(i18n.kind_label("未知"), "未知", "unknown passes through")
    check_eq(KINDS, ("决定", "尝试", "结果", "变更", "教训"), "KINDS are the schema")
    check_eq(recall.CHAIN_RELS, ("改变", "否定"), "CHAIN_RELS are the schema")


# --------------------------------------------------------------------------- plugin surface


@case
def test_tool_schemas_switch_language_but_keep_their_shape():
    with lang_of("zh"):
        zh = {t["name"]: t for t in eventmem.EventMemProvider().get_tool_schemas()}
    with lang_of("en"):
        en = {t["name"]: t for t in eventmem.EventMemProvider().get_tool_schemas()}
    check_eq(list(zh), list(en), "same tools in both languages")
    for name in zh:
        check(zh[name]["description"] and en[name]["description"], f"{name} has a description")
        check(zh[name]["description"] != en[name]["description"], f"{name} description is translated")
        check_eq(zh[name]["parameters"].get("required"), en[name]["parameters"].get("required"),
                 f"{name} required params are language-independent")
    kinds = zh["evt_add"]["parameters"]["properties"]["events"]["items"]["properties"]["kind"]["enum"]
    check_eq(kinds, list(KINDS), "kind enum stays Chinese")


@case
def test_neighbours_block_follows_the_language():
    store = None  # 用最小假 store：neighbours 只碰这几个方法
    if store is None:
        class _S:
            def get_event(self, eid):
                return {"id": 2, "ts": 1.0, "kind": "变更", "text": "中", "project": "P",
                        "session": None}

            def edges_into(self, eid):
                return [{"rel": "改变", "conf": 0.9, "src": 3}]

            def edges_from(self, eid):
                return []

            def get_events(self, ids):
                return [{"id": 3, "ts": 2.0, "kind": "结果", "text": "en", "project": "P"}]
        store = _S()
    with lang_of("zh"):
        zh = recall.neighbours(store, 2)
    with lang_of("en"):
        en = recall.neighbours(store, 2)
    check("后来的事件" in zh and "later events" in en, zh + " || " + en)
    check("同会话" not in en, en)


@case
def test_project_hint_follows_the_language():
    class _S:
        def list_projects(self, include_archived=False):
            return [{"id": 1, "name": "小主机", "status": "active"}]

        def project_events(self, pid):
            return []

    with lang_of("zh"):
        zh = recall.project_hint(_S(), None, "q")
    with lang_of("en"):
        en = recall.project_hint(_S(), None, "q")
    check("可用项目" in zh, zh)
    check("projects" in en and "proj_add" in en, en)
    check("小主机" in en, "项目名本身不翻译：" + en)


@case
def test_memory_hint_and_extraction_prompt_follow_the_language():
    with lang_of("zh"):
        zh_hint = eventmem.memory_hint()
        zh_prompt = extract.extract_system()
    with lang_of("en"):
        en_hint = eventmem.memory_hint()
        en_prompt = extract.extract_system()
    check("evt_add" in zh_hint and "evt_add" in en_hint, "both hints name the tool")
    check(zh_hint != en_hint, "hint is translated")
    check_eq(zh_hint, eventmem.MEMORY_HINT_ZH, "zh hint is the zh constant")
    check_eq(en_hint, eventmem.MEMORY_HINT_EN, "en hint is the en constant")
    check("事件记忆抽取器" in zh_prompt, "zh prompt")
    check("You extract event memory" in en_prompt, "en prompt")
    check('"events"' in zh_prompt and '"events"' in en_prompt, "both ask for the same JSON shape")


# --------------------------------------------------------------------------- recall rendering


@case
def test_recall_rendering_follows_the_language():
    with lang_of("zh"):
        check_eq(recall.unfiled(), "未归类", "zh unfiled")
        check_eq(recall.repeat_note(), "同上文", "zh repeat note")
        check_eq(recall.invalid_mark("11-06", "改变"), "已失效·被11-06取代", "zh superseded")
        check_eq(recall.invalid_mark("11-06", "否定"), "已失效·被11-06否定", "zh negated")
        body = recall._render_body({"id": 7, "kind": "变更", "text": "换 2T 盘"},
                                   recall.invalid_mark("11-06", "改变"))
        check(body.startswith("#7 [已失效·被11-06取代] 变更 换 2T 盘"), f"zh body: {body!r}")
    with lang_of("en"):
        check_eq(recall.unfiled(), "unfiled", "en unfiled")
        check_eq(recall.repeat_note(), "see above", "en repeat note")
        check_eq(recall.invalid_mark("11-06", "改变"), "superseded by 11-06", "en superseded")
        check_eq(recall.invalid_mark("11-06", "否定"), "negated by 11-06", "en negated")
        body = recall._render_body({"id": 7, "kind": "变更", "text": "switched to a 2T disk"},
                                   recall.invalid_mark("11-06", "改变"))
        check(body == "#7 [superseded by 11-06] change switched to a 2T disk", f"en body: {body!r}")


# --------------------------------------------------------------------------- decision prompts


@case
def test_decision_questions_switch_and_answers_map_back():
    with lang_of("zh"):
        key, text, options = nightly._question(nightly.AXIS_KEY, nightly.AXIS_TEXT,
                                               nightly.AXIS_TEXT_EN, nightly.AXIS_OPTIONS)
        check_eq(key, "axis", "question key is stable")
        check_eq(text, nightly.AXIS_TEXT, "zh question text")
        check_eq(list(options), ["重复", "改变", "否定", "都不是"], "zh option labels")
    with lang_of("en"):
        _key, text, options = nightly._question(nightly.AXIS_KEY, nightly.AXIS_TEXT,
                                                nightly.AXIS_TEXT_EN, nightly.AXIS_OPTIONS)
        check_eq(text, nightly.AXIS_TEXT_EN, "en question text")
        check_eq(list(options), ["repeat", "change", "negate", "none"], "en option labels")
        _k, _t, detail = nightly._question(nightly.DETAIL_KEY, nightly.DETAIL_TEXT,
                                           nightly.DETAIL_TEXT_EN, nightly.DETAIL_OPTIONS)
        check_eq(list(detail), ["because", "refine"], "en detail labels")
        check("why" in " ".join(detail.values()), "en definitions explain the criterion")
        # 英文标签的答案必须落回中文存储值
        answer = {"choice": "change", "probabilities": {"repeat": 0.1, "change": 0.7,
                                                        "negate": 0.1, "none": 0.1},
                  "none": 0.05}
        back = nightly._canonical_answer(answer)
        assert isinstance(back, dict)
        check_eq(back["choice"], "改变", "choice mapped back")
        check_eq(set(back["probabilities"]), {"重复", "改变", "否定", "都不是"},
                 "probabilities mapped back")
        check_eq(back["none"], 0.05, "none probability untouched")
    # zh 模式下是恒等映射
    with lang_of("zh"):
        answer = {"choice": "改变", "probabilities": {"改变": 1.0}}
        check_eq(nightly._canonical_answer(answer), answer, "zh answers pass through")


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
