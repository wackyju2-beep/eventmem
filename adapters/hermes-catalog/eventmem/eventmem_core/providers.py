"""eventmem_core.providers — the two wire formats a decision model speaks.

Pure functions: no network, no host, no third-party import.  The caller builds a payload,
POSTs it, and hands the body back to :func:`parse_answers`; everything else (thresholds,
the two questions, writing edges) never learns which protocol was used.

Formats
-------
``systemone``
    TypeSafe's Jev / System One contract: ``POST /v1/systemone`` with ``state`` and a
    ``questions`` **object** keyed by question id.  Served by llama.cpp (upstream since
    the /v1/systemone merge), LM Studio ("Jev Compatibility"), and the hosted API.
    ``allow_none`` makes the server append its own "none of the above" option, returned
    as ``none_probability``. A server may renormalise the other options conditionally
    or return unconditional values; both shapes are accepted. Complete sums determine
    the scale when unambiguous, and near-overlap is rejected rather than guessed.

``openai-decisions``
    OpenAI's ``POST /v1/decisions`` (``gpt-6-luna``), also served locally by LM Studio
    1.1.8+.  ``input`` plus a ``questions`` **array** of named questions; each answer
    comes back in ``answers``, an array in question order.

    This schema has **no "none of the above" slot**, so :func:`build_payload` adds an
    explicit ``none`` choice.  Without it the runner-up would always be a real label and
    every edge would come out looser than the same pair judged over System One.

Both normalise to the shape the edge builder already consumes::

    {"choice": "改变" | None, "confidence": 0.77,
     "probabilities": {"重复": 0.1, ...}, "none": 0.017}
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
from typing import Any, Iterable

from .i18n import tr

__all__ = ["PROTOCOLS", "NONE_LABEL", "build_payload", "parse_answers"]

#: Wire formats we can speak.  ``DecisionPolicy.path`` is derived from this, not typed twice.
PROTOCOLS = ("systemone", "openai-decisions")

#: The extra choice we append for OpenAI Decisions.  Never a real label: it is stripped
#: from ``probabilities`` and reported as ``none``, exactly like ``none_probability``.
NONE_LABEL = "none"

#: The "none of the above" label we send as a **real option** when a question wants it
#: (the axis question does — not every backend implements ``allow_none``).  Same word as
#: :data:`eventmem_core.nightly.NONE_LABEL`.
NONE_OF_ABOVE = "都不是"


def _none_description() -> str:
    return tr(
        "以上都不成立：后一条与前一条不是这种关系",
        "None of the above: the two events are not related in this way",
    )


def _items(questions: Iterable[tuple[str, str, Mapping[str, str]]]) -> list[tuple[str, str, dict[str, str]]]:
    """Validate ``[(key, instructions, criteria)]`` once; return plain dicts."""
    out: list[tuple[str, str, dict[str, str]]] = []
    for key, text, criteria in questions:
        labels = dict(criteria)
        if not labels:
            raise ValueError(f"question {key!r} has no options")
        out.append((str(key), str(text), {str(k): str(v) for k, v in labels.items()}))
    return out


# --------------------------------------------------------------------------- build

def build_payload(
    protocol: str,
    model: str,
    state: str,
    questions: Iterable[tuple[str, str, Mapping[str, str]]],
) -> dict[str, Any]:
    """The request body for *protocol*.  Raises ``ValueError`` on a programming mistake.

    A bad *server* is a runtime concern (see :func:`parse_answers`); a bad protocol name or
    an impossible option count is ours and should fail loudly before any bytes are sent.
    """
    if protocol not in PROTOCOLS:
        raise ValueError(f"unknown protocol {protocol!r}; expected one of {PROTOCOLS}")
    items = _items(questions)

    if protocol == "systemone":
        return {
            "model": model,
            "state": str(state),
            "questions": {
                key: {
                    "type": "choice",
                    "instructions": text,
                    "criteria": criteria,
                    "allow_none": not ({NONE_OF_ABOVE, NONE_LABEL} & criteria.keys()),
                }
                for key, text, criteria in items
            },
        }

    payload_questions = []
    for key, text, criteria in items:
        choices = [{"value": label, "description": desc} for label, desc in criteria.items()]
        if NONE_OF_ABOVE not in criteria and NONE_LABEL not in criteria:
            # OpenAI Decisions always gets its own none option; a question that already
            # carries 「都不是」 as a real label would end up with two of them.
            choices.append({"value": NONE_LABEL, "description": _none_description()})
        if not 2 <= len(choices) <= 255:
            raise ValueError(
                f"question {key!r} offers {len(choices)} choices; OpenAI Decisions allows 2–255"
            )
        payload_questions.append(
            {"type": "choice", "name": key, "instructions": text, "choices": choices}
        )
    return {"model": model, "input": str(state), "questions": payload_questions}


# --------------------------------------------------------------------------- parse

def parse_answers(
    protocol: str,
    data: Any,
    questions: Iterable[tuple[str, str, Mapping[str, str]]],
) -> list[dict[str, Any] | None] | None:
    """Normalise one response; ``None`` means "this whole call cannot be trusted".

    A single malformed answer becomes ``None`` **in its own slot** — one bad question must
    not throw away the other edge of the pair.
    """
    if protocol not in PROTOCOLS:
        raise ValueError(f"unknown protocol {protocol!r}; expected one of {PROTOCOLS}")
    items = _items(questions)
    if not items:
        return []
    if not isinstance(data, Mapping):
        return None

    answers = data.get("answers")
    if protocol == "systemone":
        if not isinstance(answers, Mapping):
            return None
        return [_parse_systemone(answers.get(key), tuple(criteria)) for key, _t, criteria in items]

    if not isinstance(answers, Sequence) or isinstance(answers, (str, bytes)):
        return None
    by_name: dict[str, Mapping] = {}
    for entry in answers:
        if isinstance(entry, Mapping) and entry.get("name") is not None:
            by_name.setdefault(str(entry["name"]), entry)
    return [_parse_decisions(by_name.get(key), tuple(criteria)) for key, _t, criteria in items]


def _clamp01(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not 0.0 <= number <= 1.0:
        return None
    return number


def _answer(choice: str | None, confidence: Any, probabilities: dict[str, float],
            none: float) -> dict[str, Any] | None:
    if confidence is None:
        conf = probabilities.get(choice or "", 0.0)
    else:
        conf = _clamp01(confidence)
        if conf is None:
            return None  # present but unusable: trust nothing about this answer
    return {
        "choice": choice,
        "confidence": conf,
        "probabilities": probabilities,
        "none": none,
    }


def _parse_systemone(answer: Any, labels: tuple[str, ...]) -> dict[str, Any] | None:
    """``{"choice": …, "confidence": …, "probabilities": {…}, "none_probability": …}``."""
    if not isinstance(answer, Mapping) or "choice" not in answer:
        return None
    raw = answer.get("choice")
    if raw is None:
        choice: str | None = None
    else:
        choice = str(raw)
        if choice not in labels:
            return None

    probabilities: dict[str, float] = {}
    raw_probs = answer.get("probabilities")
    if not isinstance(raw_probs, Mapping) or set(raw_probs) != set(labels):
        return None
    if isinstance(raw_probs, Mapping):
        for label in labels:
            prob = _clamp01(raw_probs.get(label))
            if prob is not None:
                probabilities[label] = prob

    if len(probabilities) != len(labels):
        return None

    none = _clamp01(answer.get("none_probability", answer.get("none")))
    if none is None:
        # A server without allow_none (LM Studio) omits none_probability.  When the
        # "none of the above" option is one of our own labels (「都不是」 or its English
        # spelling) its probability is already inside ``probabilities``, so the missing
        # field is not a defect — fall back to 0.0 instead of rejecting.
        if NONE_OF_ABOVE not in labels and NONE_LABEL not in labels:
            return None
        none = 0.0
    result = _answer(choice, answer.get("confidence"), probabilities, none)
    if result is not None:
        scale = probability_scale(probabilities.values(), none)
        if scale is None:
            return None
        result["probability_scale"] = scale
    return result


def probability_scale(real, none: float, tolerance: float = 1e-6) -> str | None:
    """Classify complete distributions; overlapping nonzero cases are ambiguous."""
    total = math.fsum(real)
    conditional = abs(total - 1.0) <= tolerance
    unconditional = abs(total + none - 1.0) <= tolerance
    if conditional and unconditional:
        return "unconditional" if none == 0.0 else None
    if conditional:
        return "conditional"
    if unconditional:
        return "unconditional"
    return None


def _parse_decisions(answer: Any, labels: tuple[str, ...]) -> dict[str, Any] | None:
    """``{"type": "choice", "name": …, "choice": …, "confidence": …, "probabilities": [{…}]}``."""
    if not isinstance(answer, Mapping):
        return None
    if str(answer.get("type") or "") == "refusal":
        return None
    if "choice" not in answer:
        return None  # a predicate/score answer where we asked for a choice
    raw = answer.get("choice")
    if raw is not None and not isinstance(raw, str):
        return None  # bool/int values can never be one of our labels
    if raw == NONE_LABEL and NONE_LABEL not in labels:
        choice: str | None = None
    else:
        choice = raw
        if choice is not None and choice not in labels:
            return None

    probabilities: dict[str, float] = {}
    none = None
    raw_probs = answer.get("probabilities")
    if isinstance(raw_probs, Sequence) and not isinstance(raw_probs, (str, bytes)):
        seen = set()
        for entry in raw_probs:
            if not isinstance(entry, Mapping):
                return None
            value = entry.get("value")
            if value not in (*labels, NONE_LABEL) or value in seen:
                return None
            seen.add(value)
            prob = _clamp01(entry.get("probability"))
            if prob is None:
                continue
            if value in labels:
                probabilities[value] = prob
            elif value == NONE_LABEL:
                none = prob
    if none is None and (NONE_OF_ABOVE in labels or NONE_LABEL in labels):
        none = 0.0
    if none is None or len(probabilities) != len(labels):
        return None
    if abs(math.fsum(probabilities.values()) + none - 1.0) > 1e-6:
        return None
    return _answer(choice, answer.get("confidence"), probabilities, none)
