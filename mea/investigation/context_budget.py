"""Deterministic model-window projection. No model, tokenizer service or I/O.

The journal remains authoritative. Projections carry observed rows once and
retain complete native call/reply pairs, never fabricated assistant messages.
"""
import copy
import json
import re
from ..contract import canonical, digest

VERSION='mea-context-window-v1'
ESTIMATOR='utf8-byte-ceiling-plus-25pct-framing-v1'
# Opt-in estimator. Hundredths of a token per character, fitted to provider-reported
# prompt tokens of recorded requests and raised until none of them was under-reserved.
CALIBRATED='char-class-calibrated-v1'
ESTIMATORS=(ESTIMATOR,CALIBRATED)
RATES=dict(plain=40,hex=85,non_ascii=300)
PER_MESSAGE=16
HEX_RUN=re.compile('[0-9a-f]{16,}')
PROFILE=dict(model='deepseek-flash',context_tokens=1000000,headroom_tokens=65536,
             source='https://api-docs.deepseek.com/quick_start/pricing/',verified='2026-11-08')
DEFAULTS=dict(serialized_bytes=1048576,input_tokens=128000,recent_batches=2)
MAX_BYTES=1900000  # Existing strict JSON decoder also bounds the pipe envelope to 2 MB.


class Insufficient(ValueError):
    def __init__(self,code,details):
        super().__init__(code);self.details=details


def config(value=None):
    result=dict(DEFAULTS)
    if value is not None:
        if not isinstance(value,dict) or set(value)-set(result):raise ValueError('CONTEXT_CONFIG_KEYS')
        result.update(value)
    for key,maximum in dict(serialized_bytes=MAX_BYTES,input_tokens=PROFILE['context_tokens']-PROFILE['headroom_tokens']-1600,recent_batches=6).items():
        if type(result[key]) is not int or not 1<=result[key]<=maximum:raise ValueError('CONTEXT_CONFIG_BOUND')
    return result


def identity(value=None,estimator=ESTIMATOR):
    if estimator not in ESTIMATORS:raise ValueError('CONTEXT_ESTIMATOR')
    return dict(version=VERSION,estimator=estimator,profile=copy.deepcopy(PROFILE),limits=config(value))


def local(estimator):
    """Host-side accounting choice. It is recorded with the task and never sent to the model."""
    if estimator not in ESTIMATORS:raise ValueError('CONTEXT_ESTIMATOR')
    return dict(estimator=estimator)


def estimator_of(task):
    return task['identity'].get('local_accounting',{}).get('estimator',ESTIMATOR)


def calibrated_tokens(classes,messages):
    """The calibrated input estimate from character-class counts alone."""
    return (RATES['plain']*classes['plain']+RATES['hex']*classes['hex']+RATES['non_ascii']*classes['non_ascii_bytes']+99)//100+PER_MESSAGE*messages


def estimate(body,estimator=ESTIMATOR):
    # Official char/token ratios are approximate, particularly for code. With no
    # verified model-matching local tokenizer, reserve one token per UTF-8 byte
    # of model-visible strings/schema, plus 25%, 1024 and 64/message for framing.
    # This deliberately overestimates, is NOT an exact tokenization guarantee,
    # and never discounts the reservation using a previous cheap/cache-hit run.
    strings=[]
    for m in body['messages']:
        strings.extend(v for k,v in m.items() if isinstance(v,str))
        if m.get('tool_calls'):strings.append(canonical(m['tool_calls']).decode())
    strings.append(canonical(body['tools']).decode())
    visible=sum(len(s.encode()) for s in strings)
    ceiling=(visible*5+3)//4+1024+64*len(body['messages'])
    chars=''.join(strings)
    hint=(sum(3 if ord(c)<128 else 6 if 0x3400<=ord(c)<=0x9fff else 10*len(c.encode()) for c in chars)+9)//10
    if estimator==ESTIMATOR:
        return dict(method=ESTIMATOR,input_reservation=ceiling,visible_utf8_bytes=visible,
                    character_hint_tokens=hint,margin='25% + 1024 + 64/message; hint never used for admission',
                    exact_tokenizer=False)
    if estimator!=CALIBRATED:raise ValueError('CONTEXT_ESTIMATOR')
    # ASCII text cost about 0.29 tokens per character in recorded requests and long
    # lowercase hex runs (commit ids, digests) about 0.64. Both rates carry a margin,
    # non-ASCII is charged per UTF-8 byte, and a provider report above the
    # reservation still stops the task. Still an estimate, never a tokenizer.
    hexed=sum(len(m.group()) for m in HEX_RUN.finditer(chars))
    wide=[c for c in chars if ord(c)>=128]
    plain=len(chars)-hexed-len(wide)
    classes=dict(plain=plain,hex=hexed,non_ascii=len(wide),non_ascii_bytes=sum(len(c.encode()) for c in wide))
    return dict(method=CALIBRATED,input_reservation=calibrated_tokens(classes,len(body['messages'])),visible_utf8_bytes=visible,
                byte_ceiling=ceiling,character_classes=classes,
                margin='0.40/char, 0.85/hex-run char, 3/non-ASCII byte, 16/message; provider usage above it stops the task',
                exact_tokenizer=False)


def feedback(message):
    raw=json.loads(message['content'])
    projected={k:copy.deepcopy(v) for k,v in raw.items() if k!='evidence'}
    projected['evidence_ids']=[e['id'] for e in raw['evidence']]
    projected['format']=VERSION
    return dict(role='tool',tool_call_id=message['tool_call_id'],content=canonical(projected).decode())


def expanded_feedback(body):
    """Decode the exact visible projection for Mock tests/inspection, not decisions."""
    ctx=json.loads(body['messages'][1]['content']);evidence={}
    for f in ctx.get('observed_evidence',[]):
        for ref,line,identifier,quote in f['rows']:
            evidence[ref]=dict(id=identifier,path=f['path'],side=f['side'],revision=f['revision'],line=line,quote=quote)
    values=[r for b in ctx.get('folded_history',[]) for r in b['feedback']]
    values += [json.loads(m['content']) for m in body['messages'] if m['role']=='tool']
    result=[]
    for raw in values:
        v=copy.deepcopy(raw)
        if v.get('evidence_status')!='OMITTED_FROM_MODEL_CONTEXT':
            v['evidence']=[evidence[i] for i in v.get('evidence_refs',[])]
        else:v['evidence']=[]
        result.append(v)
    return result


def project(initial,batches,body_base,limits,available_tokens,output_tokens,estimator=ESTIMATOR):
    """Pressure removes oldest whole batches' quotes explicitly, recent stays intact.

    Older native pairs are folded into a host observation index, not an assistant
    summary. Exact original calls/results and their digests remain in the journal.
    """
    cfg=config(limits);active=max(0,len(batches)-cfg['recent_batches'])
    omitted=set();initial_omitted=False
    # Whole old batches can be omitted from the wire, never the latest native pair.
    for attempt in range(len(batches)+2):
        ctx=copy.deepcopy(initial);ctx['context_policy']=identity(cfg)
        groups={};known={};refs={};folded=[];messages=[];retained_ids=[];folded_ids=[]
        for i,(assistant,replies) in enumerate(batches):
            wire=[]
            for reply in replies:
                raw=json.loads(reply['content']);pr=feedback(reply);v=json.loads(pr['content'])
                v['evidence_refs']=[refs.setdefault(e['id'],len(refs)) for e in raw['evidence']]
                del v['evidence_ids']
                if i in omitted:
                    v['evidence_status']='OMITTED_FROM_MODEL_CONTEXT'
                    if 'diff' in v:v['diff_omitted_sha256']=digest(v.pop('diff').encode())
                else:
                    for e in raw['evidence']:
                        if e['id'] in known and known[e['id']]!=e:raise ValueError('CONTEXT_EVIDENCE_CONFLICT')
                        known[e['id']]=e
                        key=(e['path'],e['side'],e['revision'])
                        source=next((f for f in raw.get('source_files',[]) if (f['path'],f['side'],f['revision'])==key),{})
                        group=groups.setdefault(key,dict(source,path=e['path'],side=e['side'],revision=e['revision'],rows={}))
                        if any(group.get(k)!=source.get(k) for k in ['content_sha256','blob_sha']):raise ValueError('CONTEXT_SOURCE_IDENTITY_CONFLICT')
                        group['rows'][e['id']]=[refs[e['id']],e['line'],e['id'],e['quote']]
                pr['content']=canonical(v).decode()
                wire.append(pr)
            ids=[c['id'] for c in assistant['tool_calls']]
            if i<active:
                folded_ids.extend(ids)
                folded.append(dict(request_index=i,assistant_sha256=digest(assistant),calls=copy.deepcopy(assistant['tool_calls']),
                                   feedback=[dict(json.loads(m['content']),tool_call_id=m['tool_call_id']) for m in wire],tool_call_ids=ids,
                                   status='HOST_INDEX_OF_COMMITTED_OBSERVATIONS_NOT_A_MODEL_MESSAGE',
                                   assistant_public_text_omitted=dict(bytes=len((assistant.get('content') or '').encode()),sha256=digest((assistant.get('content') or '').encode()))))
            else:
                retained_ids.extend(ids);messages.append(copy.deepcopy(assistant));messages.extend(wire)
        ctx['folded_history']=folded
        ctx['observed_evidence']=[dict(g,rows=list(g['rows'].values())) for g in groups.values()]
        if initial_omitted:
            ctx['initial_diffs']=[dict(path=d['path'],omission='CONTEXT_PRESSURE; original diff retained in frozen input',content_sha256=digest(d['diff'].encode())) if 'diff' in d else d for d in ctx['initial_diffs']]
        body=dict(body_base,messages=[dict(role='system',content=body_base['_prompt']),dict(role='user',content=canonical(ctx).decode())]+messages)
        del body['_prompt']
        usage=estimate(body,estimator);size=len(canonical(body));allowed=min(cfg['input_tokens'],PROFILE['context_tokens']-PROFILE['headroom_tokens']-output_tokens, max(0,available_tokens-output_tokens))
        manifest=dict(version=VERSION,config=cfg,model_profile=PROFILE,estimation=usage,input_token_limit=allowed,
                      output_tokens_reserved=output_tokens,serialized_bytes=size,serialized_byte_limit=cfg['serialized_bytes'],
                      retained_native_call_ids=retained_ids,folded_call_ids=folded_ids,omitted_quote_batches=sorted(omitted),
                      initial_diff_quotes_omitted=initial_omitted,visible_evidence_ids=sorted(known),
                      folded_assistant_text_omissions=[dict(request_index=b['request_index'],**b['assistant_public_text_omitted']) for b in folded],
                      projection_source_batches=[dict(assistant_sha256=digest(a),reply_sha256s=[digest(r) for r in rs]) for a,rs in batches],
                      original_history_bytes=sum(len(canonical(a))+sum(len(canonical(r)) for r in rs) for a,rs in batches),
                      policy='Latest native batches retained whole; older calls indexed, duplicate rows shared; pressure omissions explicit. Full journal unchanged.')
        manifest['request_body_sha256']=digest(body)
        if usage['input_reservation']<=allowed and size<=cfg['serialized_bytes']:
            return body,usage['input_reservation']+output_tokens,manifest
        if len(omitted)<active:
            omitted.add(len(omitted));continue
        if not initial_omitted and any('diff' in d for d in initial['initial_diffs']):
            initial_omitted=True;continue
        code='TOKEN_RESERVATION_BUDGET' if available_tokens-output_tokens<min(cfg['input_tokens'],PROFILE['context_tokens']-PROFILE['headroom_tokens']-output_tokens) else 'MODEL_CONTEXT_BUDGET'
        if size>cfg['serialized_bytes']:code='REQUEST_INPUT_BYTE_BOUND'
        raise Insufficient(code,manifest)
    raise ValueError('CONTEXT_PROJECTION_BOUND')


def validate_projection(body):
    """Check local row identities and complete call/reply associations on the wire."""
    from . import native
    ctx=json.loads(body['messages'][1]['content']);rows={};identifiers=set()
    for group in ctx['observed_evidence']:
        if set(group)!={'path','side','revision','content_sha256','blob_sha','rows'}:raise ValueError('CONTEXT_FILE_FIELDS')
        if group['side'] not in {'base','head'} or group['revision']!=ctx['revisions'][group['side']]:raise ValueError('CONTEXT_REVISION_MISMATCH')
        for ref,line,identifier,quote in group['rows']:
            if type(ref) is not int or ref<0 or ref in rows or identifier in identifiers or type(line) is not int or line<1 or not isinstance(quote,str):raise ValueError('CONTEXT_ROW_INVALID')
            original={k:group[k] for k in ['path','side','revision','content_sha256','blob_sha']}
            original.update(view_sha256=ctx['view_sha256'],line=line,quote=quote)
            if identifier!='ev-'+digest(original):raise ValueError('CONTEXT_EVIDENCE_ID_MISMATCH')
            rows[ref]=identifier;identifiers.add(identifier)
    replies=[];used=[]
    for folded in ctx['folded_history']:
        calls=folded['calls'];ids=[c['id'] for c in calls]
        native.parse_batch(dict(role='assistant',content=None,tool_calls=calls),used)
        if ids!=folded['tool_call_ids'] or ids!=[f['tool_call_id'] for f in folded['feedback']]:raise ValueError('CONTEXT_FOLDED_CALL_MISMATCH')
        used.extend(ids);replies.extend(folded['feedback'])
    for m in body['messages'][2:]:
        if m['role']=='assistant':
            native.parse_batch(m,used);used.extend(c['id'] for c in m['tool_calls'])
        else:replies.append(json.loads(m['content']))
    for reply in replies:
        if reply['format']!=VERSION:raise ValueError('CONTEXT_FEEDBACK_VERSION')
        refs=reply['evidence_refs']
        if not isinstance(refs,list) or any(type(i) is not int or i<0 for i in refs) or len(set(refs))!=len(refs):raise ValueError('CONTEXT_FEEDBACK_REFS')
        if reply.get('evidence_status')=='OMITTED_FROM_MODEL_CONTEXT':continue
        if any(i not in rows for i in refs):raise ValueError('CONTEXT_ORPHAN_EVIDENCE_REF')
