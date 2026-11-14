"""Provider-only catalog copy: synchronization and hermetic subprocess tests."""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "sync_hermes_core.py"
CATALOG = ROOT / "adapters" / "hermes-catalog" / "eventmem"
spec = importlib.util.spec_from_file_location("sync_hermes_catalog_test", SCRIPT)
sync = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sync)


class CatalogSyncTests(unittest.TestCase):
    def test_generated_files_match_canonical(self):
        self.assertEqual(sync.check(), [])
        self.assertFalse((CATALOG / "cli.py").exists())
        for forbidden in ("worker.py", "daemon.py", "install.py", "decision.py",
                          "packs.py", "runtime.py", "candidate_backfill.py", "ui_strings.py", "ui"):
            self.assertFalse((CATALOG / "eventmem_core" / forbidden).exists(), forbidden)

    def test_detects_missing_changed_extra_and_ignores_cache(self):
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "eventmem"
            sync.sync(dest)
            self.assertEqual(sync.check(dest), [])
            (dest / "plugin.yaml").write_text("maintained separately", encoding="utf-8")
            cache = dest / "eventmem_core" / "__pycache__"
            cache.mkdir()
            (cache / "test.pyc").write_bytes(b"cache")
            self.assertEqual(sync.check(dest), [])
            (dest / "host.py").unlink()
            (dest / "eventmem_core" / "store.py").write_text("drift", encoding="utf-8")
            (dest / "eventmem_core" / "unexpected.txt").write_text("extra", encoding="utf-8")
            (dest / "cli.py").write_text("extra", encoding="utf-8")
            problems = sync.check(dest)
            self.assertIn("missing: host.py", problems)
            self.assertIn("different: eventmem_core/store.py", problems)
            self.assertIn("extra: eventmem_core/unexpected.txt", problems)
            self.assertIn("extra: cli.py", problems)
            sync.sync(dest)
            self.assertEqual((dest / "plugin.yaml").read_text(encoding="utf-8"), "maintained separately")
            self.assertIn("extra: eventmem_core/unexpected.txt", sync.check(dest))
            self.assertIn("extra: cli.py", sync.check(dest))


class IsolatedCatalogTests(unittest.TestCase):
    def test_namespaced_copy_with_preloaded_external_core(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copied = root / "plugins" / "eventmem"
            copied.parent.mkdir()
            shutil.copytree(CATALOG, copied, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            stub = root / "_host_stub.py"
            shutil.copyfile(ROOT / "tests" / "_host_stub.py", stub)
            external = root / "external" / "eventmem_core"
            external.mkdir(parents=True)
            (external / "__init__.py").write_text("marker = 'external'\n", encoding="utf-8")
            home = root / "home"
            home.mkdir()
            code = r'''
import importlib, json, pathlib, sys, types
root = pathlib.Path(sys.argv[1]).resolve()
plugin_dir = root / "plugins" / "eventmem"
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "external"))
import _host_stub
_host_stub.install()
import eventmem_core as external
assert external.marker == "external"
parent = types.ModuleType("hermes_test_namespace")
parent.__path__ = [str(plugin_dir.parent)]
sys.modules[parent.__name__] = parent
pkg = importlib.import_module("hermes_test_namespace.eventmem")
private = importlib.import_module(pkg.__name__ + ".eventmem_core")
assert pathlib.Path(private.__file__).resolve().is_relative_to(plugin_dir.resolve())
assert sys.modules["eventmem_core"] is external
assert not any(str(root_repo) in str(pathlib.Path(p).resolve()) for p in sys.path if p for root_repo in [sys.argv[2]])
class Context:
    def register_memory_provider(self, provider): self.provider = provider
    def register_middleware(self, *args): raise AssertionError('unexpected middleware registration')
    def register_auxiliary_task(self, *args, **kwargs): raise AssertionError('unexpected auxiliary registration')
ctx = Context()
pkg.register(ctx)
assert not hasattr(pkg, 'on_tool_result')
# Two instances must resolve language from active profile context, not last initialized home.
settings_mod = importlib.import_module(pkg.__name__ + '.settings')
i18n = importlib.import_module(pkg.__name__ + '.eventmem_core.i18n')
import contextvars
home_scope = contextvars.ContextVar('test_home', default='A')
host_constants = types.ModuleType('hermes_constants')
host_constants.get_hermes_home = lambda: home_scope.get()
sys.modules['hermes_constants'] = host_constants
old_language = settings_mod.language
settings_mod.language = lambda home=None: {'A': 'zh', 'B': 'en'}.get(str(home), 'en')
settings_mod.install_language_provider('A')
assert i18n.tr('中文', 'English') == '中文'
settings_mod.install_language_provider('B')
assert i18n.tr('中文', 'English') == '中文'
token = home_scope.set('B')
assert i18n.tr('中文', 'English') == 'English'
home_scope.reset(token)
assert i18n.tr('中文', 'English') == '中文'
settings_mod.language = old_language
sys.modules.pop('hermes_constants')
provider = ctx.provider
assert provider.is_available()
provider.initialize("isolated-session", hermes_home=str(root / "home"))
response = json.loads(provider.handle_tool_call("evt_add", {"events": [{"kind": "结果", "text": "isolated cobalt memory"}]}))
assert response.get("count") == 1, response
id = response["events"][0]["id"]
expanded = json.loads(provider.handle_tool_call("expand", {"event_id": id}))
assert "isolated cobalt memory" in json.dumps(expanded, ensure_ascii=False), expanded
recall = provider.prefetch("isolated cobalt memory")
assert recall and "isolated cobalt memory" in recall, recall
# Exercise automatic extraction boundaries with an explicit fake host redactor.
provider_mod = importlib.import_module(pkg.__name__ + '.provider')
calls = []
def extract(question, answer, **kwargs):
    calls.append((question, answer))
    return [{'kind': '结果', 'text': 'result PRIVATESECRET'}]
provider_mod.extract_events = extract
provider.on_turn_start(1, 'question')
notices = []
original_note = provider._core.note_extract_failure
provider._core.note_extract_failure = lambda *args, **kwargs: notices.append((args, kwargs))
provider.sync_turn('ok', 'answer')
provider.sync_turn('question', '')
assert not notices, 'empty or trivial turns must not record a redactor failure'
provider.sync_turn('input PRIVATESECRET', 'answer PRIVATESECRET')
assert len(notices) == 1 and notices[0][0][:2] == ('call_failed', 'redactor unavailable')
provider._core.note_extract_failure = original_note
assert not calls, 'missing redactor must skip extraction'
redactor = types.ModuleType('agent.redact')
def scrub(text, *, force, redact_url_credentials):
    assert force and redact_url_credentials
    return text.replace('PRIVATESECRET', '[REDACTED]')
redactor.redact_sensitive_text = scrub
sys.modules['agent.redact'] = redactor
provider.sync_turn('input PRIVATESECRET', 'answer PRIVATESECRET')
assert calls == [('input [REDACTED]', 'answer [REDACTED]')], calls
recall = provider.prefetch('result REDACTED')
assert recall and 'result [REDACTED]' in recall and 'PRIVATESECRET' not in recall, recall
# Failure while redacting the second output must not persist the first.
def two_events(question, answer, **kwargs):
    return [{'kind': '结果', 'text': 'must not persist'}, {'kind': '结果', 'text': 'trigger failure'}]
provider_mod.extract_events = two_events
def failing_scrub(text, **kwargs):
    if text == 'trigger failure': raise ValueError('PRIVATESECRET exception')
    return text
redactor.redact_sensitive_text = failing_scrub
provider.sync_turn('another input', 'another answer')
assert provider.prefetch('must not persist') is None
# Failure messages must not retain raw exception or model output, including malformed types.
extract_mod = importlib.import_module(pkg.__name__ + '.extract')
def raising_llm(*args, **kwargs):
    raise RuntimeError('PRIVATESECRET exception')
extract_mod._aux_llm = raising_llm
try:
    extract_mod.extract_events('question', 'answer', kinds=('结果',))
    raise AssertionError('expected extraction error')
except extract_mod.ExtractError as exc:
    assert exc.reason == extract_mod.CALL_FAILED and exc.detail == '' and 'PRIVATESECRET' not in str(exc)
for raw in ('PRIVATESECRET invalid JSON', ['not', 'a', 'string']):
    extract_mod._aux_llm = lambda *a, **kw: types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=raw))])
    try:
        extract_mod.extract_events('question', 'answer', kinds=('结果',))
        raise AssertionError('expected malformed output error')
    except extract_mod.ExtractError as exc:
        assert exc.reason == extract_mod.UNPARSABLE and exc.detail == '' and 'PRIVATESECRET' not in str(exc)
provider.shutdown()
assert (root / "home" / "eventmem" / "events.db").exists()
assert sys.modules["eventmem_core"] is external
print("private core:", private.__file__)
print("external core:", external.__file__)
'''
            env = {k: v for k, v in os.environ.items() if k not in
                   {"PYTHONPATH", "PYTHONHOME", "EVENTMEM_MODELS_ROOT", "EVENTMEM_DB", "HERMES_HOME"}}
            env.update({"HERMES_HOME": str(home), "HOME": str(home), "USERPROFILE": str(home),
                        "PYTHONDONTWRITEBYTECODE": "1"})
            result = subprocess.run([sys.executable, "-I", "-B", "-c", code, str(root), str(ROOT)],
                                    cwd=root, env=env, capture_output=True, text=True, encoding="utf-8")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("private core:", result.stdout)
            self.assertIn("external core:", result.stdout)


if __name__ == "__main__":
    unittest.main()
