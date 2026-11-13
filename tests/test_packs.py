#!/usr/bin/env python3
"""eventmem P0 tests — backend packs and the two decision wire formats.

Run directly:  <python> tests/test_packs.py    (prints PASS n / FAIL m, exit 1 on failure)

Nothing here touches the network or a database: ``providers`` is pure request/response
shaping and ``packs`` is manifest parsing.  The point is to pin the two facts the edge
builder silently depends on:

* a System One payload is byte-for-byte what ``JevClient`` already sends (so swapping in
  the new layer cannot change what the running StartLux service sees);
* an OpenAI Decisions payload carries an **explicit ``none`` choice**, because that schema
  has no ``none_probability`` slot and without it every edge gets looser.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CORE = REPO / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

os.environ.setdefault("EVENTMEM_LANG", "zh")

from eventmem_core import packs, providers  # noqa: E402

CASES: list = []

AXIS = ("axis", "后一条相对前一条是什么关系？", {"重复": "语义等价", "改变": "参数替换", "否定": "方向推翻"})
DETAIL = ("detail", "后一条和前一条是什么补充关系？", {"因为": "因果解释", "细化": "执行细节"})


def case(fn):
    CASES.append(fn)
    return fn


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def check_eq(got, want, msg):
    if got != want:
        raise AssertionError(f"{msg}: got {got!r}, want {want!r}")


def check_raises(exc_type, fn, msg):
    try:
        fn()
    except exc_type:
        return
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"{msg}: raised {type(exc).__name__}({exc}), want {exc_type.__name__}")
    raise AssertionError(f"{msg}: did not raise {exc_type.__name__}")


class lang_of:
    """Run a block under one language setting, then restore the environment."""

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


# --------------------------------------------------------------------------- System One

@case
def test_systemone_payload_matches_jev_client():
    """Byte parity with the payload ``JevClient.decide_choice`` sends today."""
    payload = providers.build_payload("systemone", "jev", "状态文本", [AXIS])
    check_eq(
        payload,
        {
            "model": "jev",
            "state": "状态文本",
            "questions": {
                "axis": {
                    "type": "choice",
                    "instructions": AXIS[1],
                    "criteria": dict(AXIS[2]),
                    "allow_none": True,
                }
            },
        },
        "systemone payload",
    )
    check("input" not in payload, "systemone must not send the Decisions field")


@case
def test_systemone_payload_is_what_jev_client_really_sends():
    """The parity test that matters: swap in the new layer, the running service never notices."""
    from eventmem_core import jev

    captured: dict = {}

    def fake_post(self, payload):
        captured.update(payload)
        return {"answers": {}}

    original = jev.JevClient._post
    jev.JevClient._post = fake_post
    try:
        client = jev.JevClient(base_url="http://127.0.0.1:9", model="jev", timeout=1)
        client.decide_choice("状态文本", [(AXIS[0], AXIS[1], dict(AXIS[2]))])
    finally:
        jev.JevClient._post = original

    check_eq(captured, providers.build_payload("systemone", "jev", "状态文本", [AXIS]),
             "payload differs from JevClient")


@case
def test_systemone_parse_keeps_distribution_and_none():
    data = {
        "answers": {
            "axis": {
                "choice": "改变",
                "confidence": 0.72,
                "probabilities": {"重复": 0.1, "改变": 0.72, "否定": 0.08},
                "none_probability": 0.1,
            }
        }
    }
    out = providers.parse_answers("systemone", data, [AXIS])
    assert out is not None
    check_eq(out[0]["choice"], "改变", "choice")
    check_eq(out[0]["confidence"], 0.72, "confidence")
    check_eq(out[0]["none"], 0.1, "none")
    check_eq(out[0]["probabilities"], {"重复": 0.1, "改变": 0.72, "否定": 0.08},
             "only real labels survive")


@case
def test_systemone_none_can_win():
    data = {"answers": {"axis": {"choice": None, "confidence": 0.0,
                                 "probabilities": {"重复": 0.1, "改变": 0.1, "否定": 0.05},
                                 "none_probability": 0.75}}}
    out = providers.parse_answers("systemone", data, [AXIS])
    assert out is not None
    check_eq(out[0]["choice"], None, "none wins -> no choice")
    check_eq(out[0]["none"], 0.75, "none probability")


@case
def test_systemone_rejects_label_outside_vocabulary():
    data = {"answers": {"axis": {"choice": "派生", "confidence": 0.9, "probabilities": {}}}}
    out = providers.parse_answers("systemone", data, [AXIS])
    check_eq(out, [None], "unknown label is a malformed answer, not a new relation")


@case
def test_confidence_present_but_unusable_kills_the_slot():
    data = {"answers": {"axis": {"choice": "改变", "confidence": 1.4,
                                 "probabilities": {"改变": 1.0}}}}
    check_eq(providers.parse_answers("systemone", data, [AXIS]), [None],
             "out-of-range confidence is a malformed answer much as a bad label")
    # 缺字段才回落，写坏了不回落
    data = {"answers": {"axis": {"choice": "改变", "probabilities": {"重复": 0, "改变": 0.6, "否定": 0.4}, "none_probability": 0.0}}}
    out = providers.parse_answers("systemone", data, [AXIS])
    assert out is not None
    check_eq(out[0]["confidence"], 0.6, "missing confidence falls back to the choice's probability")


@case
def test_systemone_needs_an_answers_object():
    check_eq(providers.parse_answers("systemone", {"answers": []}, [AXIS]), None,
             "list where an object is required is a whole-call failure")
    check_eq(providers.parse_answers("systemone", "not json", [AXIS]), None, "non-mapping body")


# --------------------------------------------------------------------------- OpenAI Decisions

@case
def test_decisions_payload_appends_explicit_none():
    payload = providers.build_payload("openai-decisions", "lev-4b", "状态文本", [AXIS])
    check_eq(payload["model"], "lev-4b", "model")
    check_eq(payload["input"], "状态文本", "input")
    check("state" not in payload, "decisions must not send the System One field")
    question = payload["questions"][0]
    check_eq(question["type"], "choice", "question type")
    check_eq(question["name"], "axis", "question name")
    check_eq([c["value"] for c in question["choices"]], ["重复", "改变", "否定", "none"],
             "real labels then an explicit none")
    check(bool(question["choices"][-1]["description"]), "none choice needs a criterion")


@case
def test_decisions_none_description_follows_language():
    with lang_of("en"):
        payload = providers.build_payload("openai-decisions", "lev", "s", [AXIS])
        english = payload["questions"][0]["choices"][-1]["description"]
        check("None of the above" in english, f"en description: {english!r}")
    with lang_of("zh"):
        payload = providers.build_payload("openai-decisions", "lev", "s", [AXIS])
        chinese = payload["questions"][0]["choices"][-1]["description"]
        check("都不" in chinese, f"zh description: {chinese!r}")


@case
def test_decisions_rejects_impossible_option_count():
    wide = {"q": "x", **{f"l{i}": "y" for i in range(255)}}  # 255 + none = 256
    check_raises(ValueError, lambda: providers.build_payload(
        "openai-decisions", "lev", "s", [("axis", "t", wide)]), "over 255 choices")


@case
def test_decisions_parse_maps_by_name_not_position():
    data = {
        "answers": [
            {"type": "choice", "name": "detail", "choice": "因为", "confidence": 0.61,
             "probabilities": [{"value": "因为", "probability": 0.61},
                               {"value": "细化", "probability": 0.19},
                               {"value": "none", "probability": 0.2}]},
            {"type": "choice", "name": "axis", "choice": "改变", "confidence": 0.7,
             "probabilities": [{"value": "重复", "probability": 0.1},
                               {"value": "改变", "probability": 0.7},
                               {"value": "否定", "probability": 0.1},
                               {"value": "none", "probability": 0.1}]},
        ]
    }
    out = providers.parse_answers("openai-decisions", data, [AXIS, DETAIL])
    assert out is not None
    check_eq(out[0]["choice"], "改变", "axis answered by name, not by index")
    check_eq(out[1]["choice"], "因为", "detail answered by name")


@case
def test_decisions_splits_none_out_of_probabilities():
    data = {"answers": [{"type": "choice", "name": "axis", "choice": "none", "confidence": 0.55,
                         "probabilities": [{"value": "重复", "probability": 0.1},
                                           {"value": "改变", "probability": 0.2},
                                           {"value": "否定", "probability": 0.15},
                                           {"value": "none", "probability": 0.55}]}]}
    out = providers.parse_answers("openai-decisions", data, [AXIS])
    assert out is not None
    check_eq(out[0]["choice"], None, "none wins")
    check_eq(out[0]["none"], 0.55, "none probability kept separately")
    check_eq(set(out[0]["probabilities"]), {"重复", "改变", "否定"}, "none never a real label")


@case
def test_decisions_refusal_only_kills_its_own_slot():
    data = {"answers": [
        {"type": "refusal", "name": "axis"},
        {"type": "choice", "name": "detail", "choice": "细化", "confidence": 0.8,
         "probabilities": [{"value": "因为", "probability": 0.1},
                           {"value": "细化", "probability": 0.8},
                           {"value": "none", "probability": 0.1}]},
    ]}
    out = providers.parse_answers("openai-decisions", data, [AXIS, DETAIL])
    check_eq(out, [None, {"choice": "细化", "confidence": 0.8,
                          "probabilities": {"因为": 0.1, "细化": 0.8}, "none": 0.1}],
             "one refusal must not throw away the other edge")


@case
def test_decisions_ignores_answers_of_the_wrong_type():
    data = {"answers": [
        {"type": "predicate", "name": "axis", "probability": 0.9},
        {"type": "choice", "name": "detail", "choice": True, "confidence": 0.9,
         "probabilities": [{"value": "细化", "probability": 0.9}]},
    ]}
    out = providers.parse_answers("openai-decisions", data, [AXIS, DETAIL])
    check_eq(out, [None, None], "predicate and bool answers are not choices")


@case
def test_decisions_tolerates_unordered_probabilities():
    data = {"answers": [{"type": "choice", "name": "axis", "choice": "改变", "confidence": 0.5,
                         "probabilities": [{"value": "none", "probability": 0.2},
                                           {"value": "改变", "probability": 0.5},
                                           {"value": "否定", "probability": 0.2},
                                           {"value": "重复", "probability": 0.1}]}]}
    out = providers.parse_answers("openai-decisions", data, [AXIS])
    assert out is not None
    check_eq(out[0]["probabilities"], {"改变": 0.5, "否定": 0.2, "重复": 0.1}, "unordered labels are retained")
    check_eq(out[0]["none"], 0.2, "none found wherever it sits in the list")


@case
def test_unknown_protocol_is_a_programming_error():
    check_raises(ValueError, lambda: providers.build_payload("lev", "m", "s", [AXIS]),
                 "build")
    check_raises(ValueError, lambda: providers.parse_answers("lev", {}, [AXIS]), "parse")


# --------------------------------------------------------------------------- packs

@case
def test_self_pack_defaults():
    pack = packs.from_mapping("startlux-4b", "/tmp/backends/startlux-4b",
                              {"protocol": "systemone", "weights": "model.gguf"})
    check_eq(pack.kind, "self", "weights -> self")
    check_eq(pack.launch, "auto", "launch defaults to auto")
    check_eq(pack.port, 0, "port 0 = let the daemon pick one")
    check_eq(pack.model, "model", "model defaults to the weight file stem")
    check_eq(pack.weights, "/tmp/backends/startlux-4b/model.gguf", "weights are resolved")
    check_eq(pack.args, (), "no args by default")


@case
def test_remote_pack_needs_model_and_key():
    env_name = "EVENTMEM_TEST_KEY_UNSET_XYZ"
    os.environ.pop(env_name, None)
    pack = packs.from_mapping("jev-cloud", "/tmp/b/jev-cloud",
                              {"protocol": "systemone", "model": "jev-latest",
                               "base_url": "https://api.typesafe.ai/",
                               "api_key_env": env_name})
    check_eq(pack.kind, "remote", "base_url -> remote")
    check_eq(pack.base_url, "https://api.typesafe.ai", "trailing slash dropped")
    check(pack.needs_key, "no key in the environment -> needs_key")
    os.environ[env_name] = "sk-test"
    try:
        check_eq(pack.key(), "sk-test", "key comes from the environment")
        check(not pack.needs_key, "key present -> satisfied")
    finally:
        os.environ.pop(env_name, None)

    check_raises(packs.PackError, lambda: packs.from_mapping(
        "x", "/tmp/x", {"protocol": "systemone", "base_url": "http://h"}), "remote without model")


@case
def test_external_pack_is_never_launched():
    pack = packs.from_mapping("lmstudio-lev", "/tmp/b/lmstudio-lev",
                              {"protocol": "systemone", "model": "lev-4b",
                               "base_url": "http://127.0.0.1:1234", "external": True})
    check_eq(pack.kind, "external", "external flag wins over remote")
    check_eq(pack.launch, "", "we do not launch someone else's server")
    check_raises(packs.PackError, lambda: packs.from_mapping(
        "x", "/tmp/x", {"protocol": "systemone", "model": "m",
                        "base_url": "http://h", "launch": "auto"}), "launch on remote")


@case
def test_pack_needs_exactly_one_source():
    check_raises(packs.PackError, lambda: packs.from_mapping(
        "x", "/tmp/x", {"protocol": "systemone", "model": "m",
                        "weights": "m.gguf", "base_url": "http://h"}), "both sources")
    check_raises(packs.PackError, lambda: packs.from_mapping(
        "x", "/tmp/x", {"protocol": "systemone", "model": "m"}), "no source")
    check_raises(packs.PackError, lambda: packs.from_mapping(
        "x", "/tmp/x", {"weights": "m.gguf"}), "no protocol")


@case
def test_pack_rejects_unknown_protocol():
    check_raises(packs.PackError, lambda: packs.from_mapping(
        "x", "/tmp/x", {"protocol": "openai-chat", "weights": "m.gguf"}), "generative chat is not a decision protocol")


@case
def test_pack_reports_unknown_keys_without_failing():
    seen: list[str] = []
    pack = packs.from_mapping("x", "/tmp/x",
                              {"protocol": "systemone", "weights": "m.gguf", "tpyo": 1},
                              warn=seen.append)
    check_eq(pack.kind, "self", "the pack still loads")
    check(any("tpyo" in message for message in seen), f"warning mentions the key: {seen}")


@case
def test_pack_ignores_bad_margin_and_port():
    seen: list[str] = []
    pack = packs.from_mapping("x", "/tmp/x",
                              {"protocol": "systemone", "weights": "m.gguf",
                               "margin": 1.4, "port": "not a port"},
                              warn=seen.append)
    check_eq(pack.margin, None, "out-of-range margin dropped")
    check_eq(pack.port, 0, "bad port becomes auto")
    check_eq(len(seen), 2, f"both problems reported: {seen}")

    ok = packs.from_mapping("y", "/tmp/y",
                            {"protocol": "systemone", "weights": "m.gguf",
                             "margin": 0.25, "port": "18090"})
    check_eq(ok.margin, 0.25, "margin kept")
    check_eq(ok.port, 18090, "port parsed from a string")


@case
def test_discover_reports_broken_packs_without_hiding_good_ones():
    root = Path(tempfile.mkdtemp(prefix="eventmem-packs-"))
    try:
        good = root / "good"
        good.mkdir()
        (good / "model.gguf").write_bytes(b"x")
        (good / packs.PACK_FILE).write_text(
            "protocol: systemone\nweights: model.gguf\n", encoding="utf-8")
        broken = root / "broken"
        broken.mkdir()
        (broken / packs.PACK_FILE).write_text("protocol: nonsense\nweights: m.gguf\n",
                                              encoding="utf-8")
        (root / "not-a-pack").mkdir()
        for skipped in (".hidden", "_draft"):
            (root / skipped).mkdir()
            (root / skipped / packs.PACK_FILE).write_text(
                "protocol: systemone\nweights: m.gguf\n", encoding="utf-8")

        found, problems = packs.discover(root)
        check_eq([p.name for p in found], ["good"], "only the usable pack is returned")
        check_eq(len(problems), 1, f"one problem reported: {problems}")
        check("broken" in problems[0], "the problem names the folder")
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


@case
def test_load_pack_reads_a_hand_written_yaml():
    root = Path(tempfile.mkdtemp(prefix="eventmem-packfile-"))
    try:
        folder = root / "lmstudio"
        folder.mkdir()
        (folder / packs.PACK_FILE).write_text(
            "# 手写配置\n"
            "protocol: systemone\n"
            "model: lev-4b\n"
            "base_url: http://127.0.0.1:1234\n"
            "external: true\n",
            encoding="utf-8",
        )
        pack = packs.load_pack(folder)
        check_eq(pack.name, "lmstudio", "folder name is the backend name")
        check_eq(pack.kind, "external", "kind from the file")
        check_eq(pack.model, "lev-4b", "model id kept")
        # 传 config.yaml 本身也认
        check_eq(packs.load_pack(folder / packs.PACK_FILE).name, "lmstudio", "config path works")
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


@case
def test_describe_never_leaks_the_key():
    pack = packs.from_mapping("cloud", "/tmp/cloud",
                              {"protocol": "systemone", "model": "jev-latest",
                               "base_url": "https://api.typesafe.ai", "api_key": "sk-secret"})
    snapshot = json.dumps(pack.describe(), ensure_ascii=False)
    check("sk-secret" not in snapshot, f"key leaked: {snapshot}")
    check('"has_key": true' in snapshot, "but presence is visible")


@case
def test_check_model_is_opt_in_and_ignored_for_self_packs():
    """``check_model`` guards a remote id; a self pack cannot use it.

    The remote case is the one that matters: LM Studio with JIT will fail *at decide time*
    on an id it does not know, which stalls every round instead of falling back.
    """
    warned: list[str] = []
    pack = packs.from_mapping(
        "win-autothink", "/tmp/b/win-autothink",
        {"protocol": "systemone", "model": "StartLux-Decision-v1.1-4B-AutoThink",
         "base_url": "http://10.42.0.51:1234", "external": True, "check_model": True},
        warn=warned.append,
    )
    check_eq(pack.check_model, True, "external pack keeps the flag")
    check_eq(warned, [], "no warnings")

    plain = packs.from_mapping(
        "startlux", "/tmp/b/startlux",
        {"protocol": "systemone", "model": "jev", "base_url": "http://127.0.0.1:18090",
         "external": True},
    )
    check_eq(plain.check_model, False, "off unless the file says so")

    warned.clear()
    local = packs.from_mapping(
        "lev", "/tmp/b/lev",
        {"protocol": "systemone", "model": "lev", "weights": "/tmp/lev.gguf", "check_model": True},
        warn=warned.append,
    )
    check_eq(local.check_model, False, "a self pack cannot check an id it derives itself")
    check(len(warned) == 1 and "check_model" in warned[0], "and says so")


@case
def test_the_examples_are_real_packs():
    """``examples/backends/`` is documentation that must still load.

    A pack is a folder with a config; if the examples drift from the loader, readers copy a
    file that does not work — so this parses them exactly the way the daemon does.
    """
    found, problems = packs.discover(REPO / "examples" / "backends")
    # The lev example points at ../../models/…, which only resolves once the folder is copied
    # into ~/.hermes/eventmem/backends/.  That one warning is expected; anything else is rot.
    missing_weights = [p for p in problems if "权重文件不存在" in p]
    check_eq([p for p in problems if p not in missing_weights], [], "the example configs parse cleanly")
    check_eq(len(missing_weights), 1, "and only lev refers to weights that are not in the repo")
    check_eq(sorted(p.name for p in found), ["lev", "lmstudio", "startlux", "typesafe"],
             "one example per shape")
    by_name = {p.name: p for p in found}
    check_eq(by_name["lev"].kind, "self", "lev: we launch it")
    check_eq(by_name["lev"].launch, "auto", "and we launch the llama.cpp we installed")
    check_eq(by_name["startlux"].kind, "external", "startlux: someone else runs it")
    check_eq(by_name["lmstudio"].kind, "external", "lmstudio: same shape")
    check_eq(by_name["typesafe"].kind, "remote", "typesafe: hosted")
    check_eq(by_name["typesafe"].base_url, "https://api.typesafe.ai", "the documented endpoint")

    had = os.environ.pop("TYPESAFE_API_KEY", None)
    try:
        check_eq(by_name["typesafe"].needs_key, True, "the repo never carries a key")
    finally:
        if had is not None:
            os.environ["TYPESAFE_API_KEY"] = had


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
