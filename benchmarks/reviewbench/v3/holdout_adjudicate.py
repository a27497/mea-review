"""Owner adjudication of the blind holdout packet: a local reader, a terminal mode and the freeze.

Offline only. No model, network beyond 127.0.0.1, credential or ledger. It reads the blind packet
and one draft file, and the freeze writes one new directory. It never reads anything else of the
run, offers no default, preselection or suggestion, and every decision starts as PENDING_OWNER.

  serve   --packet P --draft D [--port N]   local reader at http://127.0.0.1:N/ (no external resource)
  tty     --packet P --draft D               page-by-page decisions in an interactive terminal
  freeze  --packet P --draft D --out O       interactive terminal only; typed confirmation sentence

Decision values: DEFECT, NOT_A_DEFECT, INSUFFICIENT (with optional needs_outside_context).
Each decision may carry evidence (path and line or line range) and a free-text reason.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import sys
import tempfile

HERE = Path(__file__).resolve().parent
VERSION = 'mea-reviewbench-v3-holdout-owner-adjudication-v1'
DRAFT_SCHEMA = 'mea-reviewbench-v3-holdout-owner-draft-v1'
FROZEN_SCHEMA = 'mea-reviewbench-v3-holdout-owner-decisions-v1'
MANIFEST_SCHEMA = 'mea-reviewbench-v3-holdout-blind-manifest-v1'
PENDING = 'PENDING_OWNER'
DECISIONS = ('DEFECT', 'NOT_A_DEFECT', 'INSUFFICIENT')
ID = re.compile(r'c-[0-9a-f]{16}')
SLUG = re.compile(r'sources/[A-Za-z0-9_.-]+--[A-Za-z0-9_.-]+--[0-9]+\.json')
REASON_CHARS = 4000
EVIDENCE_ROWS = 20
READER = HERE / 'holdout_reader.html'
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
       "img-src 'none'; font-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


class Refused(Exception):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def strict(raw):
    def pairs(items):
        keys = [k for k, _ in items]
        if len(set(keys)) != len(keys):
            raise Refused('DUPLICATE_KEY')
        return dict(items)
    return json.loads(raw, object_pairs_hook=pairs)


# ----- packet -----------------------------------------------------------------------------------

def order_of(ids_with_pr, seed):
    """Same rule as the packet manifest states: sort by SHA-256 of [seed, id], no two of a PR in a row."""
    queue = sorted(ids_with_pr, key=lambda x: sha256(canonical([seed, x[0]])))
    order, last = [], None
    while queue:
        pick = next((i for i, (_, pr) in enumerate(queue) if pr != last), None)
        if pick is None:
            raise Refused('ORDER_UNSATISFIABLE')
        cid, pr = queue.pop(pick)
        order.append(cid)
        last = pr
    return order


class Packet:
    """The blind packet after checking: manifest digest, every file digest, the ID rule and the order."""
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        raw = (self.directory / 'packet-manifest.json').read_bytes()
        self.manifest_sha256 = sha256(raw)
        self.manifest = strict(raw)
        m = self.manifest
        if m.get('schema') != MANIFEST_SCHEMA or m['pages'] != m['candidates'] or len(m['order']) != m['pages']:
            raise Refused('PACKET_MANIFEST')
        self.pages, prs = {}, []
        for row in m['page_files']:
            if not ID.fullmatch(row['id']) or row['file'] != f"pages/{row['id']}.json":
                raise Refused('PACKET_PAGE_NAME')
            raw = (self.directory / row['file']).read_bytes()
            if sha256(raw) != row['sha256']:
                raise Refused('PACKET_PAGE_DIGEST')
            page = strict(raw)
            body = {k: v for k, v in page.items() if k != 'id'}
            tag = m['id_rule'].split("'")[1]
            if page['id'] != row['id'] or 'c-' + sha256(canonical([tag, sha256(canonical(body))]))[:16] != row['id']:
                raise Refused('PACKET_PAGE_ID')
            self.pages[row['id']] = page
            prs.append((row['id'], f"{page['pr']['repository'].replace('/', '--')}--{page['pr']['number']}"))
        self.sources = {}
        for row in m['source_files']:
            if not SLUG.fullmatch(row['file']):
                raise Refused('PACKET_SOURCE_NAME')
            raw = (self.directory / row['file']).read_bytes()
            if sha256(raw) != row['sha256']:
                raise Refused('PACKET_SOURCE_DIGEST')
            self.sources[row['file']] = raw
        if order_of(prs, m['seed']) != m['order'] or sha256(canonical(m['order'])) != m['order_sha256']:
            raise Refused('PACKET_ORDER')
        self.order = list(m['order'])


# ----- draft ------------------------------------------------------------------------------------

def blank():
    return dict(decision=PENDING, needs_outside_context=None, evidence=[], reason='')


def check_entry(value):
    if not isinstance(value, dict) or set(value) != {'decision', 'needs_outside_context', 'evidence', 'reason'}:
        raise Refused('ENTRY_FIELDS')
    if value['decision'] not in DECISIONS + (PENDING,):
        raise Refused('ENTRY_DECISION')
    flag = value['needs_outside_context']
    if flag is not None and (type(flag) is not bool or value['decision'] != 'INSUFFICIENT'):
        raise Refused('ENTRY_OUTSIDE_CONTEXT')
    if not isinstance(value['reason'], str) or len(value['reason']) > REASON_CHARS or '\0' in value['reason']:
        raise Refused('ENTRY_REASON')
    rows = value['evidence']
    if not isinstance(rows, list) or len(rows) > EVIDENCE_ROWS:
        raise Refused('ENTRY_EVIDENCE')
    for row in rows:
        if (not isinstance(row, dict) or set(row) != {'path', 'start', 'end'} or not isinstance(row['path'], str)
                or not 0 < len(row['path']) <= 256 or any(type(row[k]) is not int for k in ('start', 'end'))
                or not 1 <= row['start'] <= row['end'] <= 1_000_000):
            raise Refused('ENTRY_EVIDENCE_ROW')
    if value['decision'] == PENDING and (flag is not None):
        raise Refused('ENTRY_PENDING_WITH_FLAG')
    return value


def parse_evidence(text):
    """'path:12' or 'path:12-15', one per line or comma-separated."""
    rows = []
    for part in re.split(r'[\n,]+', text or ''):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r'(.+):(\d+)(?:-(\d+))?', part)
        if not m:
            raise Refused('EVIDENCE_FORMAT')
        start = int(m.group(2))
        rows.append(dict(path=m.group(1).strip(), start=start, end=int(m.group(3) or start)))
    return rows


class Draft:
    def __init__(self, path, packet):
        self.path, self.packet = Path(path).resolve(), packet
        if packet.directory in self.path.parents or 'judge-only' in self.path.parts:
            raise Refused('DRAFT_LOCATION')
        # The draft inode changes on replace, so writers share a separate stable lock.
        self.lock_path = self.path.with_name(self.path.name + '.lock')
        if not self.path.exists():
            with self.lock_path.open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                if not self.path.exists():
                    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
                    value = dict(schema=DRAFT_SCHEMA, tool_version=VERSION, packet_manifest_sha256=packet.manifest_sha256,
                                 created_at=now, updated_at=now, entries={cid: blank() for cid in packet.order})
                    self._replace(value)
        self.load()

    def _replace(self, value):
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix='.draft-')
        try:
            with os.fdopen(fd, 'wb') as handle:
                handle.write(canonical(value) + b'\n')
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def load(self):
        raw = self.path.read_bytes()
        value = strict(raw)
        if (value.get('schema') != DRAFT_SCHEMA or value.get('packet_manifest_sha256') != self.packet.manifest_sha256
                or set(value['entries']) != set(self.packet.order)):
            raise Refused('DRAFT_BINDING')
        for entry in value['entries'].values():
            check_entry(entry)
        self.value, self.sha256 = value, sha256(raw)
        return value

    def save(self, cid, entry):
        if cid not in self.packet.pages:
            raise Refused('DRAFT_UNKNOWN_PAGE')
        entry = check_entry(entry)
        with self.lock_path.open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            value = self.load()
            value['entries'][cid] = entry
            value['updated_at'] = datetime.now(timezone.utc).isoformat(timespec='seconds')
            self._replace(value)
            return self.load()


# ----- local reader -----------------------------------------------------------------------------

def handler(packet, draft, port_ref):
    class Handler(http.server.BaseHTTPRequestHandler):
        server_version = 'mea-owner-reader'
        sys_version = ''

        def log_message(self, *args):
            pass  # no request log, no telemetry

        def headers_ok(self):
            return self.headers.get('Host') == f'127.0.0.1:{port_ref[0]}'

        def send(self, status, raw, kind='application/json'):
            self.send_response(status)
            self.send_header('Content-Type', kind + '; charset=utf-8')
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', CSP)
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if not self.headers_ok():
                return self.send(403, b'{"error":"HOST"}')
            path = self.path
            if path == '/':
                return self.send(200, READER.read_bytes(), 'text/html')
            if path == '/api/manifest':
                m = packet.manifest
                return self.send(200, canonical(dict(order=packet.order, pages=m['pages'], pages_per_pr=m['pages_per_pr'],
                                                      manifest_sha256=packet.manifest_sha256)))
            if path.startswith('/api/page/') and path[10:] in packet.pages:
                return self.send(200, canonical(packet.pages[path[10:]]))
            if path.startswith('/api/') and path[5:] in packet.sources:
                return self.send(200, packet.sources[path[5:]])
            if path == '/api/draft':
                return self.send(200, canonical(draft.load()))
            return self.send(404, b'{"error":"NOT_FOUND"}')

        def do_POST(self):
            if (not self.headers_ok() or self.headers.get('Content-Type') != 'application/json'
                    or self.headers.get('X-Owner-Draft') != '1' or self.headers.get('Origin') not in (None, f'http://127.0.0.1:{port_ref[0]}')):
                return self.send(403, b'{"error":"REQUEST_ORIGIN"}')
            if not self.path.startswith('/api/draft/'):
                return self.send(404, b'{"error":"NOT_FOUND"}')
            length = int(self.headers.get('Content-Length') or 0)
            if not 0 < length <= 64_000:
                return self.send(413, b'{"error":"SIZE"}')
            try:
                value = draft.save(self.path[11:], strict(self.rfile.read(length)))
            except (Refused, ValueError, KeyError, TypeError) as exc:
                return self.send(400, canonical(dict(error=str(exc) or type(exc).__name__)))
            return self.send(200, canonical(value['entries'][self.path[11:]]))
    return Handler


def serve(packet, draft, port):
    port_ref = [port]
    server = http.server.ThreadingHTTPServer(('127.0.0.1', port), handler(packet, draft, port_ref))
    port_ref[0] = server.server_address[1]
    print(f'Owner reader at http://127.0.0.1:{port_ref[0]}/  (draft {draft.path}); Ctrl-C to stop', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# ----- terminal ---------------------------------------------------------------------------------

def interactive():
    return (sys.stdin.isatty() and sys.stdout.isatty() and os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp())


def show(page, n, total, patch=False):
    out = [f'\n=== {n}/{total}  {page["id"]}  {page["pr"]["repository"]} #{page["pr"]["number"]} ===',
           page['notice'], f'PR title (untrusted data): {page["pr"]["title"]}',
           'PR description (untrusted data):', page['pr']['description'] or '(empty)', '',
           f'Claim title: {page["claim"]["title"]}',
           f'Category: {page["claim"]["category"]}   Severity: {page["claim"]["severity"]}',
           f'Impact: {page["claim"]["impact"]}', f'Quote: {page["claim"]["quote"]!r}',
           f'Location: {page["location"]["path"]} HEAD lines {page["location"]["start"]}-{page["location"]["end"]}', '']
    lo, hi = page['source_context']['cited_lines']
    for number, text in page['source_context']['lines']:
        out.append(f'{">>" if lo <= number <= hi else "  "}{number:6d} | {text}')
    if patch:
        out += ['', 'Frozen patch of this file:', page['file_patch'] or '(none)']
    print('\n'.join(out))


def terminal(packet, draft):
    if not interactive():
        raise Refused('TTY_REQUIRED')
    total = len(packet.order)
    for n, cid in enumerate(packet.order, 1):
        page, entry = packet.pages[cid], draft.load()['entries'][cid]
        patch = False
        while True:
            show(page, n, total, patch)
            print(f'\nSaved decision for this page: {entry["decision"]}')
            choice = input('[d] DEFECT  [n] NOT_A_DEFECT  [i] INSUFFICIENT  [p] patch  [k] keep and next  [q] quit > ').strip().lower()
            if choice == 'q':
                return
            if choice == 'k':
                break
            if choice == 'p':
                patch = not patch
                continue
            if choice not in ('d', 'n', 'i'):
                continue
            decision = dict(d='DEFECT', n='NOT_A_DEFECT', i='INSUFFICIENT')[choice]
            flag = None
            if decision == 'INSUFFICIENT':
                answer = input('Needs code or schema outside the frozen input? [y/n/blank = not stated] > ').strip().lower()
                flag = True if answer == 'y' else False if answer == 'n' else None
            try:
                evidence = parse_evidence(input('Evidence (path:line or path:start-end, comma-separated, blank for none) > '))
            except Refused as exc:
                print('Not saved:', exc)
                continue
            reason = input('Reason (free text, blank for none) > ')
            try:
                entry = draft.save(cid, dict(decision=decision, needs_outside_context=flag, evidence=evidence, reason=reason))['entries'][cid]
            except Refused as exc:
                print('Not saved:', exc)
                continue
            break


def freeze(packet, draft_path, out):
    if not interactive():
        raise Refused('FREEZE_REQUIRES_INTERACTIVE_TTY')
    out = Path(out).resolve()
    if out.exists() or 'judge-only' in out.parts or packet.directory in out.parents:
        raise Refused('FREEZE_OUTPUT_MUST_BE_NEW')
    if not Path(draft_path).is_file():
        raise Refused('FREEZE_DRAFT_MISSING')
    draft = Draft(draft_path, packet)
    first = draft.load()
    first_sha = draft.sha256
    pending = [cid for cid in packet.order if first['entries'][cid]['decision'] == PENDING]
    if pending:
        raise Refused(f'FREEZE_PENDING:{len(pending)}')
    counts = {d: sum(e['decision'] == d for e in first['entries'].values()) for d in DECISIONS}
    sentence = f'I adjudicated all {len(packet.order)} candidates myself; freeze packet {packet.manifest_sha256[:12]}'
    print(f'Draft {draft.path} (SHA-256 {first_sha})')
    print(f'Decisions: {counts}')
    print('Type this sentence exactly to freeze, anything else aborts:')
    print(sentence)
    typed = input('> ')
    if typed != sentence:
        raise Refused('FREEZE_CONFIRMATION_MISMATCH')
    again = Packet(packet.directory)
    if again.manifest_sha256 != packet.manifest_sha256 or sha256(draft.path.read_bytes()) != first_sha:
        raise Refused('FREEZE_INPUT_CHANGED')
    frozen = dict(schema=FROZEN_SCHEMA, tool_version=VERSION, tool_sha256=sha256(Path(__file__).read_bytes()),
                  packet_manifest_sha256=packet.manifest_sha256, mapping_sha256=packet.manifest['mapping_sha256'],
                  draft_sha256=first_sha, candidates=len(packet.order), counts=counts,
                  decisions=[dict(id=cid, **first['entries'][cid]) for cid in packet.order],
                  frozen_at=datetime.now(timezone.utc).isoformat(timespec='seconds'), confirmation=sentence)
    out.mkdir(mode=0o700)
    path = out / 'owner-decisions.json'
    with path.open('xb') as handle:
        handle.write(canonical(frozen) + b'\n')
    os.chmod(path, 0o444)
    print('Frozen', path, sha256(path.read_bytes()))
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
                                     allow_abbrev=False)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('serve', 'tty', 'freeze'):
        p = sub.add_parser(name, allow_abbrev=False)
        p.add_argument('--packet', type=Path, required=True)
        p.add_argument('--draft', type=Path, required=True)
        if name == 'serve':
            p.add_argument('--port', type=int, default=0)
        if name == 'freeze':
            p.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        packet = Packet(args.packet)
        if args.command == 'freeze':
            freeze(packet, args.draft, args.out)
        elif args.command == 'tty':
            if not interactive():
                raise Refused('TTY_REQUIRED')
            terminal(packet, Draft(args.draft, packet))
        else:
            serve(packet, Draft(args.draft, packet), args.port)
    except Refused as exc:
        print('Refused:', exc, file=sys.stderr)
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
