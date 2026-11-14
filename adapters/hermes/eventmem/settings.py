"""eventmem / Hermes — where the knobs come from.

The core owns defaults; Hermes owns a config file.  This module is the only place that
knows both, so ``plugins.entries.eventmem.settings`` becomes a :class:`CoreSettings`
with one rule: **a bad value never takes memory down** — it is reported and the default
(or the previous value) stays.

    plugins:
      entries:
        eventmem:
          settings:
            edges:
              margin: 0.15          # top1 must beat top2 by this much to write an edge
            recall:
              budget_tokens: 600
            decision:
              url: http://127.0.0.1:18090

Both nested and dotted (``edges.margin``) spellings work; see
``docs/settings.md`` for the full list.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Optional

from eventmem_core.i18n import set_language_provider
from eventmem_core.settings import CoreSettings, apply, describe

logger = logging.getLogger(__name__)

__all__ = ["PLUGIN_ID", "DEFAULT_HOME", "db_path", "read_section", "build", "language",
           "install_language_provider", "describe", "settings_path", "read_overrides",
           "merge", "write_overrides", "FileStore"]

#: ``plugins.entries.<id>`` — the manifest name.  Nothing else in the adapter should
#: hardcode it.
PLUGIN_ID = "eventmem"
DEFAULT_HOME = Path.home() / ".hermes"
DB_SUBDIR = "eventmem"
DB_NAME = "events.db"

#: eventmem's own settings file: written by the GUI and by hand, read by the daemon and by
#: ``build``.  Never ``config.yaml`` — that one belongs to Hermes.
SETTINGS_FILE = "settings.yaml"


def hermes_home(home: Optional[str | Path] = None) -> Path:
    return Path(home) if home else Path(os.environ.get("HERMES_HOME") or DEFAULT_HOME)


def db_path(home: Optional[str | Path] = None) -> str:
    """``<hermes_home>/eventmem/events.db`` — the one place the layout is decided."""
    return str(hermes_home(home) / DB_SUBDIR / DB_NAME)


def _yaml():
    """PyYAML, pulling in the host runtime's site-packages first when needed.

    The worker runs under Hermes' launcher interpreter, which ships stdlib only;
    ``hermes_bootstrap`` is what injects the environment that has ``yaml``.  Importing it
    is a no-op inside a normal host process.
    """
    try:
        import yaml

        return yaml
    except Exception:  # noqa: BLE001
        pass
    try:
        import hermes_bootstrap  # noqa: F401  (injects the host's site-packages)
        import yaml

        return yaml
    except Exception:  # noqa: BLE001 - a truly bare interpreter
        return None


def _same_home(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def _load_config(home: Optional[str | Path] = None) -> dict:
    """The whole ``config.yaml`` as a dict; ``{}`` when it cannot be read."""
    path = hermes_home(home) / "config.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    yaml = _yaml()
    if yaml is not None:
        try:
            data = yaml.safe_load(text)
            return data if isinstance(data, dict) else {}
        except Exception as exc:  # noqa: BLE001
            logger.warning("eventmem: cannot parse %s (%s); using defaults", path, exc)
            return {}
    # No YAML anywhere.  The host can still parse its own file; only trust it for the
    # home it actually runs on.
    try:
        from hermes_cli.config import load_config_readonly

        if _same_home(hermes_home(home), hermes_home(None)):
            data = load_config_readonly()
            return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        pass
    logger.warning("eventmem: no YAML parser available; cannot read %s, using defaults", path)
    return {}


def read_section(home: Optional[str | Path] = None) -> dict:
    """The plugin's settings subtree from ``config.yaml`` (empty when unset)."""
    data = _load_config(hermes_home(home))
    entry = ((data.get("plugins") or {}).get("entries") or {}).get(PLUGIN_ID) or {}
    if not isinstance(entry, dict):
        return {}
    # ``settings`` is current; ``config`` is the pre-0.2 spelling, kept readable.
    section = entry.get("settings")
    if isinstance(section, dict):
        return section
    legacy = entry.get("config")
    return legacy if isinstance(legacy, dict) else {}


def _host_language() -> str:
    """``agent.i18n.get_language()`` when a host is around, else ``""``."""
    try:
        from agent.i18n import get_language

        return str(get_language() or "")
    except Exception:  # noqa: BLE001 - worker, nightly, tests
        return ""


def language(home: Optional[str | Path] = None) -> str:
    """The display language of *home* (or of the running host when ``home`` is None).

    An explicit home wins over the host on purpose: the worker and the nightly CLI run
    outside any session, and a profile's ``config.yaml`` is the only truth there.  A
    value of ``auto``/``""`` is not a language — then (and only then) ask the host to
    resolve it, because only the host knows what ``auto`` meant.
    """
    raw = ""
    if home is None:
        raw = _host_language()
        if raw:
            return raw
    data = _load_config(hermes_home(home))
    raw = str((data.get("display") or {}).get("language") or "").strip()
    if raw and raw.lower() not in ("auto", "detect"):
        return raw
    return _host_language()


def install_language_provider(home: Optional[str | Path] = None) -> None:
    """Point the core's renderer at the host language, re-read on every call."""
    base = hermes_home(home)

    def provider() -> str:
        return language(base)

    set_language_provider(provider)


def _models_dir(base: Path) -> str:
    """ONNX 向量模型所在目录：优先 eventmem 自己的，退回旧的 ``localmem/models``。

    2026-11-14 之前这里直接写死 ``localmem/models``——那意味着删 localmem 插件时会把
    eventmem 的编码器一起删掉，而且没有任何报错（``encode()`` 静默返回 None、召回退 FTS）。
    模型必须归 eventmem 所有；legacy 分支只为还没搬家的老部署留着。
    """
    own = Path(base) / "eventmem" / "models"
    legacy = Path(base) / "localmem" / "models"
    if own.is_dir():
        return str(own)
    return str(legacy if legacy.is_dir() else own)


def build(home: Optional[str | Path] = None, *, db: Optional[str] = None,
          section: Optional[dict] = None, warn=None,
          strict: bool = False) -> CoreSettings:
    """Read the config and hand back a fully-defaulted :class:`CoreSettings`.

    Three layers, later wins: the defaults in this module → ``eventmem/settings.yaml``
    (GUI and hand edits) → ``config.yaml``'s ``plugins.entries.eventmem.settings`` (the
    host's explicit wiring).  *strict* refuses the first bad value, for the GUI's writer.
    """
    base = hermes_home(home)
    from dataclasses import replace

    from eventmem_core.settings import DecisionPolicy

    # Deployment defaults the old worker hardcoded: the model service can be started by
    # the worker when it is down, and the URL can be overridden per machine.  There is no
    # default start script — every deployment keeps it somewhere else — and the pack chain
    # never uses it.  Both are overridable from config afterwards (``apply`` runs below).
    decision = replace(
        DecisionPolicy(),
        url=os.environ.get("EVENTMEM_JEV_URL") or DecisionPolicy().url,
        start_script=os.environ.get("EVENTMEM_START_SCRIPT", ""),
        # 一个文件夹一个后端；GUI/手写都落在这里，链的顺序由 settings 的 backends 决定。
        packs_dir=str(Path(base) / "eventmem" / "backends"),
    )
    settings = CoreSettings(
        db_path=str(db) if db else db_path(base),
        language=language(base),
        models_root=_models_dir(base),
        decision=decision,
    )
    overrides = read_overrides(base)
    if overrides:
        settings = apply(settings, overrides, warn=warn or _warn, strict=strict)
    raw = read_section(base) if section is None else section
    if raw:
        settings = apply(settings, raw, warn=warn or _warn, strict=strict)
    return settings


def _warn(message: str) -> None:
    logger.warning("eventmem: %s", message)


# --------------------------------------------------------------------------- settings.yaml

def settings_path(home: Optional[str | Path] = None) -> Path:
    """``<hermes_home>/eventmem/settings.yaml`` — eventmem's own file, not the host's."""
    return hermes_home(home) / DB_SUBDIR / SETTINGS_FILE


def read_overrides(home: Optional[str | Path] = None) -> dict:
    """The overrides on disk; ``{}`` when there is no file or it cannot be parsed."""
    path = settings_path(home)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    yaml = _yaml()
    if yaml is None:
        logger.warning("eventmem: 没有 YAML 解析器，忽略 %s", path)
        return {}
    try:
        data = yaml.safe_load(text)
    except Exception as exc:  # noqa: BLE001
        logger.warning("eventmem: %s 不是合法 YAML（%s），忽略这份文件", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def merge(base: Optional[dict], incoming: Optional[dict]) -> dict:
    """Deep-merge *incoming* over *base*.  A ``None`` value deletes a key.

    Deleting is how the GUI undoes a setting: ``{"edges": {"margin": null}}`` drops the
    override, and the default takes over again.
    """
    out = dict(base or {})
    for key, value in (incoming or {}).items():
        if value is None:
            out.pop(key, None)
        elif isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


_HEADER = "# eventmem 自己的设置：GUI 与手写读写同一份，改完 daemon 会重读，不用重启。\n"


def write_overrides(mapping: dict, home: Optional[str | Path] = None) -> Path:
    """Persist *mapping* as the whole ``settings.yaml`` (atomic write, comments preserved
    only in the header — a settings file has no hand comments to lose)."""
    path = settings_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    yaml = _yaml()
    if yaml is None:
        raise RuntimeError("写 settings.yaml 需要 PyYAML（pip install pyyaml）")
    text = yaml.safe_dump(dict(mapping), allow_unicode=True, sort_keys=False,
                          default_flow_style=False)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(_HEADER + text, encoding="utf-8")
    temp.replace(path)
    return path


class FileStore:
    """``settings.yaml`` as the daemon sees it — ``path`` / ``read()`` / ``write()``.

    ``write`` validates the *merged* result strictly and only then touches the file, so a
    typo from the GUI is answered with a sentence and leaves the disk alone.
    """

    def __init__(self, home: Optional[str | Path] = None, *, db: Optional[str] = None,
                 section: Optional[dict] = None, warn=None):
        self.home = hermes_home(home)
        self.db = db
        self.section = section
        self.warn = warn

    @property
    def path(self) -> str:
        return str(settings_path(self.home))

    def read(self) -> dict:
        return read_overrides(self.home)

    def write(self, mapping) -> CoreSettings:
        if not isinstance(mapping, dict):
            raise ValueError("要一个 JSON 对象")
        merged = merge(self.read(), mapping)
        clean = build(self.home, db=self.db, section={}, warn=None)
        apply(clean, merged, strict=True)      # raises ValueError; nothing written
        write_overrides(merged, self.home)
        logger.info("eventmem: settings.yaml updated (%s)", ", ".join(sorted(mapping)))
        return build(self.home, db=self.db, section=self.section, warn=self.warn)
