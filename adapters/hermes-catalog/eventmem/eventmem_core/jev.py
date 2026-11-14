"""eventmem — local decision-model client (Jev / LEV ``systemone``).

The nightly job (:mod:`eventmem.nightly`) asks a small local decision model a
handful of yes/no questions about pairs of events.  This module is the only
place that talks to that service.

Default is a **no-op**: ``JevClient()`` has no ``base_url``, so
:meth:`JevClient.available` is ``False`` and :meth:`JevClient.decide` returns
``None`` without doing anything.  ``nightly.build_edges`` then skips the night
instead of guessing.

Wire format (llama.cpp LEV / LM Studio "systemone")::

    POST {base_url}/v1/systemone
    {"model": "jev",
     "state": "…",
     "questions": {"q0": {"type": "noul", "instructions": "…",
                          "criteria": {"true": "是", "false": "否"}}}}

    -> {"answers": {"q0": {"noul": 0.93}}}

``noul`` is the model's expected value for "yes" on a 0..1 scale.  Some
servers call the field ``probability``; both shapes are accepted.

Failure policy: any exception, timeout, HTTP error or malformed body makes
:meth:`JevClient.decide` return ``None``.  No retries, nothing escapes, and no
request or response content is ever logged — event text may be private.
"""

from __future__ import annotations

import json
import logging
import urllib.request

__all__ = ["JevClient", "DEFAULT_PATH", "NOUL_CRITERIA"]

_log = logging.getLogger(__name__)

DEFAULT_PATH = "/v1/systemone"
NOUL_CRITERIA = ("是", "否")

# Accept both the LEV field name and the generic one, newest first.
_PROB_FIELDS = ("probability", "noul", "prob", "p", "probability_true", "true")


class JevClient:
    """Thin, fail-silent HTTP client for a local decision model.

    Parameters
    ----------
    base_url:
        Root URL of the server, e.g. ``"http://127.0.0.1:1234"``.  ``None``
        (the default) disables the client entirely.
    model:
        Model id sent in the request body.  Ignored by most local servers.
    timeout:
        Per-request timeout in seconds.  A hung server must never hold the
        nightly job open indefinitely.
    path:
        Decision endpoint path; ``/v1/systemone`` for LEV, or
        ``/v1/decisions`` for servers that expose the generic name.
    criteria:
        ``(true_label, false_label)`` descriptions attached to every ``noul``
        question.  LEV needs them to render the prompt.
    think_mode / gate / think_budget:
        AutoThink-only controls.  They default to ``None`` (nothing is sent):
        the Instant model answers in one pass and the server rejects a request
        that asks it to think (``auto``/``always`` → 422).  Set them only when
        pointing at an AutoThink endpoint.
    """

    def __init__(
        self,
        base_url: str | None = None,
        model: str = "jev",
        timeout: float = 120.0,
        *,
        path: str = DEFAULT_PATH,
        criteria: tuple[str, str] = NOUL_CRITERIA,
        think_mode: str | None = None,
        gate: float | None = None,
        think_budget: int | None = None,
    ):
        if isinstance(base_url, str):
            base_url = base_url.strip().rstrip("/") or None
        else:
            base_url = None
        self.base_url = base_url
        self.model = model
        try:
            self.timeout = float(timeout)
        except (TypeError, ValueError):
            self.timeout = 5.0
        self.path = path if path.startswith("/") else "/" + path
        self.criteria = criteria
        # AutoThink controls.  Thinking is a token-by-token generation, which is the expensive
        # part on CPU (a few tokens/s for a 4B Q8); 256 tokens keeps "unsure" questions short
        # without turning the nightly job into an all-night generation run.
        self.think_mode = think_mode if think_mode in ("auto", "never", "always") else None
        self.gate = gate
        self.think_budget = think_budget

    # ---------------------------------------------------------------- status

    def available(self) -> bool:
        """True only when a base URL is configured.

        Deliberately cheap: it does not probe the server.  An unreachable
        server is handled by :meth:`decide` returning ``None``, which makes the
        nightly job stop after a few consecutive failures.
        """
        return self.base_url is not None

    # ---------------------------------------------------------------- decide

    def decide(
        self, state: str, questions: list[tuple[str, str]]
    ) -> list[float] | None:
        """Ask each question about ``state``; return the yes-probability per question.

        ``questions`` is ``[(key, question_text)]``.  The returned list is in
        the same order as ``questions``.  Returns ``None`` on any failure
        (disabled client, network error, timeout, non-JSON body, missing or
        out-of-range answer).
        """
        if self.base_url is None:
            return None
        if questions is None:
            return None
        try:
            items = [(str(key), str(text)) for key, text in questions]
        except (TypeError, ValueError):
            return None
        if not items:
            return []

        true_label, false_label = self.criteria
        payload = {
            "model": self.model,
            "state": str(state),
            "questions": {
                key: {
                    "type": "noul",
                    "instructions": text,
                    "criteria": {"true": true_label, "false": false_label},
                }
                for key, text in items
            },
        }
        self._apply_controls(payload)

        try:
            data = self._post(payload)
        except Exception as exc:  # noqa: BLE001 - fail silent by contract
            _log.debug("jev request failed: %s", type(exc).__name__)
            return None

        try:
            answers = data.get("answers") if isinstance(data, dict) else None
            if not isinstance(answers, dict) or len(answers) < len(items):
                return None
            out: list[float] = []
            for key, _text in items:
                prob = _probability(answers.get(key))
                if prob is None:
                    return None
                out.append(prob)
            return out
        except Exception as exc:  # noqa: BLE001
            _log.debug("jev response rejected: %s", type(exc).__name__)
            return None

    # ---------------------------------------------------------------- decide (choice)

    @staticmethod
    def _allow_none(criteria: dict[str, str]) -> bool:
        """Ask for the server's implicit none only if we did not send one ourselves."""
        from .providers import NONE_LABEL, NONE_OF_ABOVE

        return NONE_OF_ABOVE not in criteria and NONE_LABEL not in criteria

    def decide_choice(
        self, state: str, questions: list[tuple[str, str, dict[str, str]]]
    ) -> list[dict | None] | None:
        """Ask each question as a typed ``choice`` with an implicit "none of the above".

        ``questions`` is ``[(key, instructions, {label: description})]``.  The server
        appends its own "None of the above" option (``allow_none``) and reports it as
        ``none_probability``; it is never one of our labels.

        Returns one dict per question::

            {"choice": "改变" | None, "confidence": 0.77,
             "probabilities": {"重复": 0.004, ...}, "none": 0.017}

        ``choice`` is ``None`` when the server's none option won.  A malformed single
        answer becomes ``None`` in its slot; a transport/shape failure returns ``None``
        for the whole call.  Never raises, never logs event text.
        """
        if self.base_url is None or questions is None:
            return None
        try:
            items = [(str(key), str(text), dict(criteria)) for key, text, criteria in questions]
        except (TypeError, ValueError):
            return None
        if not items:
            return []
        if any(not criteria for _key, _text, criteria in items):
            return None

        payload = {
            "model": self.model,
            "state": str(state),
            "questions": {
                key: {
                    "type": "choice",
                    "instructions": text,
                    "criteria": criteria,
                    # A question that already offers 「都不是」 as a real label must not also
                    # get the server's implicit none: two "none" options split the mass and
                    # the backends stop agreeing on what the answer means.
                    "allow_none": self._allow_none(criteria),
                }
                for key, text, criteria in items
            },
        }
        self._apply_controls(payload)

        try:
            data = self._post(payload)
        except Exception as exc:  # noqa: BLE001 - fail silent by contract
            _log.debug("jev choice request failed: %s", type(exc).__name__)
            return None

        try:
            answers = data.get("answers") if isinstance(data, dict) else None
            if not isinstance(answers, dict):
                return None
            out: list[dict | None] = []
            for key, _text, criteria in items:
                out.append(_choice_answer(answers.get(key), tuple(criteria)))
            return out
        except Exception as exc:  # noqa: BLE001
            _log.debug("jev choice response rejected: %s", type(exc).__name__)
            return None

    # ---------------------------------------------------------------- wire

    def _apply_controls(self, payload: dict) -> None:
        """Add the AutoThink controls that are set; leave the rest to the server."""
        if self.think_mode:
            payload["think_mode"] = self.think_mode
        try:
            if self.gate is not None:
                payload["gate"] = float(self.gate)
        except (TypeError, ValueError):
            pass
        try:
            if self.think_budget is not None:
                payload["think_budget"] = max(1, int(self.think_budget))
        except (TypeError, ValueError):
            pass

    def _post(self, payload: dict) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.base_url + self.path,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read()
        return json.loads(raw.decode("utf-8"))


def _choice_answer(answer, labels: tuple[str, ...]) -> dict | None:
    """Parse one ``choice`` answer; ``None`` when its shape cannot be trusted.

    Accepts ``{"choice": "改变", "confidence": 0.77, "probabilities": {...},
    "none_probability": 0.017}``.  ``choice`` may be ``null`` (the server's none
    option won).  A label outside ``labels`` is malformed, not a different model.
    """
    from .providers import _parse_systemone
    return _parse_systemone(answer, labels)


def _probability(answer) -> float | None:
    """Extract a yes-probability from one ``answers[key]`` value.

    Accepts ``{"noul": 0.93}``, ``{"probability": 0.93}``, a bare number, or a
    boolean.  Anything outside [0, 1] is treated as a malformed response.
    """
    value = None
    if isinstance(answer, bool):
        return 1.0 if answer else 0.0
    if isinstance(answer, (int, float)):
        value = float(answer)
    elif isinstance(answer, dict):
        for field in _PROB_FIELDS:
            if field not in answer:
                continue
            raw = answer[field]
            if isinstance(raw, bool):
                return 1.0 if raw else 0.0
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            break
    if value is None:
        return None
    if not (-1e-6 <= value <= 1.0 + 1e-6):
        return None
    return min(1.0, max(0.0, value))
