"""eventmem_core.i18n — the language of everything the model reads.

The core never imports a host.  Resolution order:

1. ``EVENTMEM_LANG`` — explicit override, also the way to pin a language in tests and in a
   standalone worker that runs outside any host.
2. a provider registered by the adapter (:func:`set_language_provider`) — the Hermes adapter
   answers with ``display.language`` resolved through the host, per profile and per call.
3. ``display.language`` in ``$HERMES_HOME/config.yaml`` — for the worker/nightly CLI, which talk
   to the database with no host around them.
4. ``en``, which is also Hermes' own default.

Only two catalogs exist: ``zh`` (anything starting with ``zh``, including ``zh-hant``) and ``en``;
everything else falls back to English. Call sites use :func:`tr` with both strings side by side, so a
missing translation is a syntax error rather than a silent hole.

What is *not* translated: values stored in the database. Kinds and relations stay
``决定/尝试/结果/变更/教训`` and ``改变/否定/重复/因为/细化`` in every language — they are the schema, and
the decision model's answer labels are mapped back to them (see :mod:`eventmem.nightly`).
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["language", "is_zh", "tr", "kind_label", "rel_label",
           "set_language_provider"]

ZH = "zh"
EN = "en"

#: Display-only names. The stored values never change; these are for the recall block and errors.
_KIND_LABELS = {
    "决定": "decision", "尝试": "attempt", "结果": "result", "变更": "change", "教训": "lesson",
}
_REL_LABELS = {
    "改变": "changes", "否定": "negates", "重复": "repeats", "因为": "because", "细化": "refines",
}


_ZH_ALIASES = {"zh", "chinese", "mandarin", "中文", "简体", "繁體", "繁体"}
_EN_ALIASES = {"en", "english", "英文"}


def _normalize(value: object) -> str:
    text = str(value or "").strip().lower().replace("_", "-")
    if text in _ZH_ALIASES or text.startswith("zh-"):
        return ZH
    if text in _EN_ALIASES or text.startswith("en-"):
        return EN
    return "" if not text else EN


def _config_language() -> str:
    """``display.language`` from the Hermes config, without importing the host."""
    home = os.environ.get("HERMES_HOME") or str(Path.home() / ".hermes")
    path = Path(home) / "config.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    try:  # the host ships PyYAML; a bare interpreter may not
        import yaml  # noqa: PLC0415

        data = yaml.safe_load(text) or {}
        if isinstance(data, dict):
            return str((data.get("display") or {}).get("language") or "")
        return ""
    except Exception:  # noqa: BLE001 - fall through to the line scan
        pass
    in_display = False
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        if not raw.startswith((" ", "\t")):
            in_display = raw.split(":", 1)[0].strip() == "display"
            continue
        if in_display:
            key, _, value = raw.strip().partition(":")
            if key == "language":
                return value.strip().strip("'\"")
    return ""


#: The adapter's answer to "what language is the host in right now?".  A function, not a
#: value, because one process can serve several profiles and the setting can change
#: mid-session; the adapter keeps it cheap (it is consulted on every ``tr`` call).
_PROVIDER = None


def set_language_provider(provider) -> None:
    """Register (or clear, with ``None``) the adapter's language resolver."""
    global _PROVIDER
    _PROVIDER = provider


def language() -> str:
    """The active language id: ``"zh"`` or ``"en"``."""
    forced = _normalize(os.environ.get("EVENTMEM_LANG"))
    if forced:
        return forced
    if _PROVIDER is not None:
        try:
            resolved = _normalize(_PROVIDER())
        except Exception:  # noqa: BLE001 - a broken provider must not break rendering
            resolved = ""
        if resolved:
            return resolved
    return _normalize(_config_language()) or EN


def is_zh() -> bool:
    return language() == ZH


def tr(zh: str, en: str) -> str:
    """Pick the string for the active language."""
    return zh if is_zh() else en


def kind_label(kind: str) -> str:
    """Display name for a stored kind (``变更`` → ``change`` under ``en``)."""
    if is_zh():
        return kind
    return _KIND_LABELS.get(kind, kind)


def rel_label(rel: str) -> str:
    """Display name for a stored relation."""
    if is_zh():
        return rel
    return _REL_LABELS.get(rel, rel)
