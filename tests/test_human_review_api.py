"""Actual loopback product HTTP API with isolated DB, no GitHub/model network."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from mea.server import Handler
from mea.investigation.tasks import Manager
from mea.human_review.service import Service


class HumanReviewApiTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.investigations=Manager(Path(self.temp.name));self.server.human_reviews=Service(self.server.investigations.store);self.server.human_reviews.recover()
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();self.addCleanup(self.close)
        self.base='http://127.0.0.1:'+str(self.server.server_port)
    def close(self):
        self.server.shutdown();self.thread.join();self.server.investigations.close();self.server.server_close()
    def call(self,route,data=None,origin=None):
        conn=http.client.HTTPConnection('127.0.0.1',self.server.server_port);headers={'Origin':origin or self.base}
        if data is not None:headers['Content-Type']='application/json'
        conn.request('GET' if data is None else 'POST','/api/human/'+route,body=json.dumps(data) if data is not None else None,headers=headers)
        r=conn.getresponse();raw=r.read();status=r.status;conn.close()
        return status,json.loads(raw) if raw.startswith((b'{',b'[')) else raw.decode()
    def setup_draft(self):
        code,t=self.call('demo',dict(version='v1'));self.assertEqual(code,200)
        code,r=self.call('create',dict(task_id=t['task_id']));self.assertEqual(code,200)
        c=r['tasks'][0]['candidates'][0];code,d=self.call('draft',dict(review_id=r['id'],task_id=t['task_id'],candidate_id=c['id']));self.assertEqual(code,200)
        return r,d
    def test_http_exact_preview_confirm_send_and_read_only_exports(self):
        r,d=self.setup_draft();code,p=self.call('preview',dict(draft_id=d['id']));self.assertEqual(code,200)
        code,c=self.call('confirm',dict(draft_id=d['id'],preview=p,human_confirmed=True));self.assertEqual(code,200)
        for _ in range(2):
            code,s=self.call('send',dict(draft_id=d['id'],confirmation_digest=p['confirmation_digest'],mode='MOCK'));self.assertEqual(code,200);self.assertEqual(s['status'],'MOCK_PUBLISHED')
        self.assertEqual(self.server.human_reviews.remote.read(p['target'])['post_attempts'],1)
        for route in ['review/','export-json/','export-md/']:
            code,body=self.call(route+r['id']);self.assertEqual(code,200)
        self.assertEqual(self.server.human_reviews.remote.read(p['target'])['post_attempts'],1)
    def test_cross_origin_unknown_fields_unsafe_paths_and_live_sends_block(self):
        r,d=self.setup_draft()
        code,_=self.call('send',dict(draft_id=d['id'],confirmation_digest='anything',mode='REAL'));self.assertEqual(code,400)
        code,_=self.call('demo',dict(version='../../etc/passwd'));self.assertEqual(code,400)
        code,_=self.call('preview',dict(draft_id=d['id'],shell='pwd'));self.assertEqual(code,400)
        code,_=self.call('demo',dict(version='v2'),origin='https://evil.invalid');self.assertEqual(code,403)
        self.assertEqual(self.server.human_reviews.remote.read(dict(repository='mea-owned/phase4-demo',number=1))['post_attempts'],0)
    def test_http_manual_increment_is_idempotent_and_does_not_publish(self):
        r,d=self.setup_draft()
        for version in ['v2','v3','v4','v5']:
            code,t=self.call('demo',dict(version=version));self.assertEqual(code,200)
            for _ in range(2):
                code,r=self.call('increment',dict(review_id=r['id'],task_id=t['task_id']));self.assertEqual(code,200)
        self.assertEqual(len(r['versions']),5);self.assertEqual(r['comparisons'][2]['rows'][0]['status'],'OUTDATED')
        self.assertEqual(self.server.human_reviews.remote.read(dict(repository='mea-owned/phase4-demo',number=1))['post_attempts'],0)


if __name__=='__main__':unittest.main()
