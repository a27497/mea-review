"""Explicit self-owned offline demonstrations, computed from frozen source tools."""
import time
from pathlib import Path
from ..contract import decode,digest,canonical
from ..investigation.tools import View,NOTICE,TOOL_VERSION
from ..investigation.tasks import event,SCHEMA,limits
from ..investigation.decision import OfflineDecider
from ..investigation.validation import validate_candidates,RESULT_VERSION

ROOT=Path(__file__).resolve().parents[2]/'fixtures/review'
POLICY='phase4-owned-mock-none-policy-v1'
MODE='MOCK_REVIEW_DEMO_NOT_AI'


def versions():
    return decode((ROOT/'manifest.json').read_bytes())['versions']


def load(label):
    row=next((r for r in versions() if r['label']==label),None)
    if not row:raise ValueError('DEMO_VERSION_NOT_REGISTERED')
    value=decode((ROOT/row['file']).read_bytes())
    if digest(value)!=row['input_sha256'] or value['revisions']['head']!=row['head']:raise ValueError('DEMO_IDENTITY_MISMATCH')
    return value


def registered(task):
    """Match a fixture for Mock transport eligibility, independent of the candidate producer."""
    row=next((r for r in versions() if task['identity']['input_sha256']==r['input_sha256']),None)
    return bool(row and digest(task['view'])==View.from_input(load(row['label'])).sha256)


def run(store,label):
    value=load(label);view=View.from_input(value);b=limits();goal='MOCK self-owned Phase 4 investigation '+label
    identity=dict(view_sha256=view.sha256,input_sha256=view.value['input_sha256'],revisions=view.value['revisions'],policy_version=POLICY,tool_version=TOOL_VERSION,result_version=RESULT_VERSION,budget=b,goal=goal)
    identifier='task-'+digest(identity)
    with store.connection() as db:
        if db.execute('select 1 from tasks where id=?',(identifier,)).fetchone():return store.get(identifier)
    t=dict(schema=SCHEMA,id=identifier,identity=identity,mode=MODE,provider='NONE',source=dict(kind='PHASE4_OWNED_MOCK_DEMO',label=label),pr=view.value['pr'],view=view.value,status='RUNNING',reason=None,goal=goal,budget=b,
           usage=dict(rounds=0,tools=0,output_bytes=0),created_at=time.time(),updated_at=time.time(),events=[],results=[],candidates=[],rejected=[],active=None,requests=[],notice=NOTICE)
    event(t,'created','Self-owned MOCK policy demonstration; no model, remote GitHub or target execution.')
    decider=OfflineDecider()
    for _ in range(b['rounds']):
        if time.time()-t['created_at']>=b['seconds']:t.update(status='STOPPED',reason='TIME_BUDGET');break
        d=decider.decide(dict(goal=goal,paths=list(view.files),revisions=view.value['revisions'],results=t['results']))
        t['usage']['rounds']+=1;event(t,'decision',d['summary'],decision=d)
        if d['kind']=='finish':
            t['candidates'],t['rejected']=validate_candidates(view,d['candidates'],t['results'])
            t.update(status='COMPLETED',reason='CANDIDATE_VALIDATION_REJECTED' if t['rejected'] else d['reason']);break
        if t['usage']['tools']>=b['tools']:t.update(status='STOPPED',reason='TOOL_BUDGET');break
        event(t,'tool_started',d['summary'],tool=d['tool'],args=d['args'])
        result=view.execute(d['tool'],d['args']);size=len(canonical(result))
        if size+t['usage']['output_bytes']>b['output_bytes']:t.update(status='STOPPED',reason='OUTPUT_BUDGET');break
        t['usage']['tools']+=1;t['usage']['output_bytes']+=size;t['results'].append(result)
        event(t,'tool_result','Actual frozen source feedback for the next offline decision.',result_index=len(t['results'])-1,content_sha256=result['content_sha256'])
    else:t.update(status='STOPPED',reason='ROUND_BUDGET')
    t['updated_at']=time.time();event(t,'finished',t['reason'])
    with store.transaction() as db:db.execute('insert or ignore into tasks values (?,?,?)',(identifier,digest(identity),canonical(t).decode()))
    return store.get(identifier)
