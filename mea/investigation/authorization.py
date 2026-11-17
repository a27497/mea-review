"""Scoped simulation grants. No real authorization is bundled."""
import copy
import json
import re
from ..contract import canonical
from . import native, real_protocol as protocol
from .provider_view import TOOL_VERSION

MODE = 'MOCK_PROVIDER_NOT_AI'
GRANT = 'mea-review-deepseek-simulation-v1'
KEY = 'grant:' + GRANT
INPUTS = (
    '8dd6ef1de8cc1c46b500b90fc4331b3c26ad49d5a758088859ae20e683dc05fa',
    '9e41878303b42d0ad2d2e0af3e46513aed12fd42372feb73a86c403aa281080f',
)

def definition():
    return dict(id=GRANT, mode=MODE, model=protocol.MODEL, policy=protocol.POLICY,
                protocol_version=native.VERSION, tool_version=TOOL_VERSION,
                prompt_sha256=protocol.PROMPT_SHA256, tool_schema_sha256=native.SCHEMA_SHA256,
                inputs=list(INPUTS),
                max_tasks=2, max_requests=10)


def register(db):
    record = dict(authorization=definition(), tasks=[], requests=[])
    db.execute('INSERT OR IGNORE INTO task_meta VALUES (?,?)', (KEY, canonical(record).decode()))
    return ledger(db)


def ledger(db):
    row = db.execute('SELECT doc FROM task_meta WHERE name=?', (KEY,)).fetchone()
    if row is None:
        raise ValueError('MOCK_AUTHORIZATION_REQUIRED')
    value = json.loads(row[0])
    expected = definition()
    if value.get('authorization') != expected:
        raise ValueError('MOCK_AUTHORIZATION_SCOPE_MISMATCH')
    return KEY, value, (expected['max_tasks'], expected['max_requests'])


def validate_task(identity):
    scope = definition()
    if (identity.get('authorization') != GRANT or identity.get('mode') != MODE
            or identity.get('input_sha256') not in scope['inputs']
            or identity.get('model') != scope['model']
            or identity.get('policy_version') != scope['policy']
            or any(identity.get(k) != scope[k] for k in (
                'prompt_sha256', 'protocol_version', 'tool_schema_sha256', 'tool_version'))):
        raise ValueError('MOCK_AUTHORIZATION_SCOPE_MISMATCH')


# Controlled real-provider lane. Its scope comes from an operator file kept with the
# private snapshots: exact input digests, task and request caps and the tool profile.
# Nothing here reads a credential; the worker reads it only after admission.
LIVE_MODE = 'REAL_PROVIDER_DEEPSEEK'
LIVE_PREFIX = 'mea-live-'
LIVE_MAX_TASKS = 8
LIVE_MAX_REQUESTS = 48
SCOPE_FIELDS = {'id', 'inputs', 'max_tasks', 'max_requests', 'tool_profile', 'first_turn', 'approval', 'budget_usd', 'task_budget'}


class Lane:
    """One admission scope: mode, grant record, task validation and spending ledger."""

    def __init__(self, *, mode, scope, ledger, label, ledger_cap=None):
        self.mode = mode
        self.ledger_cap = ledger_cap
        self.scope = scope
        self.grant = scope['id']
        self.key = 'grant:' + self.grant
        self.spending = ledger
        self.label = label

    def definition(self):
        return copy.deepcopy(self.scope)

    def register(self, db):
        record = dict(authorization=self.definition(), tasks=[], requests=[])
        db.execute('INSERT OR IGNORE INTO task_meta VALUES (?,?)', (self.key, canonical(record).decode()))
        return self.ledger(db)

    def ledger(self, db):
        row = db.execute('SELECT doc FROM task_meta WHERE name=?', (self.key,)).fetchone()
        if row is None:
            raise ValueError(self.code('AUTHORIZATION_REQUIRED'))
        value = json.loads(row[0])
        if value.get('authorization') != self.definition():
            raise ValueError(self.code('AUTHORIZATION_SCOPE_MISMATCH'))
        return self.key, value, (self.scope['max_tasks'], self.scope['max_requests'])

    def code(self, suffix):
        return ('MOCK_' if self.mode == MODE else 'LIVE_') + suffix

    def validate_task(self, identity):
        scope = self.scope
        expected = dict(model=scope['model'], policy_version=scope['policy'], prompt_sha256=scope['prompt_sha256'],
                        protocol_version=scope['protocol_version'], tool_schema_sha256=scope['tool_schema_sha256'],
                        tool_version=scope['tool_version'])
        if self.mode != MODE:
            expected.update(tool_profile=scope['tool_profile'], first_turn=scope['first_turn'], budget=scope['task_budget'])
        if (identity.get('authorization') != self.grant or identity.get('mode') != self.mode
                or identity.get('input_sha256') not in scope['inputs']
                or any(identity.get(k) != v for k, v in expected.items())):
            raise ValueError(self.code('AUTHORIZATION_SCOPE_MISMATCH'))


def live(scope):
    """Validate an operator scope file and bind it to the live ledger."""
    from . import spending
    if not isinstance(scope, dict) or set(scope) != SCOPE_FIELDS:
        raise ValueError('LIVE_SCOPE_FIELDS')
    if not isinstance(scope['id'], str) or not re.fullmatch(re.escape(LIVE_PREFIX) + r'[a-z0-9-]{1,60}', scope['id']):
        raise ValueError('LIVE_SCOPE_ID')
    inputs = scope['inputs']
    if (not isinstance(inputs, list) or not 1 <= len(inputs) <= LIVE_MAX_TASKS or len(set(inputs)) != len(inputs)
            or any(not isinstance(i, str) or not re.fullmatch('[0-9a-f]{64}', i) for i in inputs)):
        raise ValueError('LIVE_SCOPE_INPUTS')
    if (type(scope['max_tasks']) is not int or not 1 <= scope['max_tasks'] <= len(inputs)
            or type(scope['max_requests']) is not int
            or not 1 <= scope['max_requests'] <= min(LIVE_MAX_REQUESTS, scope['max_tasks'] * protocol.LIMITS['rounds'])):
        raise ValueError('LIVE_SCOPE_CAPS')
    if scope['tool_profile'] not in native.PROFILES or scope['first_turn'] not in (None, *protocol.FIRST_TURN_PROFILES):
        raise ValueError('LIVE_SCOPE_PROFILE')
    budget = scope['budget_usd']
    if type(budget) is not int or not 1 <= budget * spending.NANO <= spending.LIVE_CAP:
        raise ValueError('LIVE_SCOPE_BUDGET')
    if not isinstance(scope['approval'], str) or not 1 <= len(scope['approval']) <= 300:
        raise ValueError('LIVE_SCOPE_APPROVAL')
    try:
        task_budget = protocol.budget(scope['task_budget'])
    except ValueError:
        raise ValueError('LIVE_SCOPE_TASK_BUDGET') from None
    if task_budget != scope['task_budget']:
        raise ValueError('LIVE_SCOPE_TASK_BUDGET')
    from .provider_view import TOOL_VERSION as DEFAULT_TOOL_VERSION, CROSS_FILE_TOOL_VERSION
    full = dict(scope, mode=LIVE_MODE, model=protocol.MODEL, policy=protocol.POLICY,
                protocol_version=native.version(scope['tool_profile']),
                tool_version=CROSS_FILE_TOOL_VERSION if scope['tool_profile'] else DEFAULT_TOOL_VERSION,
                prompt_sha256=protocol.PROMPT_SHA256, tool_schema_sha256=native.schema_sha256(scope['tool_profile']))
    return Lane(mode=LIVE_MODE, scope=full, ledger=spending.LIVE, label='real DeepSeek',
                ledger_cap=budget * spending.NANO)


def mock():
    from . import spending
    return Lane(mode=MODE, scope=definition(), ledger=spending.SIMULATED, label='Mock')


def ledger_for(mode):
    from . import spending
    return spending.LIVE if mode == LIVE_MODE else spending.SIMULATED
