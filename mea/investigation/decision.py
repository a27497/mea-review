"""Replaceable decision interface; this phase ships one deterministic offline policy.

This is a narrow protocol exercise about changed literal None returns, not model
intelligence or a general defect detector. Each branch consumes actual tool data.
Only concise public investigation summaries are recorded; no private reasoning.
"""
import re
from typing import Protocol

POLICY = 'offline-none-return-v1'
MODE = 'OFFLINE_POLICY_NOT_AI'
GOAL = 'Inspect changed literal None returns and seek guards in collected callers/tests.'


class Decider(Protocol):
    def decide(self, context: dict) -> dict: ...


def tool(name, revision, summary, **args):
    return dict(kind='tool',tool=name,args=dict(revision=revision,**args),summary=summary)


def stop(reason, summary, candidates=None):
    return dict(kind='finish',reason=reason,summary=summary,candidates=candidates or [])


class OfflineDecider:
    """Source text is only matched as data, never an instruction/command."""
    version = POLICY
    mode = MODE

    def decide(self, context):
        results=context['results']; rev=context['revisions']['head']
        paths=context['paths']
        if not results:
            path=next((p for p in paths if p.endswith('.py') and not p.split('/')[-1].startswith('test_')),None)
            if not path:return stop('EVIDENCE_INSUFFICIENT','No collected Python implementation path.')
            return tool('read_diff',rev,'Check the frozen change for a literal None return.',path=path)
        first=results[0]
        if not first['evidence'] or not isinstance(first['data'],str):
            return stop('EVIDENCE_INSUFFICIENT','No bounded source-linked diff was returned.')
        added=[r for r in first['data'].splitlines() if r.startswith('+')]
        if not any(re.match(r'\+\s+return None\s*$',r) for r in added):
            return stop('ZERO_CANDIDATES','This narrow offline policy found no changed literal None return. Other defects remain unknown.')
        path=first['evidence'][0]['path']
        if len(results)==1:
            return tool('read_source',rev,'Inspect the changed function before testing the hypothesis.',path=path,start=1,end=40)
        source=results[1]
        defs=[re.match(r'\s*(?:async )?def ([A-Za-z_]\w*)\(',r['text']) for r in source['data']]
        defs=[d[1] for d in defs if d]
        if len(defs)!=1 or not any(r['text'].strip()=='return None' for r in source['data']):
            return stop('EVIDENCE_INSUFFICIENT','Function context is missing, ambiguous or outside the snippet.')
        symbol=defs[0]
        if len(results)==2:
            return tool('find_symbol',rev,'Locate the named function in collected Python source.',symbol=symbol)
        if not results[2]['evidence']:
            return stop('EVIDENCE_INSUFFICIENT','The function definition was not located.')
        if len(results)==3:
            return tool('references',rev,'Hypothesis: a changed None result may reach a collected caller; look for a counterexample.',symbol=symbol)
        callers=sorted({r['path'] for r in results[3]['evidence'] if r['path']!=path})
        if not callers:return stop('EVIDENCE_INSUFFICIENT','No collected caller; dynamic and uncollected uses are unknown.')
        if len(callers)>3 or any(r.get('truncated') for r in results):
            return stop('EVIDENCE_INSUFFICIENT','Caller or tool coverage exceeds this offline policy.')
        read=results[4:]
        for r in read:
            # This deliberately conservative counterexample heuristic is explicit.
            if any('is None' in row['text'] for row in r['data']):
                return stop('HYPOTHESIS_REFUTED','Collected caller/test includes an explicit None check. The broad offline hypothesis is withdrawn; semantic correctness is unadjudicated.')
        if len(read)<len(callers):
            caller=callers[len(read)]
            name='read_test' if caller.split('/')[-1].startswith('test_') or caller.endswith('_test.py') else 'read_source'
            return tool(name,rev,'Read collected caller/test context and seek an explicit None check.',path=caller,start=1,end=40)
        changed=next((e for e in first['evidence'] if e['quote'].strip()=='return None'),None)
        call=next((e for e in results[3]['evidence'] if e['path'] in callers),None)
        if not changed or not call or any(not r['evidence'] for r in read):
            return stop('EVIDENCE_INSUFFICIENT','Required change or caller context was unavailable.')
        return stop('HYPOTHESIS_SUPPORTED','The narrow syntactic hypothesis has supporting references. Its runtime impact remains a conditional inference.',[dict(
            title='Changed None return may affect a collected caller',
            trigger='If this changed path returns None and the collected caller requires a non-None value without another guard.',
            change=dict(path=changed['path'],side=changed['side'],line=changed['line'],revision=changed['revision']),
            impact='The caller may fail or propagate None. Dynamic dispatch, reachability and runtime behavior are unverified.',
            evidence_ids=list(dict.fromkeys([changed['id'],call['id']])))])
