"""Attended real DeepSeek run of one scoped snapshot through the existing worker state machine.

The operator names a scope file, a snapshot and a private store. The run uses the
same Coordinator (decisions, frozen tools, candidate validation) and Broker
(reservation, SENDING marker, receipt settlement) as the Mock rehearsal, with the
live lane and the real sender. One task per invocation; one POST per request; no
retries. The task budget bounds requests (6) and tools (8); the loop has a fixed
iteration bound as a second guard.
"""
import argparse
import json
import sys
from pathlib import Path

from ..contract import canonical, decode, digest
from . import authorization, deepseek, real_protocol as protocol
from .provider_worker import Broker
from .real import Coordinator, create
from .tasks import Store, TERMINAL

MAX_ITERATIONS = 200


def drive(store, coordinator, broker, identifier, iterations=MAX_ITERATIONS):
    # Same alternation as the Mock rehearsal tests: deterministic steps, then at most one send.
    for _ in range(iterations):
        broker.heartbeat()
        coordinator.tick()
        task = store.get(identifier)
        if task['status'] in TERMINAL:
            return task
        broker.step(identifier)
    return store.get(identifier)


def summary(task, ledger):
    requests = []
    for r in task['requests']:
        receipt = r.get('receipt') or {}
        spend = r.get('shared_spending') or {}
        requests.append(dict(
            id=r['id'], state=r['state'], http_status=receipt.get('http_status'), error=receipt.get('error'),
            request_id=receipt.get('request_id'), completion_id=receipt.get('completion_id'), model=receipt.get('model'),
            finish_reason=receipt.get('finish_reason'), usage=receipt.get('usage'), elapsed_seconds=receipt.get('elapsed_seconds'),
            http_posts_attempted=receipt.get('http_posts_attempted'), token_reservation=r['token_reservation'],
            price_tier=spend.get('price_tier'), scheduled_nano_usd=spend.get('scheduled_nano_usd'),
            accounted_nano_usd=spend.get('accounted_nano_usd'), estimated_nano_usd=spend.get('estimated_nano_usd'),
            calls=[dict(id=c['id'], name=c['function']['name'], arguments=c['function']['arguments']) for c in r.get('native_calls', [])]))
    tools = [dict(tool=e['tool'], args=e['args'], tool_call_id=e['tool_call_id']) for e in task['events'] if e['kind'] == 'tool_started']
    return dict(task_id=task['id'], status=task['status'], reason=task['reason'], mode=task['mode'], provider=task['provider'],
                pr=dict(repository=task['pr']['repository'], number=task['pr']['number'], url=task['pr']['url']),
                revisions=task['view']['revisions'], input_sha256=task['identity']['input_sha256'],
                view_sha256=task['identity']['view_sha256'], tool_profile=task['identity'].get('tool_profile'),
                first_turn=task['identity'].get('first_turn'), usage=task['usage'], requests=requests, tool_sequence=tools,
                candidates=task['candidates'], rejected=task['rejected'],
                decision_rejections=[e for e in task['events'] if e['kind'] in ('decision_rejected', 'response_rejected', 'stopped')],
                ledger={k: v for k, v in ledger.items() if k != 'entries'})


def main(argv=None):
    parser = argparse.ArgumentParser(description='Attended real DeepSeek investigation of one scoped snapshot')
    parser.add_argument('--scope', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--credentials', type=Path, default=deepseek.CREDENTIALS)
    parser.add_argument('--confirm-real-provider', action='store_true')
    args = parser.parse_args(argv)
    if not args.confirm_real_provider:
        print('BLOCKED: --confirm-real-provider is required for a billed DeepSeek run.', flush=True)
        return 2
    try:
        lane = authorization.live(decode(args.scope.read_bytes()))
        snapshot = decode(args.snapshot.read_bytes())
        if digest(snapshot) not in lane.scope['inputs']:
            raise ValueError('LIVE_INPUT_NOT_IN_SCOPE')
        secret = deepseek.read_key(args.credentials)
    except (OSError, ValueError) as exc:
        print('BLOCKED: ' + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__), flush=True)
        return 2
    store = Store(args.store)
    coordinator = Coordinator(store, lane)
    coordinator.boot()
    sender = deepseek.Sender(secret)
    del secret
    broker = Broker(store, sender=sender, lane=lane)
    try:
        task = create(store, snapshot, lane=lane, confirmed=True, first_turn=lane.scope['first_turn'],
                      tool_profile=lane.scope['tool_profile'])
        sender.validate = lambda body: protocol.validate_task_request(body, task)
        if task['status'] in TERMINAL:
            print('EXISTING_TERMINAL_TASK ' + task['id'], flush=True)
        else:
            task = drive(store, coordinator, broker, task['id'])
    finally:
        broker.close()
        coordinator.shutdown()
    task = store.get(task['id'])
    with store.connection() as db:
        ledger = lane.spending.summary(db)
    result = summary(task, ledger)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(canonical(result))
    print(json.dumps(dict(task=task['id'], status=task['status'], reason=task['reason'], requests=len(task['requests']),
                          tools=task['usage']['tools'], candidates=len(task['candidates']), rejected=len(task['rejected']),
                          http_posts=sender.calls, scheduled_usd=ledger['scheduled_nano_usd'] / 1e9,
                          accounted_usd=ledger['accounted_nano_usd'] / 1e9), sort_keys=True), flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
