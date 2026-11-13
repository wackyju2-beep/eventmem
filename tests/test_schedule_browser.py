#!/usr/bin/env python3
"""Isolated browser fixture; run directly, then open /#schedule or /desktop.

The desktop fixture bundles the real plugin with the local React/SDK stub. Both
endpoints use only canned HTTP responses and never contact a production daemon.
"""
import json
import pathlib
import subprocess
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'core'))
from eventmem_core.ui_strings import EN
WORK = pathlib.Path.home() / '.hermes/cache/scratch/schedule-harness'
def build_desktop():
    import os
    WORK.mkdir(parents=True, exist_ok=True)
    plugin = ROOT / 'adapters/hermes/desktop-plugins/eventmem-gui/plugin.js'
    (WORK / 'plugin.mjs').write_text(plugin.read_text() + '\nexport { Schedule }\n')
    (WORK / 'entry.mjs').write_text("""import React from 'react'; import {createRoot} from 'react-dom/client'; import {Schedule} from './plugin.mjs';
const ctx={rest:async(path,opts)=>{const r=await fetch(path,opts?{method:opts.method,headers:{'Content-Type':'application/json'},body:JSON.stringify(opts.body)}:{});const x=await r.json();if(!r.ok)throw Error(x.error);return x}};
globalThis.errors=[];const notify=(m,k)=>errors.push([m,k]);createRoot(document.getElementById('root')).render(React.createElement(Schedule,{ctx,notify}));""")
    modules = pathlib.Path(os.environ.get('HERMES_NODE_MODULES', pathlib.Path.home() / '.hermes/hermes-agent/node_modules'))
    env = dict(os.environ, NODE_PATH=str(modules))
    subprocess.run([str(modules / '.bin/esbuild'), str(WORK / 'entry.mjs'), '--bundle', '--format=esm', '--outfile=' + str(WORK / 'bundle.js'), '--alias:@hermes/plugin-sdk=' + str(ROOT / 'tools/plugin-smoke/sdk-stub/index.mjs')], check=True, env=env)
state = {'backfill': {'state': 'running', 'pairs': 2, 'edges': 3}, 'calls': [], 'status_reads': 0, 'lang': 'zh'}
def config():
    return {'effective': {'edges': {'session_recent': 3}}, 'overrides': {}, 'file': 'fixture'}
class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs): super().__init__(*args, directory=str(ROOT / 'core/eventmem_core/ui'), **kwargs)
    def send_json(self, obj):
        data = json.dumps(obj, ensure_ascii=False).encode(); self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
    def do_GET(self):
        if self.path == '/desktop':
            data = b'<meta charset="utf-8"><div id="root"></div><script type="module" src="/bundle.js"></script>'; self.send_response(200); self.send_header('Content-Type', 'text/html'); self.end_headers(); return self.wfile.write(data)
        if self.path == '/bundle.js':
            data = (pathlib.Path.home() / '.hermes/cache/scratch/schedule-harness/bundle.js').read_bytes(); self.send_response(200); self.send_header('Content-Type', 'text/javascript'); self.end_headers(); return self.wfile.write(data)
        if self.path == '/api/i18n': return self.send_json({'lang':state['lang'], 'map':EN if state['lang'] == 'en' else {}})
        if self.path == '/api/config': return self.send_json(config())
        if self.path == '/api/status':
            state['status_reads'] += 1
            return self.send_json({'backfill': state['backfill']})
        if self.path == '/test/state': return self.send_json(state)
        if self.path == '/api/health': return self.send_json({'schema':0,'pid':0,'db':'fixture'})
        return super().do_GET()
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}'); state['calls'].append([self.path, data])
        if self.path == '/test/finish': state['backfill'] = {'state':'done','pairs':4,'edges':5,'result':{'status':'stopped'}}; return self.send_json({'ok':True})
        if self.path == '/test/lang': state['lang'] = data['lang']; return self.send_json({'ok':True})
        if self.path == '/api/recalibrate/preview': return self.send_json({'token':'r','added':{'改变':2,'否定':0,'重复':1,'因为':3,'细化':4},'removed':{'改变':1,'否定':0,'重复':0,'因为':0,'细化':0},'skipped':7,'uncovered_jev_edges':8})
        if self.path == '/api/backfill/preview': return self.send_json({'token':'b','event_count':12,'observed_pairs':6,'exact_total':None})
        if self.path == '/api/backfill/start': state['backfill'] = {'state':'running','pairs':0,'edges':0}; return self.send_json({'ok':True,'backfill':state['backfill']})
        return self.send_json({'ok':True})
if __name__ == '__main__':
    if '--serve' not in sys.argv:
        print('Browser fixture only; pass --serve to start it')
        sys.exit(0)
    build_desktop()
    ThreadingHTTPServer(('127.0.0.1', 18763), Handler).serve_forever()
