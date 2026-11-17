"""Single-attempt DeepSeek calls for the M3 Direct arm and judge, with an append-only cost ledger.

Direct requests go through the same wire adapter as the Agent (mea.investigation.wire.call). The judge
uses thinking mode with a JSON response, whose reasoning can exceed that adapter's 64 KB bound, so it
has its own single POST here: one attempt, bounded response, key redaction, reasoning dropped (only its
length is kept). Neither path retries. Costs use the official list price (peak-rate upper bound kept)
and are estimates, not the provider invoice.
"""
import http.client
import json
import ssl
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mea.contract import canonical, digest  # noqa: E402
from mea.investigation import envelope, spending, wire  # noqa: E402

PRICE_SOURCE = 'https://api-docs.deepseek.com/quick_start/pricing (read 2026-11-17)'
# nano-USD per token: cache hit, cache miss, output.
RATES = {
    'deepseek-flash': spending.LIVE_RATES,
    'deepseek-v4-pro': dict(off_peak=dict(hit=22, miss=660, output=1980), peak=dict(hit=44, miss=1320, output=3960)),
}
REQUEST_CAP_NANO = 10 * spending.NANO
JUDGE_RESPONSE_BYTES = 1_500_000
JUDGE_SECONDS = 600
DIRECT_SECONDS = 120


def cost(model, usage, at=None):
    """dict(scheduled, peak) nano-USD for provider-reported usage, or None if usage is unusable."""
    if not isinstance(usage, dict):
        return None
    p, c = usage.get('prompt_tokens'), usage.get('completion_tokens')
    if type(p) is not int or type(c) is not int or p < 0 or c < 0:
        return None
    hit, miss = usage.get('prompt_cache_hit_tokens'), usage.get('prompt_cache_miss_tokens')
    if type(hit) is not int or type(miss) is not int or hit + miss != p:
        hit, miss = 0, p
    rates = RATES[model]
    price = {t: hit * r['hit'] + miss * r['miss'] + c * r['output'] for t, r in rates.items()}
    return dict(scheduled=price[spending.tier(at)], peak=price['peak'], tier=spending.tier(at))


def reservation(model, input_bytes, max_tokens):
    """Peak-rate upper bound before sending: every input byte as one uncached token plus the full output."""
    r = RATES[model]['peak']
    return input_bytes * r['miss'] + max_tokens * r['output']


class Ledger:
    """Append-only JSONL of every billed attempt, with a declared cap checked before each send."""

    def __init__(self, path, cap_nano):
        self.path = Path(path)
        self.cap = cap_nano
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def entries(self):
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def spent_peak(self):
        return sum(e['peak_nano'] if e['peak_nano'] is not None else e['reserved_nano'] for e in self.entries())

    def admit(self, reserved):
        if reserved > REQUEST_CAP_NANO:
            raise ValueError('REQUEST_USD_CAP')
        if self.spent_peak() + reserved > self.cap:
            raise ValueError('LEDGER_CAP')

    def record(self, **row):
        with self.path.open('a') as f:
            f.write(json.dumps(row, sort_keys=True) + '\n')


def direct_call(body, secret, validate, connect=None):
    connect = connect or (lambda host, port, timeout: http.client.HTTPSConnection(host, port, timeout=DIRECT_SECONDS,
                                                                                 context=ssl.create_default_context()))
    return wire.call(body, secret, connect=connect, validate=validate)


def judge_call(body, secret, connect=None):
    """One POST; returns a redacted receipt with content, usage and reasoning length."""
    connect = connect or (lambda: http.client.HTTPSConnection('api.deepseek.com', 443, timeout=JUDGE_SECONDS,
                                                              context=ssl.create_default_context()))
    before = time.monotonic()
    conn = connect()
    try:
        conn.request('POST', '/chat/completions', body=canonical(body),
                     headers={'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json',
                              'Accept': 'application/json', 'Accept-Encoding': 'identity', 'User-Agent': 'mea-m3-judge-v1'})
        response = conn.getresponse()
        raw = response.read(JUDGE_RESPONSE_BYTES + 1)
        result = dict(http_status=response.status, response_sha256=digest(raw), response_bytes=len(raw),
                      elapsed_seconds=round(time.monotonic() - before, 3), http_posts_attempted=1,
                      request_id=(response.getheader('x-request-id') or 'UNKNOWN')[:256])
        if len(raw) > JUDGE_RESPONSE_BYTES:
            result['error'] = 'RESPONSE_BYTE_BOUND'
            return envelope.redact(result, secret)
        if response.status != 200:
            result['error'] = 'HTTP_ERROR'
            result['body'] = raw[:2000].decode('utf-8', 'replace')
            return envelope.redact(result, secret)
        value = json.loads(raw)
        choice = value['choices'][0]
        message = choice['message']
        result.update(model=value.get('model'), completion_id=value.get('id'), usage=value.get('usage'),
                      finish_reason=choice.get('finish_reason'), content=message.get('content'),
                      reasoning_chars=len(message.get('reasoning_content') or ''))
        return envelope.redact(result, secret)
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError, IndexError) as exc:
        return envelope.redact(dict(error='CONNECTION_OR_RESPONSE_UNCERTAIN', code=type(exc).__name__, http_posts_attempted=1,
                                    elapsed_seconds=round(time.monotonic() - before, 3)), secret)
    finally:
        conn.close()
