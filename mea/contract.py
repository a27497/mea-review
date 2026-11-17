"""Data-only import boundary. Hashes detect drift; they do not authenticate GitHub."""
import hashlib
import json
import re
from pathlib import PurePosixPath

MAX_BYTES = 2_000_000
MAX_FILES = 24
SHA = re.compile(r'[0-9a-f]{40}')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def git_blob_sha(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def decode(raw, *, limit=MAX_BYTES):
    if not isinstance(raw, bytes) or len(raw) > limit:
        raise ValueError('INPUT_SIZE_LIMIT')
    def pairs(rows):
        result = {}
        for key, value in rows:
            if key in result:
                raise ValueError('DUPLICATE_JSON_KEY')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('NON_FINITE_JSON')
    try:
        return json.loads(raw.decode('utf-8-sig'), object_pairs_hook=pairs, parse_constant=constant)
    except (UnicodeError, RecursionError, json.JSONDecodeError):
        raise ValueError('INVALID_JSON') from None


def fields(value, required):
    if not isinstance(value, dict) or set(value) != set(required.split()):
        raise ValueError('UNSUPPORTED_OR_INVALID_FIELDS')


def text(value, limit):
    if not isinstance(value, str) or '\0' in value or len(value.encode('utf-8')) > limit:
        raise ValueError('TEXT_BOUND')
    return value


def path(value):
    text(value, 256)
    parts = PurePosixPath(value).parts
    if (not value or value.startswith('/') or str(PurePosixPath(value)) != value or
        not re.fullmatch(r'[A-Za-z0-9_.@/-]+', value) or
        any(p in {'.', '..', '.git'} or p.startswith('.env') for p in parts) or
        value.endswith(('.pem', '.key'))):
        raise ValueError('INERT_PATH_REQUIRED')
    return value


GAP_KINDS = ('MISSING', 'TRUNCATED', 'TRANSPORT')
CONTEXT_SELECTION = {'COLLECTED', 'PARTIAL', 'FAILED', 'SKIPPED_INCOMPLETE_CHANGE_LISTING', 'NOT_REQUESTED'}


def capture_status(value):
    """Optional typed capture status: overall state, context selection and every gap, with or without a path."""
    fields(value, 'status context_selection gap_counts gaps')
    if value['context_selection'] not in CONTEXT_SELECTION:
        raise ValueError('CAPTURE_CONTEXT_SELECTION')
    counts = value['gap_counts']
    fields(counts, ' '.join(GAP_KINDS))
    if any(type(counts[k]) is not int or counts[k] < 0 for k in GAP_KINDS):
        raise ValueError('CAPTURE_GAP_COUNTS')
    total = sum(counts.values())
    if value['status'] != ('INCOMPLETE' if total else 'COMPLETE'):
        raise ValueError('CAPTURE_STATUS')
    gaps = value['gaps']
    if not isinstance(gaps, list) or len(gaps) > min(48, total) or (total and not gaps):
        raise ValueError('CAPTURE_GAP_BOUND')
    for row in gaps:
        fields(row, 'kind code request path side')
        if row['kind'] not in GAP_KINDS or row['side'] not in (None, 'base', 'head'):
            raise ValueError('CAPTURE_GAP')
        text(row['code'], 160)
        if row['request'] is not None:
            text(row['request'], 40)
        if row['path'] is not None:
            path(row['path'])
    if value['context_selection'] == 'COLLECTED' and any(row['path'] is None and row['request'] in ('tree', 'search') for row in gaps):
        raise ValueError('CAPTURE_CONTEXT_SELECTION')
    return value


def validate(data):
    fields(data, 'schema id pr revisions provenance scope files')
    if data['schema'] != 'mea-snapshot-v1' or len(canonical(data)) > MAX_BYTES:
        raise ValueError('UNSUPPORTED_SNAPSHOT_OR_SIZE')
    if not isinstance(data['id'], str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}', data['id']):
        raise ValueError('INVALID_ID')
    pr = data['pr']
    fields(pr, 'url repository number title body')
    if not isinstance(pr['repository'], str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', pr['repository']):
        raise ValueError('INVALID_REPOSITORY')
    if type(pr['number']) is not int or pr['number'] < 1:
        raise ValueError('INVALID_PR_NUMBER')
    if pr['url'] != f"https://github.com/{pr['repository']}/pull/{pr['number']}":
        raise ValueError('CANONICAL_PUBLIC_PR_URL_REQUIRED')
    text(pr['title'], 500)
    text(pr['body'], 16000)
    fields(data['revisions'], 'base head target_base')
    if any(not isinstance(v, str) or not SHA.fullmatch(v) for v in data['revisions'].values()):
        raise ValueError('FULL_REVISION_REQUIRED')
    if data['revisions']['base'] == data['revisions']['head']:
        raise ValueError('DISTINCT_REVISIONS_REQUIRED')
    provenance = data['provenance']
    fields(provenance, 'kind captured_at state_at_capture merged_at_capture pending_review_version capture_sha256 source_authentication')
    if provenance['kind'] not in {'KNOWN_HISTORICAL_DEVELOPMENT', 'FROZEN_PENDING_REVIEW'}:
        raise ValueError('PROVENANCE_KIND')
    text(provenance['captured_at'], 80)
    if provenance['state_at_capture'] not in {'open', 'closed'} or type(provenance['merged_at_capture']) is not bool or type(provenance['pending_review_version']) is not bool:
        raise ValueError('PROVENANCE_STATE')
    if provenance['source_authentication'] != 'NOT_INDEPENDENTLY_VERIFIED':
        raise ValueError('NO_SOURCE_AUTHENTICATION_CLAIM')
    if not isinstance(provenance['capture_sha256'], str) or not re.fullmatch('[0-9a-f]{64}', provenance['capture_sha256']):
        raise ValueError('CAPTURE_DIGEST')
    if provenance['kind'] == 'FROZEN_PENDING_REVIEW' and (not provenance['pending_review_version'] or provenance['state_at_capture'] != 'open' or provenance['merged_at_capture']):
        raise ValueError('PENDING_REVIEW_VERSION_REQUIRED')
    scope = data['scope']
    if isinstance(scope, dict) and 'capture' in scope:
        fields(scope, 'reported_changed_files complete_listing omitted_sources capture')
        capture_status(scope['capture'])
    else:
        fields(scope, 'reported_changed_files complete_listing omitted_sources')
    if type(scope['reported_changed_files']) is not int or scope['reported_changed_files'] < 1 or type(scope['complete_listing']) is not bool:
        raise ValueError('FILE_SCOPE')
    if not isinstance(scope['omitted_sources'], list) or len(scope['omitted_sources']) > 48:
        raise ValueError('OMISSION_BOUND')
    for row in scope['omitted_sources']:
        fields(row, 'path side reason')
        path(row['path'])
        if row['side'] not in {'base', 'head'}:
            raise ValueError('OMISSION_SIDE')
        text(row['reason'], 160)
    files = data['files']
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_FILES:
        raise ValueError('FILE_COUNT_BOUND')
    if scope['reported_changed_files'] < len(files) or scope['complete_listing'] != (scope['reported_changed_files'] == len(files)):
        raise ValueError('LISTING_SCOPE_MISMATCH')
    seen = set()
    for file in files:
        fields(file, 'path base_path status patch sides')
        name = path(file['path'])
        path(file['base_path'])
        if name in seen or file['status'] not in {'modified', 'added', 'removed', 'renamed', 'copied', 'changed', 'context'}:
            raise ValueError('FILE_IDENTITY')
        # Unchanged context collected beside the change carries no diff.
        if file['status'] == 'context' and (file['patch'] is not None or file['base_path'] != name):
            raise ValueError('CONTEXT_FILE_IDENTITY')
        seen.add(name)
        if file['patch'] is not None:
            text(file['patch'], 60000)
            if len(file['patch'].splitlines()) > 4000:
                raise ValueError('PATCH_LINE_BOUND')
        if not isinstance(file['sides'], dict) or set(file['sides']) - {'base', 'head'}:
            raise ValueError('SOURCE_SIDES')
        for source in file['sides'].values():
            fields(source, 'text sha256 blob_sha')
            raw = text(source['text'], 120000).encode()
            lines = source['text'].splitlines()
            if len(lines) > 8000 or any(ch in source['text'] for ch in '\v\f\x1c\x1d\x1e\x85\u2028\u2029'):
                raise ValueError('SOURCE_LINE_BOUND')
            if source['sha256'] != digest(raw) or source['blob_sha'] != git_blob_sha(raw):
                raise ValueError('SOURCE_HASH_MISMATCH')
    return data
