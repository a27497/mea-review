"""Literal reference/changed-line validation, never correctness adjudication."""
from ..contract import fields, text, digest

RESULT_VERSION='mea-investigation-result-v1'


def validate_candidates(view, proposed, results):
    if not isinstance(proposed,list) or len(proposed)>8:raise ValueError('CANDIDATE_BOUND')
    observed={e['id']:e for r in results for e in r.get('evidence',[])}
    accepted=[];rejected=[];seen=set()
    for index,c in enumerate(proposed):
        try:
            fields(c,'title trigger change impact evidence_ids')
            for k,limit in [('title',200),('trigger',1200),('impact',1200)]:
                text(c[k],limit)
                if not c[k].strip():raise ValueError('CANDIDATE_TEXT_REQUIRED')
            change=c['change'];fields(change,'path side line revision')
            side=change['side']
            if side not in {'base','head'} or change['revision']!=view.value['revisions'][side]:raise ValueError('CANDIDATE_VERSION_MISMATCH')
            anchor=(side,change['path'],change['line'])
            if type(change['line']) is not int or anchor not in view.changes:raise ValueError('CHANGE_ASSOCIATION_REQUIRED')
            ids=c['evidence_ids']
            if not isinstance(ids,list) or not 1<=len(ids)<=16 or len(set(ids))!=len(ids):raise ValueError('CANDIDATE_EVIDENCE_BOUND')
            citations=[]
            for identifier in ids:
                e=observed.get(identifier)
                if e is None or view.citation(e['side'],e['path'],e['line'])!=e:raise ValueError('REFERENCE_INVALID_OR_UNOBSERVED')
                citations.append(e)
            if not any((e['side'],e['path'],e['line'])==anchor for e in citations):raise ValueError('CHANGE_CITATION_REQUIRED')
            # Conservative location-based duplicate rejection despite reworded titles.
            if anchor in seen:raise ValueError('DUPLICATE_CHANGE_CANDIDATE')
            seen.add(anchor)
            accepted.append(dict(id='candidate-'+digest(c),**c,citations=citations,citation_validity='VALID',
                                 inference='CONDITIONAL_INFERENCE',correctness='UNADJUDICATED',result_version=RESULT_VERSION))
        except (ValueError,KeyError,TypeError):
            rejected.append(dict(index=index,reason='DETERMINISTIC_VALIDATION_REJECTED',candidate=c))
    return accepted,rejected
