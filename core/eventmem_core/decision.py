"""eventmem_core.decision — which backend answers the pair questions.

One *pass* (one worker round, one nightly run) asks the same two questions about a few
dozen event pairs.  This module decides **once per pass** which backend answers them, and
then sticks with it:

* providers are tried in the order given (``decision.backends`` = pack names, or the single
  legacy ``decision.url``);
* the first one that answers a cheap probe wins; the rest are not contacted;
* if it dies mid-pass, the pass fails — we do **not** switch halfway.  Two models produce
  two different probability distributions, and a night whose edges mix them cannot be
  recalibrated from ``edges.probs`` later.

That is the whole failover story.  Retrying every request against every backend would
double the latency of a slow night and hide a broken endpoint behind a quiet fallback.

The legacy single endpoint keeps working untouched: with no ``backends`` configured the
chain holds exactly one provider built from ``decision.url`` — the behaviour v2.1 had.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

from . import packs as _packs
from . import runtime as _runtime
from .providers import PROTOCOLS, build_payload, parse_answers

_log = logging.getLogger(__name__)

__all__ = ["PATH_BY_PROTOCOL", "Endpoint", "UrlProvider", "Chain", "from_pack", "from_settings"]

#: Endpoint path per protocol.  Same shape twice, two paths.
PATH_BY_PROTOCOL = {
    "systemone": "/v1/systemone",
    "openai-decisions": "/v1/decisions",
}

#: Paths that answer "are you alive" without spending a token.
_PROBE_PATHS = {
    "systemone": ("/health", "/v1/models"),
    "openai-decisions": ("/v1/models",),
}


def _model_ids(body: Any) -> Optional[set[str]]:
    """Model ids from an OpenAI-ish listing.

    ``None`` means "this is not a listing I understand" — then a probe can only judge
    liveness, never the id (an older server, or a shim like StartLux).
    """
    if not isinstance(body, dict):
        return None
    rows = body.get("data")
    if rows is None:
        rows = body.get("models")
    if not isinstance(rows, list):
        return None
    ids: set[str] = set()
    for row in rows:
        if isinstance(row, dict):
            value = row.get("id") or row.get("name") or row.get("model")
            if value:
                ids.add(str(value))
        elif isinstance(row, str):
            ids.add(row)
    return ids


@dataclass(frozen=True)
class Endpoint:
    """One addressable decision service."""

    name: str
    protocol: str = "systemone"
    base_url: str = ""
    model: str = "jev"
    #: Empty = derived from ``protocol`` (``/v1/systemone`` / ``/v1/decisions``).
    path: str = ""
    api_key: str = ""
    api_key_env: str = ""
    #: A full decision, not a token: Instant models answer in one pass, a slow CPU does not.
    timeout: float = 900.0
    probe_timeout: float = 2.0
    #: Probe must also see ``model`` in ``/v1/models`` — see ``packs.Pack.check_model``.
    check_model: bool = False

    def endpoint_path(self) -> str:
        if self.path:
            return self.path if self.path.startswith("/") else "/" + self.path
        return PATH_BY_PROTOCOL.get(self.protocol, "/v1/systemone")

    def key(self) -> str:
        """Environment first: a key in an env var never has to be written to disk."""
        if self.api_key_env:
            value = os.environ.get(self.api_key_env, "")
            if value:
                return value
        return self.api_key


class UrlProvider:
    """One HTTP endpoint speaking System One or OpenAI Decisions."""

    def __init__(self, endpoint: Endpoint):
        self.endpoint = endpoint
        #: Why the last probe/call failed: ``unreachable`` / ``needs_key`` / ``bad-response`` …
        self.last_error = ""

    # ------------------------------------------------------------------ identity

    @property
    def name(self) -> str:
        return self.endpoint.name

    @property
    def protocol(self) -> str:
        return self.endpoint.protocol

    def available(self) -> bool:
        return bool(self.endpoint.base_url)

    def __repr__(self) -> str:  # pragma: no cover - logs only
        return f"<UrlProvider {self.name} {self.protocol} {self.endpoint.base_url}>"

    # ------------------------------------------------------------------ probe

    def probe(self) -> bool:
        """Cheap liveness check.  Never sends the state, never spends tokens.

        A loaded model is *not* required: LM Studio with JIT loads whatever the request
        names, so "the port answers" is the only thing worth asking. With
        ``check_model`` we also confirm the id exists — a wrong id fails at decide time,
        mid-round, which would otherwise stall every round until someone reads the log.
        """
        self.last_error = ""
        if not self.available():
            self.last_error = "no-url"
            return False
        if self.endpoint.check_model:
            return self._probe_model()
        saw_response = False
        for path in _PROBE_PATHS.get(self.endpoint.protocol, ("/health",)):
            status = self._get(path)
            if status is None:
                self.last_error = "unreachable"
                return False
            saw_response = True
            if status in (401, 403):
                self.last_error = "needs_key"
                return False
            if 200 <= status < 300:
                return True
        self.last_error = "unreachable" if not saw_response else "bad-status"
        return False

    def _probe_model(self) -> bool:
        """Liveness + "the model I am about to name really exists"."""
        status, body = self._get_json("/v1/models")
        if status is None:
            self.last_error = "unreachable"
            return False
        if status in (401, 403):
            self.last_error = "needs_key"
            return False
        if not 200 <= status < 300:
            self.last_error = "bad-status"
            return False
        ids = _model_ids(body)
        if ids is None:
            return True  # unknown shape (a shim, an older server): liveness is enough
        if self.endpoint.model not in ids:
            self.last_error = "no-model"
            return False
        return True

    # ------------------------------------------------------------------ decide

    def decide_choice(
        self, state: str, questions: Iterable[tuple[str, str, Mapping[str, str]]]
    ) -> list[dict[str, Any] | None] | None:
        """Ask both questions in one request; ``None`` means "this call cannot be trusted"."""
        items = list(questions)
        if not self.available():
            self.last_error = "no-url"
            return None
        try:
            payload = build_payload(self.endpoint.protocol, self.endpoint.model, state, items)
        except ValueError:
            self.last_error = "bad-question"
            return None
        try:
            data = self._post(payload)
        except Exception as exc:  # noqa: BLE001 - fail silent by contract
            _log.debug("decision request failed: %s", type(exc).__name__)
            self.last_error = "transport"
            return None
        answer = parse_answers(self.endpoint.protocol, data, items)
        if answer is None:
            self.last_error = "bad-response"
        return answer

    # ------------------------------------------------------------------ wire

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        key = self.endpoint.key()
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _get(self, path: str) -> Optional[int]:
        request = urllib.request.Request(
            self.endpoint.base_url.rstrip("/") + path,
            headers=self._headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.endpoint.probe_timeout) as resp:
                return int(getattr(resp, "status", 200) or 200)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except Exception as exc:  # noqa: BLE001 - any failure means "not reachable"
            _log.debug("probe %s failed: %s", path, type(exc).__name__)
            return None

    def _get_json(self, path: str) -> tuple[Optional[int], Any]:
        """GET and parse. ``(None, None)`` means the socket never answered."""
        request = urllib.request.Request(
            self.endpoint.base_url.rstrip("/") + path,
            headers=self._headers(),
            method="GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.endpoint.probe_timeout) as resp:
                raw = resp.read()
                try:
                    return int(getattr(resp, "status", 200) or 200), json.loads(raw or b"{}")
                except ValueError:
                    return int(getattr(resp, "status", 200) or 200), None
        except urllib.error.HTTPError as exc:
            return int(exc.code), None
        except Exception as exc:  # noqa: BLE001 - any failure means "not reachable"
            _log.debug("probe %s failed: %s", path, type(exc).__name__)
            return None, None

    def _post(self, payload: Mapping[str, Any]) -> Any:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint.base_url.rstrip("/") + self.endpoint.endpoint_path(),
            data=body,
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=self.endpoint.timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))


class Chain:
    """An ordered list of providers, resolved once per pass and then frozen."""

    def __init__(self, providers: Iterable[UrlProvider]):
        self.providers = [p for p in providers if p is not None]
        self._active: Optional[UrlProvider] = None
        self._probed = False

    # The edge builder only ever calls these two.
    def available(self) -> bool:
        return any(p.available() for p in self.providers)

    def decide_choice(
        self, state: str, questions: Iterable[tuple[str, str, Mapping[str, str]]]
    ) -> list[dict[str, Any] | None] | None:
        if not self._probed:
            self.probe()
        if self._active is None:
            return None
        # One provider for the whole pass: a mid-pass switch would mix two distributions.
        return self._active.decide_choice(state, questions)

    # ------------------------------------------------------------------ status

    @property
    def active(self) -> Optional[UrlProvider]:
        return self._active

    @property
    def active_name(self) -> str:
        return self._active.name if self._active is not None else ""

    def probe(self) -> str:
        """Pick the first live provider.  Idempotent: a pass probes exactly once."""
        if self._probed:
            return self.active_name
        self._probed = True
        for provider in self.providers:
            if provider.probe():
                self._active = provider
                _log.info("eventmem decision: using %s", provider.name)
                return provider.name
            _log.warning(
                "eventmem decision: %s unavailable (%s)", provider.name,
                provider.last_error or "probe-failed",
            )
        _log.warning("eventmem decision: no backend available")
        return ""


# --------------------------------------------------------------------------- building

def _warn(warn: Optional[Callable[[str], None]], message: str) -> None:
    _log.warning("eventmem decision: %s", message)
    if warn is not None:
        warn(message)


def from_pack(pack: _packs.Pack, *, timeout: float = 900.0,
              probe_timeout: float = 2.0) -> Optional[UrlProvider]:
    """A pack becomes a provider when it has an address.

    A ``self`` pack has no ``base_url`` in its file: the daemon picks a port when it starts
    the process and records it in ``.runtime.json``.  So "is it running" and "where" are the
    same question, and a pack that is not up is simply not in the chain.
    """
    if pack.base_url:
        base_url = pack.base_url
    elif pack.kind == "self":
        state = _runtime.read_state(pack) or {}
        port = int(state.get("port") or 0)
        if not port or not _runtime.alive(state.get("pid")):
            _log.info("eventmem decision: pack %s 没在跑（daemon 还没启动它）", pack.name)
            return None
        base_url = f"http://127.0.0.1:{port}"
    else:
        return None
    return UrlProvider(Endpoint(
        name=pack.name,
        protocol=pack.protocol,
        base_url=base_url,
        model=pack.model,
        api_key=pack.api_key,
        api_key_env=pack.api_key_env,
        timeout=timeout,
        probe_timeout=probe_timeout,
        check_model=pack.check_model,
    ))


def from_settings(settings: Any, *, warn: Optional[Callable[[str], None]] = None,
                  avoid: Iterable[str] = ()) -> Chain:
    """Build the chain a pass should use.

    ``decision.backends`` wins when set (pack names, in order).  Otherwise the legacy single
    ``decision.url`` is wrapped as one provider, which is byte-for-byte what v2.1 did.

    ``avoid`` drops backends the caller has just seen fail: a backend can answer ``/health``
    and still refuse every decision (a model id the server lists but will not serve, a key
    with no quota).  Without this the chain would pick it again every round and never fall
    back, because "the port answers" is all a probe can know.
    """
    skipped = {str(name) for name in avoid}
    pol = settings.decision
    providers: list[UrlProvider] = []

    names = tuple(getattr(pol, "backends", ()) or ())
    if names:
        root = getattr(pol, "packs_dir", "") or ""
        found, problems = _packs.discover(root) if root else ([], [])
        for problem in problems:
            _warn(warn, problem)
        by_name = {pack.name: pack for pack in found}
        for name in names:
            if str(name) in skipped:
                _warn(warn, f"backend {name!r} 刚刚失败过，这一轮跳过它")
                continue
            pack = by_name.get(str(name))
            if pack is None:
                _warn(warn, f"decision.backends 里的 {name!r} 不是一个已装的 pack（{root}）")
                continue
            provider = from_pack(pack, timeout=float(pol.timeout),
                                 probe_timeout=float(getattr(pol, "probe_timeout", 2.0)))
            if provider is not None:
                providers.append(provider)
        if not providers:
            _warn(warn, "decision.backends 没有可用的后端；回落到 decision.url")

    if not providers and pol.url:
        providers.append(UrlProvider(Endpoint(
            name="default",
            protocol="systemone",
            base_url=str(pol.url),
            model=str(pol.model),
            path=str(pol.path),
            timeout=float(pol.timeout),
            probe_timeout=float(getattr(pol, "probe_timeout", 2.0)),
        )))

    return Chain(providers)


def describe(chain: Chain) -> dict[str, Any]:
    """Snapshot for ``--print-settings`` / the daemon's status page."""
    return {
        "active": chain.active_name,
        "providers": [
            {"name": p.name, "protocol": p.protocol, "url": p.endpoint.base_url,
             "model": p.endpoint.model, "has_key": bool(p.endpoint.key())}
            for p in chain.providers
        ],
    }


# Keep the import used so a future protocol addition fails loudly here rather than at runtime.
for _protocol in PROTOCOLS:  # pragma: no cover
    if _protocol not in PATH_BY_PROTOCOL:
        raise RuntimeError(f"decision: no endpoint path for protocol {_protocol!r}")
