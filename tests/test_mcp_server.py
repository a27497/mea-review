"""Read-only MCP server: protocol, bounds and the official SDK client round trip. Synthetic fixtures only."""
import ast
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from mea.contract import canonical, decode
from mea import mcp_server
from mea.mcp_server import MESSAGE_BYTES, Server, serve

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures/review/v4.json'


def load(path=FIXTURE):
    return decode(path.read_bytes())


def exchange(server, *messages):
    raw = b''.join((m if isinstance(m, bytes) else canonical(m)) + b'\n' for m in messages)
    out = io.BytesIO()
    serve(server, io.BytesIO(raw), out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


def request(identifier, method, params=None):
    value = dict(jsonrpc='2.0', id=identifier, method=method)
    if params is not None:
        value['params'] = params
    return value


def init_params(version):
    return dict(protocolVersion=version, capabilities={}, clientInfo=dict(name='test-client', version='0'))


class McpServerTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = load()
        self.server = Server(self.snapshot)
        exchange(self.server, request(0, 'initialize', init_params('2025-11-25')),
                 dict(jsonrpc='2.0', method='notifications/initialized'))
        self.head = self.snapshot['revisions']['head']
        self.base = self.snapshot['revisions']['base']

    def call(self, name, arguments):
        reply, = exchange(self.server, request(1, 'tools/call', dict(name=name, arguments=arguments)))
        return reply

    def test_initialize_negotiates_a_supported_version(self):
        for asked, answered in (('2025-06-18', '2025-06-18'), ('2025-11-25', '2025-11-25'), ('1999-01-01', '2025-11-25')):
            with self.subTest(asked=asked):
                reply, = exchange(Server(self.snapshot), request(1, 'initialize', init_params(asked)))
                self.assertEqual(reply['result']['protocolVersion'], answered)
                self.assertEqual(reply['result']['capabilities'], dict(tools=dict(listChanged=False)))
        # A notification gets no reply; ping answers an empty result.
        self.assertTrue(self.server.initialized)
        pending = Server(self.snapshot)
        replies = exchange(pending, dict(jsonrpc='2.0', method='notifications/initialized'),
                           request(1, 'tools/list'), request(2, 'initialize', init_params(None)),
                           request(3, 'initialize', init_params('2025-11-25')), request(4, 'tools/list'),
                           dict(jsonrpc='2.0', method='notifications/initialized'), request(5, 'tools/list'))
        self.assertEqual([r.get('error', {}).get('code') for r in replies], [-32002, -32602, None, -32002, None])

    def test_tools_are_the_read_only_investigation_set(self):
        reply, = exchange(self.server, request(1, 'tools/list'))
        tools = reply['result']['tools']
        self.assertEqual(sorted(t['name'] for t in tools), ['find_symbol', 'read_diff', 'read_source', 'read_test', 'references',
                                                            'related_files', 'search', 'snapshot_info'])
        for tool in tools:
            self.assertEqual(tool['annotations'], dict(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False))
            schema = tool['inputSchema']
            self.assertFalse(schema['additionalProperties'])
            if 'revision' in schema['properties']:
                self.assertEqual(schema['properties']['revision']['enum'], [self.base, self.head])

    def test_calls_return_cited_frozen_evidence(self):
        info = self.call('snapshot_info', {})['result']
        self.assertFalse(info['isError'])
        value = info['structuredContent']
        self.assertEqual(value['revisions'], self.snapshot['revisions'])
        self.assertEqual(json.loads(info['content'][0]['text']), value)
        self.assertNotIn('text', json.dumps(value['files']))  # No source text in the listing.
        page = self.call('read_source', dict(path='limits.py', revision=self.head, start=1, end=2))['result']['structuredContent']
        self.assertEqual([r['line'] for r in page['data']], [1, 2])
        self.assertEqual({(e['path'], e['side'], e['revision']) for e in page['evidence']}, {('limits.py', 'head', self.head)})
        # Same result as the Agent's view for the same arguments.
        from mea.investigation.provider_view import CrossFileView
        same = CrossFileView.from_input(self.snapshot).execute('read_source', dict(path='limits.py', revision=self.head, start=1, end=2))
        self.assertEqual(page, same)

    def test_refused_inputs_are_tool_errors_without_source(self):
        cases = {
            'traversal': ('read_source', dict(path='../secrets', revision=self.head, start=1, end=1), 'INERT_PATH_REQUIRED'),
            'absolute': ('read_source', dict(path='/etc/passwd', revision=self.head, start=1, end=1), 'INERT_PATH_REQUIRED'),
            'dotenv': ('read_diff', dict(path='.env', revision=self.head), 'INERT_PATH_REQUIRED'),
            'foreign revision': ('search', dict(query='x', revision='f' * 40), 'TOOL_VERSION_MISMATCH'),
            'extra argument': ('search', dict(query='x', revision=self.head, url='https://example.invalid'), 'TOOL_NOT_ALLOWED_OR_ARGUMENTS'),
            'missing argument': ('read_source', dict(path='limits.py', revision=self.head), 'TOOL_NOT_ALLOWED_OR_ARGUMENTS'),
            'long query': ('search', dict(query='x' * 101, revision=self.head), 'QUERY_BOUND'),
            'symbol syntax': ('find_symbol', dict(symbol='a.b', revision=self.head), 'PYTHON_SYMBOL_REQUIRED'),
            'arguments not an object': ('search', ['x'], 'TOOL_NOT_ALLOWED_OR_ARGUMENTS'),
            'info arguments': ('snapshot_info', dict(path='x'), 'TOOL_NOT_ALLOWED_OR_ARGUMENTS'),
        }
        for label, (name, args, code) in cases.items():
            with self.subTest(label):
                result = self.call(name, args)['result']
                self.assertEqual((result['isError'], result['content']), (True, [dict(type='text', text=code)]))
                self.assertNotIn('structuredContent', result)
        # A file outside the snapshot is reported as not collected, never read from the host.
        missing = self.call('read_source', dict(path='mea/contract.py', revision=self.head, start=1, end=5))['result']['structuredContent']
        self.assertEqual((missing['data'], missing['read']['status']), ([], 'SOURCE_NOT_COLLECTED'))

    def test_protocol_errors(self):
        replies = exchange(self.server,
                           request(1, 'tools/call', dict(name='send_comment', arguments={})),
                           request(2, 'resources/list'),
                           dict(jsonrpc='2.0', id=None, method='ping'),
                           dict(jsonrpc='1.0', id=3, method='ping'),
                           [request(4, 'ping')],
                           request(5, 'tools/call', ['x']),
                           b'{not json',
                           b'\n',
                           request(6, 'ping'),
                           request(7, 'tools/call', dict(name=[])),
                           b'{"jsonrpc":"2.0","id":"\\ud800","method":"ping"}')
        self.assertEqual([(r['id'], r.get('error', {}).get('code')) for r in replies],
                         [(1, -32602), (2, -32601), (None, -32600), (None, -32600), (None, -32600),
                          (5, -32602), (None, -32700), (6, None), (7, -32602), (None, -32600)])

    def test_oversized_message_is_refused_and_the_session_continues(self):
        big = b'{"jsonrpc":"2.0","id":1,"method":"ping","params":{"x":"' + b'a' * MESSAGE_BYTES + b'"}}'
        replies = exchange(self.server, big, request(2, 'ping'))
        self.assertEqual([(r['id'], r.get('error', {}).get('code')) for r in replies], [(None, -32700), (2, None)])

    def test_result_byte_bound_is_enforced(self):
        original = mcp_server.RESULT_BYTES
        mcp_server.RESULT_BYTES = 100
        try:
            result = self.call('snapshot_info', {})['result']
        finally:
            mcp_server.RESULT_BYTES = original
        self.assertEqual((result['isError'], result['content'][0]['text']), (True, 'TOOL_RESULT_BYTE_BOUND'))

    def test_server_imports_no_write_model_or_network_module(self):
        tree = ast.parse((ROOT / 'mea/mcp_server.py').read_text())
        modules = {n.module or '' for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
        modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        self.assertEqual(modules, {'argparse', 'sys', 'pathlib', 'contract', 'investigation.provider_view'})
        # The view module it uses has no network, subprocess or writeback import either.
        for name in ('mea/investigation/provider_view.py', 'mea/investigation/tools.py', 'mea/contract.py', 'mea/patch.py'):
            tree = ast.parse((ROOT / name).read_text())
            used = {n.module or '' for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            used |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            self.assertFalse(used & {'socket', 'http', 'urllib', 'subprocess', 'github_writeback', 'deepseek', 'urllib.request', 'http.client'}, name)

    def test_cli_rejects_a_bad_snapshot_and_serves_a_good_one(self):
        env = dict(PATH=os.environ['PATH'], LANG='C.UTF-8')
        bad = subprocess.run(['python3', '-B', '-m', 'mea.mcp_server', '--snapshot', str(ROOT / 'package.json')],
                             cwd=ROOT, env=env, capture_output=True, input=b'', timeout=30)
        self.assertEqual((bad.returncode, bad.stdout), (2, b''))
        good = subprocess.run(['python3', '-B', '-m', 'mea.mcp_server', '--snapshot', str(FIXTURE)], cwd=ROOT, env=env,
                              capture_output=True, input=canonical(request(1, 'ping')) + b'\n', timeout=30)
        self.assertEqual(good.returncode, 0)
        self.assertEqual([json.loads(x) for x in good.stdout.splitlines()], [dict(jsonrpc='2.0', id=1, result={})])

    def test_official_sdk_client_round_trip(self):
        # initialize -> tools/list -> tools/call with @modelcontextprotocol/sdk (devDependency, installed by npm ci).
        node = shutil.which('node')
        if not node or not (ROOT / 'node_modules/@modelcontextprotocol/sdk/package.json').exists():
            self.skipTest('node and npm ci are needed for the official MCP client')
        done = subprocess.run([node, 'scripts/mcp-roundtrip.mjs'], cwd=ROOT, capture_output=True, text=True, timeout=60,
                              env=dict(PATH=os.environ['PATH'], LANG='C.UTF-8'))
        self.assertEqual(done.returncode, 0, done.stderr)
        summary = json.loads(done.stdout.splitlines()[-1])
        self.assertEqual(summary['result'], 'MCP_ROUNDTRIP_PASS')
        self.assertEqual(summary['messages'][:4], ['initialize', 'notifications/initialized', 'ping', 'tools/list'])
        self.assertIn('tools/call', summary['messages'])


if __name__ == '__main__':
    unittest.main()
