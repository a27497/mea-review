"""Offline product HTTP, worker lease and actual process termination."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from mea.contract import decode
from mea.investigation import authorization, spending
from mea.investigation.real import meta
from mea.investigation.tasks import Store

ROOT = Path(__file__).resolve().parents[1]
MODE = authorization.MODE
FAULT_WORKER = """
import sys, time
from pathlib import Path
from mea.investigation.mock_provider import Sender
from mea.investigation.provider_worker import Broker
from mea.investigation.tasks import Store

store = Store(Path(sys.argv[1]))
probe = Path(sys.argv[2])
def fault(point, task):
    if point == 'after_launch':
        probe.write_text(task['requests'][-1]['id'])
        while True:
            time.sleep(0.1)

broker = Broker(store, sender=Sender(delay=0), fault=fault)
print('READY', flush=True)
while True:
    broker.heartbeat()
    for task in store.pending('MOCK_PROVIDER_NOT_AI'):
        broker.step(task['id'])
    time.sleep(0.05)
"""


class ProviderProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / 'tasks'
        self.snapshot = decode((ROOT / 'fixtures/investigation/phase3/case-a.json').read_bytes())

    def child(self, *args):
        process = subprocess.Popen([sys.executable, '-B', *args], cwd=ROOT, text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.addCleanup(self.close, process)
        self.assertTrue(select.select([process.stdout], [], [], 8)[0], 'Process startup timed out')
        return process, process.stdout.readline().strip()

    @staticmethod
    def close(process):
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        process.stdout.close()

    def call(self, path, data=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=8)
        try:
            if data is None:
                conn.request('GET', path)
            else:
                conn.request('POST', path, json.dumps(data), {'Content-Type': 'application/json'})
            response = conn.getresponse()
            return response.status, json.loads(response.read())
        finally:
            conn.close()

    def test_worker_refuses_by_default_before_opening_database(self):
        result = subprocess.run([sys.executable, '-B', '-m', 'mea.investigation.provider_worker',
                                 '--store', str(self.db)], cwd=ROOT, capture_output=True, text=True, timeout=8)
        self.assertEqual(result.returncode, 2)
        self.assertIn('live admission is not configured', result.stdout)
        self.assertFalse(self.db.exists())

    def test_concurrent_http_submit_and_real_process_kill_preserve_unknown_without_resend(self):
        _, url = self.child('-m', 'mea', 'serve', '--port', '0', '--task-dir', str(self.db))
        self.port = int(url.rsplit(':', 1)[1])
        self.assertFalse(self.call('/api/investigation-provider')[1]['ready'])
        body = dict(snapshot=self.snapshot, mode=MODE, confirmed=True)
        self.assertEqual(self.call('/api/tasks', body), (400, {'error': 'PROVIDER_WORKER_NOT_READY'}))
        self.assertEqual(self.call('/api/tasks', dict(body, mode='REAL_AI')),
                         (403, {'error': 'LIVE_ADMISSION_NOT_CONFIGURED'}))
        probe = self.root / 'launched'
        worker, ready = self.child('-c', FAULT_WORKER, str(self.db), str(probe))
        self.assertEqual(ready, 'READY')
        self.assertEqual(self.call('/api/tasks', dict(body, confirmed=False)),
                         (400, {'error': 'EXPLICIT_MOCK_START_REQUIRED'}))
        with ThreadPoolExecutor(max_workers=8) as pool:
            submissions = list(pool.map(lambda _: self.call('/api/tasks', body), range(8)))
        self.assertEqual({status for status, _ in submissions}, {200})
        identifiers = {row['id'] for _, row in submissions}
        self.assertEqual(len(identifiers), 1)
        identifier = identifiers.pop()
        second = subprocess.run([sys.executable, '-B', '-m', 'mea.investigation.provider_worker',
                                 '--mock', '--store', str(self.db)], cwd=ROOT,
                                capture_output=True, text=True, timeout=8)
        self.assertEqual(second.returncode, 2)
        deadline = time.monotonic() + 5
        while not probe.exists() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(probe.exists())
        store = Store(self.db)
        task = store.get(identifier)
        self.assertEqual(len(task['requests']), 1)
        request = task['requests'][0]
        self.assertEqual(request['state'], 'SENDING')
        self.assertEqual(request['id'], probe.read_text())
        reserve = request['shared_spending']['reserved_nano_usd']
        self.assertGreater(reserve, 0)

        worker.kill()
        self.assertEqual(worker.wait(timeout=5), -signal.SIGKILL)
        _, ready = self.child('-m', 'mea.investigation.provider_worker', '--mock', '--store', str(self.db))
        self.assertTrue(ready.startswith('READY ' + MODE))
        saved = store.get(identifier)
        self.assertEqual((saved['status'], saved['reason']), ('STOPPED', 'REQUEST_UNCERTAIN_ON_WORKER_RESTART'))
        self.assertEqual(saved['requests'][0]['state'], 'UNCERTAIN')
        self.assertIsNone(saved['requests'][0]['receipt'])
        self.assertEqual(saved['usage']['tokens_accounted'], request['token_reservation'])
        self.assertEqual(self.call('/api/tasks', body)[1]['id'], identifier)
        self.assertEqual(self.call('/api/task-json/' + identifier), (200, saved))
        with store.connection() as db:
            grant = meta(db, authorization.KEY)
            ledger = spending.summary(db)
        self.assertEqual(grant['tasks'], [identifier])
        self.assertEqual(grant['requests'], [request['id']])
        self.assertEqual(len(ledger['entries']), 1)
        self.assertEqual(ledger['entries'][0]['state'], 'UNCERTAIN')
        self.assertEqual(ledger['accounted_nano_usd'], reserve)
