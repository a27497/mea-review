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

    def test_authored_demo_binds_evidence_and_preserves_judgments_on_reload(self):
        self.assertEqual(self.call('risk-demo', {}, origin='https://evil.invalid')[0], 403)
        self.assertEqual(self.call('risk-demo', {'snapshot': {}})[0], 400)
        self.assertEqual(self.call('reviews'), (200, []))
        code, seed = self.call('risk-demo', {})
        self.assertEqual(code, 200)
        _, rows = self.call('reviews')
        self.assertEqual({row['pr']['number'] for row in rows}, {242, 246, 248, 251})
        reviews = {row['pr']['number']: self.call('review/' + row['id'])[1] for row in rows}
        self.assertEqual(seed['review_id'], reviews[248]['id'])
        task = reviews[248]['tasks'][0]
        candidate = task['candidates'][0]
        self.assertEqual(task['view']['coverage']['reported_changed_files'], 14)
        self.assertEqual(candidate['change'], dict(path='services/dispatch/settings.py', side='head',
                                                  line=11, revision=task['view']['revisions']['head']))
        self.assertEqual(reviews[248]['versions'][0]['writeback_mode'], 'LOCAL_PREVIEW_ONLY')
        self.assertEqual(reviews[242]['dispositions'][0]['disposition'], 'ACCEPTED')
        binding = dict(review_id=reviews[248]['id'], task_id=task['id'], candidate_id=candidate['id'])
        status, saved = self.call('disposition', dict(binding, disposition='IGNORED'))
        self.assertEqual(status, 200)
        self.assertEqual(self.call('risk-demo', {}), (200, seed))
        _, restored = self.call('review/' + reviews[248]['id'])
        self.assertEqual(restored['dispositions'], [saved])
        self.assertEqual(restored['sends'], [])

        incomplete = reviews[251]['tasks'][0]
        self.assertEqual(incomplete['reason'], 'EVIDENCE_INSUFFICIENT')
        self.assertEqual(incomplete['view']['coverage']['omitted_sources'], [
            dict(path='services/dispatch/worker.py', side='head', reason='SOURCE_NOT_CAPTURED'),
        ])
        self.assertFalse(any(ref['path'] == 'services/dispatch/worker.py'
                             for ref in incomplete['candidates'][0]['citations']))
        self.assertEqual(self.call('draft', dict(review_id=reviews[251]['id'], task_id=incomplete['id'],
                                                 candidate_id=incomplete['candidates'][0]['id'])),
                         (400, {'error': 'EVIDENCE_INSUFFICIENT_FOR_COMMENT'}))

    def test_disposition_is_bound_to_candidate_version_without_affecting_publication(self):
        _, first = self.call('demo', dict(version='v1'))
        _, review = self.call('create', dict(task_id=first['task_id']))
        self.assertEqual(review['dispositions'], [])
        candidate = review['tasks'][0]['candidates'][0]
        binding = dict(review_id=review['id'], task_id=first['task_id'], candidate_id=candidate['id'])
        for changes, error in [
            (dict(disposition=[]), 'DISPOSITION_INVALID'),
            (dict(task_id='task-' + '0' * 64), 'REVIEW_VERSION_NOT_LINKED'),
            (dict(candidate_id='candidate-' + '0' * 64), 'CANDIDATE_NOT_FOUND'),
        ]:
            status, body = self.call('disposition', dict(dict(binding, disposition='IGNORED'), **changes))
            self.assertEqual((status, body['error']), (400, error))
        self.assertEqual(self.call('disposition', dict(binding, disposition='ACCEPTED'), origin='https://evil.invalid')[0], 403)
        for judgment in ['ACCEPTED', 'IGNORED', 'UNDECIDED']:
            payload = dict(binding, disposition=judgment)
            status, record = self.call('disposition', payload)
            self.assertEqual(status, 200)
            self.assertEqual(record['identity']['anchor'], candidate['change'])
            self.assertEqual(record['disposition'], judgment)
            self.assertEqual(self.call('disposition', payload), (status, record))
            _, restored = self.call('review/' + review['id'])
            self.assertEqual(restored['dispositions'], [record])
            self.assertEqual(restored['drafts'], [])
            self.assertEqual(restored['sends'], [])
        _, next_task = self.call('demo', dict(version='v2'))
        _, increment = self.call('increment', dict(review_id=review['id'], task_id=next_task['task_id']))
        updated = dict(binding, task_id=next_task['task_id'], candidate_id=increment['tasks'][1]['candidates'][0]['id'])
        self.assertEqual(self.call('disposition', dict(updated, disposition='ACCEPTED'))[0], 200)
        _, restored = self.call('review/' + review['id'])
        self.assertEqual({d['task_id']: d['disposition'] for d in restored['dispositions']},
                         {first['task_id']: 'UNDECIDED', next_task['task_id']: 'ACCEPTED'})
        self.assertEqual(restored['sends'], [])
        self.assertEqual(self.server.human_reviews.remote.read(dict(repository='mea-owned/phase4-demo',number=1))['post_attempts'],0)


if __name__=='__main__':unittest.main()
