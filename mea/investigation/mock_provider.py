"""Explicit MOCK_PROVIDER_NOT_AI for tests/browser contracts; never a real sender."""
import json
import re
import time
from .decision import tool,stop
from .real_protocol import MODEL
from .native import FINISH
from .context_budget import expanded_feedback


class Sender:
    def __init__(self,plan=None,delay=.15):self.plan=plan;self.delay=delay;self.calls=[]
    def start(self,body):
        self.calls.append(body)
        return body
    def collect(self,body,seconds,cancelled,heartbeat):
        start = time.monotonic()
        deadline = start + seconds
        until = min(start + self.delay, deadline)
        while time.monotonic()<until:
            heartbeat()
            if cancelled():return dict(error='MOCK_CANCELLED_UNCERTAIN',http_posts_attempted=0)
            time.sleep(.01)
        if cancelled():
            return dict(error='MOCK_CANCELLED_UNCERTAIN', http_posts_attempted=0)
        if time.monotonic() >= deadline:
            return dict(error='MOCK_DEADLINE_UNCERTAIN', http_posts_attempted=0)
        ctx=self.context(body)
        d=self.plan(ctx) if self.plan else self.choose(ctx)
        message=self.message(d,'mock-call-'+str(len(self.calls)))
        return dict(http_status=200,request_id='MOCK-REQUEST-'+str(len(self.calls)),completion_id='MOCK-COMPLETION',model=MODEL,
                    usage=dict(prompt_tokens=100,completion_tokens=100,total_tokens=200),finish_reason='tool_calls',message=message,
                    http_posts_attempted=0,response_bytes=len(json.dumps(message).encode()),elapsed_seconds=self.delay)
    @staticmethod
    def context(body):
        ctx=json.loads(body['messages'][1]['content'])
        ctx['tool_feedback']=expanded_feedback(body)
        return ctx
    @staticmethod
    def message(d,call_id='mock-call'):
        # Test-provider serialization only. Production never converts text JSON
        # or internal decisions into an allegedly native Provider response.
        if isinstance(d,dict) and 'role' in d:return d
        if isinstance(d,str):return dict(role='assistant',content=d,tool_calls=[])
        if d['kind']=='tool':name=d['tool'];args=dict(**d['args'],summary=d['summary'])
        else:name=FINISH;args={k:d[k] for k in ['reason','summary','candidates']}
        return dict(role='assistant',content=None,tool_calls=[dict(id=call_id,type='function',function=dict(name=name,arguments=json.dumps(args)))])
    @staticmethod
    def choose(ctx):
        feedback=ctx['tool_feedback'];rev=ctx['revisions']['head']
        if not feedback:
            path=next(f['path'] for f in ctx['files'] if f['changed'])
            return tool('read_diff',rev,'MOCK: inspect change',path=path)
        diff=next((r for r in feedback if r['tool']=='read_diff'),None)
        if diff is None:return stop('EVIDENCE_INSUFFICIENT','MOCK: bounded diff absent')
        match=re.search(r'def ([A-Za-z_]\w*)\(',diff.get('diff',''))
        if not match:return stop('ZERO_CANDIDATES','MOCK: no function context')
        symbol=match[1]
        refs=next((r for r in feedback if r['tool']=='references'),None)
        if refs is None:return tool('references',rev,'MOCK: investigate callers',symbol=symbol)
        caller=next((e for e in refs['evidence'] if not e['path'].split('/')[-1].startswith('test_')),None)
        if not caller:return stop('EVIDENCE_INSUFFICIENT','MOCK: caller unknown')
        source=next((r for r in feedback if r['tool']=='read_source'),None)
        if source is None:return tool('read_source',rev,'MOCK: seek a caller counterexample',path=caller['path'],start=1,end=40)
        if any('is None' in e['quote'] for e in source['evidence']):return stop('HYPOTHESIS_REFUTED','MOCK: caller handles None; withdraw broad hypothesis')
        anchor=diff['evidence'][0]
        return stop('HYPOTHESIS_SUPPORTED','MOCK: conditional changed behavior',[dict(title='MOCK conditional return change',trigger='If the missing key reaches the collected caller without another guard',change=dict(path=anchor['path'],side='head',line=anchor['line'],revision=rev),impact='Caller arithmetic may fail; runtime unverified',evidence_ids=[anchor['id'],caller['id']])])
