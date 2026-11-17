"""Live GitHub pull request line-comment remote for an explicit repository allowlist.

Reads (repository permission, authenticated login, pull head/state, changed-file
patches, review-comment listing) are GET requests. The only write is one POST to
/pulls/{n}/comments per send intent, built by github_contract. There is no retry.

Outcomes are typed. NotSent means no POST left this process, or GitHub gave a
definitive rejection (400/401/403/404/422), so no comment exists. Uncertain means a
POST may have had an effect (timeout, connection error, 5xx, 429, or an unusable 2xx
receipt); the caller must reconcile read-only before any further send.

The token is passed in by the caller (read at runtime from `gh auth token`) and only
appears in the Authorization header. Receipts, errors and reprs never include it.
"""
import http.client
import json
import re
import ssl

from .contract import canonical, digest
from .patch import parse as parse_patch
from .github_input import token
from .human_review import github_contract
from .human_review.remote import LIVE_MODE, NotSent, Uncertain, binding as binding_of

HOST = 'api.github.com'
ACCEPT = 'application/vnd.github+json'
RESPONSE_BYTES = 4_000_000
COMMENT_PAGES = 10
FILE_PAGES = 3
DEFINITIVE_REJECTIONS = {400, 401, 403, 404, 422}


class TransportError(Exception):
    """No HTTP response was read. The message is a fixed code without request data."""
    def __init__(self, code):
        super().__init__(code)
        self.code = code


class Response:
    def __init__(self, status, body, headers):
        self.status = status
        self.body = body
        self.headers = {k.lower(): v for k, v in headers.items()}

    def json(self):
        return json.loads(self.body.decode('utf-8'))

    def receipt(self, method, path):
        """Redacted record: status, GitHub request id, and the response body digest."""
        return dict(method=method, path=path, http_status=self.status,
                    github_request_id=self.headers.get('x-github-request-id'), date=self.headers.get('date'),
                    response_sha256=digest(self.body), response_bytes=len(self.body))


class Transport:
    """Single-attempt HTTPS client. connect is injectable for a local fake server in tests."""

    def __init__(self, secret, *, connect=None, timeout=20, user_agent='mea-review-m4'):
        if not isinstance(secret, str) or not secret.strip():
            raise ValueError('GITHUB_TOKEN_REQUIRED')
        self._secret = secret.strip()
        self.user_agent = user_agent
        self.connect = connect or (lambda: http.client.HTTPSConnection(HOST, 443, timeout=timeout, context=ssl.create_default_context()))
        self.log = []

    def __repr__(self):
        return '<Transport redacted>'

    def request(self, method, path, body=None, headers=None):
        if method not in ('GET', 'POST') or not path.startswith('/'):
            raise ValueError('TRANSPORT_REQUEST')
        h = {'Accept': ACCEPT, 'User-Agent': self.user_agent, 'X-GitHub-Api-Version': github_contract.API_VERSION}
        h.update(headers or {})
        h['Authorization'] = 'Bearer ' + self._secret
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode('utf-8')
            h['Content-Type'] = 'application/json'
        conn = self.connect()
        try:
            conn.request(method, path, body=data, headers=h)
            raw = conn.getresponse()
            payload = raw.read(RESPONSE_BYTES + 1)
            if len(payload) > RESPONSE_BYTES:
                raise TransportError('RESPONSE_BYTE_BOUND')
            response = Response(raw.status, payload, dict(raw.getheaders()))
        except TransportError:
            self.log.append(dict(method=method, path=path, outcome='RESPONSE_BYTE_BOUND'))
            raise
        except (OSError, http.client.HTTPException) as exc:
            self.log.append(dict(method=method, path=path, outcome=type(exc).__name__))
            raise TransportError(type(exc).__name__) from None
        finally:
            conn.close()
        self.log.append(response.receipt(method, path))
        return response


def commentable_lines(patch):
    """Lines GitHub accepts for a line comment: every row of the file's PR diff hunks, by side."""
    rows = parse_patch(patch, {})
    return dict(RIGHT=sorted({r['head'] for r in rows if r['head'] is not None}),
                LEFT=sorted({r['base'] for r in rows if r['base'] is not None}))


def normalize(c):
    """Comparable view of a GitHub review comment, keyed by the commit and line it was written on."""
    if (not isinstance(c, dict) or type(c.get('id')) is not int or not isinstance(c.get('html_url'), str)
            or not isinstance(c.get('user'), dict) or not isinstance(c['user'].get('login'), str)
            or not isinstance(c.get('body'), str) or not isinstance(c.get('path'), str)):
        raise ValueError('REMOTE_COMMENT_SHAPE')
    return dict(id=c['id'], html_url=c['html_url'], user=dict(login=c['user']['login']),
                commit_id=c.get('original_commit_id'), path=c['path'], line=c.get('original_line'), side=c.get('side'),
                body=c['body'], in_reply_to_id=c.get('in_reply_to_id'),
                current=dict(commit_id=c.get('commit_id'), line=c.get('line'), position=c.get('position'),
                             outdated=c.get('line') is None and c.get('position') is None))


class LiveGitHub:
    mode = LIVE_MODE
    published = 'PUBLISHED'
    reconciled = 'PUBLISHED_RECONCILED'
    confirmed = 'CONFIRMED_LIVE'
    send_mode = 'LIVE'

    def __init__(self, transport, allow):
        allow = set(allow)
        if not allow or any(not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', r) for r in allow):
            raise ValueError('LIVE_ALLOWLIST_REQUIRED')
        self.transport = transport
        self.allow = frozenset(allow)
        self.last_receipt = None

    def admits(self, target):
        return isinstance(target, dict) and target.get('repository') in self.allow and type(target.get('number')) is int

    def comment_url(self, target, identifier):
        return f'https://github.com/{target["repository"]}/pull/{target["number"]}#discussion_r{identifier}'

    @staticmethod
    def identity_of(remote):
        # The comment listing is not part of the confirmation: other confirmed comments on the
        # same PR may be sent in between. Duplicates are checked against a fresh listing instead.
        return {k: remote[k] for k in ['identity', 'head', 'base', 'state', 'can_comment', 'can_read', 'principal', 'generation', 'complete_listing']}

    def _json(self, path):
        response = self.transport.request('GET', path)
        if response.status != 200:
            raise ValueError(f'REMOTE_READ_HTTP_{response.status}')
        try:
            return response.json()
        except (ValueError, UnicodeError, RecursionError):
            raise ValueError('REMOTE_READ_MALFORMED') from None

    def read(self, target):
        """Read-only remote state. Raises ValueError or TransportError; never writes."""
        if not self.admits(target):
            raise ValueError('LIVE_TARGET_NOT_ALLOWLISTED')
        base = f'/repos/{target["repository"]}'
        repo = self._json(base)
        user = self._json('/user')
        pr = self._json(f'{base}/pulls/{target["number"]}')
        try:
            if repo['full_name'].lower() != target['repository'].lower() or pr['number'] != target['number']:
                raise ValueError('REMOTE_IDENTITY_MISMATCH')
            perms = repo.get('permissions') or {}
            principal = user['login']
            head, base_sha, state = pr['head']['sha'], pr['base']['sha'], pr['state']
            if not all(isinstance(x, str) and re.fullmatch('[0-9a-f]{40}', x) for x in (head, base_sha)):
                raise ValueError('REMOTE_SHA')
        except (KeyError, TypeError, AttributeError):
            raise ValueError('REMOTE_READ_MALFORMED') from None
        commentable, files_complete = {}, False
        for page in range(1, FILE_PAGES + 1):
            rows = self._json(f'{base}/pulls/{target["number"]}/files?per_page=100&page={page}')
            if not isinstance(rows, list):
                raise ValueError('REMOTE_READ_MALFORMED')
            for f in rows:
                patch = f.get('patch') if isinstance(f, dict) else None
                if isinstance(patch, str) and patch:
                    try:
                        commentable[f['filename']] = commentable_lines(patch)
                    except ValueError:
                        pass
            if len(rows) < 100:
                files_complete = True
                break
        comments, complete = [], False
        for page in range(1, COMMENT_PAGES + 1):
            rows = self._json(f'{base}/pulls/{target["number"]}/comments?per_page=100&page={page}')
            if not isinstance(rows, list):
                raise ValueError('REMOTE_READ_MALFORMED')
            comments.extend(normalize(c) for c in rows)
            if len(rows) < 100:
                complete = True
                break
        can_read = perms.get('pull') is True
        value = dict(identity=dict(repository=target['repository'], number=target['number']), head=head, base=base_sha, state=state,
                     can_comment=can_read and state == 'open', can_read=can_read, principal=principal,
                     private=repo.get('private') is True, complete_listing=complete, comments=comments,
                     commentable=commentable, commentable_complete=files_complete)
        value['generation'] = digest({k: value[k] for k in ['identity', 'head', 'base', 'state', 'can_comment', 'principal']})
        return value

    def post(self, target, payload, expected_generation):
        """One POST after a fresh read-only admission. NotSent before the POST; typed outcome after."""
        self.last_receipt = None
        wire = github_contract.comment_request(target, payload)
        try:
            remote = self.read(target)
        except (ValueError, TransportError) as exc:
            raise NotSent('PRE_SEND_READ_FAILED:' + str(exc)) from None
        if remote['generation'] != expected_generation or remote['head'] != payload['commit_id'] or not remote['can_comment']:
            raise NotSent('REMOTE_CHANGED_AFTER_ADMISSION')
        if not remote['complete_listing']:
            raise NotSent('REMOTE_DUPLICATES_UNKNOWN')
        if any(c['commit_id'] == payload['commit_id'] and c['path'] == payload['path'] and c['line'] == payload['line'] and c['side'] == payload['side'] for c in remote['comments']):
            raise NotSent('DUPLICATE_REMOTE_ANCHOR')
        if payload['line'] not in remote['commentable'].get(payload['path'], {}).get(payload['side'], []):
            raise NotSent('REMOTE_DIFF_ANCHOR_INVALID')
        try:
            response = self.transport.request('POST', wire['path'], wire['json'], wire['headers'])
        except TransportError as exc:
            raise Uncertain('TRANSPORT:' + exc.code) from None
        receipt = response.receipt('POST', wire['path'])
        try:
            data = response.json()
        except (ValueError, UnicodeError, RecursionError):
            data = None
        receipt['response'] = data if isinstance(data, (dict, list)) else None
        self.last_receipt = receipt
        if response.status == 201:
            try:
                return normalize(data)
            except ValueError:
                raise Uncertain('RECEIPT_UNUSABLE', receipt) from None
        if response.status in DEFINITIVE_REJECTIONS:
            raise NotSent(f'REMOTE_REJECTED_HTTP_{response.status}', receipt)
        raise Uncertain(f'HTTP_{response.status}', receipt)


def _write(path, value):
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(canonical(value))


def _summary(review):
    drafts = [dict(id=d['id'], task_id=d['task_id'], candidate_id=d['candidate_id'], status=d['status'], revision=d['revision'],
                   anchor=d['anchor'], approval=d.get('approval')) for d in review['drafts']]
    sends = [dict(id=s['id'], draft_id=s['draft_id'], status=s['status'], mode=s['mode'], payload=s['payload'],
                  comment=s.get('remote_comment'), receipts=s.get('receipts'), reconciled_absent=s.get('reconciled_absent'))
             for s in review['sends']]
    return dict(review_id=review['id'], mode=review['mode'], pr=review['pr'], versions=review['versions'], drafts=drafts,
                sends=sends, rereviews=review.get('rereviews', []))


def import_task(store, source, identifier):
    """Copy one terminal task row from a provider store into the review store, then re-validate it."""
    from .human_review.service import task_checked
    with source.connection() as db:
        row = db.execute('SELECT id, identity, doc FROM tasks WHERE id=?', (identifier,)).fetchone()
    if not row:
        raise ValueError('TASK_NOT_FOUND')
    with store.transaction() as db:
        existing = db.execute('SELECT doc FROM tasks WHERE id=?', (identifier,)).fetchone()
        if existing and existing[0] != row[2]:
            raise ValueError('TASK_IMPORT_CONFLICT')
        if not existing:
            db.execute('INSERT INTO tasks VALUES (?,?,?)', row)
    task, _ = task_checked(store, identifier)
    return task


def main(argv=None):
    import argparse
    import sys
    from pathlib import Path
    from .contract import decode
    from .investigation.tasks import Store
    from .human_review.publisher import Publisher
    from .human_review.service import Service

    parser = argparse.ArgumentParser(description='Attended GitHub line-comment writeback for allowlisted repositories')
    parser.add_argument('--store', type=Path, required=True)
    parser.add_argument('--allow-repository', required=True, action='append')
    parser.add_argument('--out', type=Path)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('import-task'); p.add_argument('--from-store', type=Path, required=True); p.add_argument('--task-id', required=True)
    p = sub.add_parser('open'); p.add_argument('--task-id', required=True)
    p = sub.add_parser('draft'); p.add_argument('--review', required=True); p.add_argument('--task-id', required=True); p.add_argument('--candidate', required=True)
    p = sub.add_parser('preview'); p.add_argument('--draft', required=True)
    p = sub.add_parser('confirm'); p.add_argument('--draft', required=True); p.add_argument('--preview', type=Path, required=True)
    p.add_argument('--approval-ref', required=True); p.add_argument('--binding', required=True); p.add_argument('--human-approved', action='store_true')
    p = sub.add_parser('send'); p.add_argument('--draft', required=True); p.add_argument('--confirm-live-write', action='store_true')
    p = sub.add_parser('reconcile'); p.add_argument('--intent', required=True)
    p = sub.add_parser('release-after-reconcile'); p.add_argument('--intent', required=True); p.add_argument('--human-approved', action='store_true')
    p = sub.add_parser('read'); p.add_argument('--repository', required=True); p.add_argument('--pr', type=int, required=True)
    p = sub.add_parser('rereview'); p.add_argument('--review', required=True); p.add_argument('--task-id', required=True)
    p = sub.add_parser('status'); p.add_argument('--review', required=True)
    args = parser.parse_args(argv)

    if args.command == 'send' and not args.confirm_live_write:
        print('BLOCKED: --confirm-live-write is required for a GitHub write.', flush=True)
        return 2
    if args.command in ('confirm', 'release-after-reconcile') and not args.human_approved:
        print('BLOCKED: --human-approved is required and must reflect a recorded human decision.', flush=True)
        return 2
    store = Store(args.store)
    network = {'open', 'preview', 'confirm', 'send', 'reconcile', 'read', 'rereview'}
    remote = None
    try:
        if args.command in network:
            secret = token()
            if not secret:
                print('BLOCKED: GITHUB_TOKEN_UNAVAILABLE', flush=True)
                return 2
            remote = LiveGitHub(Transport(secret), args.allow_repository)
            del secret
        service = Service(store, remote) if remote else Service(store)
        if args.command == 'import-task':
            t = import_task(store, Store(args.from_store), args.task_id)
            result = dict(task_id=t['id'], status=t['status'], reason=t['reason'], candidates=len(t['candidates']), pr=t['pr']['url'],
                          revisions=t['view']['revisions'])
        elif args.command == 'open':
            r = service.create(args.task_id)
            if r['mode'] != 'LIVE_GITHUB_REVIEW' and r['pr']['repository'] not in args.allow_repository:
                raise ValueError('LIVE_TARGET_NOT_ALLOWLISTED')
            result = _summary(r)
        elif args.command == 'draft':
            d = service.draft(args.review, args.task_id, args.candidate)
            result = dict(draft_id=d['id'], status=d['status'], anchor=d['anchor'], body=d['body'])
        elif args.command == 'preview':
            value = service.preview(args.draft)
            result = dict(preview=value, binding_sha256=binding_of(value))
        elif args.command == 'confirm':
            preview = decode(args.preview.read_bytes())['preview']
            d = service.confirm(args.draft, preview, True, dict(reference=args.approval_ref, binding_sha256=args.binding))
            result = dict(draft_id=d['id'], status=d['status'], approval=d['approval'], confirmation_digest=d['confirmation']['confirmation_digest'])
        elif args.command == 'send':
            with store.connection() as db:
                from .human_review.store import get
                d = get(db, args.draft, 'draft')
            if not d.get('confirmation'):
                raise ValueError('HUMAN_CONFIRMATION_REQUIRED')
            s = Publisher(service).send(args.draft, d['confirmation']['confirmation_digest'], 'LIVE')
            result = dict(intent_id=s['id'], status=s['status'], payload=s['payload'], comment=s.get('remote_comment'),
                          receipts=s.get('receipts'), events=s['events'])
        elif args.command == 'reconcile':
            s = Publisher(service).reconcile(args.intent)
            result = dict(intent_id=s['id'], status=s['status'], comment=s.get('remote_comment'), events=s['events'])
        elif args.command == 'release-after-reconcile':
            d = Publisher(service).release_after_reconcile(args.intent, True)
            result = dict(draft_id=d['id'], status=d['status'])
        elif args.command == 'read':
            result = remote.read(dict(repository=args.repository, number=args.pr))
        elif args.command == 'rereview':
            r = service.open(args.review)
            observed = remote.read(dict(repository=r['pr']['repository'], number=r['pr']['number']))
            result = _summary(service.rereview(args.review, args.task_id, observed))
            result['observed_remote'] = dict(head=observed['head'], comments=observed['comments'])
        else:
            result = _summary(service.open(args.review))
        if remote is not None:
            result = dict(result=result, github_requests=remote.transport.log)
    except (ValueError, TransportError) as exc:
        print('BLOCKED: ' + (exc.code if isinstance(exc, TransportError) else str(exc)), flush=True)
        if remote is not None:
            _write(args.out, dict(error=str(exc), github_requests=remote.transport.log))
        return 2
    _write(args.out, result)
    print(json.dumps(result, ensure_ascii=False, indent=1, sort_keys=True)[:20000], flush=True)
    return 0


if __name__ == '__main__':
    import sys
    sys.exit(main())
