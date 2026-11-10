"""Scoped simulation grants. No real authorization is bundled."""
import json
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
