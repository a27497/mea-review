"""Explicit controlled provider process; never imported by HTTP/tool runtime."""
import argparse
import fcntl
import sys
import time
from pathlib import Path
from secrets import token_hex

from ..contract import canonical, digest
from . import authorization, envelope, real_protocol as protocol, spending
from .real import authorized, compatible, ensure, meta, put_meta, stop
from .tasks import Store, TERMINAL, event

MODE = authorization.MODE


class Broker:
    def __init__(self, store, *, sender, fault=None):
        self.store = store
        self.sender = sender
        self.fault = fault or (lambda *_: None)
        self.session = token_hex(16)
        ensure(store)
        self.lease = (store.root / 'provider.lock').open('a')
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lease.close()
            raise ValueError('PROVIDER_ALREADY_OWNED') from None
        try:
            with store.launch_lock(), store.transaction() as db:
                self.epoch = meta(db, 'epoch')
                if not self.epoch:
                    raise ValueError('WORKBENCH_NOT_RUNNING')
                authorization.register(db)
                spending.initialize(db)
                spending.ready(db)
                put_meta(db, 'provider', dict(status='READY', mode=MODE, session=self.session,
                                             epoch=self.epoch, heartbeat=time.time(),
                                             authorization=authorization.GRANT))
                for row in store.pending(MODE):
                    t = store.get(row['id'])
                    t['activation_revoked'] = True
                    a = t['active']
                    if not compatible(t):
                        stop(store, db, t, 'REAL_POLICY_VERSION_MISMATCH')
                    elif a and a['type'] == 'provider_request' and t['requests'][-1]['state'] != 'RECEIVED':
                        reason = ('REQUEST_UNCERTAIN_ON_WORKER_RESTART'
                                  if t['requests'][-1]['state'] in ('SENDING', 'UNCERTAIN')
                                  else 'REQUEST_NOT_SENT_ON_WORKER_RESTART')
                        stop(store, db, t, reason)
                    elif not a:
                        stop(store, db, t, 'EXPLICIT_START_REQUIRED_AFTER_WORKER_RESTART')
                    else:
                        store._put(db, t)
        except BaseException:
            self.lease.close()
            raise

    def heartbeat(self):
        with self.store.transaction() as db:
            p=meta(db,'provider',{})
            if p.get('session')!=self.session:return
            p['heartbeat']=time.time();put_meta(db,'provider',p)

    def revoked(self,identifier):
        t=self.store.get(identifier)
        with self.store.connection() as db:epoch=meta(db,'epoch')
        return t['status'] in TERMINAL or t.get('activation_revoked') or epoch!=self.epoch

    def prepare(self,identifier):
        with self.store.transaction() as db:
            t=self.store.get(identifier)
            if t['status'] in TERMINAL or t['mode']!=MODE or t['active'] is not None:return None
            if not authorized(db,t) or t['activation']['session']!=self.session:return None
            if not compatible(t):stop(self.store,db,t,'REAL_POLICY_VERSION_MISMATCH');return None
            if identifier != 'task-' + digest(t['identity']) or digest(t['view'])!=t['identity']['view_sha256'] or t['view']['revisions']!=t['identity']['revisions']:
                stop(self.store,db,t,'VIEW_IDENTITY_MISMATCH');return None
            if protocol.remaining(t)<=0:stop(self.store,db,t,'TIME_BUDGET');return None
            if t['usage']['rounds']>=t['budget']['rounds']:stop(self.store,db,t,'REQUEST_BUDGET');return None
            try:
                grant_key, grant, caps = authorization.ledger(db)
                authorization.validate_task(t['identity'])
            except ValueError as exc:
                stop(self.store, db, t, str(exc))
                return None
            if identifier not in grant['tasks']:stop(self.store,db,t,'AUTHORIZATION_TASK_NOT_CLAIMED');return None
            if len(grant['requests'])>=caps[1]:stop(self.store,db,t,'PHASE3_POST_CAP');return None
            try:body,reservation,context_manifest=protocol.prepare_request(t)
            except (ValueError,KeyError,TypeError,IndexError,UnicodeError,RecursionError) as exc:
                if isinstance(exc,protocol.window.Insufficient):
                    t['context_failure']=exc.details
                    event(t,'context_admission_rejected','Context cannot fit without dropping recent complete feedback; no request intent or fee reservation.',reason=str(exc),resources=exc.details)
                stop(self.store,db,t,str(exc) if str(exc) in {'REQUEST_INPUT_BYTE_BOUND','MODEL_CONTEXT_BUDGET','TOKEN_RESERVATION_BUDGET'} else 'NATIVE_REQUEST_HISTORY_INVALID');return None
            if t['usage']['tokens_accounted']+reservation>t['budget']['tokens']:stop(self.store,db,t,'TOKEN_RESERVATION_BUDGET');return None
            identifier_request = 'request-' + token_hex(16)
            try:
                money = spending.reserve(db, identifier_request, identifier, body, reservation)
            except ValueError as exc:
                stop(self.store, db, t, str(exc))
                return None
            r=dict(id=identifier_request,state='PREPARED',intent_at=time.time(),input_bytes=len(canonical(body)),request_body_sha256=digest(body),
                   request_body=body,context_manifest=context_manifest,context_manifest_sha256=digest(context_manifest),token_reservation=reservation,accounted_tokens=reservation,max_output_tokens=protocol.OUTPUT_TOKENS,
                   timeout_seconds=min(protocol.REQUEST_SECONDS,max(.001,protocol.remaining(t))),
                   configured_model=protocol.MODEL,prompt_sha256=protocol.PROMPT_SHA256,receipt=None,
                   billed_actual='NOT_APPLICABLE_MOCK',cost_source='SIMULATED',shared_spending=money)
            t['requests'].append(r);t['active']=dict(type='provider_request',request_id=identifier_request)
            t['usage']['rounds']+=1;t['usage']['tokens_accounted']+=reservation;t['status']='RUNNING'
            event(t,'request_intent','Request intent, token and simulated cost reservations committed before Mock launch.',request_id=identifier_request,token_reservation=reservation)
            grant['requests'].append(identifier_request);put_meta(db,grant_key,grant);self.store._put(db,t)
        self.fault('after_intent',t)
        return r

    def step(self,identifier):
        r=self.prepare(identifier)
        if r is None:return False
        # Cancellation/restart and launch serialize across both processes. The
        # SENDING marker is durable before spawning a potentially paid request.
        with self.store.launch_lock():
            with self.store.transaction() as db:
                t=self.store.get(identifier)
                if t['status'] in TERMINAL:
                    return False
                if not authorized(db, t):
                    stop(self.store, db, t, 'EXPLICIT_START_REQUIRED')
                    return False
                remaining=protocol.remaining(t)
                if remaining<=0:stop(self.store,db,t,'TIME_BUDGET');return False
                current = t['requests'][-1]
                try:
                    _, grant, _ = authorization.ledger(db)
                    authorization.validate_task(t['identity'])
                    if (identifier not in grant['tasks'] or r['id'] not in grant['requests']
                            or current['id'] != r['id'] or current['state'] != 'PREPARED'
                            or current['request_body_sha256'] != digest(current['request_body'])
                            or current['request_body'] != r['request_body']
                            or identifier != 'task-' + digest(t['identity'])
                            or digest(t['view']) != t['identity']['view_sha256']):
                        raise ValueError('REQUEST_JOURNAL_MISMATCH')
                    spending.sending(db, r['id'], current['request_body'])
                except ValueError as exc:
                    stop(self.store, db, t, str(exc))
                    return False
                current['timeout_seconds']=min(current['timeout_seconds'],remaining,protocol.REQUEST_SECONDS)
                r['timeout_seconds']=current['timeout_seconds']
                current.update(state='SENDING',started_at=time.time());event(t,'request_started','Mock launch intent committed. Loss of acknowledgement leaves the outcome unknown.',request_id=r['id'])
                self.store._put(db,t)
            self.fault('after_sending_intent',t)
            try:handle=self.sender.start(r['request_body'])
            except Exception:
                self.persist_receipt(identifier,r['id'],dict(error='LAUNCH_RESULT_UNCERTAIN',http_posts_attempted='UNKNOWN'));return False
        self.fault('after_launch',t)
        try:
            value=self.sender.collect(handle,r['timeout_seconds'],lambda:self.revoked(identifier),self.heartbeat)
        except Exception:
            value=dict(error='REQUEST_RESULT_UNCERTAIN',http_posts_attempted='UNKNOWN')
        self.fault('after_wire',t)
        self.persist_receipt(identifier,r['id'],value)
        self.fault('after_receipt',self.store.get(identifier))
        return True

    def persist_receipt(self, identifier, request_id, value):
        try:
            value = protocol.receipt(envelope.redact(value))
            http_status = value.get('http_status')
            known_http = type(http_status) is int and 100 <= http_status <= 599
            if not known_http and (not value.get('error') or value['error'] == 'HTTP_ERROR'):
                raise ValueError('WIRE_OUTCOME_UNKNOWN')
        except (ValueError, TypeError, RecursionError):
            value = dict(error='INVALID_WIRE_UNCERTAIN', http_posts_attempted='UNKNOWN')
        with self.store.transaction() as db:
            t = self.store.get(identifier)
            r = t['requests'][-1]
            if r['id'] != request_id or r['state'] not in ('SENDING', 'UNCERTAIN') or r['receipt'] is not None:
                return
            r['receipt'] = value
            r['ended_at'] = time.time()
            r['shared_spending'] = spending.settle(db, r['id'], value)
            known_error=value.get('error')=='HTTP_ERROR'
            if value.get('error') and not known_error:
                r['state']='UNCERTAIN'
                event(t,'request_uncertain','Request outcome unknown; full reservation retained and automatic resend forbidden.',request_id=r['id'])
                if t['status'] not in TERMINAL:stop(self.store,db,t,'REQUEST_UNCERTAIN')
                else:self.store._put(db,t)
                return
            r['state']='RECEIVED'
            u=protocol.usage(value.get('usage'),r['token_reservation'])
            if u is not None:
                t['usage']['tokens_accounted']+=u['total_tokens']-r['accounted_tokens'];r['accounted_tokens']=u['total_tokens']
                t['usage']['reported_tokens']+=u['total_tokens'];r['reported_usage']=u
                r['token_estimate_observation']=dict(method=r.get('context_manifest',{}).get('estimation',{}).get('method'),reserved_input=r['token_reservation']-protocol.OUTPUT_TOKENS,provider_prompt_tokens=u['prompt_tokens'],difference=(r['token_reservation']-protocol.OUTPUT_TOKENS)-u['prompt_tokens'],note='Reported usage settles known usage; future reservations are not discounted.')
            else:t['usage']['unknown_usage_requests']+=1
            counts=value.get('usage',{}) if isinstance(value.get('usage'),dict) else {}
            if (type(counts.get('prompt_tokens')) is int and counts['prompt_tokens']>r['token_reservation']-protocol.OUTPUT_TOKENS) or (type(counts.get('total_tokens')) is int and counts['total_tokens']>r['token_reservation']) or (type(counts.get('completion_tokens')) is int and counts['completion_tokens']>protocol.OUTPUT_TOKENS):
                event(t,'usage_bound_violation','Provider-reported usage exceeds the reserved/requested bound; no further request is allowed.')
                if t['status'] not in TERMINAL:stop(self.store,db,t,'PROVIDER_USAGE_BOUND_VIOLATION')
                else:self.store._put(db,t)
                return
            size=len(canonical(value));t['usage']['output_bytes']+=size
            event(t,'response_saved','Sanitized Mock response saved before parsing and tool dispatch.',request_id=r['id'])
            if t['status'] not in TERMINAL and t['usage']['output_bytes']>t['budget']['output_bytes']:stop(self.store,db,t,'OUTPUT_BUDGET')
            else:self.store._put(db,t)

    def close(self):
        if self.lease.closed:
            return
        try:
            with self.store.launch_lock(), self.store.transaction() as db:
                p = meta(db, 'provider', {})
                if p.get('session') == self.session:
                    p['status'] = 'STOPPED'
                    put_meta(db, 'provider', p)
                for row in self.store.pending(MODE):
                    t = self.store.get(row['id'])
                    if t['activation']['session'] != self.session:
                        continue
                    t['activation_revoked'] = True
                    a = t['active']
                    if a and a['type'] == 'provider_request' and t['requests'][-1]['state'] in ('SENDING', 'UNCERTAIN'):
                        stop(self.store, db, t, 'REQUEST_UNCERTAIN_ON_WORKER_EXIT')
                    elif a and a['type'] == 'provider_request' and t['requests'][-1]['state'] == 'PREPARED':
                        stop(self.store, db, t, 'REQUEST_NOT_SENT_ON_WORKER_EXIT')
                    elif not a:
                        stop(self.store, db, t, 'EXPLICIT_START_REQUIRED_AFTER_WORKER_EXIT')
                    else:
                        self.store._put(db, t)
        finally:
            self.lease.close()


def main():
    parser = argparse.ArgumentParser(description='Controlled DeepSeek protocol rehearsal')
    parser.add_argument('--mock', action='store_true')
    parser.add_argument('--store', type=Path)
    args = parser.parse_args()
    if not args.mock or args.store is None:
        print('BLOCKED: live admission is not configured. Mock requires --mock and --store.', flush=True)
        return 2
    from .mock_provider import Sender

    broker = None
    try:
        broker = Broker(Store(args.store), sender=Sender())
        print('READY ' + MODE + ' ' + protocol.MODEL, flush=True)
        while True:
            broker.heartbeat()
            with broker.store.connection() as db:
                if meta(db, 'epoch') != broker.epoch:
                    break
            for t in broker.store.pending(MODE):
                broker.step(t['id'])
            time.sleep(.1)
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError):
        print('BLOCKED: Mock worker admission or runtime unavailable.', flush=True)
        return 2
    finally:
        if broker:
            broker.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
