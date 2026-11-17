"""Shared application spending gate. Integer nano-USD; no credentials/network.

Uses the task database transaction, so request intent and money reservation commit
together. Reservations are never released for uncertain outcomes. Usage-based
estimates are not invoices or an account-level payment hard cap.

Two ledgers exist. SIMULATED keeps the Mock rehearsal arithmetic. LIVE prices
provider-reported usage at the official DeepSeek list price for the controlled
real worker. Neither is reconciled with the provider invoice.
"""
import json
import time
from datetime import datetime, timezone
from ..contract import canonical, digest

ID = 'mea-review-deepseek-simulation-v1'
CAP = 10_000_000_000
PRICE_ID = 'deepseek-flash-simulation-v1'
# Frozen example rates for local arithmetic; no production pricing attestation.
RATES = dict(off_peak=dict(hit=3, miss=150, output=600), peak=dict(hit=6, miss=300, output=1200))

# Official deepseek-flash list price in nano-USD per token, read from
# https://api-docs.deepseek.com/quick_start/pricing on 2026-11-17: per 1M tokens
# cache hit 0.003/0.006, cache miss 0.15/0.30, output 0.60/1.20 (off-peak/peak).
LIVE_ID = 'mea-m2-deepseek-live-v1'
LIVE_PRICE_ID = 'deepseek-flash-official-20261117'
LIVE_PRICE_SOURCE = 'https://api-docs.deepseek.com/quick_start/pricing (read 2026-11-17)'
LIVE_RATES = dict(off_peak=dict(hit=3, miss=150, output=600), peak=dict(hit=6, miss=300, output=1200))
NANO = 1_000_000_000
# Both limits are local ledger limits kept inside one task store (--store). A new store starts
# at zero, so neither is an account-level or phase-wide spending cap; real spend is decided by the
# provider invoice and tracked outside the store (agent-control/SPEND.md).
LIVE_CAP = 30 * NANO          # Largest budget_usd one store's live ledger accepts
LIVE_REQUEST_CAP = 10 * NANO  # Per-request reservation limit in one store's live ledger


def tier(at=None):
    """Official schedule: peak 01:00-04:00 and 06:00-10:00 UTC, Monday-Friday.

    Chinese public holidays are off-peak but are not modeled; such a day counts as
    peak, which only overstates the cost.
    """
    moment = datetime.fromtimestamp(time.time() if at is None else at, timezone.utc)
    peak = moment.weekday() < 5 and (1 <= moment.hour < 4 or 6 <= moment.hour < 10)
    return 'peak' if peak else 'off_peak'


class Ledger:
    def __init__(self, *, id, cap, pricing_id, rates, mode, billed_actual, tables, request_cap=None, price_source=None):
        self.id = id
        self.cap = cap
        self.pricing_id = pricing_id
        self.rates = rates
        self.mode = mode
        self.billed_actual = billed_actual
        self.meta_table, self.entry_table = tables
        self.request_cap = request_cap
        self.price_source = price_source

    def estimate(self, usage):
        if not isinstance(usage,dict):raise ValueError('SHARED_USAGE_UNKNOWN')
        p,c,total=[usage.get(k) for k in ['prompt_tokens','completion_tokens','total_tokens']]
        if any(type(n) is not int or n<0 for n in [p,c,total]) or p+c!=total:raise ValueError('SHARED_USAGE_INVALID')
        hit,miss=usage.get('prompt_cache_hit_tokens'),usage.get('prompt_cache_miss_tokens')
        if type(hit) is not int or type(miss) is not int or min(hit,miss)<0 or hit+miss!=p:
            hit,miss=0,p
        return {name:hit*r['hit']+miss*r['miss']+c*r['output'] for name,r in self.rates.items()}

    def initialize(self, db, cap=None):
        """Explicitly bootstrap the ledger once. Subsequent workers never reset it."""
        if cap is not None and (type(cap) is not int or not 0 < cap <= self.cap):
            raise ValueError('SHARED_BUDGET_IDENTITY')
        db.execute(f'CREATE TABLE IF NOT EXISTS {self.meta_table} (id TEXT PRIMARY KEY, doc TEXT NOT NULL)')
        db.execute(f'CREATE TABLE IF NOT EXISTS {self.entry_table} (id TEXT PRIMARY KEY, doc TEXT NOT NULL)')
        row = db.execute(f'SELECT doc FROM {self.meta_table} WHERE id=?', (self.id,)).fetchone()
        if row:
            if cap is not None and self.spec(db)['cap_nano_usd'] != cap:
                raise ValueError('SHARED_BUDGET_ALREADY_INITIALIZED')
            return
        value = dict(id=self.id, cap_nano_usd=self.cap if cap is None else cap, currency='USD',
                     pricing_id=self.pricing_id, blocked=False, mode=self.mode, billed_actual=self.billed_actual)
        if self.request_cap is not None:
            value.update(request_cap_nano_usd=self.request_cap, price_source=self.price_source)
        db.execute(f'INSERT INTO {self.meta_table} VALUES (?,?)', (self.id, canonical(value).decode()))

    def spec(self, db):
        import sqlite3
        try:
            row = db.execute(f'SELECT doc FROM {self.meta_table} WHERE id=?', (self.id,)).fetchone()
        except sqlite3.OperationalError as exc:
            if 'no such table' not in str(exc):
                raise
            row = None
        if not row:
            raise ValueError('SHARED_BUDGET_NOT_INITIALIZED')
        value = json.loads(row[0])
        if (value['id'] != self.id or value['pricing_id'] != self.pricing_id or value['mode'] != self.mode
                or type(value['cap_nano_usd']) is not int or not 0 < value['cap_nano_usd'] <= self.cap):
            raise ValueError('SHARED_BUDGET_IDENTITY')
        return value

    def ready(self, db):
        value = self.spec(db)
        if value['blocked']:
            raise ValueError('SHARED_BUDGET_RECONCILIATION_REQUIRED')
        return value

    def summary(self, db):
        value=self.spec(db)
        entries=[json.loads(r[0]) for r in db.execute(f'SELECT doc FROM {self.entry_table} ORDER BY id')]
        used=sum(r['accounted_nano_usd'] for r in entries)
        result=dict(id=self.id, cap_nano_usd=value['cap_nano_usd'], accounted_nano_usd=used,
                    remaining_nano_usd=max(0, value['cap_nano_usd'] - used),
                    billed_actual=self.billed_actual, entries=entries,
                    pricing_id=self.pricing_id, blocked=value['blocked'], mode=self.mode)
        if self.request_cap is not None:
            result.update(scheduled_nano_usd=sum(r.get('scheduled_nano_usd', 0) for r in entries),
                          request_cap_nano_usd=self.request_cap, price_source=self.price_source)
        return result

    def put(self,db,row):
        db.execute(f'INSERT OR REPLACE INTO {self.entry_table} VALUES (?,?)',(row['id'],canonical(row).decode()))

    def get(self,db,identifier):
        row=db.execute(f'SELECT doc FROM {self.entry_table} WHERE id=?',(identifier,)).fetchone()
        if not row:raise ValueError('SHARED_RESERVATION_MISSING')
        return json.loads(row[0])

    def reserve(self,db,identifier,task_id,body,token_reservation):
        self.ready(db)
        if body.get('model')!='deepseek-flash' or type(token_reservation) is not int or token_reservation<=body['max_tokens']:raise ValueError('SHARED_RESERVATION_INVALID')
        if db.execute(f'SELECT 1 FROM {self.entry_table} WHERE id=?',(identifier,)).fetchone():raise ValueError('SHARED_REQUEST_ID_REUSED')
        output=body['max_tokens'];prompt=token_reservation-output
        amount=prompt*self.rates['peak']['miss']+output*self.rates['peak']['output']
        if self.request_cap is not None and amount>self.request_cap:raise ValueError('SHARED_REQUEST_USD_CAP')
        if amount>self.summary(db)['remaining_nano_usd']:raise ValueError('SHARED_USD_BUDGET')
        row=dict(id=identifier,task_id=task_id,state='RESERVED',body_sha256=digest(body),input_token_bound=prompt,output_token_bound=output,
                 reserved_nano_usd=amount,accounted_nano_usd=amount,billed_actual=self.billed_actual,pricing_id=self.pricing_id,created_at=time.time())
        self.put(db,row);return dict(budget_id=self.id,reserved_nano_usd=amount,pricing_id=self.pricing_id)

    def sending(self,db,identifier,body):
        self.ready(db);row=self.get(db,identifier)
        if row['state']!='RESERVED' or row['body_sha256']!=digest(body):raise ValueError('SHARED_REQUEST_NOT_SENDABLE')
        row.update(state='SENDING',started_at=time.time())
        if self.request_cap is not None:row['price_tier']=tier(row['started_at'])
        self.put(db,row)

    def settle(self,db,identifier,receipt):
        row=self.get(db,identifier)
        if row.get('receipt_sha256')==digest(receipt):return self.public_entry(row)
        if row['state'] not in {'SENDING','UNCERTAIN'}:raise ValueError('SHARED_RECEIPT_STATE')
        if receipt.get('error') and receipt['error']!='HTTP_ERROR':
            row.update(state='UNCERTAIN',outcome='UNKNOWN_FULL_RESERVATION_RETAINED')
        else:
            try:
                costs=self.estimate(receipt.get('usage'));usage=receipt['usage']
                # A bound violation blocks future paid sends and keeps the larger
                # known estimate; it is never hidden by clipping to the reservation.
                violated=usage['prompt_tokens']>row['input_token_bound'] or usage['completion_tokens']>row['output_token_bound']
                row.update(state='USAGE_ESTIMATED',usage=usage,estimated_nano_usd=costs,
                           accounted_nano_usd=max(row['reserved_nano_usd'],costs['peak']) if violated else costs['peak'])
                if self.request_cap is not None:
                    row['scheduled_nano_usd']=costs[row.get('price_tier','peak')]
                if violated:
                    value=self.spec(db);value['blocked']=True
                    db.execute(f'UPDATE {self.meta_table} SET doc=? WHERE id=?',(canonical(value).decode(),self.id))
            except ValueError:row.update(state='RESPONSE_USAGE_UNKNOWN',outcome='UNKNOWN_FULL_RESERVATION_RETAINED')
        row.update(receipt_sha256=digest(receipt),ended_at=time.time());self.put(db,row)
        return self.public_entry(row)

    def public_entry(self,row):
        result=dict(budget_id=self.id,state=row['state'],reserved_nano_usd=row['reserved_nano_usd'],
                    accounted_nano_usd=row['accounted_nano_usd'],estimated_nano_usd=row.get('estimated_nano_usd','UNKNOWN'),
                    billed_actual=self.billed_actual,pricing_id=self.pricing_id)
        if self.request_cap is not None:
            result.update(price_tier=row.get('price_tier','UNKNOWN'),scheduled_nano_usd=row.get('scheduled_nano_usd','UNKNOWN'))
        return result

    def interrupt(self, db, identifier):
        """Record the interruption in the same transaction as the task state."""
        row = self.get(db, identifier)
        if row['state'] == 'SENDING':
            row.update(state='UNCERTAIN', outcome='UNKNOWN_FULL_RESERVATION_RETAINED')
            self.put(db, row)
        elif row['state'] == 'RESERVED':
            row.update(state='NOT_SENT', outcome='UNSENT_RESERVATION_RETAINED')
            self.put(db, row)
        return self.public_entry(row)


SIMULATED = Ledger(id=ID, cap=CAP, pricing_id=PRICE_ID, rates=RATES, mode='SIMULATED',
                   billed_actual='NOT_APPLICABLE_MOCK', tables=('mock_spending_meta', 'mock_spending_entries'))
LIVE = Ledger(id=LIVE_ID, cap=LIVE_CAP, pricing_id=LIVE_PRICE_ID, rates=LIVE_RATES, mode='OFFICIAL_PRICE_USAGE_ESTIMATE',
              billed_actual='NOT_RECONCILED_WITH_PROVIDER_INVOICE', tables=('live_spending_meta', 'live_spending_entries'),
              request_cap=LIVE_REQUEST_CAP, price_source=LIVE_PRICE_SOURCE)


def estimate(usage):return SIMULATED.estimate(usage)
def initialize(db, cap=None):return SIMULATED.initialize(db, cap)
def spec(db):return SIMULATED.spec(db)
def ready(db):return SIMULATED.ready(db)
def summary(db):return SIMULATED.summary(db)
def put(db,row):return SIMULATED.put(db,row)
def get(db,identifier):return SIMULATED.get(db,identifier)
def reserve(db,identifier,task_id,body,token_reservation):return SIMULATED.reserve(db,identifier,task_id,body,token_reservation)
def sending(db,identifier,body):return SIMULATED.sending(db,identifier,body)
def settle(db,identifier,receipt):return SIMULATED.settle(db,identifier,receipt)
def public_entry(row):return SIMULATED.public_entry(row)
def interrupt(db, identifier):return SIMULATED.interrupt(db, identifier)
