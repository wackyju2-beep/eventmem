#!/usr/bin/env python3
"""Generate the provider-only Hermes catalog copy from canonical sources.

The copy has its own relative core namespace and does not include worker, daemon,
model installer, or GUI. Run with --check in CI; run without it to regenerate.
The catalog manifest and any other adapter-level files are maintained separately.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADAPTER = ROOT / "adapters" / "hermes" / "eventmem"
CORE = ROOT / "core" / "eventmem_core"
DEST = ROOT / "adapters" / "hermes-catalog" / "eventmem"

ADAPTER_FILES = ("__init__.py", "extract.py", "host.py", "provider.py", "settings.py", "tools.py")
CORE_FILES = ("__init__.py", "core.py", "embed.py", "i18n.py", "jev.py", "nightly.py",
              "providers.py", "recall.py", "schema.py", "settings.py", "store.py")


def _adapter_content(name: str) -> bytes:
    text = (ADAPTER / name).read_text(encoding="utf-8")
    if name == "__init__.py":
        start = text.index("#: When this package is a symlink")
        end = text.index("from eventmem_core.i18n import tr", start)
        text = text[:start] + text[end:]
        text = text.replace("import sys\n", "").replace("from pathlib import Path\n", "")
        start = text.index('from .middleware import (')
        end = text.index('\n)', start) + 2
        text = text[:start] + text[end:]
        for exported in ('    "MEMORY_HINT_EN",\n', '    "MEMORY_HINT_ZH",\n',
                         '    "is_state_changing",\n', '    "memory_hint",\n', '    "on_tool_result",\n'):
            text = text.replace(exported, '')
        text = text.replace('installs the memory provider, the tool-result middleware and the\n  auxiliary extraction task;', 'installs the memory provider;')
        text = text.replace('* ``middleware`` — "this changed state, write it down" after Hermes tool calls;\n', '')
        text = text.replace('"""Install the provider, the tool-execution middleware and the auxiliary task."""', '"""Install the memory provider without process-wide registrations."""')
        text = text.replace('* ``cli`` — the nightly pass and the resident worker, with the same settings.\n', '')
        text = text.replace(
            '# ``EventMemProvider`` is imported lazily: it subclasses ``agent.memory_provider``, and\n'
            '# the worker/nightly CLI (no host around) must be able to ``import eventmem.cli``.',
            '# ``EventMemProvider`` is imported lazily because it subclasses a Hermes host class.',
        )
        start = text.index('    register_middleware = getattr(ctx, "register_middleware", None)')
        text = text[:start] + (
            '    # Memory-provider discovery also calls register(), even when the provider is\n'
            '    # not active. Avoid registering process-wide middleware or auxiliary tasks\n'
            '    # here; repeated discovery would leak and duplicate those registrations.\n'
        )
        text = text.replace(
            "The memory logic itself (schema, store, recall, encoder, edges) lives in ``core/`` at the\n"
            "repository root and is imported as ``eventmem_core``; nothing here re-implements it.\n"
            "A second adapter (another agent harness) imports the same core and brings its own copy\n"
            "of the six modules above.",
            "This catalog copy includes the provider-only canonical core in a private namespace.\n"
            "The worker, daemon, model installer and GUI are not included in this copy.",
        )
    if name == "settings.py":
        start = text.index('def install_language_provider(')
        end = text.index('\n\ndef _models_dir(', start)
        text = (text[:start] +
                'def install_language_provider(home: Optional[str | Path] = None) -> None:\n'
                '    """Resolve the active Hermes profile at each call, never capture one home.\n\n'
                '    Hermes runs provider hooks under the caller profile context. When no context\n'
                '    is set, its getter deliberately falls back to the process home.\n'
                '    """\n'
                '    def provider() -> str:\n'
                '        try:\n'
                '            from hermes_constants import get_hermes_home\n'
                '            return language(get_hermes_home())\n'
                '        except Exception:\n'
                '            return language()\n\n'
                '    set_language_provider(provider)\n' + text[end:])
    if name == "provider.py":
        old = '        question = squash(user_content, 2000)\n        answer = squash(assistant_content, 3000)\n'
        new = ('        # Skip empty and trivial turns before attempting any redaction or extraction.\n'
               '        if not (user_content or "").strip() or not (assistant_content or "").strip() or is_trivial(user_content):\n'
               '            return\n'
               '        # Redact before collapsing or truncating: both can break secret patterns.\n'
               '        try:\n'
               '            from agent.redact import redact_sensitive_text\n'
               '            question = redact_sensitive_text(user_content or "", force=True, redact_url_credentials=True)\n'
               '            answer = redact_sensitive_text(assistant_content or "", force=True, redact_url_credentials=True)\n'
               '            if not isinstance(question, str) or not isinstance(answer, str):\n'
               '                raise TypeError("redactor returned non-text")\n'
               '        except Exception:\n'
               '            logger.warning("eventmem: automatic extraction skipped: redactor unavailable")\n'
               '            core = self._ensure_core()\n'
               '            if core is not None:\n'
               '                core.note_extract_failure("call_failed", "redactor unavailable", session=session_id or self._session_id or None)\n'
               '            return\n'
               '        question = squash(question, 2000)\n'
               '        answer = squash(answer, 3000)\n')
        if text.count(old) != 1:
            raise ValueError("provider extraction input no longer matches expected source")
        text = text.replace(old, new)
        old = '            core.note_extract_failure(exc.reason, exc.detail,\n'
        if text.count(old) != 1:
            raise ValueError("provider extraction failure no longer matches expected source")
        text = text.replace(old, '            core.note_extract_failure(exc.reason, "",\n')
        old = '        project = self._session_project or core.session_project(self._sid(session_id) or "") or None\n'
        new = ('        # Redact the whole output batch before any write. Never log model content.\n'
               '        try:\n'
               '            for item in events:\n'
               '                cleaned = redact_sensitive_text(str(item.get("text") or ""), force=True, redact_url_credentials=True)\n'
               '                if not isinstance(cleaned, str):\n'
               '                    raise TypeError("redactor returned non-text")\n'
               '                item["text"] = cleaned\n'
               '        except Exception:\n'
               '            logger.warning("eventmem: automatic extraction skipped: redactor unavailable")\n'
               '            core.note_extract_failure("call_failed", "redactor unavailable", session=session_id or self._session_id or None)\n'
               '            return\n\n' + old)
        if text.count(old) != 1:
            raise ValueError("provider extraction output no longer matches expected source")
        text = text.replace(old, new)
        old = 'logger.warning("eventmem: auto-record failed (%s)", exc)'
        if text.count(old) != 1:
            raise ValueError("provider write failure no longer matches expected source")
        text = text.replace(old, 'logger.warning("eventmem: auto-record failed")')
    if name == "extract.py":
        replacements = (
            ('logger.warning("eventmem: extraction call failed (%s)", exc)',
             'logger.warning("eventmem: extraction call failed")'),
            ('raise ExtractError(CALL_FAILED, f"{type(exc).__name__}: {exc}") from exc',
             'raise ExtractError(CALL_FAILED) from None'),
            ('raise ExtractError(UNPARSABLE, squash(raw, 300))',
             'raise ExtractError(UNPARSABLE)'),
            ('    parsed = parse_events(raw, kinds, limit)',
             '    parsed = parse_events(raw, kinds, limit) if isinstance(raw, str) else None'),
        )
        for old, new in replacements:
            if text.count(old) != 1:
                raise ValueError("extract security boundary no longer matches expected source")
            text = text.replace(old, new)
    # These imports must remain relative even when Hermes loads the plugin under a
    # generated namespaced package name. Do not mutate sys.path or sys.modules.
    text = re.sub(r"(?m)^(\s*)from eventmem_core(?=\.| import )", r"\1from .eventmem_core", text)
    text = re.sub(r"(?m)^(\s*)import eventmem_core\.store(?=\s|$)",
                  r"\1from .eventmem_core import store", text)
    if re.search(r"(?m)^\s*(?:from|import) eventmem_core(?:\.|\s)", text):
        raise ValueError(f"unhandled absolute core import in {name}")
    return text.encode("utf-8")


def expected_files() -> dict[Path, bytes]:
    result = {Path(name): _adapter_content(name) for name in ADAPTER_FILES}
    result.update({Path("eventmem_core") / name: (CORE / name).read_bytes() for name in CORE_FILES})
    return result


def check(dest: Path = DEST) -> list[str]:
    expected = expected_files()
    problems = []
    for relative, contents in expected.items():
        path = dest / relative
        if not path.is_file():
            problems.append(f"missing: {relative.as_posix()}")
        elif path.read_bytes() != contents:
            problems.append(f"different: {relative.as_posix()}")
    # Only generated Python entrypoints are permitted at the plugin root.
    if dest.exists():
        for path in dest.glob("*.py"):
            if path.name not in ADAPTER_FILES:
                problems.append(f"extra: {path.name}")
    core = dest / "eventmem_core"
    if core.exists():
        for path in core.rglob("*"):
            if "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
                continue
            if path.is_file() and path.relative_to(dest) not in expected:
                problems.append(f"extra: {path.relative_to(dest).as_posix()}")
    return sorted(problems)


def sync(dest: Path = DEST) -> None:
    """Regenerate owned files without deleting any unexpected files."""
    expected = expected_files()
    # Unexpected files in the generated core are reported by check(), not deleted.
    # Adapter-level manifest, documentation and other files are not owned here.
    for relative, contents in expected.items():
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="report drift without writing")
    args = parser.parse_args()
    if not args.check:
        sync()
    problems = check()
    for problem in problems:
        print(problem)
    if problems:
        return 1
    print("Hermes catalog provider copy matches canonical sources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
