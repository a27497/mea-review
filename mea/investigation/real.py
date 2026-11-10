"""Credential-free task admission, provider coordination and deterministic tools.

The HTTP process never imports the credential reader or transport supervisor.
Only an explicit create request may bind a fresh task to a local Mock worker session.
"""
import json
import os
import time
from ..contract import canonical, digest, text
from . import real_protocol as protocol
from . import native, context_budget as window
from . import authorization, spending
from .tasks import event, TERMINAL, MAX_ACTIVE, SCHEMA
from .provider_view import View, NOTICE, TOOL_VERSION
from .validation import validate_candidates, RESULT_VERSION

MODE = authorization.MODE


def ensure(store):
    with store.connection() as db:
        db.execute('CREATE TABLE IF NOT EXISTS task_meta (name TEXT PRIMARY KEY, doc TEXT NOT NULL)')


def meta(db,name,default=None):
    row=db.execute('SELECT doc FROM task_meta WHERE name=?',(name,)).fetchone()
    return json.loads(row[0]) if row else default


def put_meta(db,name,value):
    db.execute('INSERT OR REPLACE INTO task_meta VALUES (?,?)',(name,canonical(value).decode()))


def readiness(store):
    with store.connection() as db:
        return status(db)


def status(db):
    row = meta(db, 'provider', {})
    epoch = meta(db, 'epoch')
    ready = bool(row and epoch and row.get('epoch') == epoch and row.get('status') == 'READY'
                 and row.get('mode') == MODE and row.get('authorization') == authorization.GRANT
                 and 0 <= time.time() - row.get('heartbeat', 0) < 3)
    return dict(ready=ready, mode=MODE if ready else 'UNAVAILABLE', model=protocol.MODEL,
                real_enabled=False, real_reason='LIVE_ADMISSION_NOT_CONFIGURED',
                session=row.get('session') if ready else None,
                authorization=authorization.GRANT if ready else None, limits=protocol.LIMITS,
                context_policy=window.identity())


def create(store, value, goal=protocol.GOAL, budget=None, *, mode=MODE, confirmed=False,
           context_limits=None):
    if mode == 'REAL_AI':
        raise ValueError('LIVE_ADMISSION_NOT_CONFIGURED')
    if mode != MODE or confirmed is not True:
        raise ValueError('EXPLICIT_MOCK_START_REQUIRED')
    view = View.from_input(value)
    text(goal, 500)
    if not goal.strip():
        raise ValueError('GOAL_REQUIRED')
    b=protocol.budget(budget)
    identity=dict(view_sha256=view.sha256,input_sha256=view.value['input_sha256'],revisions=view.value['revisions'],policy_version=protocol.POLICY,
                  tool_version=TOOL_VERSION,result_version=RESULT_VERSION,budget=b,goal=goal,mode=MODE,authorization=authorization.GRANT,model=protocol.MODEL,prompt_sha256=protocol.PROMPT_SHA256,protocol_version=native.VERSION,tool_schema_sha256=native.SCHEMA_SHA256,context_policy=window.identity(context_limits))
    authorization.validate_task(identity)
    with store.transaction() as db:
        ready = status(db)
        identifier='task-'+digest(identity)
        if db.execute('SELECT 1 FROM tasks WHERE id=?',(identifier,)).fetchone():return store.get(identifier)
        if not ready['ready'] or ready['mode']!=mode:raise ValueError('PROVIDER_WORKER_NOT_READY')
        spending.ready(db)
        grant_key, grant, caps = authorization.ledger(db)
        if len(grant['tasks'])>=caps[0]:raise ValueError('PHASE3_TASK_CAP')
        count=db.execute("SELECT count(*) FROM tasks WHERE json_extract(doc,'$.status') NOT IN ('COMPLETED','STOPPED','CANCELLED')").fetchone()[0]
        if count>=MAX_ACTIVE:raise ValueError('ACTIVE_TASK_BOUND')
        t=dict(schema=SCHEMA,id=identifier,identity=identity,mode=MODE,provider='MOCK_NO_NETWORK',
               source=dict(kind='REGISTERED_OWNED_SAMPLE'),admission=authorization.GRANT,pr=view.value['pr'],view=view.value,status='QUEUED',reason=None,goal=goal,budget=b,
               usage=dict(rounds=0,tools=0,output_bytes=0,tokens_accounted=0,reported_tokens=0,unknown_usage_requests=0),
               created_at=time.time(),updated_at=time.time(),deadline_monotonic=time.monotonic()+b['seconds'],events=[],results=[],candidates=[],rejected=[],active=None,requests=[],
               activation=dict(session=ready['session'],epoch=meta(db,'epoch')),activation_revoked=False,
               model_identity=dict(provider='DeepSeek',endpoint=protocol.ENDPOINT,model=protocol.MODEL,prompt_version=protocol.PROMPT_VERSION,prompt_sha256=protocol.PROMPT_SHA256,protocol_version=native.VERSION,tool_schema_sha256=native.SCHEMA_SHA256,thinking='disabled',context_policy=window.identity(context_limits)),
               billed_actual='NOT_APPLICABLE_MOCK',cost_source='SIMULATED',notice=NOTICE)
        event(t,'created','Explicit user start bound this investigation to the current offline Mock worker. No automatic retries, repairs or fallback.')
        grant['tasks'].append(identifier);put_meta(db,grant_key,grant)
        db.execute('INSERT INTO tasks VALUES (?,?,?)',(identifier,digest(identity),canonical(t).decode()))
    return store.get(identifier)


def interrupt_request(db, task):
    active = task.get('active')
    if active and active['type'] == 'provider_request':
        request = task['requests'][-1]
        if request['state'] in ('PREPARED', 'SENDING'):
            request['shared_spending'] = spending.interrupt(db, request['id'])
            request['state'] = 'UNCERTAIN' if request['state'] == 'SENDING' else 'NOT_SENT'


def stop(store,db,t,reason):
    interrupt_request(db, t)
    t.update(status='STOPPED',reason=reason,activation_revoked=True)
    event(t,'stopped',reason+'. No further provider requests will start.')
    store._put(db,t)


def authorized(db,t):
    p=meta(db,'provider',{})
    return (not t.get('activation_revoked') and t['activation']['epoch']==meta(db,'epoch') and
            t['identity'].get('authorization')==p.get('authorization') and
            t['activation']['session']==p.get('session') and p.get('epoch')==meta(db,'epoch') and
            p.get('status')=='READY' and p.get('mode') == MODE and 0 <= time.time() - p.get('heartbeat', 0) < 3)


def compatible(t):
    i=t['identity']
    try:
        return i.get('context_policy')==window.identity(i.get('context_policy',{}).get('limits')) and i.get('protocol_version')==native.VERSION and i.get('tool_schema_sha256')==native.SCHEMA_SHA256 and i['policy_version']==protocol.POLICY and i['prompt_sha256']==protocol.PROMPT_SHA256 and i['tool_version']==TOOL_VERSION and i['result_version']==RESULT_VERSION
    except (ValueError,KeyError,TypeError,AttributeError):return False



class Coordinator:
    def __init__(self,store):
        self.store=store;ensure(store)

    def boot(self):
        # Restart revokes prior activation before any launch can be permitted.
        with self.store.launch_lock(),self.store.transaction() as db:
            put_meta(db,'epoch',os.urandom(16).hex())
            for row in self.store.pending(MODE):
                t=self.store.get(row['id']);t['activation_revoked']=True
                if not compatible(t):stop(self.store,db,t,'REAL_POLICY_VERSION_MISMATCH');continue
                a=t['active']
                if a and a['type']=='provider_request':
                    r=t['requests'][-1]
                    if r['state']=='RECEIVED':
                        event(t,'recovered','Persisted response will be parsed without a new request.');self.store._put(db,t)
                    elif r['state'] in {'SENDING','UNCERTAIN'}:
                        stop(self.store,db,t,'REQUEST_UNCERTAIN_ON_RESTART')
                    else:stop(self.store,db,t,'REQUEST_NOT_SENT_ON_RESTART')
                elif a and a['type'] in {'frozen_tool','finish'}:
                    event(t,'recovered','Only persisted deterministic work may resume; activation never survives restart.')
                    self.store._put(db,t)
                else:stop(self.store,db,t,'EXPLICIT_START_REQUIRED_AFTER_RESTART')

    def tick(self):
        for row in self.store.pending(MODE):
            try:self.step(row['id'])
            except Exception:
                with self.store.transaction() as db:
                    t=self.store.get(row['id'])
                    if t['status'] not in TERMINAL:stop(self.store,db,t,'DETERMINISTIC_INTERNAL_ERROR')

    def shutdown(self):
        with self.store.launch_lock(),self.store.transaction() as db:
            put_meta(db,'epoch',os.urandom(16).hex())
            for row in self.store.pending(MODE):
                t=self.store.get(row['id']);t['activation_revoked']=True;a=t['active']
                if a and a['type']=='provider_request' and t['requests'][-1]['state'] in {'SENDING','UNCERTAIN'}:
                    stop(self.store,db,t,'REQUEST_UNCERTAIN_ON_SERVER_SHUTDOWN')
                elif a and a['type']=='provider_request' and t['requests'][-1]['state']=='PREPARED':
                    stop(self.store,db,t,'REQUEST_NOT_SENT_ON_SERVER_SHUTDOWN')
                elif not a:stop(self.store,db,t,'EXPLICIT_START_REQUIRED_AFTER_SHUTDOWN')
                else:self.store._put(db,t)

    def step(self,identifier):
        with self.store.transaction() as db:
            t=self.store.get(identifier)
            if t['status'] in TERMINAL or t['mode'] != MODE:return False
            if not compatible(t):stop(self.store,db,t,'REAL_POLICY_VERSION_MISMATCH');return False
            if protocol.remaining(t)<=0:
                # A launched request remains uncertain even when task time expires.
                if t['active'] and t['active']['type']=='provider_request' and t['requests'][-1]['state']=='SENDING':
                    stop(self.store,db,t,'TASK_TIME_REQUEST_UNCERTAIN')
                else:stop(self.store,db,t,'TIME_BUDGET')
                return False
            a=t['active']
            if a is None:
                if not authorized(db,t):stop(self.store,db,t,'EXPLICIT_START_REQUIRED')
                return False  # Only the controlled worker may choose/send a model step.
            if a['type']=='provider_request':
                r=t['requests'][-1]
                if r['state']!='RECEIVED':return False
                value=r['receipt']
                if value.get('error'):
                    stop(self.store,db,t,'PROVIDER_HTTP_ERROR');return False
                if value.get('protocol_error'):
                    event(t,'response_rejected','Completed Provider response failed field validation; sanitized evidence retained.',diagnostics=value.get('diagnostics',[]),category='PROVIDER_ENVELOPE')
                    stop(self.store,db,t,'INVALID_PROVIDER_ENVELOPE');return False
                if value.get('model')!=protocol.MODEL:
                    stop(self.store,db,t,'PROVIDER_MODEL_MISMATCH');return False
                if value.get('finish_reason')!='tool_calls':
                    stop(self.store,db,t,'PROVIDER_OUTPUT_INCOMPLETE');return False
                try:
                    batch=protocol.decisions(value,protocol.prior_ids(t,len(t['requests'])-1))
                    view=View(t['view'])
                    if view.sha256!=t['identity']['view_sha256']:raise ValueError('VIEW_IDENTITY_MISMATCH')
                    # Atomic admission of the complete batch. An invalid later
                    # call, mixed finish, or insufficient budget executes NONE.
                    for i,(d,call) in enumerate(batch):
                        if d['kind']=='tool':
                            try:view.validate_call(d['tool'],d['args'])
                            except ValueError as exc:
                                raise native.Invalid(str(exc),f'$.message.tool_calls[{i}].function.arguments','SOURCE_PERMISSION') from None
                    if sum(d['kind']=='tool' for d,c in batch)>t['budget']['tools']-t['usage']['tools']:raise ValueError('TOOL_BUDGET')
                except (ValueError,KeyError,TypeError,UnicodeError,RecursionError) as exc:
                    event(t,'decision_rejected','Complete native call batch rejected before dispatch; no repair request.',request_id=r['id'],rejection_code=str(exc),
                          diagnostic=getattr(exc,'diagnostic',dict(stage='MODEL_ARGUMENTS',code=str(exc),path='$.message.tool_calls')))
                    stop(self.store,db,t,'TOOL_BUDGET' if str(exc)=='TOOL_BUDGET' else 'INVALID_MODEL_DECISION');return False
                d,call=batch[0]
                t['active']=dict(type='frozen_tool' if d['kind']=='tool' else 'finish',decision=d,started=False,call_index=0)
                r.update(decision_applied=True,native_calls=[c for d,c in batch],tool_messages=[],result_indices=[],result_sha256s=[])
                if len(batch)==1:r['native_call']=call
                for d,call in batch:
                    event(t,'decision',d['summary'],decision=d,request_id=r['id'],tool_call_id=call['id'],source_kind=MODE)
                self.store._put(db,t);return True
            if a['type'] not in {'frozen_tool','finish'}:stop(self.store,db,t,'RECOVERY_UNCERTAIN');return False
            try:
                batch=protocol.bound_calls(t,len(t['requests'])-1)
                index=a['call_index']
                if type(index) is not int or index!=len(t['requests'][-1]['tool_messages']):raise ValueError('NATIVE_ACTIVE_INDEX_MISMATCH')
                d,call=batch[index]
                if d!=a['decision']:raise ValueError('NATIVE_ACTIVE_DECISION_MISMATCH')
                if d['kind']=='tool':View(t['view']).validate_call(d['tool'],d['args'])
            except (ValueError,KeyError,TypeError,RecursionError):
                stop(self.store,db,t,'NATIVE_JOURNAL_CALL_MISMATCH');return False
            if a['type']=='frozen_tool' and not a.get('started'):
                if t['usage']['tools']>=t['budget']['tools']:stop(self.store,db,t,'TOOL_BUDGET');return False
                a['started']=True;t['usage']['tools']+=1
                event(t,'tool_started',a['decision']['summary'],tool=a['decision']['tool'],args=a['decision']['args'],tool_call_id=call['id'])
            t['status']='RUNNING';self.store._put(db,t)
        try:
            view=View(t['view'])
            if view.sha256!=t['identity']['view_sha256']:raise ValueError('VIEW_VERSION')
            d=a['decision']
            output=view.execute(d['tool'],d['args']) if a['type']=='frozen_tool' else validate_candidates(view,d['candidates'],t['results'],detailed=True)
        except (ValueError,KeyError,TypeError,RecursionError):
            with self.store.transaction() as db:
                t=self.store.get(identifier)
                if t['status'] not in TERMINAL:
                    event(t,'tool_failed','Validated frozen tool failed; no model retry.',category='TOOL_EXECUTION',tool_call_id=call['id'])
                    stop(self.store,db,t,'TOOL_EXECUTION_FAILED')
            return False
        with self.store.transaction() as db:
            current=self.store.get(identifier)
            if current['status'] in TERMINAL:return False
            if protocol.remaining(current)<=0:stop(self.store,db,current,'TIME_BUDGET');return False
            r=current['requests'][-1];call_id=r['native_calls'][a['call_index']]['id']
            if a['type']=='frozen_tool':
                reply=protocol.tool_message(call_id,output)
            else:
                reply=dict(role='tool',tool_call_id=call_id,content=canonical(dict(reason='CANDIDATE_VALIDATION_REJECTED' if output[1] else a['decision']['reason'],accepted=[c['id'] for c in output[0]],rejected=[c['reason'] for c in output[1]],correctness='UNADJUDICATED')).decode())
            size=len(canonical(output))+len(canonical(reply))
            if current['usage']['output_bytes']+size>current['budget']['output_bytes']:stop(self.store,db,current,'OUTPUT_BUDGET');return False
            current['usage']['output_bytes']+=size;r['tool_messages'].append(reply)
            if len(r['native_calls'])==1:r['tool_message']=reply
            if a['type']=='frozen_tool':
                ri=len(current['results']);r['result_indices'].append(ri);r['result_sha256s'].append(output['content_sha256'])
                if len(r['native_calls'])==1:r.update(result_index=ri,result_sha256=output['content_sha256'])
                current['results'].append(output)
                next_index=a['call_index']+1
                if next_index<len(r['native_calls']):
                    next_d=protocol.bound_calls(current,len(current['requests'])-1)[next_index][0]
                    current['active']=dict(type='frozen_tool',decision=next_d,call_index=next_index,started=False)
                else:current['active']=None
                event(current,'tool_result','Actual frozen tool feedback committed before a subsequent model decision.',result_index=len(current['results'])-1,content_sha256=output['content_sha256'],tool_call_id=call_id)
                if not authorized(db,current):
                    current['activation_revoked']=True;stop(self.store,db,current,'DETERMINISTIC_RECOVERY_COMPLETE_NO_RESEND');return False
            else:
                current['candidates'],current['rejected']=output;reason=d['reason']
                if current['rejected']:reason='CANDIDATE_VALIDATION_REJECTED'
                current.update(status='COMPLETED',reason=reason,active=None,result_version=RESULT_VERSION)
                event(current,'finished',d['summary'],reason=reason,correctness='UNADJUDICATED',tool_call_id=call_id)
            self.store._put(db,current)
        return True
