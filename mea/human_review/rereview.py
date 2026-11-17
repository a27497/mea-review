"""Incremental re-review: map sent comment anchors through the increment diff.

The new frozen version must be an increment snapshot whose base is the previous
head (mea.github_input --since). A sent RIGHT-side comment whose exact line is
deleted or replaced in the increment is OUTDATED. Otherwise it is CURRENT and its
line moves by the hunk offsets. Nothing is re-posted: an OUTDATED comment stays on
its original commit, and a new candidate at a CURRENT comment's mapped line is a
duplicate that cannot be drafted.
"""
import re

from ..patch import parse as parse_patch

POLICY = 'm4-incremental-anchor-v1'
SENT = {'PUBLISHED', 'PUBLISHED_RECONCILED', 'MOCK_PUBLISHED', 'MOCK_PUBLISHED_RECONCILED'}


def hunks(patch):
    """[(base_start, base_count, head_start, head_count, rows)] from a unified patch."""
    result = []
    for row in parse_patch(patch, {}):
        if row['kind'] == 'header':
            m = re.fullmatch(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@.*', row['text'])
            result.append([int(m[1]), int(m[2] or 1), int(m[3]), int(m[4] or 1), []])
        elif result:
            result[-1][4].append(row)
    return result


def map_line(patch, line):
    """('CURRENT', new_line, None) or ('OUTDATED', None, (start, end)) for an old head-side line.

    start..end are the new lines that replaced the changed block (end < start for a pure deletion)."""
    delta = 0
    for base_start, base_count, head_start, head_count, rows in hunks(patch):
        end = base_start + base_count - 1
        if base_count == 0:
            # Pure insertion after base_start: lines up to base_start are before the hunk.
            if line <= base_start:
                return 'CURRENT', line + delta, None
        elif line < base_start:
            return 'CURRENT', line + delta, None
        elif line <= end:
            i = next(i for i, r in enumerate(rows) if r['base'] == line)
            if rows[i]['kind'] == 'deletion':
                # The contiguous change block (deletions and additions) holding the deleted line.
                lo = hi = i
                while lo > 0 and rows[lo - 1]['kind'] in ('deletion', 'addition'):
                    lo -= 1
                while hi + 1 < len(rows) and rows[hi + 1]['kind'] in ('deletion', 'addition', 'marker'):
                    hi += 1
                added = [r['head'] for r in rows[lo:hi + 1] if r['kind'] == 'addition']
                if added:
                    return 'OUTDATED', None, (min(added), max(added))
                after = next((r['head'] for r in rows[hi + 1:] if r['head'] is not None), head_start + head_count)
                return 'OUTDATED', None, (after, after - 1)
            return 'CURRENT', rows[i]['head'], None
        delta += head_count - base_count
    return 'CURRENT', line + delta, None


def check(old, new, sends, observed=None, prior=()):
    ov, nv = old['view'], new['view']
    old_head, new_head = ov['revisions']['head'], nv['revisions']['head']
    relation = 'INCREMENTAL_LINEAR' if nv['revisions']['base'] == old_head and new_head != old_head else 'NOT_INCREMENTAL'
    remote = {c['id']: c for c in (observed or {}).get('comments', [])}
    complete = (nv.get('coverage') or {}).get('complete_listing') is True
    # Latest earlier result per send: a later increment starts from that mapped anchor.
    last = {}
    for earlier in prior:
        for row in earlier['comments']:
            last[row['send_id']] = row
    comments = []
    for s in sends:
        if s['status'] not in SENT or not s.get('remote_comment'):
            continue
        c = s['remote_comment']
        p = s['payload']
        before = last.get(s['id'])
        if before and before['status'] == 'CURRENT' and before['new']:
            p = dict(before['new'])
        row = dict(send_id=s['id'], comment_id=c['id'], html_url=c['html_url'],
                   old=dict(commit_id=p['commit_id'], path=p['path'], line=p['line'], side=p['side']),
                   new=None, status='NEEDS_RECHECK', replaced_head_lines=None, republish='NEVER_AUTOMATIC', reason=None)
        if before and before['status'] != 'CURRENT':
            row.update(status=before['status'], reason='Carried from the earlier re-review of ' + before['old']['commit_id'][:12] + '.')
        elif relation != 'INCREMENTAL_LINEAR' or p['commit_id'] != old_head:
            row['reason'] = 'Comment commit is not the base of a linear increment; manual recheck.'
        elif p['side'] == 'LEFT':
            row.update(status='CURRENT', new=dict(commit_id=new_head, path=p['path'], line=p['line'], side='LEFT'),
                       reason='Base-side line; the increment does not change the merge base.')
        else:
            # A copy leaves the original file in place, so only renames and edits move the anchor.
            f = next((f for f in nv['files'] if f['base_path'] == p['path'] and f['status'] not in ('context', 'copied')), None)
            if f is None and not complete:
                row['reason'] = 'Increment file listing is incomplete; absence does not prove the file is unchanged.'
            elif f is None:
                row.update(status='CURRENT', new=dict(commit_id=new_head, path=p['path'], line=p['line'], side='RIGHT'),
                           reason='File unchanged in the increment.')
            elif f['status'] == 'removed':
                row.update(status='OUTDATED', reason='File removed in the increment.')
            elif 'head' not in f['sides']:
                row['reason'] = 'Head source missing from the increment capture; manual recheck.'
            elif not f['patch']:
                row['reason'] = 'Increment patch missing or truncated; manual recheck.'
            else:
                status, line, replaced = map_line(f['patch'], p['line'])
                if status == 'OUTDATED':
                    row.update(status='OUTDATED', replaced_head_lines=dict(path=f['path'], start=replaced[0], end=replaced[1]),
                               reason='Anchored line was modified or deleted in the increment.')
                else:
                    row.update(status='CURRENT', new=dict(commit_id=new_head, path=f['path'], line=line, side='RIGHT'),
                               reason='Anchored line unchanged; mapped through the increment hunks.')
        if c['id'] in remote:
            row['github'] = dict(remote[c['id']]['current'])
        elif observed is not None:
            row['github'] = 'NOT_LISTED'
        comments.append(row)
    candidates = []
    for cand in new['candidates']:
        ch = cand['change']
        dup = next((r for r in comments if r['status'] == 'CURRENT' and r['new'] and r['new']['path'] == ch['path']
                    and r['new']['line'] == ch['line'] and r['new']['side'] == ('RIGHT' if ch['side'] == 'head' else 'LEFT')), None)
        near = next((r for r in comments if r['status'] == 'OUTDATED' and r['replaced_head_lines'] and ch['side'] == 'head'
                     and r['replaced_head_lines']['path'] == ch['path']
                     and r['replaced_head_lines']['start'] <= ch['line'] <= r['replaced_head_lines']['end']), None)
        if dup:
            candidates.append(dict(candidate_id=cand['id'], change=ch, status='DUPLICATE_OF_SENT_COMMENT', comment_id=dup['comment_id'],
                                   reason='A sent comment already covers this mapped line; drafting is refused.'))
        elif near:
            candidates.append(dict(candidate_id=cand['id'], change=ch, status='AT_OUTDATED_COMMENT_LOCATION', comment_id=near['comment_id'],
                                   reason='Inside the lines that replaced an outdated comment. Not a relocation; a human decides whether it is a new issue.'))
        else:
            candidates.append(dict(candidate_id=cand['id'], change=ch, status='NEW', comment_id=None,
                                   reason='Validated candidate on an incremental change; not tied to a sent comment.'))
    return dict(policy=POLICY, relation=relation, old_head=old_head, new_head=new_head, comments=comments, candidates=candidates,
                observed_remote=observed is not None,
                limitations='Line mapping uses the increment patch only. GitHub may mark a comment outdated by its own diff-hunk rule; both are recorded.')
