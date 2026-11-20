"""Loopback workbench for frozen snapshots and deterministic investigations."""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from .contract import MAX_BYTES, decode, validate
from .human_review import api as human_api
from .human_review.service import Service
from .investigation import real, real_protocol, tasks
from .investigation.decision import GOAL
from .investigation.tools import View
from .review import review
from .export import markdown

ROOT = Path(__file__).resolve().parents[1]
DEMOS = (ROOT / 'fixtures/demo', ROOT / 'fixtures/investigation/phase3')
WEB = ROOT / 'web'


class Handler(BaseHTTPRequestHandler):
    server_version = 'MEA'

    def log_message(self, *_):
        pass  # Never persist imported source, credentials or request bodies.

    def allowed(self):
        expected = f'127.0.0.1:{self.server.server_port}'
        return (self.headers.get_all('Host') == [expected] and
                self.headers.get_all('Origin') in (None, ['http://' + expected]) and
                self.headers.get('Sec-Fetch-Site') in {None, 'same-origin', 'none'})

    def send(self, status, value, kind='application/json'):
        raw = json.dumps(value, ensure_ascii=False).encode() if kind == 'application/json' else value
        self.send_response(status)
        self.send_header('Content-Type', kind + ('; charset=utf-8' if kind.startswith(('text/', 'application/json')) else ''))
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if not self.allowed():
            return self.send(403, {'error': 'LOOPBACK_ORIGIN_REQUIRED'})
        static = {
            '/': ('risk-review.html', 'text/html'),
            '/workbench': ('review-v3.html', 'text/html'),
            '/frozen-code.js': ('frozen-code.js', 'text/javascript'),
            '/review-components.js': ('review-components.js', 'text/javascript'),
            '/comment-editor.js': ('comment-editor.js', 'text/javascript'),
            '/risk-review.js': ('risk-review.js', 'text/javascript'),
            '/risk-review.css': ('risk-review.css', 'text/css'),
            '/review-v3.js': ('review-v3.js', 'text/javascript'),
            '/review-v3.css': ('review-v3.css', 'text/css'),
            '/review-font/inter.woff2': ('review-font/inter.woff2', 'font/woff2'),
            '/review-font/plex-mono.woff2': ('review-font/plex-mono.woff2', 'font/woff2'),
            '/classic': ('index.html', 'text/html'),
            '/i18n.js': ('i18n.js', 'text/javascript'),
            '/messages-zh.js': ('messages-zh.js', 'text/javascript'),
            '/app.js': ('app.js', 'text/javascript'),
            '/style.css': ('style.css', 'text/css'),
        }
        route, _, query = self.path.partition('?')
        # Keep previously shared workbench links and their pinned anchors valid.
        if route == '/' and parse_qs(query).keys() & {'case', 'task', 'review', 'comparison'}:
            route = '/workbench'
        if route in static:
            filename, kind = static[route]
            return self.send(200, (WEB / filename).read_bytes(), kind)
        if self.path.startswith('/api/human/'):
            return human_api.get_request(self)
        if self.path == '/api/investigation-provider':
            return self.send(200, real.readiness(self.server.investigations.store))
        if self.path == '/api/tasks':
            return self.send(200, self.server.investigations.store.list())
        for prefix in ('/api/task/', '/api/task-export/', '/api/task-json/'):
            if self.path.startswith(prefix):
                try:
                    task = self.server.investigations.store.get(self.path[len(prefix):])
                except ValueError:
                    return self.send(404, {'error': 'TASK_NOT_FOUND'})
                if prefix == '/api/task-export/':
                    return self.send(200, tasks.markdown(task).encode(), 'text/markdown')
                return self.send(200, task if prefix == '/api/task-json/' else tasks.public(task))
        prefix = '/api/demo/'
        if self.path == '/api/demos' or self.path.startswith(prefix):
            demos = [validate(decode(p.read_bytes())) for folder in DEMOS for p in sorted(folder.glob('*.json'))]
            if self.path == '/api/demos':
                return self.send(200, [dict(id=d['id'], title=d['pr']['title']) for d in demos])
            for demo in demos:
                if demo['id'] == self.path[len(prefix):]:
                    return self.send(200, demo)
        return self.send(404, {'error': 'NOT_FOUND'})

    def do_POST(self):
        if not self.allowed():
            return self.send(403, {'error': 'LOOPBACK_ORIGIN_REQUIRED'})
        if self.path.startswith('/api/human/'):
            return human_api.post_request(self)
        cancelling = self.path.startswith('/api/task-cancel/')
        investigating = cancelling or self.path == '/api/tasks'
        if not investigating and self.path not in {'/api/review', '/api/export'}:
            return self.send(404, {'error': 'NOT_FOUND'})
        body_bound = MAX_BYTES + 4096 if investigating and not cancelling else MAX_BYTES
        length = self.headers.get('Content-Length', '')
        if (self.headers.get('Transfer-Encoding') or len(self.headers.get_all('Content-Length') or []) != 1
                or not length.isascii() or not length.isdecimal() or len(length) > len(str(body_bound))
                or not 0 < int(length) <= body_bound):
            return self.send(413, {'error': 'BODY_BOUND'})
        if self.headers.get('Content-Type') != 'application/json':
            return self.send(415, {'error': 'JSON_REQUIRED'})
        self.connection.settimeout(5)
        try:
            data = decode(self.rfile.read(int(length)), limit=body_bound)
            if investigating:
                return self.task_post(data, cancelling)
            report = review(data)
            if self.path == '/api/export':
                return self.send(200, markdown(report).encode(), 'text/markdown')
            return self.send(200, report)
        except (ValueError, TypeError, KeyError, RecursionError, UnicodeError, OSError):
            code = 'TASK_INPUT_OR_BUDGET_REJECTED' if investigating else 'SNAPSHOT_REJECTED'
            return self.send(400, {'error': code})

    def task_post(self, data, cancelling):
        store = self.server.investigations.store
        if cancelling:
            if data != {}:
                raise ValueError('CANCEL_BODY')
            identifier = self.path.removeprefix('/api/task-cancel/')
            return self.send(200, tasks.public(store.cancel(identifier)))
        if not isinstance(data, dict) or 'snapshot' not in data:
            raise ValueError('FROZEN_INPUT_ONLY')
        mode = data.get('mode', tasks.MODE)
        if mode == 'REAL_AI':
            return self.send(403, {'error': 'LIVE_ADMISSION_NOT_CONFIGURED'})
        if mode == real.MODE:
            if set(data) - {'snapshot', 'goal', 'budget', 'mode', 'confirmed', 'context_limits'}:
                raise ValueError('FROZEN_INPUT_ONLY')
            try:
                task = real.create(store, data['snapshot'], data.get('goal', real_protocol.GOAL),
                                   data.get('budget'), confirmed=data.get('confirmed', False),
                                   context_limits=data.get('context_limits'))
            except ValueError as exc:
                return self.send(400, {'error': str(exc)})
            return self.send(200, tasks.public(task))
        if mode != tasks.MODE or set(data) - {'snapshot', 'goal', 'budget', 'mode'}:
            raise ValueError('FROZEN_INPUT_ONLY')
        view = View.from_input(data['snapshot'])
        task = store.create(view, data.get('goal', GOAL), data.get('budget'), {'kind': 'VALIDATED_SNAPSHOT'})
        return self.send(200, tasks.public(task))


def serve(port=8810, task_dir=None, bind='127.0.0.1', *, round3_demo=False):
    if bind not in ('127.0.0.1', '0.0.0.0'):
        raise ValueError('BIND_ADDRESS')
    if round3_demo and (task_dir is None or bind != '127.0.0.1'):
        raise ValueError('DEMO_REQUIRES_LOCAL_TASK_DIR')
    with ThreadingHTTPServer((bind, port), Handler) as server:
        server.investigations = tasks.Manager(task_dir)
        try:
            server.human_reviews = Service(server.investigations.store)
            server.human_reviews.recover()
            if round3_demo:
                from .human_review.risk_demo import seed
                seed(server.human_reviews)
            print(f'MEA OFFLINE http://127.0.0.1:{server.server_port}', flush=True)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.investigations.close()
