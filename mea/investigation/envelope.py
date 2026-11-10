"""Bounded public Provider evidence and field diagnostics, never tool authority.

Unknown metadata is retained for diagnosis but excluded from executable messages.
Only the native grammar and frozen-view validator may authorize a source tool.
"""
import json
import math
import re
from ..contract import decode

PRIVATE={'reasoning_content','reasoning','analysis','chain_of_thought'}
SENSITIVE={'authorization','api_key','apikey','api-key','access_token','refresh_token','secret','password','credentials'}


def redact(value,secret=''):
    if isinstance(value,str):
        value=value.encode('utf-8',errors='replace').decode('utf-8')
        if secret:value=value.replace(secret,'[REDACTED]')
        value=re.sub(r'(?i)\bsk-[A-Za-z0-9_-]{12,}', '[REDACTED]',value)
        return re.sub(r'(?i)\bBearer\s+[^\s"\x27,}]+','Bearer [REDACTED]',value)
    if isinstance(value,list):return [redact(v,secret) for v in value]
    if isinstance(value,dict):
        return {redact(k,secret):('[REDACTED]' if k.lower() in SENSITIVE else redact(v,secret))
                for k,v in value.items() if k.lower() not in PRIVATE}
    if isinstance(value,float) and not math.isfinite(value):return '[NON_FINITE_NUMBER]'
    return value


class Invalid(ValueError):
    def __init__(self,path,expected,value,code='FIELD_INVALID'):
        super().__init__(code)
        self.diagnostic=dict(stage='PROVIDER_ENVELOPE',code=code,path=path,expected=expected,actual_type=type(value).__name__)


def evidence(raw,secret):
    try:
        value=decode(raw)
        return dict(format='JSON_REDACTED',body=redact(value,secret),private_fields_omitted=True)
    except ValueError as exc:
        # A diagnostic-only parse can still remove sensitive properties from
        # duplicate-key/non-finite JSON. It never supplies executable fields.
        try:
            return dict(format='INVALID_JSON_REDACTED',body=redact(json.loads(raw),secret),parse_error=str(exc),private_fields_omitted=True)
        except (ValueError,UnicodeError,RecursionError):pass
        text=raw.decode('utf-8',errors='replace')
        # Invalid JSON cannot be traversed to reliably remove private reasoning
        # or sensitive properties. Withhold it when those markers occur.
        sensitive='\\u' in text or any(re.search(r'(?i)["\x27]?'+re.escape(k)+r'["\x27]?\s*[:=]',text) for k in PRIVATE|SENSITIVE)
        return dict(format='INVALID_JSON_REDACTED',body='[WITHHELD: unparseable sensitive/private fields]' if sensitive else redact(text,secret),
                    parse_error=str(exc),private_fields_omitted=True)


def parse(raw):
    try:e=decode(raw)
    except ValueError as exc:raise Invalid('$','complete strict JSON object',None,str(exc)) from None
    def require(condition,path,expected,value):
        if not condition:raise Invalid(path,expected,value)
    require(isinstance(e,dict),'$','object',e)
    require(e.get('object')=='chat.completion','$.object','chat.completion',e.get('object'))
    for key,bound in [('id',256),('model',128)]:
        v=e.get(key);require(isinstance(v,str) and 1<=len(v.encode())<=bound,'$.'+key,'nonempty bounded string',v)
    choices=e.get('choices');require(isinstance(choices,list) and len(choices)==1,'$.choices','exactly one complete choice',choices)
    choice=choices[0];require(isinstance(choice,dict),'$.choices[0]','object',choice)
    index=choice.get('index');require(type(index) is int and index==0,'$.choices[0].index','integer 0',index)
    finish=choice.get('finish_reason');require(isinstance(finish,str),'$.choices[0].finish_reason','string',finish)
    m=choice.get('message');prefix='$.choices[0].message';require(isinstance(m,dict),prefix,'object',m)
    require(m.get('role')=='assistant',prefix+'.role','assistant',m.get('role'))
    content=m.get('content')
    require(content is None or (isinstance(content,str) and '\0' not in content and len(content.encode())<=16000),prefix+'.content','null or bounded public string',content)
    refusal=m.get('refusal');require(refusal in (None,''),prefix+'.refusal','no refusal',refusal)
    calls=m.get('tool_calls',[]);require(isinstance(calls,list),prefix+'.tool_calls','array',calls)
    # Metadata never controls dispatch. Project only official executable fields;
    # ID grammar, function whitelist, arguments and budget are validated later.
    normalized=[]
    ignored=[]
    for path,value,keys in [('$',e,{'object','id','model','choices','usage'}),('$.choices[0]',choice,{'index','finish_reason','message'}),(prefix,m,{'role','content','tool_calls'})]:
        ignored.extend(path+'.'+k for k in value if k not in keys and k.lower() not in PRIVATE)
    require(len(calls)<=8,prefix+'.tool_calls','at most 8 calls',calls)
    for i,c in enumerate(calls):
        path=prefix+f'.tool_calls[{i}]';require(isinstance(c,dict),path,'object',c)
        for key in ['id','type']:
            require(isinstance(c.get(key),str),path+'.'+key,'string',c.get(key))
        f=c.get('function');require(isinstance(f,dict),path+'.function','object',f)
        for key in ['name','arguments']:
            require(isinstance(f.get(key),str),path+'.function.'+key,'string',f.get(key))
        normalized.append(dict(id=c['id'],type=c['type'],function=dict(name=f['name'],arguments=f['arguments'])))
        ignored.extend(path+'.'+k for k in c if k not in {'id','type','function'})
        ignored.extend(path+'.function.'+k for k in f if k not in {'name','arguments'})
    return dict(completion_id=e['id'],model=e['model'],finish_reason=finish,
                message=dict(role='assistant',content=content,tool_calls=normalized),
                compatibility=dict(ignored_metadata_paths=ignored[:64],content_defaulted='content' not in m))
