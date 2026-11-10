"""Shared application spending gate. Integer nano-USD; no credentials/network.

Uses the task database transaction, so request intent and money reservation commit
together. Reservations are never released for uncertain outcomes. Usage-based
estimates are not invoices or an account-level payment hard cap.
"""
import json
import time
from ..contract import canonical, digest

ID = 'mea-review-deepseek-simulation-v1'
CAP = 10_000_000_000
PRICE_ID = 'deepseek-flash-simulation-v1'
# Frozen example rates for local arithmetic; no production pricing attestation.
RATES = dict(off_peak=dict(hit=3, miss=150, output=600), peak=dict(hit=6, miss=300, output=1200))


def estimate(usage):
    if not isinstance(usage,dict):raise ValueError('SHARED_USAGE_UNKNOWN')
    p,c,total=[usage.get(k) for k in ['prompt_tokens','completion_tokens','total_tokens']]
    if any(type(n) is not int or n<0 for n in [p,c,total]) or p+c!=total:raise ValueError('SHARED_USAGE_INVALID')
    hit,miss=usage.get('prompt_cache_hit_tokens'),usage.get('prompt_cache_miss_tokens')
    if type(hit) is not int or type(miss) is not int or min(hit,miss)<0 or hit+miss!=p:
        hit,miss=0,p
    return {tier:hit*r['hit']+miss*r['miss']+c*r['output'] for tier,r in RATES.items()}


def initialize(db, cap=None):
    """Explicitly bootstrap the mock ledger once. Subsequent workers never reset it."""
    if cap is not None and (type(cap) is not int or not 0 < cap <= CAP):
        raise ValueError('SHARED_BUDGET_IDENTITY')
    db.execute('CREATE TABLE IF NOT EXISTS mock_spending_meta (id TEXT PRIMARY KEY, doc TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS mock_spending_entries (id TEXT PRIMARY KEY, doc TEXT NOT NULL)')
    row = db.execute('SELECT doc FROM mock_spending_meta WHERE id=?', (ID,)).fetchone()
    if row:
        if cap is not None and spec(db)['cap_nano_usd'] != cap:
            raise ValueError('SHARED_BUDGET_ALREADY_INITIALIZED')
        return
    value = dict(id=ID, cap_nano_usd=CAP if cap is None else cap, currency='USD',
                 pricing_id=PRICE_ID, blocked=False, mode='SIMULATED', billed_actual='NOT_APPLICABLE_MOCK')
    db.execute('INSERT INTO mock_spending_meta VALUES (?,?)', (ID, canonical(value).decode()))


def spec(db):
    import sqlite3
    try:
        row = db.execute('SELECT doc FROM mock_spending_meta WHERE id=?', (ID,)).fetchone()
    except sqlite3.OperationalError as exc:
        if 'no such table' not in str(exc):
            raise
        row = None
    if not row:
        raise ValueError('SHARED_BUDGET_NOT_INITIALIZED')
    value = json.loads(row[0])
    if (value['id'] != ID or value['pricing_id'] != PRICE_ID or value['mode'] != 'SIMULATED'
            or type(value['cap_nano_usd']) is not int or not 0 < value['cap_nano_usd'] <= CAP):
        raise ValueError('SHARED_BUDGET_IDENTITY')
    return value


def ready(db):
    value = spec(db)
    if value['blocked']:
        raise ValueError('SHARED_BUDGET_RECONCILIATION_REQUIRED')
    return value


def summary(db):
    value=spec(db)
    entries=[json.loads(r[0]) for r in db.execute('SELECT doc FROM mock_spending_entries ORDER BY id')]
    used=sum(r['accounted_nano_usd'] for r in entries)
    return dict(id=ID, cap_nano_usd=value['cap_nano_usd'], accounted_nano_usd=used,
                remaining_nano_usd=max(0, value['cap_nano_usd'] - used),
                billed_actual='NOT_APPLICABLE_MOCK', entries=entries,
                pricing_id=PRICE_ID, blocked=value['blocked'], mode='SIMULATED')


def put(db,row):
    db.execute('INSERT OR REPLACE INTO mock_spending_entries VALUES (?,?)',(row['id'],canonical(row).decode()))


def get(db,identifier):
    row=db.execute('SELECT doc FROM mock_spending_entries WHERE id=?',(identifier,)).fetchone()
    if not row:raise ValueError('SHARED_RESERVATION_MISSING')
    return json.loads(row[0])


def reserve(db,identifier,task_id,body,token_reservation):
    ready(db)
    if body.get('model')!='deepseek-flash' or type(token_reservation) is not int or token_reservation<=body['max_tokens']:raise ValueError('SHARED_RESERVATION_INVALID')
    if db.execute('SELECT 1 FROM mock_spending_entries WHERE id=?',(identifier,)).fetchone():raise ValueError('SHARED_REQUEST_ID_REUSED')
    output=body['max_tokens'];prompt=token_reservation-output
    amount=prompt*RATES['peak']['miss']+output*RATES['peak']['output']
    if amount>summary(db)['remaining_nano_usd']:raise ValueError('SHARED_USD_BUDGET')
    row=dict(id=identifier,task_id=task_id,state='RESERVED',body_sha256=digest(body),input_token_bound=prompt,output_token_bound=output,
             reserved_nano_usd=amount,accounted_nano_usd=amount,billed_actual='NOT_APPLICABLE_MOCK',pricing_id=PRICE_ID,created_at=time.time())
    put(db,row);return dict(budget_id=ID,reserved_nano_usd=amount,pricing_id=PRICE_ID)


def sending(db,identifier,body):
    ready(db);row=get(db,identifier)
    if row['state']!='RESERVED' or row['body_sha256']!=digest(body):raise ValueError('SHARED_REQUEST_NOT_SENDABLE')
    row.update(state='SENDING',started_at=time.time());put(db,row)


def settle(db,identifier,receipt):
    row=get(db,identifier)
    if row.get('receipt_sha256')==digest(receipt):return public_entry(row)
    if row['state'] not in {'SENDING','UNCERTAIN'}:raise ValueError('SHARED_RECEIPT_STATE')
    if receipt.get('error') and receipt['error']!='HTTP_ERROR':
        row.update(state='UNCERTAIN',outcome='UNKNOWN_FULL_RESERVATION_RETAINED')
    else:
        try:
            costs=estimate(receipt.get('usage'));usage=receipt['usage']
            # A bound violation blocks future paid sends and keeps the larger
            # known estimate; it is never hidden by clipping to the reservation.
            violated=usage['prompt_tokens']>row['input_token_bound'] or usage['completion_tokens']>row['output_token_bound']
            row.update(state='USAGE_ESTIMATED',usage=usage,estimated_nano_usd=costs,
                       accounted_nano_usd=max(row['reserved_nano_usd'],costs['peak']) if violated else costs['peak'])
            if violated:
                value=spec(db);value['blocked']=True
                db.execute('UPDATE mock_spending_meta SET doc=? WHERE id=?',(canonical(value).decode(),ID))
        except ValueError:row.update(state='RESPONSE_USAGE_UNKNOWN',outcome='UNKNOWN_FULL_RESERVATION_RETAINED')
    row.update(receipt_sha256=digest(receipt),ended_at=time.time());put(db,row)
    return public_entry(row)


def public_entry(row):
    return dict(budget_id=ID,state=row['state'],reserved_nano_usd=row['reserved_nano_usd'],
                accounted_nano_usd=row['accounted_nano_usd'],estimated_nano_usd=row.get('estimated_nano_usd','UNKNOWN'),
                billed_actual='NOT_APPLICABLE_MOCK',pricing_id=PRICE_ID)


def interrupt(db, identifier):
    """Record the interruption in the same transaction as the task state."""
    row = get(db, identifier)
    if row['state'] == 'SENDING':
        row.update(state='UNCERTAIN', outcome='UNKNOWN_FULL_RESERVATION_RETAINED')
        put(db, row)
    elif row['state'] == 'RESERVED':
        row.update(state='NOT_SENT', outcome='UNSENT_RESERVATION_RETAINED')
        put(db, row)
    return public_entry(row)
