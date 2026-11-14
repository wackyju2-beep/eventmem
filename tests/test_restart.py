"""Restart contract; never replaces or restarts the test runner."""
import json
import os
import socket
import subprocess
import sys
import time
import threading
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))
from eventmem_core import daemon as mod
from eventmem_core.settings import CoreSettings


def request(port, method='POST', origin=None):
    headers = {} if origin is None else {'Origin': origin}
    req = urllib.request.Request(f'http://127.0.0.1:{port}/api/restart',
                                 method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_restart_http_contract(tmp_path):
    d = mod.Daemon(CoreSettings(db_path=str(tmp_path / 'events.db')))
    server = mod.make_server(d, port=0)
    t = threading.Thread(target=server.serve_forever)
    t.start()
    try:
        port = server.server_address[1]
        assert request(port)[0] == 503
        d._restart_enabled = True
        assert request(port, origin='https://evil.example')[0] == 403
        assert not d._restart_requested.is_set()
        status, body = request(port, origin=f'http://127.0.0.1:{port}')
        assert status == 202 and body['restarting'] and body['instance_id'] == d.instance_id
        assert d._stop.wait(2)
        assert request(port)[0] == 409
        assert request(port, method='GET')[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        t.join()


def test_health_instance_id_changes(tmp_path):
    settings = CoreSettings(db_path=str(tmp_path / 'events.db'))
    first, second = mod.Daemon(settings), mod.Daemon(settings)
    assert first.health()['instance_id'] != second.health()['instance_id']


def test_main_executes_original_arguments_after_shutdown(tmp_path):
    calls = []
    def fake_serve(d, **kwargs):
        assert d._restart_enabled
        d._restart_requested.set()
        calls.append('drained')
    def fake_exec(exe, argv):
        calls.append(('exec', exe, argv))
        raise OSError('simulated failure')
    with patch.object(mod, 'serve', fake_serve), patch.object(mod.os, 'execv', fake_exec), \
         patch.object(mod, 'acquire_lock', return_value=mod.NO_GUARD), \
         patch.object(sys, 'orig_argv', ['python', '-I', '-m', 'eventmem_core.daemon']):
        assert mod.main(['--db', str(tmp_path / 'x.db')]) == 1
    expected = [sys.executable, '-I', '-m', 'eventmem_core.daemon']
    if os.name == 'nt':
        import subprocess
        expected = [subprocess.list2cmdline([arg]) for arg in expected]
    assert calls == ['drained', ('exec', sys.executable, expected)]


def test_isolated_process_restart(tmp_path):
    # This is a dedicated disposable process/database, never an existing service.
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    env = os.environ.copy()
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'core')
    db = tmp_path / 'path with spaces' / 'events.db'
    db.parent.mkdir()
    proc = subprocess.Popen([sys.executable, '-m', 'eventmem_core.daemon', '--db', str(db),
                             '--port', str(port)], env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    url = f'http://127.0.0.1:{port}/api/health'
    new_pid = None
    job = None
    if os.name == 'nt':
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype = wintypes.HANDLE
        kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        job = kernel.CreateJobObjectW(None, None)
        if not job or not kernel.AssignProcessToJobObject(job, int(proc._handle)):
            proc.terminate()
            proc.wait(timeout=10)
            if job:
                kernel.CloseHandle(job)
            raise ctypes.WinError(ctypes.get_last_error())
    try:
        deadline = time.monotonic() + 20
        while True:
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    before = json.load(response)
                break
            except (OSError, ValueError):
                assert time.monotonic() < deadline, 'isolated daemon failed to start'
                time.sleep(0.1)
        assert request(port)[0] == 202
        deadline = time.monotonic() + 20
        while True:
            try:
                with urllib.request.urlopen(url, timeout=1) as response:
                    after = json.load(response)
                if after['instance_id'] != before['instance_id']:
                    new_pid = after['pid']
                    break
            except (OSError, ValueError):
                pass
            assert time.monotonic() < deadline, 'isolated daemon failed to restart'
            time.sleep(0.1)
        assert after['db'] == str(db)
    finally:
        # Windows exec creates a new process; stop only the PID reported by our isolated
        # daemon, and only when it differs from the original process we started.
        if job:
            kernel.TerminateJobObject(job, 1)
            kernel.CloseHandle(job)
        elif new_pid and new_pid != proc.pid:
            os.kill(new_pid, 15)
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=10)
