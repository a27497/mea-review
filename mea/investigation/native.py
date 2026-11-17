"""DeepSeek native function-call grammar. Data validation only; no execution/I/O."""
import copy
import re
from ..contract import canonical, decode, digest, fields, text
from .tasks import Runner, REASONS
from .provider_view import MAX_READ_LINE, LIMITS as TOOL_LIMITS

VERSION = 'mea-deepseek-native-tools-v3-byte-pages'
FINISH = 'finish_investigation'
CALL_ID = re.compile(r'[A-Za-z0-9_-]{1,128}')


def obj(properties):
    return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)


def string(limit, **extra):
    return dict(type='string', maxLength=limit, **extra)


REVISION = string(40, pattern='[0-9a-f]{40}')
SUMMARY = string(1200)
SOURCE_ARGS = {
    'read_diff': dict(path=string(256), revision=REVISION),
    'read_source': dict(path=string(256), revision=REVISION, start=dict(type='integer', minimum=1, maximum=MAX_READ_LINE), end=dict(type='integer', minimum=1, maximum=MAX_READ_LINE)),
    'find_symbol': dict(symbol=string(80), revision=REVISION),
    'references': dict(symbol=string(80), revision=REVISION),
    'search': dict(query=string(100), revision=REVISION),
    'read_test': dict(path=string(256), revision=REVISION, start=dict(type='integer', minimum=1, maximum=MAX_READ_LINE), end=dict(type='integer', minimum=1, maximum=MAX_READ_LINE)),
}
CANDIDATE = obj(dict(title=string(200), trigger=string(1200), change=obj(dict(path=string(256), side=string(4, enum=['base', 'head']), line=dict(type='integer', minimum=1, maximum=8000), revision=REVISION)), impact=string(1200), evidence_ids=dict(type='array', items=string(100), maxItems=16)))
PARAMETERS = {name: obj(dict(**args, summary=SUMMARY)) for name, args in SOURCE_ARGS.items()}
PARAMETERS[FINISH] = obj(dict(reason=string(40, enum=sorted(REASONS)), summary=SUMMARY, candidates=dict(type='array', items=CANDIDATE, maxItems=8)))
DESCRIPTIONS = {
    'read_diff': 'Read the collected frozen diff and changed-line references.',
    'read_source': f'Read an inclusive frozen source range (1-based start <= end). Returns whole lines within {TOOL_LIMITS["result_bytes"]} serialized bytes, EOF/coverage and continuation when needed; no host files.',
    'find_symbol': 'Find collected Python definitions. Uncollected/dynamic behavior UNKNOWN.',
    'references': 'Find bounded syntactic Python occurrences, not a complete call graph.',
    'search': 'Literal search only over collected frozen source, no regex or external access.',
    'read_test': f'Read an inclusive frozen test source range within {TOOL_LIMITS["result_bytes"]} serialized bytes. Whole lines only; EOF/coverage and continuation when needed. Never run tests.',
    FINISH: 'End with conditional candidates, refutation, insufficient evidence or zero candidates. No additional model call.',
}


# Opt-in cross-file tool profile. Tasks that do not name it keep the exact v3 schema.
CROSS_FILE = 'cross-file-tools-v1'
CROSS_FILE_VERSION = 'mea-deepseek-native-tools-v4-cross-file'
CROSS_FILE_PARAMETERS = {name: obj(dict(**args, summary=SUMMARY)) for name, args in SOURCE_ARGS.items()}
CROSS_FILE_PARAMETERS['related_files'] = obj(dict(path=string(256), revision=REVISION, summary=SUMMARY))
CROSS_FILE_PARAMETERS[FINISH] = PARAMETERS[FINISH]
CROSS_FILE_DESCRIPTIONS = dict(
    DESCRIPTIONS,
    find_symbol='Find collected definitions: Python by AST, JavaScript/TypeScript by lexical declaration patterns. Uncollected/dynamic behavior UNKNOWN.',
    references='Find bounded occurrences: Python syntactic names, JavaScript/TypeScript lexical identifier matches (may include comments/strings). Not a complete call graph.',
    read_test=f'Read an inclusive frozen test source range (Python test_*.py/*_test.py, JS/TS *.test.*, *.spec.*, __tests__/) within {TOOL_LIMITS["result_bytes"]} serialized bytes. Never run tests.',
    related_files='List import edges between collected files at one revision: modules the path imports and collected files that import it. Each edge cites the import line. Bare package and dynamic imports stay UNKNOWN.',
)
PROFILES = {None: (VERSION, PARAMETERS, DESCRIPTIONS), CROSS_FILE: (CROSS_FILE_VERSION, CROSS_FILE_PARAMETERS, CROSS_FILE_DESCRIPTIONS)}


def profile(name=None):
    if name not in PROFILES:
        raise ValueError('TOOL_PROFILE')
    return PROFILES[name]


def version(name=None):
    return profile(name)[0]


def parameters(name=None):
    return profile(name)[1]


def definitions(name=None):
    # Normal official endpoint, no Beta strict-mode dependence. Local validation
    # remains mandatory even when a Provider uses these schemas successfully.
    _, params, descriptions = profile(name)
    return [dict(type='function', function=dict(name=n, description=descriptions[n], parameters=copy.deepcopy(p))) for n, p in params.items()]


def schema_sha256(name=None):
    return digest(definitions(name))


SCHEMA_SHA256 = digest(definitions())


def validate(value, schema, path="$.arguments"):
    kind = schema['type']
    if kind == 'object':
        if not isinstance(value, dict) or set(value) != set(schema['properties']):
            raise Invalid('NATIVE_ARGUMENT_FIELDS',path)
        for k, v in value.items():
            validate(v, schema['properties'][k],path+'.'+k)
    elif kind == 'array':
        if not isinstance(value, list) or len(value) > schema['maxItems']:
            raise Invalid('NATIVE_ARGUMENT_ARRAY_BOUND',path)
        for i,v in enumerate(value):
            validate(v, schema['items'],path+f'[{i}]')
    elif kind == 'integer':
        if type(value) is not int or not schema['minimum'] <= value <= schema['maximum']:
            raise Invalid('NATIVE_ARGUMENT_INTEGER',path)
    elif kind == 'string':
        try:text(value, schema['maxLength'])  # Enforce UTF-8 bytes and NUL bound.
        except ValueError:raise Invalid('NATIVE_ARGUMENT_STRING',path) from None
        if 'pattern' in schema and not re.fullmatch(schema['pattern'], value):
            raise Invalid('NATIVE_ARGUMENT_PATTERN',path)
        if 'enum' in schema and value not in schema['enum']:
            raise Invalid('NATIVE_ARGUMENT_ENUM',path)
    else:
        raise Invalid('NATIVE_SCHEMA_UNSUPPORTED',path)


class Invalid(ValueError):
    def __init__(self,code,path,stage='MODEL_ARGUMENTS'):
        super().__init__(code)
        self.diagnostic=dict(stage=stage,code=code,path=path)


def parse_message(message,used_ids=(),parameters=None):
    """Single-call accessor for tests/callers that explicitly expect one call."""
    batch=parse_batch(message,used_ids,parameters)
    if len(batch)!=1:raise ValueError('NATIVE_SINGLE_CALL_REQUIRED')
    return batch[0]


def parse_call(c,used_ids=(),parameters=None):
    params = PARAMETERS if parameters is None else parameters
    fields(c, 'id type function')
    if not isinstance(c['id'], str) or not CALL_ID.fullmatch(c['id']):
        raise ValueError('NATIVE_CALL_ID')
    if c['id'] in used_ids:
        raise ValueError('NATIVE_CALL_ID_REUSED')
    if c['type'] != 'function':
        raise ValueError('NATIVE_FUNCTION_REQUIRED')
    f = c['function']
    fields(f, 'name arguments')
    if not isinstance(f['name'], str) or f['name'] not in params:
        raise ValueError('NATIVE_FUNCTION_NOT_ALLOWED')
    if not isinstance(f['arguments'], str) or len(f['arguments'].encode()) > 20000:
        raise ValueError('NATIVE_ARGUMENT_BYTES')
    try:args = decode(f['arguments'].encode())
    except ValueError as exc:raise Invalid(str(exc),'$.arguments') from None
    validate(args, params[f['name']])
    if f['name'] == FINISH:
        d = dict(kind='finish', **args)
    else:
        d = dict(kind='tool', tool=f['name'], args={k: v for k, v in args.items() if k != 'summary'}, summary=args['summary'])
    Runner._decision(d)
    if d['kind'] == 'finish' and ((d['reason'] == 'HYPOTHESIS_SUPPORTED') != bool(d['candidates'])):
        raise ValueError('DECISION_STATUS_CONFLICT')
    return d, copy.deepcopy(c)


def parse_batch(message,used_ids=(),parameters=None):
    fields(message,'role content tool_calls')
    if message['role']!='assistant' or (message['content'] is not None and not isinstance(message['content'],str)):
        raise Invalid('NATIVE_ASSISTANT_FIELDS','$.message')
    if message['content'] is not None:text(message['content'],16000)
    calls=message['tool_calls']
    if not isinstance(calls,list) or not 1<=len(calls)<=8:
        raise Invalid('NATIVE_CALL_BATCH_BOUND','$.message.tool_calls')
    seen=set(used_ids);actions=set();result=[]
    for i,c in enumerate(calls):
        try:d,c=parse_call(c,seen,parameters)
        except ValueError as exc:
            stage='TOOL_NOT_ALLOWED' if str(exc)=='NATIVE_FUNCTION_NOT_ALLOWED' else 'MODEL_ARGUMENTS'
            suffix=getattr(exc,'diagnostic',{}).get('path','').removeprefix('$.arguments')
            path=f'$.message.tool_calls[{i}]'+('.function.arguments'+suffix if hasattr(exc,'diagnostic') else '')
            raise Invalid(str(exc),path,stage) from None
        seen.add(c['id'])
        action=digest(dict(tool=d.get('tool'),args=d.get('args'))) if d['kind']=='tool' else 'finish'
        if action in actions:raise Invalid('NATIVE_DUPLICATE_ACTION',f'$.message.tool_calls[{i}]')
        actions.add(action);result.append((d,c))
    if len(result)>1 and any(d['kind']=='finish' for d,c in result):
        raise Invalid('NATIVE_FINISH_EXCLUSIVE','$.message.tool_calls')
    return result


def validate_messages(messages, prompt, parameters=None):
    if not isinstance(messages, list) or len(messages) < 2:
        raise ValueError('NATIVE_MESSAGES')
    if messages[0] != dict(role='system', content=prompt):
        raise ValueError('NATIVE_SYSTEM_MESSAGE')
    fields(messages[1], 'role content')
    if messages[1]['role'] != 'user' or not isinstance(messages[1]['content'], str):
        raise ValueError('NATIVE_USER_MESSAGE')
    used=set();index=2
    while index<len(messages):
        batch=parse_batch(messages[index],used,parameters)
        if any(d['kind']!='tool' for d,c in batch):raise ValueError('NATIVE_FINISH_CANNOT_CONTINUE')
        expected=[c['id'] for d,c in batch];used.update(expected)
        index+=1
        for call_id in expected:
            if index>=len(messages):raise ValueError('NATIVE_UNPAIRED_HISTORY')
            result=messages[index];fields(result,'role tool_call_id content')
            if result['role']!='tool' or result['tool_call_id']!=call_id or not isinstance(result['content'],str):
                raise ValueError('NATIVE_FEEDBACK_CALL_ID')
            if not isinstance(decode(result['content'].encode()),dict):raise ValueError('NATIVE_FEEDBACK_OBJECT')
            index+=1
