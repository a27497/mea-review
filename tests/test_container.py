"""One-command start: container files stay offline and loopback-only; --bind keeps the Host check."""
import http.client
import os
from pathlib import Path
import re
import socket
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class ContainerFileTests(unittest.TestCase):
    def test_compose_publishes_on_loopback_with_limits_and_no_secrets(self):
        compose = '\n'.join(line for line in (ROOT / 'compose.yaml').read_text().splitlines() if not line.lstrip().startswith('#'))
        self.assertIn('"127.0.0.1:8810:8810"', compose)
        self.assertRegex(compose, r'(?m)^\s+mem_limit: \d+m$')
        for setting in ('read_only: true', 'cap_drop: [ALL]', 'no-new-privileges:true', 'pids_limit:'):
            self.assertIn(setting, compose)
        for word in ('environment', 'env_file', 'secrets', 'volumes', 'DEEPSEEK', 'GH_TOKEN', 'GITHUB_TOKEN', 'network_mode'):
            self.assertNotIn(word, compose)

    def test_image_installs_nothing_and_runs_unprivileged_offline(self):
        docker = '\n'.join(line for line in (ROOT / 'Dockerfile').read_text().splitlines() if not line.startswith('#'))
        self.assertRegex(docker, r'(?m)^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$')
        self.assertRegex(docker, r'(?m)^USER 65534:65534$')
        for word in ('pip ', 'apt-get', 'curl', 'ENV DEEPSEEK', 'GH_TOKEN', 'ARG '):
            self.assertNotIn(word, docker)
        cmd = re.search(r'(?m)^CMD (.+)$', docker)[1]
        self.assertIn('"serve"', cmd)
        self.assertNotIn('provider_worker', docker)
        ignore = (ROOT / '.dockerignore').read_text().split()
        self.assertEqual(ignore[0], '*')
        self.assertEqual(sorted(x for x in ignore if x.startswith('!')), ['!LICENSE', '!THIRD_PARTY_NOTICES.md', '!fixtures/', '!mea/', '!web/'])


class BindTests(unittest.TestCase):
    def serve(self, *extra):
        port = free_port()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        proc = subprocess.Popen(['python3', '-B', '-m', 'mea', 'serve', '--port', str(port), '--task-dir', tmp.name, *extra],
                                cwd=ROOT, env=dict(PATH=os.environ['PATH'], LANG='C.UTF-8'),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: (proc.kill(), proc.wait(), proc.stdout.close(), proc.stderr.close()))
        line = proc.stdout.readline().decode()
        self.assertEqual(line.strip(), f'MEA OFFLINE http://127.0.0.1:{port}')
        return port

    def get(self, port, host, address='127.0.0.1'):
        for _ in range(50):
            try:
                c = http.client.HTTPConnection(address, port, timeout=5)
                c.putrequest('GET', '/api/demos', skip_host=True)
                c.putheader('Host', host)
                c.endheaders()
                return c.getresponse().status
            except ConnectionRefusedError:
                time.sleep(0.05)
        raise AssertionError('server did not start')

    def test_all_interfaces_still_require_the_loopback_host(self):
        port = self.serve('--bind', '0.0.0.0')
        self.assertEqual(self.get(port, f'127.0.0.1:{port}'), 200)
        self.assertEqual(self.get(port, f'evil.example:{port}'), 403)
        self.assertEqual(self.get(port, f'localhost:{port}'), 403)

    def test_default_bind_is_loopback_and_other_addresses_are_refused(self):
        port = self.serve()
        self.assertEqual(self.get(port, f'127.0.0.1:{port}'), 200)
        bad = subprocess.run(['python3', '-B', '-m', 'mea', 'serve', '--bind', '192.0.2.1'], cwd=ROOT, capture_output=True, timeout=30)
        self.assertEqual(bad.returncode, 2)
        self.assertIn(b'invalid choice', bad.stderr)


if __name__ == '__main__':
    unittest.main()
