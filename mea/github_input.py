"""Read-only GitHub pull request capture into a mea-snapshot-v1 input.

Given owner/repo#number (optionally @head-sha), the reader pins the head SHA, takes
the merge base from the compare API, and collects each changed file's full content at
both sides plus a bounded set of unchanged context files. Only GET requests are made.

Three gap kinds stay distinct in the capture report:
  MISSING    the evidence does not exist in a usable form (404, binary, not UTF-8,
             unsupported path, blob or patch inconsistent with the pinned source)
  TRUNCATED  the evidence exists but a provider or MEA bound cut it (patch omitted
             by GitHub, file list or source over the size bound, description cut)
  TRANSPORT  the request did not produce a definitive answer (network error,
             timeout, 401/403/429/5xx, oversized or malformed response)
Hashes bind content to the pinned revisions; they do not authenticate GitHub.
"""
import argparse
import base64
import http.client
import json
import os
import re
import ssl
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

from .contract import MAX_FILES, canonical, digest, git_blob_sha, path as safe_path, validate
from .patch import parse as parse_patch
from .investigation.tools import LIMITS as TOOL_LIMITS, View, import_edges, is_test_path, language

SCHEMA = 'mea-github-capture-v1'
HOST = 'api.github.com'
JSON = 'application/vnd.github+json'
REF = re.compile(r'([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([1-9][0-9]{0,8})(?:@([0-9a-f]{40}))?')
MISSING, TRUNCATED, TRANSPORT = 'MISSING', 'TRUNCATED', 'TRANSPORT'
SOURCE_BYTES = TOOL_LIMITS['source_bytes']
TOTAL_BYTES = TOOL_LIMITS['total_bytes']
SOURCE_LINES = TOOL_LIMITS['lines']
PATCH_BYTES = 60000
PATCH_LINES = 4000
BODY_BYTES = 16000
RESPONSE_BYTES = 8_000_000
PROVIDER_FILE_LIST = 300
COMMIT_LIST_LIMIT = 250  # GitHub's ceiling for GET /pulls/{n}/commits
COMMIT_PAGES = 3
CONTEXT_FILES = 8
SEARCHES = 3
FORBIDDEN = '\v\f\x1c\x1d\x1e\x85\u2028\u2029'
DEFINITION = re.compile(r'^[+-]?\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:def|class|function)\s+([A-Za-z_][A-Za-z0-9_]{2,79})'
                        r'|^[+-]?\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]{2,79})\s*=')


class TransportError(Exception):
    def __init__(self, code, status=None):
        super().__init__(code)
        self.code = code
        self.status = status


class Response:
    def __init__(self, status, body, headers=None):
        self.status = status
        self.body = body
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}

    def json(self):
        return json.loads(self.body.decode('utf-8'))


def token():
    """GitHub token from GH_TOKEN/GITHUB_TOKEN or `gh auth token`. Never logged."""
    value = os.environ.get('GH_TOKEN') or os.environ.get('GITHUB_TOKEN')
    if value:
        return value.strip()
    try:
        result = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None if result.returncode == 0 else None


class RestFetcher:
    """Single-attempt HTTPS GET against api.github.com. No retries."""

    def __init__(self, secret=None, *, connect=None, timeout=30):
        self._secret = secret
        self.timeout = timeout
        self.connect = connect or (lambda: http.client.HTTPSConnection(HOST, 443, timeout=timeout, context=ssl.create_default_context()))

    def get(self, path, accept=JSON):
        headers = {'Accept': accept, 'User-Agent': 'mea-review-readonly', 'X-GitHub-Api-Version': '2022-11-28'}
        if self._secret:
            headers['Authorization'] = 'Bearer ' + self._secret
        conn = self.connect()
        try:
            conn.request('GET', path, headers=headers)
            response = conn.getresponse()
            raw = response.read(RESPONSE_BYTES + 1)
            if len(raw) > RESPONSE_BYTES:
                raise TransportError('RESPONSE_BYTE_BOUND', response.status)
            return Response(response.status, raw, dict(response.getheaders()))
        except TransportError:
            raise
        except (OSError, http.client.HTTPException) as exc:
            raise TransportError(type(exc).__name__) from None
        finally:
            conn.close()


def parse_ref(value):
    match = REF.fullmatch(value or '')
    if not match or {match[1], match[2]} & {'.', '..'}:
        raise ValueError('PR_REFERENCE')
    return match[1], match[2], int(match[3]), match[4]


class Capture:
    def __init__(self, fetcher, owner, repo):
        self.fetcher = fetcher
        self.base = f'/repos/{owner}/{repo}'
        self.requests = []
        self.gaps = []

    def gap(self, kind, code, path=None, side=None, **detail):
        assert kind in (MISSING, TRUNCATED, TRANSPORT)
        row = dict(kind=kind, code=code, path=path, side=side, **detail)
        self.gaps.append(row)
        return row

    def get(self, path, *, accept=JSON, what, file=None, side=None):
        """Response for 200/404/422, None after a TRANSPORT gap is recorded."""
        log = dict(method='GET', path=path, accept=accept)
        try:
            response = self.fetcher.get(path, accept)
        except TransportError as exc:
            log.update(outcome='TRANSPORT', code=exc.code, http_status=exc.status)
            self.requests.append(log)
            self.gap(TRANSPORT, exc.code, file, side, request=what, http_status=exc.status)
            return None
        log.update(http_status=response.status, response_sha256=digest(response.body), response_bytes=len(response.body))
        self.requests.append(log)
        if response.status in (200, 404, 422):
            return response
        code = {401: 'HTTP_401_UNAUTHORIZED', 403: 'HTTP_403_FORBIDDEN_OR_RATE_LIMITED', 429: 'HTTP_429_RATE_LIMITED'}.get(
            response.status, f'HTTP_{response.status}')
        self.gap(TRANSPORT, code, file, side, request=what, http_status=response.status)
        return None

    def json(self, path, *, what, file=None, side=None):
        """(response, data). A 200 whose JSON or structure is unusable is a TRANSPORT gap: (None, None)."""
        response = self.get(path, what=what, file=file, side=side)
        if response is None or response.status != 200:
            return response, None
        try:
            data = response.json()
        except (ValueError, UnicodeError, RecursionError):
            self.gap(TRANSPORT, 'MALFORMED_JSON_RESPONSE', file, side, request=what)
            return None, None
        if not valid_unicode(data):
            # JSON escapes such as "\ud800" decode to lone surrogates that cannot be encoded later.
            self.gap(TRANSPORT, 'INVALID_UNICODE_IN_RESPONSE', file, side, request=what)
            return None, None
        check = SHAPES.get(what)
        if check is not None and not check(data):
            self.gap(TRANSPORT, 'MALFORMED_RESPONSE_STRUCTURE', file, side, request=what)
            return None, None
        return response, data


def valid_unicode(data):
    """True when every string in a decoded JSON value encodes as UTF-8 (no lone surrogates)."""
    try:
        json.dumps(data, ensure_ascii=False).encode('utf-8')
    except (UnicodeError, RecursionError):
        return False
    return True


def _sha(value):
    return isinstance(value, str) and bool(re.fullmatch('[0-9a-f]{40}', value))


def _optional(value, kind):
    return value is None or isinstance(value, kind)


def _has(d, key, kind):
    """The field must be present with this type; a missing field is never read as a default."""
    return key in d and isinstance(d[key], kind) and not (kind is int and type(d[key]) is bool)


# Every field the reader uses must be present with its documented type. Only fields that
# GitHub documents as nullable or as present for some records (body, merged_at, patch,
# previous_filename on non-renames) may be null or absent. A required list that is absent is
# MALFORMED_RESPONSE_STRUCTURE (TRANSPORT); an explicitly empty list means no results.
def _pull(d):
    return (isinstance(d, dict) and _has(d, 'head', dict) and _sha(d['head'].get('sha'))
            and _has(d, 'base', dict) and _sha(d['base'].get('sha'))
            and d.get('state') in ('open', 'closed') and _has(d, 'title', str) and 'body' in d and _optional(d['body'], str)
            and _has(d, 'merged', bool) and 'merged_at' in d and _optional(d['merged_at'], str))


def _commits(d):
    return isinstance(d, list) and all(isinstance(x, dict) and _sha(x.get('sha')) for x in d)


def _compare_file(f):
    if not (isinstance(f, dict) and _has(f, 'filename', str) and _has(f, 'status', str) and _has(f, 'changes', int)
            and _optional(f.get('patch'), str) and _optional(f.get('previous_filename'), str)):
        return False
    if f['status'] in ('renamed', 'copied') and not _has(f, 'previous_filename', str):
        return False
    if f['status'] == 'removed':
        return f.get('sha') is None or _sha(f['sha'])
    # GitHub documents diff-entry sha as required but nullable. The key must be present; a null
    # value means compare supplied no blob id, so contents are read at the pinned head and checked
    # against their own Git blob. An empty or malformed string is never accepted as a blob id.
    return 'sha' in f and (f['sha'] is None or _sha(f['sha']))


def _compare(d):
    return (isinstance(d, dict) and _has(d, 'merge_base_commit', dict) and _sha(d['merge_base_commit'].get('sha'))
            and _has(d, 'files', list) and all(_compare_file(f) for f in d['files']))


def _tree_entry(e):
    # GitHub's git-tree schema requires path, mode, type and sha; size is optional. A missing
    # size is "size unknown" and the byte bound is enforced when the contents are read.
    if not (isinstance(e, dict) and _has(e, 'path', str) and _has(e, 'type', str)):
        return False
    return e['type'] != 'blob' or (_sha(e.get('sha')) and ('size' not in e or _has(e, 'size', int)))


def _tree(d):
    return isinstance(d, dict) and _has(d, 'tree', list) and _has(d, 'truncated', bool) and all(_tree_entry(e) for e in d['tree'])


def _search(d):
    return (isinstance(d, dict) and _has(d, 'items', list) and _has(d, 'incomplete_results', bool)
            and all(isinstance(i, dict) and _has(i, 'path', str) for i in d['items']))


def _contents(d):
    # A directory answers with a list; symlinks and submodules carry another type. Both are
    # well formed and become MISSING (not a regular file) later.
    if isinstance(d, list):
        return True
    if not (isinstance(d, dict) and _has(d, 'type', str)):
        return False
    return d['type'] != 'file' or (_has(d, 'size', int) and _has(d, 'sha', str) and _has(d, 'encoding', str) and _has(d, 'content', str))


SHAPES = dict(pull=_pull, pull_recheck=_pull, pull_commits=_commits, compare=_compare, tree=_tree, search=_search,
              contents=_contents)


def decode_source(capture, data, path, side, expected_blob=None):
    """Full UTF-8 text from a contents response, or None after a MISSING/TRUNCATED gap."""
    if not isinstance(data, dict) or data['type'] != 'file':
        capture.gap(MISSING, 'NOT_A_REGULAR_FILE', path, side)
        return None
    size = data['size']  # Shape-checked: size, sha, encoding and content are present.
    if size > SOURCE_BYTES:
        capture.gap(TRUNCATED, 'SOURCE_BYTE_BOUND', path, side, bytes=size, limit=SOURCE_BYTES)
        return None
    if data['encoding'] != 'base64':
        capture.gap(TRUNCATED, 'PROVIDER_CONTENT_OMITTED', path, side)
        return None
    try:
        raw = base64.b64decode(data['content'], validate=False)
    except ValueError:
        capture.gap(MISSING, 'CONTENT_ENCODING_INVALID', path, side)
        return None
    blob = git_blob_sha(raw)
    if blob != data['sha'] or (expected_blob is not None and blob != expected_blob):
        capture.gap(MISSING, 'BLOB_SHA_MISMATCH', path, side)
        return None
    if b'\0' in raw:
        capture.gap(MISSING, 'BINARY_CONTENT', path, side)
        return None
    try:
        text = raw.decode('utf-8')
    except UnicodeError:
        capture.gap(MISSING, 'NOT_UTF8_TEXT', path, side)
        return None
    if any(c in text for c in FORBIDDEN):
        capture.gap(MISSING, 'UNSUPPORTED_LINE_SEPARATOR', path, side)
        return None
    if len(raw) > SOURCE_BYTES or len(text.splitlines()) > SOURCE_LINES:
        capture.gap(TRUNCATED, 'SOURCE_BOUND', path, side, bytes=len(raw), lines=len(text.splitlines()))
        return None
    return dict(text=text, sha256=digest(raw), blob_sha=blob)


def fetch_source(capture, path, ref, side, expected_blob=None):
    response, data = capture.json(f'{capture.base}/contents/{quote(path, safe="/")}?ref={ref}',
                                  what='contents', file=path, side=side)
    if response is None:
        return None
    if response.status != 200:
        capture.gap(MISSING, f'CONTENT_HTTP_{response.status}', path, side)
        return None
    if data is None:
        return None
    return decode_source(capture, data, path, side, expected_blob)


def accepted(value):
    try:
        return safe_path(value)
    except ValueError:
        return None


COMMON = {'read', 'write', 'close', 'open', 'init', 'main', 'test', 'tests', 'render', 'value', 'values', 'update',
          'handle', 'handler', 'process', 'parse', 'node', 'self', 'start', 'stop', 'reset', 'setup', 'result',
          'request', 'response', 'error', 'string', 'number', 'object', 'default', 'options', 'config', 'index'}


def symbols(patch):
    """Distinctive names defined on changed lines first, then enclosing hunk-header names."""
    changed, enclosing = [], []
    for line in (patch or '').splitlines():
        header = line.startswith('@@')
        if not header and not line.startswith(('+', '-')):
            continue
        text = line.split('@@', 2)[-1].strip() if header else line
        match = DEFINITION.search(text)
        if match:
            (enclosing if header else changed).append(match[1] or match[2])
    names = changed + [n for n in enclosing if len(n) >= 6]
    return list(dict.fromkeys(n for n in names if len(n) >= 5 and not n.startswith('__') and n.lower() not in COMMON))


def test_names(path):
    stem = path.split('/')[-1].rsplit('.', 1)[0]
    if is_test_path(path, True) or len(stem) < 3:
        return set()
    return {stem}


def name_matches(stem, path):
    name = path.split('/')[-1]
    return (name.startswith(('test_' + stem + '.', 'test_' + stem + '_')) or name.startswith(stem + '_test.')
            or name.startswith((stem + '.test.', stem + '.spec.')))


def changed_words(patch):
    rows = [l[1:] for l in (patch or '').splitlines() if l[:1] in '+-' and not l.startswith(('+++', '---'))]
    return set(re.findall(r'[A-Za-z_][A-Za-z0-9_]{2,79}', '\n'.join(rows)))


def select_context(capture, files, tree, budget, head, owner, repo, limit, pause=0):
    """Ordered (path, reason) context candidates.

    Priority: tests named after a changed module, files found by code search for a
    changed definition (likely callers), imports whose names occur in changed lines,
    then other imports. Search runs on the default branch; every hit is re-resolved
    in the pinned head tree and fetched at the head SHA.
    """
    known = {e['path']: e for e in tree}  # Blob entries only, each with a checked sha; size may be unknown.
    changed = {f['path'] for f in files} | {f['base_path'] for f in files}
    buckets = dict(tests=[], callers=[], used_imports=[], imports=[])
    seen = set()

    def add(bucket, path, reason):
        entry = known.get(path)
        if (path in changed or path in seen or entry is None or language(path) is None
                or accepted(path) is None or entry.get('size', 0) > SOURCE_BYTES):
            return
        seen.add(path)
        buckets[bucket].append((path, reason))

    for f in files:
        for stem in test_names(f['path']):
            for path in sorted(known):
                if name_matches(stem, path):
                    add('tests', path, f'TEST_NAME_MATCH:{f["path"]}')
    names = list(dict.fromkeys(n for f in files for n in symbols(f['patch'])))
    for i, name in enumerate(names[:SEARCHES]):
        if i and pause:
            time.sleep(pause)  # GitHub code search allows about 10 requests per minute.
        response, data = capture.json(f'/search/code?q={quote(name)}+repo:{owner}/{repo}&per_page=20',
                                      what='search', file=None, side='head')
        if data is None:
            # TRANSPORT and malformed answers were recorded by capture.json(); a definitive
            # 404/422 is recorded here, so context selection never reads it as "no match".
            if response is not None:
                capture.gap(MISSING, f'SEARCH_HTTP_{response.status}', None, 'head', request='search')
            continue
        if data['incomplete_results']:
            capture.gap(TRUNCATED, 'PROVIDER_SEARCH_INCOMPLETE', None, 'head', request='search')
        for item in data['items'][:20]:
            add('callers', item['path'], f'CODE_SEARCH_DEFAULT_BRANCH:{name}')
    for f in files:
        head_side = f['sides'].get('head')
        if not head_side:
            continue
        words = changed_words(f['patch'])
        rows = head_side['text'].splitlines()
        edges, _ = import_edges(f['path'], head_side['text'], known)
        for line, target, spec in edges or []:
            used = set(re.findall(r'[A-Za-z_][A-Za-z0-9_]{2,79}', rows[line - 1])) - {'import', 'from', 'require'}
            add('used_imports' if used & words else 'imports', target, f'IMPORTED_BY_CHANGED:{f["path"]}:{line}')
    quota = dict(tests=2, callers=3, used_imports=limit, imports=limit)
    picks = []
    for name in ('tests', 'callers', 'used_imports'):
        picks.extend(buckets[name][:quota[name]])
    leftovers = [x for name in buckets for x in buckets[name] if x not in picks]
    return (picks + leftovers)[:limit * 2]


def capture(reference, fetcher, *, context_limit=CONTEXT_FILES, now=None, search_pause=0):
    """Return (report, snapshot). snapshot is None when status is FAILED.

    Responses are shape-checked before use. If a response still breaks an assumption,
    the capture fails as TRANSPORT with every request made so far kept in the report.
    """
    owner, repo, number, pin = parse_ref(reference)
    c = Capture(fetcher, owner, repo)
    try:
        return _capture(c, reference, owner, repo, number, pin, context_limit, now, search_pause)
    except (KeyError, TypeError, AttributeError, IndexError, UnicodeError) as exc:
        gap = c.gap(TRANSPORT, 'UNEXPECTED_RESPONSE_STRUCTURE', request=type(exc).__name__)
        return dict(schema=SCHEMA, reference=reference, repository=f'{owner}/{repo}', number=number, read_only=True,
                    status='FAILED', failure=gap, gaps=c.gaps, requests=c.requests), None


def _capture(c, reference, owner, repo, number, pin, context_limit, now, search_pause):
    report = dict(schema=SCHEMA, reference=reference, repository=f'{owner}/{repo}', number=number,
                  read_only=True, status=None, failure=None, gaps=c.gaps, requests=c.requests)

    def failed(gap):
        report.update(status='FAILED', failure=gap)
        return report, None

    response, pr = c.json(f'{c.base}/pulls/{number}', what='pull')
    if response is None:
        return failed(c.gaps[-1])
    if pr is None:
        return failed(c.gap(MISSING, f'PULL_HTTP_{response.status}'))
    head = pin or pr['head']['sha']
    if pin:
        found = None
        listed_commits = 0
        for page in range(1, COMMIT_PAGES + 1):
            suffix = '' if page == 1 else f'&page={page}'
            response, commits = c.json(f'{c.base}/pulls/{number}/commits?per_page=100{suffix}', what='pull_commits')
            if response is None:
                return failed(c.gaps[-1])
            if commits is None:
                return failed(c.gap(MISSING, f'PULL_COMMITS_HTTP_{response.status}'))
            listed_commits += len(commits)
            if pin in [x['sha'] for x in commits]:
                found = page
                break
            if len(commits) < 100:
                break
        if found is None:
            if listed_commits >= COMMIT_LIST_LIMIT:
                # GitHub lists at most 250 commits for a pull; a pin beyond that cannot be verified here.
                return failed(c.gap(TRUNCATED, 'PULL_COMMITS_LIST_LIMIT', listed=listed_commits, limit=COMMIT_LIST_LIMIT))
            return failed(c.gap(MISSING, 'PINNED_HEAD_NOT_IN_PULL_COMMITS'))
    target_base = pr['base']['sha']
    response, compare = c.json(f'{c.base}/compare/{target_base}...{head}', what='compare')
    if response is None:
        return failed(c.gaps[-1])
    if compare is None:
        return failed(c.gap(MISSING, f'COMPARE_HTTP_{response.status}'))
    merge_base = compare['merge_base_commit']['sha']
    if merge_base == head:
        return failed(c.gap(MISSING, 'HEAD_EQUALS_MERGE_BASE'))
    listed = compare['files']  # Shape-checked list; [] is an explicit empty change listing.
    reported = len(listed)
    complete = True
    if reported >= PROVIDER_FILE_LIST:
        c.gap(TRUNCATED, 'PROVIDER_FILE_LIST_LIMIT', listed=reported)
        complete = False
    files = []
    null_sha = []
    used = 0
    for entry in listed:
        # previous_filename exists only for renames and copies (shape-checked for those).
        name, before = entry['filename'], entry.get('previous_filename') or entry['filename']
        if accepted(name) is None or accepted(before) is None:
            c.gap(MISSING, 'PATH_NOT_ACCEPTED', None, None, github_path=name[:256])
            complete = False
            continue
        if len(files) >= MAX_FILES:
            c.gap(TRUNCATED, 'FILE_COUNT_BOUND', name, None, limit=MAX_FILES)
            complete = False
            continue
        status = entry['status'] if entry['status'] in {'modified', 'added', 'removed', 'renamed', 'copied', 'changed'} else 'changed'
        sides = {}
        if status != 'removed':
            if entry['sha'] is None:
                null_sha.append(name)  # Read at the pinned head; checked against its own Git blob.
            source = fetch_source(c, name, head, 'head', entry['sha'])
            if source:
                sides['head'] = source
        if status != 'added':
            source = fetch_source(c, before, merge_base, 'base')
            if source:
                sides['base'] = source
        size = sum(len(s['text'].encode()) for s in sides.values())
        if used + size > TOTAL_BYTES:
            c.gap(TRUNCATED, 'TOTAL_SOURCE_BOUND', name, None, limit=TOTAL_BYTES)
            sides = {}
            size = 0
        used += size
        patch = entry.get('patch')
        if patch is None:
            if entry['changes'] != 0 and all(g['path'] not in (name, before) or g['kind'] == TRANSPORT for g in c.gaps):
                c.gap(TRUNCATED, 'PROVIDER_PATCH_OMITTED', name, None)
        elif len(patch.encode()) > PATCH_BYTES or len(patch.splitlines()) > PATCH_LINES:
            c.gap(TRUNCATED, 'PATCH_BOUND', name, None, bytes=len(patch.encode()))
            patch = None
        else:
            try:
                parse_patch(patch, sides)
            except ValueError as exc:
                c.gap(MISSING, 'PATCH_INCONSISTENT:' + str(exc), name, None)
                patch = None
        files.append(dict(path=name, base_path=before, status=status, patch=patch, sides=sides))
    if not files:
        return failed(c.gap(MISSING, 'NO_COLLECTABLE_CHANGED_FILE'))
    context = []
    selection_method = 'imports, test names, GitHub code search (default branch) resolved at the pinned head'
    if not complete:
        report['context_selection'] = dict(status='SKIPPED_INCOMPLETE_CHANGE_LISTING', files=[])
    elif context_limit <= 0 or len(files) >= MAX_FILES:
        report['context_selection'] = dict(status='NOT_REQUESTED', files=[])
    else:
        before_context = len(c.gaps)
        response, tree = c.json(f'{c.base}/git/trees/{head}?recursive=1', what='tree')
        if tree is None:
            if response is not None:  # A definitive 404/422: the pinned tree is not available.
                c.gap(MISSING, f'TREE_HTTP_{response.status}', None, 'head', request='tree')
            entries = None
        else:
            entries = [e for e in tree['tree'] if e['type'] == 'blob']  # [] is an explicitly empty tree.
            if tree['truncated']:
                c.gap(TRUNCATED, 'PROVIDER_TREE_TRUNCATED', None, 'head', request='tree')
        picks = select_context(c, files, entries, TOTAL_BYTES - used, head, owner, repo, context_limit, search_pause) if entries else []
        blobs = {e['path']: e['sha'] for e in entries or []}
        sizes = {e['path']: e.get('size') for e in entries or []}  # None: the tree omitted the optional size.
        for path, reason in picks:
            if len(context) >= context_limit or len(files) + len(context) >= MAX_FILES:
                break
            source = fetch_source(c, path, head, 'head', blobs.get(path))
            if not source:
                continue
            size = len(source['text'].encode()) * 2
            if used + size > TOTAL_BYTES:
                continue
            used += size
            # Unchanged between merge base and head because compare listed every changed path.
            context.append((dict(path=path, base_path=path, status='context', patch=None,
                                 sides=dict(base=dict(source), head=dict(source))), reason))
        # FAILED: no tree, so no context could be selected. PARTIAL: tree, search or a
        # context read had a gap. COLLECTED: every context request answered.
        status = 'FAILED' if entries is None else 'PARTIAL' if len(c.gaps) > before_context else 'COLLECTED'
        report['context_selection'] = dict(status=status, method=selection_method,
                                           files=[dict(path=f['path'], reason=r, tree_size=sizes.get(f['path'])) for f, r in context],
                                           candidates_considered=len(picks),
                                           gaps=[dict(g) for g in c.gaps[before_context:]])
    files.extend(f for f, _ in context)
    body = pr['body'] or ''  # GitHub returns null for an empty description.
    if '\0' in body:
        body = body.replace('\0', '')
        c.gap(MISSING, 'PR_BODY_NUL_REMOVED')
    if len(body.encode()) > BODY_BYTES:
        body = body.encode()[:BODY_BYTES].decode('utf-8', 'ignore')
        c.gap(TRUNCATED, 'PR_BODY_BYTE_BOUND', limit=BODY_BYTES)
    title = pr['title'].replace('\0', '')
    if len(title.encode()) > 500:
        title = title.encode()[:500].decode('utf-8', 'ignore')
        c.gap(TRUNCATED, 'PR_TITLE_BYTE_BOUND')
    # The pinned head is re-read once; a moved open head makes the capture stale. When the head
    # is unchanged, the final response decides provenance (a pull merged during capture is no
    # longer pending review); the collected revisions stay as captured.
    final = pr
    if not pin and pr['state'] == 'open':
        response, again = c.json(f'{c.base}/pulls/{number}', what='pull_recheck')
        if again is None:
            return failed(c.gaps[-1] if response is None else c.gap(MISSING, 'PULL_RECHECK_UNAVAILABLE'))
        if again['head']['sha'] != head:
            return failed(c.gap(MISSING, 'HEAD_MOVED_DURING_CAPTURE'))
        final = again
    pending = final['state'] == 'open' and head == final['head']['sha']
    omitted = []
    for g in c.gaps:
        if g['path'] and accepted(g['path']):
            for side in ([g['side']] if g['side'] in ('base', 'head') else ['base', 'head']):
                row = dict(path=g['path'], side=side, reason=(g['kind'] + ':' + g['code'])[:160])
                if row not in omitted and len(omitted) < 48:
                    omitted.append(row)
    included = len(files) - len(context)
    # reported_changed_files counts every collected file (changed + context) plus changed
    # files left out, so complete_listing is true only when no changed file is missing.
    scope_count = len(files) if complete else max(reported, included + 1) + len(context)
    identifier = re.sub('[^a-zA-Z0-9_-]', '-', f'gh-{owner}-{repo}-{number}-{head[:12]}')[:80]
    snapshot = dict(
        schema='mea-snapshot-v1', id=identifier,
        pr=dict(url=f'https://github.com/{owner}/{repo}/pull/{number}', repository=f'{owner}/{repo}', number=number, title=title, body=body),
        revisions=dict(base=merge_base, head=head, target_base=target_base),
        provenance=dict(kind='FROZEN_PENDING_REVIEW' if pending else 'KNOWN_HISTORICAL_DEVELOPMENT',
                        captured_at=time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now)),
                        state_at_capture=final['state'], merged_at_capture=final['merged'],
                        pending_review_version=pending, capture_sha256=digest(c.requests),
                        source_authentication='NOT_INDEPENDENTLY_VERIFIED'),
        scope=dict(reported_changed_files=scope_count, complete_listing=complete, omitted_sources=omitted,
                   capture=capture_scope(c.gaps, report['context_selection']['status'])),
        files=files)
    try:
        validate(snapshot)
        View.from_input(snapshot)
    except ValueError as exc:
        return failed(c.gap(MISSING, 'SNAPSHOT_VALIDATION:' + str(exc)))
    report.update(status='COMPLETE' if not c.gaps else 'INCOMPLETE', head_sha=head, merge_base=merge_base,
                  target_base=target_base, pinned_head=bool(pin), pr_head_at_capture=pr['head']['sha'],
                  pr_state=final['state'], merged=final['merged'],
                  counts=dict(changed_reported=reported, changed_included=included, context_included=len(context)),
                  compare_sha_null=null_sha,
                  snapshot_id=identifier, input_sha256=digest(snapshot),
                  gap_counts={k: sum(g['kind'] == k for g in c.gaps) for k in (MISSING, TRUNCATED, TRANSPORT)})
    return report, snapshot


def capture_scope(gaps, selection):
    """Typed capture status carried by the snapshot, including gaps without a path (tree, search)."""
    rows = [dict(kind=g['kind'], code=g['code'][:160], request=g.get('request'), path=g['path'] if g['path'] and accepted(g['path']) else None,
                 side=g['side'] if g['side'] in ('base', 'head') else None) for g in gaps]
    return dict(status='INCOMPLETE' if gaps else 'COMPLETE', context_selection=selection,
                gap_counts={k: sum(g['kind'] == k for g in gaps) for k in (MISSING, TRUNCATED, TRANSPORT)},
                gaps=rows[:48])


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only GitHub PR capture into mea-snapshot-v1')
    parser.add_argument('reference', help='owner/repo#number or owner/repo#number@head-sha')
    parser.add_argument('--out-dir', type=Path, required=True)
    parser.add_argument('--context-files', type=int, default=CONTEXT_FILES)
    args = parser.parse_args(argv)
    report, snapshot = capture(args.reference, RestFetcher(token()), context_limit=args.context_files, search_pause=7)
    code = 0 if snapshot is not None else 3
    report['exit_status'] = code
    args.out_dir.mkdir(parents=True, exist_ok=True)
    if snapshot is None:
        # A failed recapture must not leave an earlier snapshot at the documented path.
        (args.out_dir / 'snapshot.json').unlink(missing_ok=True)
    (args.out_dir / 'capture.json').write_bytes(canonical(report))
    if snapshot is not None:
        (args.out_dir / 'snapshot.json').write_bytes(canonical(snapshot))
    print(json.dumps(dict(status=report['status'], failure=report['failure'], gaps=report.get('gap_counts'),
                          counts=report.get('counts'), input_sha256=report.get('input_sha256'), requests=len(report['requests'])),
                     sort_keys=True))
    return code


if __name__ == '__main__':
    sys.exit(main())
