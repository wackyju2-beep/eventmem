"""eventmem_core.settings — every knob the memory logic exposes, and nothing else.

The core owns the *values*; a host adapter owns *where they come from*.  Nothing in
this module reads a config file, an environment variable or a host module: an adapter
builds a :class:`CoreSettings` (usually via :func:`apply`) and hands it to
:class:`eventmem_core.core.EventMem`.

Defaults are the values the project shipped with before the core/adapter split — an
adapter that sets nothing must behave byte-for-byte like the old plugin.

Only knobs that were actually constants inside the core live here.  Thresholds that are
part of the design contract (the relation vocabulary, the labels stored in the database)
stay module constants: changing them is a schema change, not a tuning knob.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Mapping, Optional

__all__ = [
    "KINDS",
    "RELS",
    "EdgePolicy",
    "DecisionPolicy",
    "RecallPolicy",
    "CoreSettings",
    "DEFAULT_SETTINGS",
    "apply",
    "coerce",
]

#: Stored values.  Never translated, never configurable: they are the schema.
KINDS = ("决定", "尝试", "结果", "变更", "教训")
RELS = ("改变", "否定", "重复", "因为", "细化")


@dataclass(frozen=True)
class EdgePolicy:
    """How the nightly pass picks candidates and decides whether an edge is written."""

    #: A real label wins only if it beats the runner-up (「都不是」 included) by this much
    #: on the raw distribution.  The knob the "top1 vs top2" question is about.
    margin: float = 0.15
    min_conf_change: float = 0.0
    min_conf_negate: float = 0.0
    min_conf_repeat: float = 0.0
    min_conf_because: float = 0.0
    min_conf_refine: float = 0.0
    #: Q1 — global active heads by vector: minimum cosine and at most this many asked.
    heads_cos_min: float = 0.35
    heads_vec_top_k: int = 10
    #: Bridged heads: the same session's most recent heads.
    session_heads_top_k: int = 10
    #: Tier 3 — cross-project pairs, both ends in ``cross_kinds``, stricter cosine, tiny top-k.
    cross_kinds: tuple[str, ...] = ("变更", "决定")
    cross_cos_min: float = 0.7
    cross_top_k: int = 3
    #: Q2 sets: same session's recent events / open lessons / the decision skeleton.
    session_recent: int = 3
    unclosed_lessons: int = 2
    decision_skeleton: int = 5
    #: Stop the pass after this many consecutive decision calls fail.
    max_fail_streak: int = 3
    #: Resident worker: seconds between polls, and the first-run look-back window
    #: used when the database has no cursor yet.
    poll_seconds: float = 60.0
    first_run_hours: float = 24.0


@dataclass(frozen=True)
class DecisionPolicy:
    """Where the nightly relation model lives and how long we wait for it."""

    #: Root URL of the decision service; empty string disables edge building entirely.
    url: str = "http://127.0.0.1:18090"
    #: Decision endpoint path: ``/v1/systemone`` for LEV-compatible servers.
    path: str = "/v1/systemone"
    model: str = "jev"
    #: Per-request timeout (a full thought, not a token).
    timeout: float = 900.0
    #: AutoThink-only control; empty means "send nothing" (Instant models reject it).
    think_mode: str = ""
    #: Optional start script for the service, used by the resident worker.
    start_script: str = ""
    #: Ordered backend pack names under ``packs_dir``.  Empty = the single ``url`` above,
    #: which is exactly what v2.1 did.  The chain is probed once per pass: the first live
    #: pack answers the whole pass, and a mid-pass failure aborts instead of switching
    #: models (two models give two distributions; mixed edges cannot be recalibrated).
    backends: tuple[str, ...] = ()
    #: Directory of backend packs; empty = "the adapter did not say".
    packs_dir: str = ""
    #: Liveness probe timeout.  A probe costs one HTTP GET and no tokens.
    probe_timeout: float = 2.0


@dataclass(frozen=True)
class RecallPolicy:
    """How much context a recall injects and how it is fused/expanded."""

    budget_tokens: int = 600
    skeleton_budget: int = 850
    skeleton_max: int = 5
    top_k: int = 20
    max_items: int = 12
    min_score: float = 0.35
    rrf_k: int = 60
    chain_max_depth: int = 1
    repeat_max: int = 8
    neighbour_rels: tuple[str, ...] = ("改变", "否定", "因为")
    neighbour_cap: int = 30
    neighbour_depth_max: int = 3
    project_hint_limit: int = 5


@dataclass(frozen=True)
class CoreSettings:
    """Everything one core instance needs.  Frozen: replace it, never mutate it."""

    #: events.db path.  Empty means "the adapter did not say" and the core refuses to open.
    db_path: str = ""
    #: ``zh`` / ``en`` / ``""`` (auto-detect).  Adapters normally pass the host language.
    language: str = ""
    #: Hard cap on one ``evt_add`` call; the tool schema advertises the same number.
    max_events_per_call: int = 3
    #: Directory holding the embedding model (the vector arm).  Empty = the legacy
    #: ``$HERMES_HOME/localmem/models`` (or ``$EVENTMEM_MODELS_ROOT`` when set).
    models_root: str = ""
    recall: RecallPolicy = field(default_factory=RecallPolicy)
    edges: EdgePolicy = field(default_factory=EdgePolicy)
    decision: DecisionPolicy = field(default_factory=DecisionPolicy)


#: The pre-split defaults, for parity tests and "everything unset" adapters.
DEFAULT_SETTINGS = CoreSettings()


# --------------------------------------------------------------------------- coercion

def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")


def _as_float(value: Any) -> float:
    return float(value)


def _as_int(value: Any) -> int:
    return int(float(value))


def _as_str(value: Any) -> str:
    return str(value)


def _as_rel_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        parts = [p for p in value.replace("/", ",").replace(" ", ",").split(",") if p]
    else:
        parts = [str(p) for p in value]
    return tuple(parts)


_COERCERS = {
    float: _as_float,
    int: _as_int,
    str: _as_str,
    bool: _as_bool,
    tuple[str, ...]: _as_rel_tuple,
}


def _field_types(cls: type) -> dict[str, Any]:
    """Resolved annotation per field (``from __future__ import annotations`` makes them strings)."""
    from typing import get_type_hints

    hints = get_type_hints(cls)
    return {f.name: hints.get(f.name, f.type) for f in fields(cls)}


def coerce(target: Any, value: Any) -> Any:
    """Coerce *value* to *target*; raises ``TypeError``/``ValueError`` on garbage."""
    for typ, fn in _COERCERS.items():
        if target == typ:
            return fn(value)
    return value



# --------------------------------------------------------------------------- validation

def _validate(policy: Any, name: str, warn) -> None:
    """Range checks that catch typos, not taste.  A bad value falls back to the default."""
    if isinstance(policy, EdgePolicy):
        if not 0.0 <= policy.margin <= 1.0:
            warn(f"{name}.margin={policy.margin} 不在 0–1；用默认值")
            return False
        for key in ('min_conf_change', 'min_conf_negate', 'min_conf_repeat',
                    'min_conf_because', 'min_conf_refine'):
            if not 0.0 <= getattr(policy, key) <= 1.0:
                warn(f'{name}.{key} must be between 0 and 1')
                return False
        if policy.heads_vec_top_k < 1 or policy.session_heads_top_k < 1 or policy.cross_top_k < 1:
            warn(f"{name}.*_top_k 必须 ≥1；用默认值")
            return False
        bad = [k for k in policy.cross_kinds if k not in KINDS]
        if bad:
            warn(f"{name}.cross_kinds 含非法 kind {bad}；用默认值")
            return False
        if policy.max_fail_streak < 1:
            warn(f"{name}.max_fail_streak 必须 ≥1；用默认值")
            return False
    if isinstance(policy, RecallPolicy):
        if policy.budget_tokens < 100 or policy.skeleton_budget < 100:
            warn(f"{name}.*_budget 必须 ≥100；用默认值")
            return False
        if policy.rrf_k < 1 or policy.max_items < 1 or policy.top_k < 1:
            warn(f"{name}.* 计数必须 ≥1；用默认值")
            return False
        bad = [r for r in policy.neighbour_rels if r not in RELS]
        if bad:
            warn(f"{name}.neighbour_rels 含非法 rel {bad}；用默认值")
            return False
    if isinstance(policy, DecisionPolicy):
        if policy.timeout <= 0 or policy.probe_timeout <= 0:
            warn(f"{name}: timeout/probe_timeout 必须 > 0；用默认值")
            return False
        if any(not str(name).strip() for name in policy.backends):
            warn(f"{name}.backends 里有空名字；用默认值")
            return False
    return True


# --------------------------------------------------------------------------- mapping

def _flatten(mapping: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """``{"edges": {"margin": 0.2}}`` and ``{"edges.margin": 0.2}`` mean the same thing."""
    flat: dict[str, Any] = {}
    for key, value in mapping.items():
        path = f"{prefix}{key}"
        if isinstance(value, Mapping):
            flat.update(_flatten(value, prefix=f"{path}."))
        else:
            flat[path] = value
    return flat


_POLICY_FIELDS = {
    "recall": RecallPolicy,
    "edges": EdgePolicy,
    "decision": DecisionPolicy,
}


def _split(path: str) -> tuple[Optional[str], str]:
    head, _, rest = path.partition(".")
    if head in _POLICY_FIELDS and rest:
        return head, rest
    return None, path


def apply(base: CoreSettings, mapping: Mapping[str, Any], *, warn=None,
          strict: bool = False) -> CoreSettings:
    """Return ``base`` with every recognised key from *mapping* applied.

    Unknown keys and values that fail to coerce are reported through *warn* (a callable
    taking one string) and left at their previous value — a typo in a config file must
    never take the memory down.  Returns a new :class:`CoreSettings`; *base* is untouched.

    *strict* turns the first complaint into a :class:`ValueError` instead of a warning.
    The GUI writes with ``strict=True`` so a rejected value is never persisted; the read
    paths stay lenient.
    """
    if not mapping:
        return base
    problems: list[str] = []

    def emit(message: str) -> None:
        problems.append(message)
        if warn is not None:
            warn(message)

    top: dict[str, Any] = {}
    policies: dict[str, dict[str, Any]] = {name: {} for name in _POLICY_FIELDS}
    for path, value in _flatten(mapping).items():
        section, leaf = _split(path)
        if section is None:
            top[leaf] = value
        else:
            policies[section][leaf] = value

    replaced: dict[str, Any] = {}
    for name, values in policies.items():
        if not values:
            continue
        cls = _POLICY_FIELDS[name]
        types = _field_types(cls)
        current = getattr(base, name)
        changes: dict[str, Any] = {}
        for key, value in values.items():
            if key not in types:
                emit(f"未知配置项 {name}.{key}（已忽略）")
                continue
            try:
                changes[key] = coerce(types[key], value)
            except (TypeError, ValueError) as exc:
                emit(f"{name}.{key}={value!r} 无法解析（{exc}）；用默认值")
                continue
        if changes:
            candidate = cls(**{**{f.name: getattr(current, f.name) for f in fields(cls)}, **changes})
            if _validate(candidate, name, emit):
                replaced[name] = candidate

    top_types = _field_types(CoreSettings)
    top_changes: dict[str, Any] = {}
    for key, value in top.items():
        if key not in top_types or key in _POLICY_FIELDS:
            emit(f"未知配置项 {key}（已忽略）")
            continue
        try:
            top_changes[key] = coerce(top_types[key], value)
        except (TypeError, ValueError) as exc:
            emit(f"{key}={value!r} 无法解析（{exc}）；用默认值")


    if strict and problems:
        raise ValueError(problems[0])
    if not replaced and not top_changes:
        return base
    return _replace(base, **{**top_changes, **replaced})


def _replace(settings: CoreSettings, **changes: Any) -> CoreSettings:
    values = {f.name: getattr(settings, f.name) for f in fields(CoreSettings)}
    values.update(changes)
    return CoreSettings(**values)


def describe(settings: CoreSettings) -> dict[str, Any]:
    """Plain-dict snapshot (logs, diagnostics, ``--print-settings``)."""
    out: dict[str, Any] = {}
    for f in fields(CoreSettings):
        value = getattr(settings, f.name)
        if is_dataclass(value):
            out[f.name] = {g.name: getattr(value, g.name) for g in fields(value)}
        else:
            out[f.name] = value
    return out
