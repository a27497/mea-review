"""Finite static rules over bounded supplied snapshots. Never execute PR code."""
import ast
import copy
import re
from .contract import digest, validate
from .patch import parse

POLICY = 'mea-static-v1'
MAX_FINDINGS = 40
MAX_OBSERVATIONS = 40
NOTICE = 'Static rules only. Real AI NOT_RUN. Target code NOT_EXECUTED. Human review required. No finding does not mean a safe PR.'


def review(snapshot):
    data = validate(snapshot)
    evidence = {}
    findings, observations, abstentions = [], [], []

    def cite(file, side, line, rows):
        source = file['sides'].get(side)
        name = file['base_path'] if side == 'base' else file['path']
        if source:
            quote = source['text'].splitlines()[line-1]
            binding = dict(kind='SOURCE', sha256=source['sha256'], blob_sha=source['blob_sha'])
        else:
            row = next(r for r in rows if r[side] == line)
            quote = row['text'][1:]
            binding = dict(kind='PATCH_ONLY', sha256=digest(file['patch'].encode()), patch_line=row['line'])
        item = dict(path=name, side=side, revision=data['revisions'][side], start=line, end=line,
                    quote=quote, binding=binding)
        item['id'] = 'E' + digest(item)[:20]
        evidence[item['id']] = item
        return item['id']

    def finding(rule, title, message, priority, file, line, rows):
        eid = cite(file, 'head', line, rows)
        identity = dict(rule=rule, path=file['path'], side='head', line=line)
        findings.append(dict(id='F'+digest(identity)[:20], **identity, title=title, message=message,
                             priority=priority, status='NEEDS_HUMAN_REVIEW', evidence_ids=[eid]))

    for file in data['files']:
        try:
            rows = parse(file['patch'], file['sides'])
        except ValueError as exc:
            abstentions.append(dict(path=file['path'], reason=str(exc)))
            continue
        changed = [r for r in rows if r['kind'] in {'addition', 'deletion'}]
        refs = []
        for side in ('base', 'head'):
            row = next((r for r in changed if r[side] is not None), None)
            if row:
                refs.append(cite(file, side, row[side], rows))
        observations.append(dict(path=file['path'], kind='CHANGE_OBSERVATION',
                                 message='Changed text is available for review; this observation is not a defect finding.',
                                 evidence_ids=refs))
        added = {r['head'] for r in rows if r['kind'] == 'addition'}
        if file['path'].startswith('.github/workflows/'):
            for row in rows:
                if row['kind'] != 'addition':
                    continue
                match = re.fullmatch(r'\s*-?\s*uses:\s*[\w.-]+/[\w./-]+@([^\s#]+)\s*(?:#.*)?', row['text'][1:])
                if match and not re.fullmatch(r'[0-9a-fA-F]{40}', match[1]):
                    finding('ACTION_UNPINNED', 'Action uses a mutable reference',
                            'Pin this third-party action to a reviewed full commit SHA if reproducible workflow execution is required. A version tag can move; this does not establish a malicious action.',
                            3, file, row['head'], rows)
        if not file['path'].endswith('.py'):
            continue
        head = file['sides'].get('head')
        if head is None:
            abstentions.append(dict(path=file['path'], reason='PYTHON_FULL_HEAD_UNAVAILABLE'))
            continue
        try:
            tree = ast.parse(head['text'])
        except (SyntaxError, ValueError, RecursionError):
            abstentions.append(dict(path=file['path'], reason='PYTHON_PARSE_INCOMPLETE'))
            continue
        for node in ast.walk(tree):
            line = getattr(node, 'lineno', None)
            if line not in added:
                continue
            if isinstance(node, ast.Assert) and isinstance(node.test, ast.Constant) and node.test.value is True:
                finding('ASSERT_TRUE', 'Assertion cannot check the result',
                        'Replace the constant True with a predicate over the behavior under test. This assertion always passes when reached.',
                        2, file, line, rows)
            elif isinstance(node, ast.ExceptHandler) and node.type is None and len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                finding('BARE_EXCEPT_PASS', 'Bare exception handler silently discards failures',
                        'This handler also catches process-control exceptions. Catch the expected exception and handle or report it explicitly.',
                        2, file, line, rows)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(isinstance(d, (ast.List, ast.Dict, ast.Set)) for d in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]):
                    finding('MUTABLE_DEFAULT', 'Mutable default is shared between calls',
                            'Use a None default and initialize per call unless shared mutable state is intentional.',
                            2, file, line, rows)
    if not data['scope']['complete_listing']:
        abstentions.append(dict(path=None, reason='FILE_LIST_INCOMPLETE'))
    for row in data['scope']['omitted_sources']:
        abstentions.append(dict(path=row['path'], reason=row['reason'], side=row['side']))
    capture = data['scope'].get('capture')
    if capture and capture['status'] != 'COMPLETE':
        # Gaps without a path (tree, code search) never appear in omitted_sources.
        abstentions.append(dict(path=None, reason='CAPTURE_INCOMPLETE', context_selection=capture['context_selection'],
                                gap_counts=copy.deepcopy(capture['gap_counts'])))
        for row in capture['gaps']:
            if row['path'] is None:
                abstentions.append(dict(path=None, reason=row['kind'] + ':' + row['code'], request=row['request']))
    if len(findings) > MAX_FINDINGS:
        abstentions.append(dict(path=None, reason='FINDING_LIMIT', omitted=len(findings)-MAX_FINDINGS))
    if len(observations) > MAX_OBSERVATIONS:
        abstentions.append(dict(path=None, reason='OBSERVATION_LIMIT'))
    findings = findings[:MAX_FINDINGS]
    observations = observations[:MAX_OBSERVATIONS]
    used = {e for row in findings + observations for e in row['evidence_ids']}
    report = dict(schema='mea-review-v1', policy=POLICY, snapshot_id=data['id'], snapshot_sha256=digest(data),
                  pr=copy.deepcopy(data['pr']), revisions=copy.deepcopy(data['revisions']),
                  mode='STATIC_RULES_NOT_AI', real_ai='NOT_RUN', runtime='NOT_EXECUTED',
                  human_review='PENDING', conclusion='REVIEW_REQUIRED',
                  capabilities=dict(writeback=False, autofix=False, automerge=False),
                  findings=findings, observations=observations, abstentions=abstentions,
                  evidence=[e for k, e in evidence.items() if k in used], notice=NOTICE)
    report['sha256'] = digest(report)
    return report


def verify_report(report, snapshot):
    """Deterministic replay check, not an independent review or source attestation."""
    if report != review(snapshot):
        raise ValueError('REPORT_REPLAY_MISMATCH')
    return True
