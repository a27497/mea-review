"""Single-attempt DeepSeek wire contract with an injected connection.

No worker or CLI in this candidate supplies a real connection or reads keys.
The adapter bounds response bytes and strips private fields before journaling.
"""
import http.client
import time
from ..contract import canonical, decode, digest
from . import envelope
from .real_protocol import REQUEST_SECONDS, validate_request


def call(body, secret, *, connect):
    validate_request(body)
    before=time.monotonic();conn=connect('api.deepseek.com',443,timeout=REQUEST_SECONDS)
    attempted=0
    try:
        attempted=1
        conn.request('POST','/chat/completions',body=canonical(body),headers={'Authorization':'Bearer '+secret,'Content-Type':'application/json','Accept':'application/json','Accept-Encoding':'identity','User-Agent':'mea-investigation-v1'})
        response=conn.getresponse();raw=response.read(64001)
        if len(raw)>64000:raise ValueError('RESPONSE_BYTE_BOUND')
        rid=response.getheader('x-request-id') or response.getheader('request-id') or 'UNKNOWN'
        result=dict(http_status=response.status,request_id=rid[:256],response_sha256=digest(raw),response_bytes=len(raw),elapsed_seconds=round(time.monotonic()-before,6),http_posts_attempted=attempted)
        # Evidence survives every completed response, including malformed JSON
        # and HTTP errors, after exact-secret and private-field redaction.
        result['response_evidence']=envelope.evidence(raw,secret)
        if response.status!=200:
            result['error']='HTTP_ERROR';return envelope.redact(result,secret)
        try:
            e=decode(raw);source=e.get('usage',{}) if isinstance(e,dict) else {}
            result['usage']={k:source[k] for k in ['prompt_tokens','completion_tokens','total_tokens','prompt_cache_hit_tokens','prompt_cache_miss_tokens'] if isinstance(source,dict) and type(source.get(k)) is int}
            # Preserve useful identity even when another field fails validation.
            if isinstance(e,dict):
                for src,dst in [('id','completion_id'),('model','model')]:
                    if isinstance(e.get(src),str):result[dst]=e[src][:256]
            result.update(envelope.parse(raw))
        except envelope.Invalid as exc:
            result.update(protocol_error='INVALID_COMPLETE_ENVELOPE',diagnostics=[exc.diagnostic])
        except (ValueError,KeyError,TypeError,RecursionError) as exc:
            result.update(protocol_error='INVALID_COMPLETE_ENVELOPE',diagnostics=[dict(stage='PROVIDER_ENVELOPE',code=str(exc) if isinstance(exc,ValueError) else 'MALFORMED_STRUCTURE',path='$')])
        return envelope.redact(result,secret)
    except (OSError,http.client.HTTPException,ValueError,KeyError,TypeError,RecursionError) as exc:
        result=dict(error='CONNECTION_OR_RESPONSE_UNCERTAIN',http_posts_attempted=attempted,elapsed_seconds=round(time.monotonic()-before,6),
                    diagnostics=[dict(stage='TRANSPORT',code='RESPONSE_BYTE_BOUND' if isinstance(exc,ValueError) and str(exc)=='RESPONSE_BYTE_BOUND' else type(exc).__name__,path='$')])
        if isinstance(exc,http.client.IncompleteRead):raw=exc.partial[:64001]
        if 'raw' in locals():
            result.update(response_sha256=digest(raw),response_bytes=len(raw),response_evidence=envelope.evidence(raw,secret))
        return envelope.redact(result,secret)
    finally:conn.close()
