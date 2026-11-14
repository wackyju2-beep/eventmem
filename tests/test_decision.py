#!/usr/bin/env python3
"""eventmem P1 tests — the decision chain: which backend answers a pass, and provenance.

Run directly:  <python> tests/test_decision.py

No sockets: ``urllib.request.urlopen`` is swapped for a fake transport that records every
request and replays canned routes, so the tests really exercise URL composition, method,
headers, payload and response parsing — not a mock of our own function.

The rules being pinned:

* a pass probes **once** and then stays on that backend (two models → two distributions);
* a provider that is down is skipped, not retried per request;
* 401/403 is ``needs_key``, not "the machine is broken";
* the edge row records which backend decided it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import traceback
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
CORE = REPO / "core"
if str(CORE) not in sys.path:
    sys.path.insert(0, str(CORE))

os.environ.setdefault("EVENTMEM_LANG", "zh")

import eventmem_core.decision as decision  # noqa: E402
import eventmem_core.nightly as nightly  # noqa: E402
from eventmem_core.settings import CoreSettings, DecisionPolicy  # noqa: E402

CASES: list = []

AXIS = ("axis", "后一条相对前一条是什么关系？", {"重复": "语义等价", "改变": "参数替换"})
DETAIL = ("detail", "后一条和前一条是什么补充关系？", {"因为": "因果解释"})

SYSTEMONE_OK = {
    "answers": {
        "axis": {"choice": "改变", "confidence": 0.7,
                 "probabilities": {"重复": 0.1, "改变": 0.7},
                 "none_probability": 0.2},
        "detail": {"choice": None, "confidence": 0.0, "probabilities": {"因为": 0.1}, "none_probability": 0.9},
    }
}

DECISIONS_OK = {
    "answers": [
        {"type": "choice", "name": "axis", "choice": "改变", "confidence": 0.7,
         "probabilities": [{"value": "重复", "probability": 0.1},
                           {"value": "改变", "probability": 0.7},
                           {"value": "none", "probability": 0.2}]},
        {"type": "choice", "name": "detail", "choice": "因为", "confidence": 0.6,
         "probabilities": [{"value": "因为", "probability": 0.6},
                           {"value": "none", "probability": 0.4}]},
    ]
}


def case(fn):
    CASES.append(fn)
    return fn


def check(cond, msg):
    if not cond:
        raise AssertionError(msg)


def check_eq(got, want, msg):
    if got != want:
        raise AssertionError(f"{msg}: got {got!r}, want {want!r}")


# --------------------------------------------------------------------------- fake wire

class FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeTransport:
    """``urlopen`` stand-in.  ``routes`` maps a full URL to ``(status, body)`` or an exception."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls: list[dict] = []

    def __call__(self, request, timeout=None):
        self.calls.append({
            "url": request.full_url,
            "method": request.get_method(),
            "headers": {str(k).lower(): str(v) for k, v in request.header_items()},
            "body": request.data,
            "timeout": timeout,
        })
        route = self.routes.get(request.full_url)
        if route is None:
            raise OSError("connection refused")
        if isinstance(route, Exception):
            raise route
        status, body = route
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, "err", {}, None)
        return FakeResponse(status, body)


class transport_of:
    """Swap ``urlopen`` for one block, restore afterwards."""

    def __init__(self, fake: FakeTransport):
        self.fake = fake
        self.prev = None

    def __enter__(self):
        self.prev = urllib.request.urlopen
        urllib.request.urlopen = self.fake
        return self.fake

    def __exit__(self, *exc):
        urllib.request.urlopen = self.prev
        return False


def endpoint(**kw) -> decision.Endpoint:
    base = {"name": "p1", "protocol": "systemone", "base_url": "http://127.0.0.1:9",
            "model": "jev", "timeout": 30.0, "probe_timeout": 1.0}
    base.update(kw)
    return decision.Endpoint(**base)


# --------------------------------------------------------------------------- UrlProvider

@case
def test_probe_health_is_enough():
    provider = decision.UrlProvider(endpoint())
    fake = FakeTransport({"http://127.0.0.1:9/health": (200, b'{"status":"ok"}')})
    with transport_of(fake):
        check_eq(provider.probe(), True, "healthy server")
    check_eq([c["url"] for c in fake.calls], ["http://127.0.0.1:9/health"], "one probe only")
    check_eq(fake.calls[0]["method"], "GET", "probe is a GET")
    check("authorization" not in fake.calls[0]["headers"], "no key configured, no header")


@case
def test_probe_falls_back_to_models():
    provider = decision.UrlProvider(endpoint())
    fake = FakeTransport({
        "http://127.0.0.1:9/health": (404, b""),
        "http://127.0.0.1:9/v1/models": (200, b'{"data":[]}'),
    })
    with transport_of(fake):
        check_eq(provider.probe(), True, "/health missing, /v1/models answers")
    check_eq(len(fake.calls), 2, "tried both paths")


@case
def test_probe_401_is_needs_key_not_dead():
    provider = decision.UrlProvider(endpoint(base_url="https://api.typesafe.ai"))
    fake = FakeTransport({"https://api.typesafe.ai/health": (401, b"")})
    with transport_of(fake):
        check_eq(provider.probe(), False, "no key -> unusable")
    check_eq(provider.last_error, "needs_key", "the reason is specific")


@case
def test_probe_reports_unreachable():
    provider = decision.UrlProvider(endpoint())
    with transport_of(FakeTransport({})):
        check_eq(provider.probe(), False, "nothing listening")
    check_eq(provider.last_error, "unreachable", "reason")


@case
def test_probe_check_model_requires_the_id():
    """JIT means "not loaded" is fine — a *wrong id* is not.

    LM Studio lists every model it could load, loaded or not, so the id can be checked
    before a round spends minutes on it. Without this, a renamed weight fails at decide
    time, mid-round, and every later round fails the same way: the chain never falls back.
    """
    provider = decision.UrlProvider(
        endpoint(model="local-judge-4b", check_model=True)
    )
    fake = FakeTransport({
        "http://127.0.0.1:9/v1/models": (200, json.dumps({"data": [
            {"id": "0.8-jev-qwen-lev"}, {"id": "jev-style-0.8b-decision-v3"}]}).encode()),
    })
    with transport_of(fake):
        check_eq(provider.probe(), False, "id absent -> this backend cannot answer")
    check_eq(provider.last_error, "no-model", "the reason is specific")
    check_eq([c["url"] for c in fake.calls], ["http://127.0.0.1:9/v1/models"],
             "one request, no /health detour")


@case
def test_probe_check_model_accepts_a_model_that_is_not_loaded():
    """The whole point of JIT: an unloaded model is still a usable backend."""
    provider = decision.UrlProvider(
        endpoint(model="local-judge-4b", check_model=True)
    )
    body = json.dumps({"data": [{"id": "local-judge-4b", "object": "model"}]})
    fake = FakeTransport({"http://127.0.0.1:9/v1/models": (200, body.encode())})
    with transport_of(fake):
        check_eq(provider.probe(), True, "listed but not loaded is fine")


@case
def test_probe_check_model_ignores_a_shape_it_does_not_know():
    """A shim that answers /v1/models with something else is still just a live server."""
    provider = decision.UrlProvider(endpoint(check_model=True))
    fake = FakeTransport({"http://127.0.0.1:9/v1/models": (200, b'{"status":"ok"}')})
    with transport_of(fake):
        check_eq(provider.probe(), True, "unknown shape -> liveness only")
    fake = FakeTransport({"http://127.0.0.1:9/v1/models": (200, b"not json at all")})
    with transport_of(fake):
        check_eq(provider.probe(), True, "unparseable body -> liveness only")


@case
def test_probe_check_model_missing_id_still_reports_needs_key():
    provider = decision.UrlProvider(endpoint(base_url="https://api.typesafe.ai", check_model=True))
    fake = FakeTransport({"https://api.typesafe.ai/v1/models": (401, b"")})
    with transport_of(fake):
        check_eq(provider.probe(), False, "no key")
    check_eq(provider.last_error, "needs_key", "auth wins over the model check")


@case
def test_decide_posts_to_the_systemone_path():
    provider = decision.UrlProvider(endpoint())
    body = json.dumps(SYSTEMONE_OK).encode()
    fake = FakeTransport({"http://127.0.0.1:9/v1/systemone": (200, body)})
    with transport_of(fake):
        out = provider.decide_choice("状态", [AXIS, DETAIL])
    assert out is not None
    check_eq(out[0]["choice"], "改变", "parsed through providers.parse_answers")
    call = fake.calls[0]
    check_eq(call["method"], "POST", "method")
    check_eq(call["url"], "http://127.0.0.1:9/v1/systemone", "systemone path")
    payload = json.loads(call["body"].decode("utf-8"))
    check_eq(payload["state"], "状态", "state")
    check_eq(sorted(payload["questions"]), ["axis", "detail"], "both questions in one request")
    check_eq(call["headers"]["content-type"], "application/json", "content type")


@case
def test_decide_uses_the_decisions_path_and_explicit_none():
    provider = decision.UrlProvider(endpoint(protocol="openai-decisions", model="lev-4b"))
    body = json.dumps(DECISIONS_OK).encode()
    fake = FakeTransport({"http://127.0.0.1:9/v1/decisions": (200, body)})
    with transport_of(fake):
        out = provider.decide_choice("状态", [AXIS, DETAIL])
    assert out is not None
    check_eq(out[0]["choice"], "改变", "decision answer")
    payload = json.loads(fake.calls[0]["body"].decode("utf-8"))
    choices = payload["questions"][0]["choices"]
    check_eq([c["value"] for c in choices], ["重复", "改变", "none"], "none appended")
    check_eq(payload["model"], "lev-4b", "model id passed through")


@case
def test_bearer_token_only_when_a_key_exists():
    provider = decision.UrlProvider(endpoint(api_key="sk-test"))
    fake = FakeTransport({"http://127.0.0.1:9/v1/systemone": (200, json.dumps(SYSTEMONE_OK).encode())})
    with transport_of(fake):
        provider.decide_choice("s", [AXIS, DETAIL])
    check_eq(fake.calls[0]["headers"]["authorization"], "Bearer sk-test", "key sent")

    env_name = "EVENTMEM_TEST_DECISION_KEY"
    os.environ.pop(env_name, None)
    keyed = decision.UrlProvider(endpoint(api_key_env=env_name))
    check_eq(keyed.endpoint.key(), "", "env unset -> no key")
    os.environ[env_name] = "from-env"
    try:
        check_eq(keyed.endpoint.key(), "from-env", "env wins")
    finally:
        os.environ.pop(env_name, None)


@case
def test_decide_returns_none_instead_of_raising():
    provider = decision.UrlProvider(endpoint())
    with transport_of(FakeTransport({})):  # connection refused
        check_eq(provider.decide_choice("s", [AXIS]), None, "transport failure")
    check_eq(provider.last_error, "transport", "reason")

    bad = decision.UrlProvider(endpoint())
    with transport_of(FakeTransport({"http://127.0.0.1:9/v1/systemone": (200, b"not json")})):
        check_eq(bad.decide_choice("s", [AXIS]), None, "garbage body")


# --------------------------------------------------------------------------- Chain

class StubProvider:
    """Minimal provider: no HTTP, just counters — but it honours the same interface."""

    def __init__(self, name: str, *, up: bool = True, answer=None):
        self.name = name
        self.up = up
        self.last_error = ""
        self.answer = answer if answer is not None else [
            {"choice": None, "confidence": 0.0, "probabilities": {}, "none": 1.0}]
        self.probe_calls = 0
        self.decide_calls = 0

    def available(self):
        return True

    def probe(self):
        self.probe_calls += 1
        self.last_error = "" if self.up else "unreachable"
        return self.up

    def decide_choice(self, state, questions):
        self.decide_calls += 1
        return self.answer if self.up else None


@case
def test_chain_probes_once_and_sticks():
    first, second = StubProvider("first"), StubProvider("second")
    chain = decision.Chain([first, second])
    chain.decide_choice("s", [AXIS])
    first.up = False           # dies mid-pass
    chain.decide_choice("s", [AXIS])
    check_eq(first.probe_calls, 1, "probed exactly once per pass")
    check_eq(second.probe_calls, 0, "the runner-up is never contacted")
    check_eq(first.decide_calls, 2, "still sending to the chosen backend")
    check_eq(chain.active_name, "first", "no mid-pass switch")


@case
def test_chain_falls_through_to_the_first_live_backend():
    first, second = StubProvider("first", up=False), StubProvider("second")
    chain = decision.Chain([first, second])
    chain.decide_choice("s", [AXIS])
    check_eq(chain.active_name, "second", "second answers")
    check_eq(second.decide_calls, 1, "and receives the work")


@case
def test_chain_with_nothing_live_is_silent():
    chain = decision.Chain([StubProvider("a", up=False), StubProvider("b", up=False)])
    check_eq(chain.decide_choice("s", [AXIS]), None, "no answer")
    check_eq(chain.active_name, "", "no active backend")


@case
def test_chain_available_means_configured():
    check_eq(decision.Chain([]).available(), False, "empty chain")
    check_eq(decision.Chain([StubProvider("a")]).available(), True, "one provider")


# --------------------------------------------------------------------------- from_settings

@case
def test_from_settings_keeps_the_legacy_single_endpoint():
    settings = CoreSettings(decision=DecisionPolicy(url="http://127.0.0.1:18090"))
    chain = decision.from_settings(settings)
    check_eq([p.name for p in chain.providers], ["default"], "one provider")
    endpoint_ = chain.providers[0].endpoint
    check_eq(endpoint_.endpoint_path(), "/v1/systemone", "path from the policy")
    check_eq(endpoint_.model, "jev", "model from the policy")
    check_eq(endpoint_.timeout, 900.0, "timeout from the policy")


@case
def test_from_settings_honours_a_custom_path():
    settings = CoreSettings(decision=DecisionPolicy(url="http://h", path="/v1/decisions"))
    chain = decision.from_settings(settings)
    check_eq(chain.providers[0].endpoint.endpoint_path(), "/v1/decisions",
             "the old path knob still works")


@case
def test_from_settings_orders_packs_and_ignores_the_legacy_url():
    root = Path(tempfile.mkdtemp(prefix="eventmem-backends-"))
    try:
        for name, port in (("alpha", 1234), ("beta", 5678)):
            folder = root / name
            folder.mkdir()
            (folder / "config.yaml").write_text(
                "protocol: systemone\n"
                f"model: {name}-model\n"
                f"base_url: http://127.0.0.1:{port}\n"
                "external: true\n",
                encoding="utf-8",
            )
        settings = CoreSettings(decision=DecisionPolicy(
            url="http://legacy:1", backends=("beta", "alpha"), packs_dir=str(root)))
        chain = decision.from_settings(settings)
        check_eq([p.name for p in chain.providers], ["beta", "alpha"], "order is the policy's")
        check_eq(chain.providers[0].endpoint.model, "beta-model", "pack model used")
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


@case
def test_from_settings_reports_a_missing_pack_and_falls_back():
    warnings: list[str] = []
    settings = CoreSettings(decision=DecisionPolicy(
        url="http://legacy:1", backends=("nope",), packs_dir="/tmp/definitely-not-here"))
    chain = decision.from_settings(settings, warn=warnings.append)
    check_eq([p.name for p in chain.providers], ["default"], "legacy endpoint still there")
    check(any("nope" in w for w in warnings), f"the missing pack is named: {warnings}")


@case
def test_from_settings_skips_a_self_pack_until_the_daemon_exists():
    root = Path(tempfile.mkdtemp(prefix="eventmem-selfpack-"))
    try:
        folder = root / "local-judge"
        folder.mkdir()
        (folder / "model.gguf").write_bytes(b"x")
        (folder / "config.yaml").write_text(
            "protocol: systemone\nweights: model.gguf\n", encoding="utf-8")
        warnings: list[str] = []
        settings = CoreSettings(decision=DecisionPolicy(
            url="http://legacy:1", backends=("local-judge",), packs_dir=str(root)))
        chain = decision.from_settings(settings, warn=warnings.append)
        check_eq([p.name for p in chain.providers], ["default"], "no address yet")
        check(any("回落" in w for w in warnings), f"fallback is explained: {warnings}")
    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)


@case
def test_from_settings_with_no_url_and_no_packs_is_unavailable():
    chain = decision.from_settings(CoreSettings(decision=DecisionPolicy(url="")))
    check_eq(chain.available(), False, "empty url disables edge building, as before")
    check_eq(chain.decide_choice("s", [AXIS]), None, "nothing to ask")


# --------------------------------------------------------------------------- provenance

class RecordingStore:
    def __init__(self):
        self.edges: list[dict] = []

    def add_edge(self, src, dst, rel, conf, decided_by, cross=None, probs=None, backend=""):
        self.edges.append({"src": src, "dst": dst, "rel": rel, "conf": conf,
                           "decided_by": decided_by, "probs": probs, "backend": backend})


class StubChain:
    active_name = "lev-4b"


@case
def test_nightly_records_the_backend_on_the_edge():
    store = RecordingStore()
    engine = nightly._Nightly(store, StubChain())
    check(engine._write(2, 1, "改变", 0.9, {"改变": 0.9}), "edge written")
    check_eq(store.edges[0]["backend"], "lev-4b", "provenance")
    check_eq(store.edges[0]["probs"], {"改变": 0.9}, "distribution still stored")


@case
def test_nightly_still_works_with_a_store_that_lacks_backend():
    class LegacyStore:
        def __init__(self):
            self.edges = []

        def add_edge(self, src, dst, rel, conf, decided_by, probs=None):
            self.edges.append({"rel": rel, "probs": probs})

    store = LegacyStore()
    engine = nightly._Nightly(store, StubChain())
    check(engine._write(2, 1, "改变", 0.9, {"改变": 0.9}), "the fallback ladder writes anyway")
    check_eq(store.edges[0]["rel"], "改变", "recorded")


@case
def test_nightly_backend_is_empty_for_the_legacy_client():
    class PlainClient:
        def available(self):
            return True

        def decide_choice(self, state, questions):
            return None

    store = RecordingStore()
    engine = nightly._Nightly(store, PlainClient())
    engine._write(2, 1, "改变", 0.9, None)
    check_eq(store.edges[0]["backend"], "", "no name -> empty, not a crash")


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
