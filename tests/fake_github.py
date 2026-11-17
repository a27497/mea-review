"""Loopback fake of the GitHub REST endpoints used by mea.github_writeback. Synthetic data only."""
import http.client
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from mea.patch import parse as parse_patch

TOKEN = 'ghp_FAKE_TOKEN_FOR_TESTS_ONLY_0123456789'


class FakeGitHub:
    """One repository with one open pull request. fault is consumed by the next POST."""

    def __init__(self, repository, number, head, base, patches, login='sandbox-owner'):
        self.repository, self.number, self.head, self.base = repository, number, head, base
        self.patches = dict(patches)  # path -> PR diff patch (merge base...head)
        self.login, self.state, self.pull_permission = login, 'open', True
        self.comments, self.posts, self.requests = [], [], []
        self.fault = None
        self.page_size = 100
        self.lock = threading.Lock()
        self.auth_seen = set()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                fake.handle(self, 'GET')

            def do_POST(self):
                fake.handle(self, 'POST')

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def connect(self, timeout=2):
        return lambda: http.client.HTTPConnection('127.0.0.1', self.server.server_address[1], timeout=timeout)

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def push(self, head, patches, outdated_ids=(), moved=None):
        """New head; the listed comments become outdated (line and position null) as GitHub reports them."""
        with self.lock:
            self.head = head
            self.patches = dict(patches)
            for c in self.comments:
                if c['id'] in outdated_ids:
                    c.update(line=None, position=None)
                elif moved and c['id'] in moved:
                    c.update(line=moved[c['id']], commit_id=head)

    def commentable(self, path, side):
        patch = self.patches.get(path)
        if not patch:
            return set()
        rows = parse_patch(patch, {})
        return {r['head' if side == 'RIGHT' else 'base'] for r in rows if r['head' if side == 'RIGHT' else 'base'] is not None}

    def send(self, handler, status, value, delay=0):
        raw = json.dumps(value).encode()
        if delay:
            time.sleep(delay)
        try:
            handler.send_response(status)
            handler.send_header('Content-Type', 'application/json')
            handler.send_header('X-GitHub-Request-Id', f'FAKE:{len(self.requests)}')
            handler.send_header('Content-Length', str(len(raw)))
            handler.end_headers()
            handler.wfile.write(raw)
        except OSError:
            pass  # The client gave up (timeout fault); the effect is already stored.

    def handle(self, handler, method):
        status, value, delay = self.route(handler, method)
        self.send(handler, status, value, delay)

    def route(self, handler, method):
        self.auth_seen.add(handler.headers.get('Authorization'))
        self.requests.append((method, handler.path))
        if handler.headers.get('Authorization') != 'Bearer ' + TOKEN:
            return 401, dict(message='Bad credentials'), 0
        base = f'/repos/{self.repository}'
        path, _, query = handler.path.partition('?')
        page = int(dict(p.split('=') for p in query.split('&') if p).get('page', 1)) if query else 1
        with self.lock:
            if method == 'GET' and path == base:
                return 200, dict(full_name=self.repository, private=True, permissions=dict(pull=self.pull_permission, push=True)), 0
            if method == 'GET' and path == '/user':
                return 200, dict(login=self.login), 0
            if method == 'GET' and path == f'{base}/pulls/{self.number}':
                return 200, dict(number=self.number, state=self.state, head=dict(sha=self.head), base=dict(sha=self.base)), 0
            if method == 'GET' and path == f'{base}/pulls/{self.number}/files':
                rows = [dict(filename=p, patch=t) for p, t in sorted(self.patches.items())]
                return 200, rows[(page - 1) * 100:page * 100], 0
            if method == 'GET' and path == f'{base}/pulls/{self.number}/comments':
                if self.fault == 'LIST_500':
                    return 500, dict(message='list failed'), 0
                size = 100
                return 200, self.comments[(page - 1) * size:page * size], 0
            if method == 'POST' and path == f'{base}/pulls/{self.number}/comments':
                body = json.loads(handler.rfile.read(int(handler.headers['Content-Length'])))
                self.posts.append(body)
                fault, self.fault = self.fault, None
                if fault == 'HTTP_500_NO_EFFECT':
                    return 500, dict(message='Server Error'), 0
                if set(body) != {'commit_id', 'path', 'line', 'side', 'body'} or body['line'] not in self.commentable(body['path'], body['side']):
                    return 422, dict(message='Validation Failed', errors=[dict(field='line', code='invalid')]), 0
                if fault == 'HTTP_422':
                    return 422, dict(message='Validation Failed'), 0
                identifier = 9000 + len(self.comments) + 1
                comment = dict(id=identifier, html_url=f'https://github.com/{self.repository}/pull/{self.number}#discussion_r{identifier}',
                               user=dict(login=self.login), body=body['body'], path=body['path'], side=body['side'],
                               commit_id=body['commit_id'], original_commit_id=body['commit_id'],
                               line=body['line'], original_line=body['line'], position=1, original_position=1, in_reply_to_id=None)
                self.comments.append(comment)
                if fault == 'HTTP_502_AFTER_EFFECT':
                    return 502, dict(message='Bad Gateway'), 0
                if fault == 'TIMEOUT_AFTER_EFFECT':
                    return 201, comment, 3
                if fault == 'BAD_RECEIPT':
                    return 201, dict(id='x'), 0
                return 201, comment, 0
        return 404, dict(message='Not Found'), 0


def strip_header(diff_lines):
    return ''.join(diff_lines[2:])


SECRET_RE = re.compile(re.escape(TOKEN))
