import copy
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from mea.contract import canonical, digest, git_blob_sha
from mea.investigation.tools import View, LIMITS
from mea.investigation.tasks import Store, Runner, Manager, public, markdown, TERMINAL, POLICY
from mea.investigation.decision import OfflineDecider, stop, tool
from mea.investigation.validation import validate_candidates
from investigation_fixtures import capture, changed_file, diff, snapshot, AFTER, CALLER, GUARDED, TEST


class Crash(BaseException):pass


class InvestigationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name)
        self.store=Store(self.root)
        self.view=View.from_input(capture())
        self.rev=self.view.value['revisions']['head']

    def run_task(self,view=None,budget=None,decider=None):
        t=self.store.create(view or self.view,budget=budget)
        runner=Runner(self.store,decider)
        for _ in range(100):
            if not runner.step(t['id']):break
        else:self.fail('Loop did not stop')
        return self.store.get(t['id'])

    def test_tools_crosscheck_known_frozen_source_and_expected_lines(self):
        # Expected positions come from fixture literals, independently of tools.
        self.assertEqual(self.view.sources['head','src/f.py']['text'],AFTER)
        self.assertEqual(self.view.sources['head','src/caller.py']['text'],CALLER)
        for name,args,expected in [
            ('read_source',dict(path='src/f.py',start=1,end=2),[('src/f.py',1,'def lookup():'),('src/f.py',2,'    return None')]),
            ('find_symbol',dict(symbol='lookup'),[('src/f.py',1,'def lookup():')]),
            ('references',dict(symbol='lookup'),[('src/caller.py',4,'    value = lookup()'),('tests/test_lookup.py',4,'    assert lookup() == 1')]),
            ('search',dict(query='return None'),[('src/f.py',2,'    return None')]),
            ('read_test',dict(path='tests/test_lookup.py',start=4,end=4),[('tests/test_lookup.py',4,'    assert lookup() == 1')])]:
            result=self.view.execute(name,dict(revision=self.rev,**args))
            self.assertEqual([(e['path'],e['line'],e['quote']) for e in result['evidence']],expected)
            for e in result['evidence']:
                source={'src/f.py':AFTER,'src/caller.py':CALLER,'tests/test_lookup.py':TEST}[e['path']]
                self.assertEqual(e['content_sha256'],digest(source.encode()))
                self.assertEqual(e['view_sha256'],self.view.sha256)
        diff=self.view.execute('read_diff',dict(path='src/f.py',revision=self.rev))
        self.assertEqual(diff['data'],'@@ -1,2 +1,2 @@\n def lookup():\n-    return 1\n+    return None\n')
        self.assertEqual([(e['line'],e['quote']) for e in diff['evidence']],[(2,'    return None')])

    def test_importable_frozen_examples_match_known_source_and_policy_results(self):
        from mea.contract import decode
        root=Path(__file__).resolve().parents[1]/'fixtures/demo'
        for name,expected,caller in [('none-return','HYPOTHESIS_SUPPORTED',CALLER),('counterexample','HYPOTHESIS_REFUTED',GUARDED)]:
            view=View.from_input(decode((root/(name+'.json')).read_bytes()))
            self.assertEqual(view.sources['head','src/f.py']['text'],AFTER)
            self.assertEqual(view.sources['head','src/caller.py']['text'],caller)
            self.assertEqual(self.run_task(view)['reason'],expected)

    def test_active_queue_is_bounded_and_duplicate_still_reopens(self):
        from mea.investigation.tasks import MAX_ACTIVE
        first=None
        for i in range(MAX_ACTIVE):
            task=self.store.create(self.view,goal=str(i))
            if i==0:first=task
        self.assertEqual(len(self.store.pending()),MAX_ACTIVE)
        self.assertEqual(self.store.create(self.view,goal='0')['id'],first['id'])
        with self.assertRaisesRegex(ValueError,'ACTIVE_TASK_BOUND'):self.store.create(self.view,goal='overflow')

    def test_uncollected_reads_never_fall_back_to_host_files(self):
        with patch('builtins.open',side_effect=AssertionError('No host read')):
            result=self.view.execute('read_source',dict(path='missing.py',revision=self.rev,start=1,end=2))
        self.assertEqual(result['unknown'],['SOURCE_NOT_COLLECTED'])
        self.assertEqual(result['evidence'],[])

    def test_tools_do_not_claim_complete_dynamic_references(self):
        result=self.view.execute('references',dict(symbol='uncollected_dynamic_name',revision=self.rev))
        self.assertEqual(result['evidence'],[])
        self.assertEqual(result['coverage'],'COLLECTED_FILES_ONLY')
        self.assertIn('dynamic dispatch',result['notice'])

    def test_tool_hit_and_byte_caps_are_explicit_and_include_content_digest(self):
        for line,expected in [('# needle\n','hits'),('# needle '+('x'*1000)+'\n','bytes')]:
            value = snapshot([changed_file('src/f.py', AFTER, AFTER + line * 35)])
            view = View.from_input(value)
            result=view.execute('search',dict(query='needle',revision=self.rev))
            self.assertTrue(result['truncated'])
            self.assertLessEqual(len(canonical(result)),LIMITS['result_bytes'])
            if expected=='hits':self.assertEqual(len(result['evidence']),32)
            else:
                self.assertEqual(result['evidence'],[])
                self.assertIn('TOOL_OUTPUT_BYTE_BOUND',result['unknown'])

    def test_invalid_candidate_is_rejected_by_the_complete_loop(self):
        class Forged(OfflineDecider):
            def decide(_,context):
                d=super().decide(context)
                if d['kind']=='finish' and d['candidates']:
                    d['candidates'][0]['evidence_ids']=['forged-location']
                return d
        t=self.run_task(decider=Forged())
        self.assertEqual(t['reason'],'CANDIDATE_VALIDATION_REJECTED')
        self.assertEqual(t['candidates'],[]);self.assertEqual(len(t['rejected']),1)

    def test_feedback_changes_subsequent_decisions_and_crossfile_candidate(self):
        supported=self.run_task()
        refuted=self.run_task(View.from_input(capture(guard=True)))
        self.assertEqual(supported['reason'],'HYPOTHESIS_SUPPORTED')
        self.assertEqual(refuted['reason'],'HYPOTHESIS_REFUTED')
        a=[e['decision']['tool'] for e in supported['events'] if e['kind']=='decision' and e['decision']['kind']=='tool']
        b=[e['decision']['tool'] for e in refuted['events'] if e['kind']=='decision' and e['decision']['kind']=='tool']
        self.assertEqual(a,['read_diff','read_source','find_symbol','references','read_source','read_test'])
        self.assertEqual(b,a[:-1])
        self.assertEqual(supported['results'][4]['data'][4]['text'],'    return value + 1')
        self.assertEqual(refuted['results'][4]['data'][4]['text'],'    if value is None:')
        c=supported['candidates'][0]
        self.assertEqual({e['path'] for e in c['citations']},{'src/f.py','src/caller.py'})
        self.assertEqual(c['citation_validity'],'VALID');self.assertEqual(c['inference'],'CONDITIONAL_INFERENCE')
        self.assertEqual(c['correctness'],'UNADJUDICATED');self.assertEqual(refuted['candidates'],[])
        kinds=[e['kind'] for e in supported['events']]
        self.assertLess(kinds.index('decision'),kinds.index('tool_started'))
        self.assertLess(kinds.index('tool_result'),kinds.index('decision',kinds.index('tool_result')))

    def test_insufficient_and_zero_candidates_are_distinct(self):
        self.assertEqual(self.run_task(View.from_input(capture(missing=True)))['reason'],'EVIDENCE_INSUFFICIENT')
        # Source unchanged: diff is absent, so insufficiency is the correct result.
        v=View.from_input(capture());f=v.value['files'][0]
        f['sides']['head']=copy.deepcopy(f['sides']['base'])
        f['sides']['head']['text']='def lookup():\n    return 2\n'
        raw=f['sides']['head']['text'].encode();f['sides']['head'].update(sha256=digest(raw),blob_sha=git_blob_sha(raw))
        f['patch']=diff(f['sides']['base']['text'],f['sides']['head']['text'])
        result=self.run_task(View(v.value))
        self.assertEqual(result['reason'],'ZERO_CANDIDATES');self.assertEqual(result['candidates'],[])

    def test_malicious_source_is_inert_and_does_not_authorize_actions(self):
        with patch('socket.create_connection',side_effect=AssertionError('No network')),patch('subprocess.run',side_effect=AssertionError('No execution')):
            t=self.run_task(View.from_input(capture(malicious=True)))
        self.assertEqual(t['reason'],'HYPOTHESIS_SUPPORTED')
        self.assertTrue(any('<script>' in str(r['data']) for r in t['results']))
        self.assertEqual({r['tool'] for r in t['results']},{'read_diff','read_source','find_symbol','references','read_test'})
        self.assertEqual(t['provider'],'NONE')

    def test_tool_and_path_permission_arguments_and_revision_enforced(self):
        for name,args in [
            ('shell',{'command':'id','revision':self.rev}),('http',{'url':'https://example.org','revision':self.rev}),
            ('read_source',dict(path='/etc/passwd',start=1,end=1,revision=self.rev)),
            ('read_source',dict(path='../outside.py',start=1,end=1,revision=self.rev)),
            ('read_source',dict(path='src/f.py',start=1,end=8000,revision=self.rev)),
            ('read_diff',dict(path='src/f.py',revision='f'*40)),
            ('search',dict(query='return',revision=self.rev,extra=True))]:
            with self.subTest(name=name,args=args),self.assertRaises(ValueError):self.view.execute(name,args)
        missing=self.view.execute('read_test',dict(path='tests/test_uncollected.py',start=1,end=4,revision=self.rev))
        self.assertEqual(missing['evidence'],[]);self.assertEqual(missing['unknown'],['SOURCE_NOT_COLLECTED'])
        with self.assertRaises(ValueError):self.view.execute('read_test',dict(path='src/f.py',start=1,end=2,revision=self.rev))

    def test_invalid_reference_crossversion_change_and_duplicates_rejected(self):
        t=self.run_task();candidate={k:v for k,v in t['candidates'][0].items() if k in {'title','trigger','change','impact','evidence_ids'}}
        for mutate in [lambda c:c['evidence_ids'].append('ev-forged'),lambda c:c['change'].update(revision='f'*40),
                       lambda c:c['change'].update(line=1),lambda c:c['change'].update(path='other.py')]:
            bad=copy.deepcopy(candidate);mutate(bad)
            accepted,rejected=validate_candidates(self.view,[bad],t['results'])
            self.assertEqual(accepted,[]);self.assertEqual(len(rejected),1)
        forged=copy.deepcopy(t['results']);forged[0]['evidence'][0]['quote']='invented'
        # Tamper every occurrence of the reference, including repeated tool reads.
        for r in forged:
            for e in r['evidence']:
                if e['id']==candidate['evidence_ids'][0]:e['line']=7000
        self.assertEqual(validate_candidates(self.view,[candidate],forged)[0],[])
        duplicate=copy.deepcopy(candidate);duplicate['title']='Same location, reworded'
        a,r=validate_candidates(self.view,[candidate,duplicate],t['results'])
        self.assertEqual((len(a),len(r)),(1,1))

    def test_invalid_provider_decision_cannot_dispatch_privileged_tool(self):
        class Bad:
            def decide(_,context):return tool('shell',self.rev,'Untrusted attempt',command='id')
        t=self.run_task(decider=Bad())
        self.assertEqual(t['reason'],'INVALID_DECISION_OR_TOOL_OR_VERSION');self.assertEqual(t['results'],[])

    def test_all_four_budget_dimensions_stop(self):
        for budget,reason in [({'rounds':1},'ROUND_BUDGET'),({'tools':1},'TOOL_BUDGET'),({'output_bytes':1},'OUTPUT_BUDGET')]:
            with self.subTest(budget=budget):self.assertEqual(self.run_task(budget=budget)['reason'],reason)
        t=self.store.create(self.view,budget={'seconds':1})
        with self.store.connection() as db:
            t['created_at']-=2;self.store._put(db,t)
        self.assertFalse(Runner(self.store).step(t['id']))
        self.assertEqual(self.store.get(t['id'])['reason'],'TIME_BUDGET')
        for bad in [{'tools':0},{'rounds':33},{'seconds':True},{'output_bytes':float('inf')},{'unknown':1}]:
            with self.assertRaises(ValueError):self.store.create(self.view,budget=bad)

    def test_repeated_and_concurrent_submit_do_not_create_duplicate_execution(self):
        ids=[]
        threads=[threading.Thread(target=lambda:ids.append(self.store.create(self.view)['id'])) for _ in range(6)]
        for thread in threads:thread.start()
        for thread in threads:thread.join()
        self.assertEqual(len(set(ids)),1);self.assertEqual(len(self.store.list()),1)
        t=self.run_task();self.assertEqual(self.store.create(self.view)['events'],t['events'])
        self.assertNotEqual(self.store.create(self.view,goal='Separate experiment')['id'],t['id'])
        changed=copy.deepcopy(self.view.value);changed['revisions']['head']='f'*40
        self.assertNotEqual(self.store.create(View(changed))['id'],t['id'])

    def test_cancel_before_and_during_step_launches_no_followup(self):
        t=self.store.create(self.view);self.store.cancel(t['id'])
        self.assertFalse(Runner(self.store).step(t['id']));self.assertEqual(self.store.get(t['id'])['usage']['rounds'],0)
        t=self.store.create(self.view,goal='cancel during decision')
        class Cancel:
            def decide(_,context):
                self.store.cancel(t['id'])
                return OfflineDecider().decide(context)
        self.assertFalse(Runner(self.store,Cancel()).step(t['id']))
        saved=self.store.get(t['id']);self.assertEqual(saved['status'],'CANCELLED');self.assertEqual(saved['results'],[])
        self.assertIsNone(saved['active'].get('decision'))
        Runner(self.store).recover();self.assertFalse(Runner(self.store).step(t['id']))
        self.assertEqual(self.store.create(self.view,goal='cancel during decision')['status'],'CANCELLED')

    def test_fault_injection_replays_only_pure_intents_without_double_charge(self):
        for point in ['after_intent','after_compute','after_commit']:
            for crash_tool in [False,True]:
                with self.subTest(point=point,tool=crash_tool):
                    store=Store(self.root/(point+str(crash_tool)))
                    t=store.create(self.view);normal=Runner(store)
                    if crash_tool:normal.step(t['id'])
                    def crash(p,_):
                        if p==point:raise Crash()
                    with self.assertRaises(Crash):Runner(store,fault=crash).step(t['id'])
                    restarted=Store(store.root);runner=Runner(restarted);runner.recover()
                    for _ in range(100):
                        if not runner.step(t['id']):break
                    final=restarted.get(t['id'])
                    self.assertEqual(final['reason'],'HYPOTHESIS_SUPPORTED')
                    self.assertEqual(final['usage']['tools'],6)
                    self.assertEqual(final['usage']['rounds'],7)
                    self.assertEqual(len(final['results']),6)

    def test_uncertain_external_intent_stops_never_retried(self):
        t=self.store.create(self.view)
        with self.store.connection() as db:
            t.update(status='RUNNING',active=dict(type='external_request',request_id='unknown-request-outcome'))
            self.store._put(db,t)
        r=Runner(self.store);r.recover()
        self.assertEqual(self.store.get(t['id'])['reason'],'RECOVERY_UNCERTAIN');self.assertFalse(r.step(t['id']))

    def test_view_tamper_and_policy_mismatch_stop_recovery(self):
        t=self.store.create(self.view)
        with self.store.connection() as db:
            t['view']['revisions']['head']='f'*40;self.store._put(db,t)
        Runner(self.store).step(t['id'])
        self.assertEqual(self.store.get(t['id'])['reason'],'INVALID_DECISION_OR_TOOL_OR_VERSION')
        t=self.store.create(self.view,goal='stale policy')
        with self.store.connection() as db:
            t['active']=dict(type='offline_decision',policy_version='old',started=True);self.store._put(db,t)
        Runner(self.store).recover()
        self.assertEqual(self.store.get(t['id'])['reason'],'RECOVERY_POLICY_MISMATCH')

    def test_manager_single_owner_persistence_and_export(self):
        manager=Manager(self.root/'managed');self.addCleanup(manager.close)
        with self.assertRaises(ValueError):Manager(self.root/'managed')
        t=manager.store.create(self.view)
        deadline=time.monotonic()+10
        while manager.store.get(t['id'])['status'] not in TERMINAL and time.monotonic()<deadline:time.sleep(.05)
        saved=manager.store.get(t['id']);self.assertEqual(saved['reason'],'HYPOTHESIS_SUPPORTED')
        self.assertEqual(Store(self.root/'managed').get(t['id']),saved)
        self.assertNotIn('view',public(saved));self.assertIn('OFFLINE_POLICY_NOT_AI',markdown(saved))
        self.assertIn('CONDITIONAL_INFERENCE',markdown(saved));self.assertIn('src/caller.py:4',markdown(saved))
