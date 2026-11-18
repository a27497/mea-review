"""Incremental re-review: map sent comment anchors through the increment diff.

The new frozen version must be an increment snapshot whose base is the previous
head (mea.github_input --since). A sent RIGHT-side comment whose exact line is
deleted or replaced in the increment is OUTDATED. Otherwise it is CURRENT and its
line moves by the hunk offsets. Nothing is re-posted: an OUTDATED comment stays on
its original commit, and a new candidate at a CURRENT comment's mapped line is a
duplicate that cannot be drafted. The lines that replaced an OUTDATED comment are
mapped through every later increment; a candidate inside them, or in a file whose
earlier replacement lines could not be mapped, cannot be drafted either. When an
incomplete listing or a non-linear step leaves the file's path lineage unknown (a
rename may have been omitted), no head-side candidate can be drafted until a later
listing names the file by its last known path.
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


def changed_file(nv, path):
    # A copy leaves the original file in place, so only renames and edits move a line.
    return next((f for f in nv['files'] if f['base_path'] == path and f['status'] not in ('context', 'copied')), None)


def carry_region(region, region_head, relation, old_head, nv, complete):
    """Map an earlier replacement region (lines of region_head) through this increment.

    Returns (region, None) with the mapped region or None when nothing remains to protect, or
    (None, reason) when the region cannot be mapped and the location must stay unresolved."""
    if region is None or region['end'] < region['start']:
        return None, None  # Nothing replaced the outdated line (pure deletion), or the file was removed.
    if relation != 'INCREMENTAL_LINEAR' or region_head != old_head:
        return None, 'Earlier replacement lines are not on the base of this linear increment.'
    f = changed_file(nv, region['path'])
    if f is None:
        if not complete:
            return None, 'Increment file listing is incomplete; the earlier replacement lines cannot be mapped.'
        return dict(region), None
    if f['status'] == 'removed':
        return None, None
    if 'head' not in f['sides'] or not f['patch']:
        return None, 'Increment patch or head source missing; the earlier replacement lines cannot be mapped.'
    lines = []
    for line in range(region['start'], region['end'] + 1):
        status, mapped, replaced = map_line(f['patch'], line)
        if status == 'CURRENT':
            lines.append(mapped)
        elif replaced[1] >= replaced[0]:
            lines.extend((replaced[0], replaced[1]))  # Lines that replaced a replacement line stay protected.
    if not lines:
        return None, None
    return dict(path=f['path'], start=min(lines), end=max(lines)), None


def carry_unresolved(marker, linked, nv, complete):
    """Carry an unresolved replacement-region marker through this increment, or None once the file is removed.

    lineage KNOWN: the file is at marker['path']. lineage UNKNOWN: it may have been renamed in a
    step whose listing was incomplete or not linear; the marker keeps the last known path."""
    if not linked:
        return dict(path=marker['path'], reason=marker['reason'], lineage='UNKNOWN')
    f = changed_file(nv, marker['path'])
    if f is not None:
        # Listed by its last known path: the lineage is known again from this increment's base.
        return None if f['status'] == 'removed' else dict(path=f['path'], reason=marker['reason'], lineage='KNOWN')
    if marker.get('lineage') == 'UNKNOWN' or not complete:
        return dict(path=marker['path'], reason=marker['reason'], lineage='UNKNOWN')
    return dict(path=marker['path'], reason=marker['reason'], lineage='KNOWN')


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
            last[row['send_id']] = (row, earlier.get('new_head'))
    comments = []
    for s in sends:
        if s['status'] not in SENT or not s.get('remote_comment'):
            continue
        c = s['remote_comment']
        p = s['payload']
        before, before_head = last.get(s['id'], (None, None))
        if p['commit_id'] == old_head:
            before = None  # Sent against this increment's base: map the payload itself.
        if before and before['status'] == 'CURRENT' and before['new']:
            p = dict(before['new'])
        row = dict(send_id=s['id'], comment_id=c['id'], html_url=c['html_url'],
                   old=dict(commit_id=p['commit_id'], path=p['path'], line=p['line'], side=p['side']),
                   new=None, status='NEEDS_RECHECK', replaced_head_lines=None, republish='NEVER_AUTOMATIC', reason=None)
        if before and before['status'] != 'CURRENT':
            row.update(status=before['status'], reason='Carried from the earlier re-review of ' + before['old']['commit_id'][:12] + '.')
            if before['status'] == 'OUTDATED':
                # Keep protecting the lines that replaced the outdated comment, in this increment's coordinates.
                earlier = before.get('replaced_region_unresolved')
                linked = relation == 'INCREMENTAL_LINEAR' and before_head == old_head
                if earlier:
                    # Once unresolved, the file stays unresolved; a listed rename moves the marker, and an
                    # incomplete or non-linear step leaves its path unknown.
                    marker = carry_unresolved(earlier, linked, nv, complete)
                    if marker:
                        row['replaced_region_unresolved'] = marker
                else:
                    region, unresolved = carry_region(before.get('replaced_head_lines'), before_head, relation, old_head, nv, complete)
                    row['replaced_head_lines'] = region
                    if unresolved:
                        marker = carry_unresolved(dict(path=before['replaced_head_lines']['path'], reason=unresolved), linked, nv, complete)
                        if marker:
                            row['replaced_region_unresolved'] = marker
        elif relation != 'INCREMENTAL_LINEAR' or p['commit_id'] != old_head:
            row['reason'] = 'Comment commit is not the base of a linear increment; manual recheck.'
        elif p['side'] == 'LEFT':
            row.update(status='CURRENT', new=dict(commit_id=new_head, path=p['path'], line=p['line'], side='LEFT'),
                       reason='Base-side line; the increment does not change the merge base.')
        else:
            f = changed_file(nv, p['path'])
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
        # An earlier replacement region that could not be mapped keeps the whole file unresolved. While its
        # path lineage is unknown, any head-side file may hold that region.
        near = near or next((r for r in comments if r['status'] == 'OUTDATED' and r.get('replaced_region_unresolved')
                             and ch['side'] == 'head' and (r['replaced_region_unresolved'].get('lineage') == 'UNKNOWN'
                                                           or r['replaced_region_unresolved']['path'] == ch['path'])), None)
        if dup:
            candidates.append(dict(candidate_id=cand['id'], change=ch, status='DUPLICATE_OF_SENT_COMMENT', comment_id=dup['comment_id'],
                                   reason='A sent comment already covers this mapped line; drafting is refused.'))
        elif near:
            unknown = (near.get('replaced_region_unresolved') or {}).get('lineage') == 'UNKNOWN'
            candidates.append(dict(candidate_id=cand['id'], change=ch, status='AT_OUTDATED_COMMENT_LOCATION', comment_id=near['comment_id'],
                                   reason='The file holding the lines that replaced an outdated comment is unknown after an incomplete or non-linear increment; '
                                          'refused until frozen evidence names it.' if unknown else
                                          'Inside the lines that replaced an outdated comment. Not a relocation; a human decides whether it is a new issue.'))
        else:
            candidates.append(dict(candidate_id=cand['id'], change=ch, status='NEW', comment_id=None,
                                   reason='Validated candidate on an incremental change; not tied to a sent comment.'))
    return dict(policy=POLICY, relation=relation, old_head=old_head, new_head=new_head, comments=comments, candidates=candidates,
                observed_remote=observed is not None,
                limitations='Line mapping uses the increment patch only. GitHub may mark a comment outdated by its own diff-hunk rule; both are recorded.')
