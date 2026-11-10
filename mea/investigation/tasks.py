"""SQLite task journal and bounded offline orchestration.

Intent is committed before computation; feedback before another decision. Only
pure offline decisions and immutable reads can resume. Unknown request intents
stop as RECOVERY_UNCERTAIN; no external transport is implemented in this phase.
"""
import copy
import fcntl
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from ..contract import canonical, digest, fields, text
from .tools import View, NOTICE, TOOL_VERSION
from .decision import OfflineDecider, POLICY, MODE, GOAL
from .validation import validate_candidates, RESULT_VERSION

ROOT=Path(__file__).resolve().parents[2]
TASKS=ROOT/'artifacts/investigation-tasks'
SCHEMA='mea-investigation-task-v1'
DEFAULTS=dict(rounds=10,tools=8,output_bytes=160000,seconds=60)
MAXIMUMS=dict(rounds=32,tools=24,output_bytes=500000,seconds=300)
MAX_ACTIVE=16
REASONS={'HYPOTHESIS_SUPPORTED','HYPOTHESIS_REFUTED','EVIDENCE_INSUFFICIENT','ZERO_CANDIDATES'}
TERMINAL={'COMPLETED','CANCELLED','STOPPED'}


def limits(value=None):
    result=dict(DEFAULTS)
    if value is not None:
        if not isinstance(value,dict) or set(value)-set(DEFAULTS):raise ValueError('BUDGET_KEYS')
        result.update(value)
    for k,v in result.items():
        if type(v) is not int or not 1<=v<=MAXIMUMS[k]:raise ValueError('BUDGET_BOUND')
    return result


class Store:
    def __init__(self, root=None):
        self.root=Path(root) if root is not None else TASKS
        self.root.mkdir(parents=True,exist_ok=True)
        self.path=self.root/'tasks.sqlite3'
        self.lock=threading.RLock()
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, identity TEXT UNIQUE NOT NULL, doc TEXT NOT NULL)')

    @contextmanager
    def connection(self):
        db=sqlite3.connect(self.path,timeout=5)
        try:
            db.execute('PRAGMA synchronous=FULL')
            with db:
                yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.lock, self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            yield db

    @contextmanager
    def launch_lock(self):
        # Serialize Mock dispatch, recovery and review-version updates across processes.
        with self.lock, (self.root / 'launch.lock').open('a') as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def _put(self,db,t):
        t['updated_at']=time.time()
        db.execute('UPDATE tasks SET doc=? WHERE id=?',(canonical(t).decode(),t['id']))

    def get(self,identifier):
        if not isinstance(identifier,str) or not re.fullmatch('task-[0-9a-f]{64}',identifier):raise ValueError('TASK_ID')
        with self.lock,self.connection() as db:
            row=db.execute('SELECT doc FROM tasks WHERE id=?',(identifier,)).fetchone()
        if not row:raise ValueError('TASK_NOT_FOUND')
        return json.loads(row[0])

    def list(self):
        with self.lock,self.connection() as db:
            rows=db.execute('SELECT doc FROM tasks ORDER BY rowid DESC LIMIT 200').fetchall()
        return [public(json.loads(row[0]),summary=True) for row in rows]

    def pending(self, mode=None):
        # Recover tasks even when the bounded recent-task display is full.
        with self.lock, self.connection() as db:
            rows = db.execute(
                "SELECT doc FROM tasks WHERE json_extract(doc,'$.status') NOT IN ('COMPLETED','STOPPED','CANCELLED') "
                "AND (? IS NULL OR json_extract(doc,'$.mode')=?) ORDER BY rowid LIMIT ?",
                (mode, mode, MAX_ACTIVE),
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def create(self,view,goal=GOAL,budget=None,source=None):
        text(goal,500)
        if not goal.strip():raise ValueError('GOAL_REQUIRED')
        budget=limits(budget)
        identity=dict(view_sha256=view.sha256,input_sha256=view.value['input_sha256'],revisions=view.value['revisions'],
                      policy_version=POLICY,tool_version=TOOL_VERSION,result_version=RESULT_VERSION,budget=budget,goal=goal)
        identifier='task-'+digest(identity)
        t=dict(schema=SCHEMA,id=identifier,identity=identity,mode=MODE,provider='NONE',source=source,
               pr=view.value['pr'],view=view.value,status='QUEUED',reason=None,goal=goal,budget=budget,
               usage=dict(rounds=0,tools=0,output_bytes=0),created_at=time.time(),updated_at=time.time(),
               events=[],results=[],candidates=[],rejected=[],active=None,notice=NOTICE)
        event(t,'created','Offline policy task saved. No model/target execution. Goal is a label, not a policy instruction.')
        with self.transaction() as db:
            if db.execute('SELECT 1 FROM tasks WHERE id=?',(identifier,)).fetchone():
                return self.get(identifier)
            active=db.execute("SELECT count(*) FROM tasks WHERE json_extract(doc,'$.status') NOT IN ('COMPLETED','STOPPED','CANCELLED')").fetchone()[0]
            if active>=MAX_ACTIVE:raise ValueError('ACTIVE_TASK_BOUND')
            db.execute('INSERT OR IGNORE INTO tasks VALUES (?,?,?)',(identifier,digest(identity),canonical(t).decode()))
        return self.get(identifier)

    def cancel(self, identifier):
        from . import real

        with self.launch_lock(), self.transaction() as db:
            task = self.get(identifier)
            if task['status'] not in TERMINAL:
                task.update(status='CANCELLED', reason='USER_CANCELLED')
                if task['mode'] == real.MODE:
                    real.interrupt_request(db, task)
                    task['activation_revoked'] = True
                    if any(r['state'] == 'UNCERTAIN' for r in task['requests']):
                        task['reason'] = 'USER_CANCELLED_REQUEST_UNCERTAIN'
                event(task, 'cancelled', 'Cancellation committed; no new step may start.')
                self._put(db, task)
        return self.get(identifier)


def event(t,kind,summary,**data):
    t['events'].append(dict(seq=len(t['events'])+1,kind=kind,summary=summary,at=time.time(),**data))


def public(t,summary=False):
    result={k:copy.deepcopy(v) for k,v in t.items() if k!='view'}
    if summary:
        for k in ['events','results','active','candidates','rejected']:result.pop(k,None)
    return result


class Runner:
    def __init__(self,store,decider=None,fault=None):
        self.store=store;self.decider=decider or OfflineDecider();self.fault=fault or (lambda *_:None)

    def recover(self):
        for row in self.store.pending(MODE):
            with self.store.lock,self.store.connection() as db:
                t=self.store.get(row['id'])
                if t['status'] in TERMINAL:continue
                active=t['active']
                if not self.compatible(t):
                    t.update(status='STOPPED',reason='RECOVERY_POLICY_MISMATCH')
                    event(t,'stopped','Policy/tool/result version changed; automatic replay forbidden.')
                elif active and active['type'] not in {'offline_decision','frozen_tool','finish'}:
                    t.update(status='STOPPED',reason='RECOVERY_UNCERTAIN')
                    event(t,'stopped','Uncertain external request outcome; automatic resend forbidden.')
                else:
                    if active and active['type']=='offline_decision' and active.get('policy_version')!=POLICY:
                        t.update(status='STOPPED',reason='RECOVERY_POLICY_MISMATCH')
                    else:
                        t['status']='QUEUED'
                        event(t,'recovered','Only committed offline decision/frozen-read intent will resume; counters remain charged.')
                self.store._put(db,t)

    def step(self,identifier):
        # Launch and cancellation serialize on the same lock. Computing occurs
        # outside it, so cancel remains responsive. No retry on unknown requests.
        with self.store.lock,self.store.connection() as db:
            t=self.store.get(identifier)
            if t['status'] in TERMINAL or t['mode'] != MODE:return False
            if not self.compatible(t):
                self._stop(db,t,'RECOVERY_POLICY_MISMATCH');return False
            if time.time()-t['created_at']>=t['budget']['seconds']:
                self._stop(db,t,'TIME_BUDGET');return False
            active=t['active']
            if active is None:
                if t['usage']['rounds']>=t['budget']['rounds']:
                    self._stop(db,t,'ROUND_BUDGET');return False
                active=dict(type='offline_decision',policy_version=POLICY,started=True)
                t['active']=active;t['usage']['rounds']+=1
                event(t,'decision_started','Run deterministic offline decision; no provider request.')
            elif active['type']=='frozen_tool' and not active.get('started'):
                if t['usage']['tools']>=t['budget']['tools']:
                    self._stop(db,t,'TOOL_BUDGET');return False
                active['started']=True;t['usage']['tools']+=1
                event(t,'tool_started',active['decision']['summary'],tool=active['decision']['tool'],args=active['decision']['args'])
            elif active['type'] not in {'offline_decision','frozen_tool','finish'}:
                self._stop(db,t,'RECOVERY_UNCERTAIN');return False
            t['status']='RUNNING';self.store._put(db,t)
        self.fault('after_intent',t)
        try:
            view=View(t['view'])
            if view.sha256!=t['identity']['view_sha256']:raise ValueError('TASK_VIEW_VERSION_MISMATCH')
            if active['type']=='offline_decision':
                output=self.decider.decide(dict(goal=t['goal'],paths=list(view.files),revisions=view.value['revisions'],results=copy.deepcopy(t['results'])))
            elif active['type']=='frozen_tool':
                d=active['decision'];output=view.execute(d['tool'],d['args'])
            else:
                d=active['decision'];output=validate_candidates(view,d['candidates'],t['results'])
            self.fault('after_compute',t)
        except (ValueError,KeyError,TypeError,RecursionError,UnicodeError):
            with self.store.lock,self.store.connection() as db:
                current=self.store.get(identifier)
                if current['status'] not in TERMINAL:self._stop(db,current,'INVALID_DECISION_OR_TOOL_OR_VERSION')
            return False
        with self.store.lock,self.store.connection() as db:
            current=self.store.get(identifier)
            if current['status'] in TERMINAL:return False
            if time.time()-current['created_at']>=current['budget']['seconds']:
                self._stop(db,current,'TIME_BUDGET');return False
            size=len(canonical(output))
            if current['usage']['output_bytes']+size>current['budget']['output_bytes']:
                self._stop(db,current,'OUTPUT_BUDGET');return False
            current['usage']['output_bytes']+=size
            if active['type']=='offline_decision':
                try:
                    self._decision(output)
                except (ValueError,KeyError,TypeError):
                    self._stop(db,current,'INVALID_DECISION');return False
                kind='frozen_tool' if output['kind']=='tool' else 'finish'
                current['active']=dict(type=kind,decision=output,started=False)
                event(current,'decision',output['summary'],decision=output)
            elif active['type']=='frozen_tool':
                current['results'].append(output);current['active']=None
                event(current,'tool_result','Frozen feedback committed before the next decision.',result_index=len(current['results'])-1,content_sha256=output['content_sha256'])
            else:
                current['candidates'],current['rejected']=output
                reason=active['decision']['reason']
                if current['rejected']:reason='CANDIDATE_VALIDATION_REJECTED'
                if reason=='HYPOTHESIS_SUPPORTED' and not current['candidates']:reason='EVIDENCE_INSUFFICIENT'
                current.update(status='COMPLETED',reason=reason,active=None,result_version=RESULT_VERSION)
                event(current,'finished',active['decision']['summary'],reason=reason,citation_validity='VALID' if current['candidates'] else 'NO_ACCEPTED_CANDIDATES',inference='CONDITIONAL_INFERENCE' if current['candidates'] else 'EVIDENCE_INSUFFICIENT_OR_NO_CANDIDATE')
            self.store._put(db,current)
        self.fault('after_commit',current)
        return current['status'] not in TERMINAL

    @staticmethod
    def compatible(t):
        i=t['identity']
        return i['policy_version']==POLICY and i['tool_version']==TOOL_VERSION and i['result_version']==RESULT_VERSION

    @staticmethod
    def _decision(d):
        if not isinstance(d,dict):raise ValueError('DECISION_SCHEMA')
        text(d['summary'],1200)
        if d['kind']=='tool':
            fields(d,'kind tool args summary');text(d['tool'],60)
            if not isinstance(d['args'],dict) or len(canonical(d))>3000:raise ValueError('DECISION_BOUND')
        elif d['kind']=='finish':
            fields(d,'kind reason summary candidates')
            if d['reason'] not in REASONS or not isinstance(d['candidates'],list) or len(canonical(d))>20000:raise ValueError('FINISH_SCHEMA')
        else:raise ValueError('DECISION_KIND')

    def _stop(self,db,t,reason):
        t.update(status='STOPPED',reason=reason)
        event(t,'stopped',reason+'. No further steps will start; this is not approval.')
        self.store._put(db,t)


class Manager:
    def __init__(self,root=None):
        self.store=Store(root)
        self.lease=(self.store.root/'runner.lock').open('a')
        try:fcntl.flock(self.lease,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.lease.close();raise ValueError('TASK_STORE_ALREADY_OWNED') from None
        from .real import Coordinator
        try:
            self.coordinator = Coordinator(self.store)
            self.coordinator.boot()
            self.runner = Runner(self.store)
            self.runner.recover()
            self.closed = threading.Event()
            self.thread = threading.Thread(target=self._work, daemon=True)
            self.thread.start()
        except BaseException:
            self.lease.close()
            raise

    def _work(self):
        while not self.closed.wait(.15):
            self.coordinator.tick()
            for row in self.store.pending(MODE):
                if self.closed.is_set():
                    break
                try:
                    self.runner.step(row['id'])
                except Exception:
                    with self.store.transaction() as db:
                        task = self.store.get(row['id'])
                        if task['status'] not in TERMINAL:
                            self.runner._stop(db, task, 'INTERNAL_ERROR')

    def close(self):
        self.closed.set()
        self.thread.join(timeout=5)
        try:
            self.coordinator.shutdown()
        finally:
            self.lease.close()


def markdown(t):
    from ..export import plain
    out=['# Offline investigation · '+t['mode'],'',t['id'],'','Goal: '+plain(t['goal']),
         'Status: '+t['status']+' · '+str(t['reason']),'',
         'Input SHA-256: '+t['identity']['input_sha256'],'View SHA-256: '+t['identity']['view_sha256'],
         'Versions: '+json.dumps(t['identity']['revisions']),
         'Policy: '+t['identity']['policy_version']+' · Tools: '+t['identity']['tool_version']+' · Result: '+t['identity']['result_version'],
         'Budget: '+json.dumps(t['budget'])+' · Usage: '+json.dumps(t['usage']),'',plain(t['notice']),
         '', 'No model called. Offline policy validates orchestration only. Citation validity is not correctness.','']
    for request in t.get('requests', []):
        out += ['', 'Mock request: ' + request['id'] + ' · ' + request['state'],
                'Request body SHA-256: ' + request['request_body_sha256'],
                'Simulated reservation: ' + plain(json.dumps(request['shared_spending']))]
        for message in request.get('tool_messages', []):
            out.append('Feedback tool call: ' + plain(message['tool_call_id']))
    for e in t['events']:out.append(str(e['seq'])+'. '+e['kind']+' · '+plain(e['summary']))
    for r in t['results']:
        out+=['','Tool: '+r['tool']+' · '+r['revision']+' · '+r['content_sha256'],plain(str(r['unknown']))]
        out += [plain(e['path'])+':'+str(e['line'])+' · '+e['side']+' · '+e['revision']+' · '+e['content_sha256']+'\n> '+plain(e['quote']) for e in r['evidence']]
    for c in t['candidates']:
        out+=['','## '+plain(c['title']),plain(c['trigger']),plain(c['impact']),
              'Citation validation: VALID · CONDITIONAL_INFERENCE · correctness UNADJUDICATED']
    if t['rejected']:out+=['','Rejected candidate count: '+str(len(t['rejected']))]
    return '\n'.join(out)+'\n'
