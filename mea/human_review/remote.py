"""Remote outcome types shared by the Mock and the live writeback lane. No transport here."""
import re

from ..contract import digest

LIVE_MODE = 'LIVE_GITHUB'


def binding(value):
    """Digest of what a human approves: PR, head commit, file, side, line and exact body."""
    return digest(dict(target=value['target'], pr_url=value['pr_url'], payload=value['payload']))


class NotSent(Exception):
    """Definitively no remote comment: no POST was issued, or GitHub rejected it."""
    def __init__(self, code, receipt=None):
        super().__init__(code)
        self.code = code
        self.receipt = receipt


class Uncertain(Exception):
    """A POST was issued and its effect is unknown."""
    def __init__(self, code, receipt=None):
        super().__init__(code)
        self.code = code
        self.receipt = receipt


def anchor_others(payload, remote):
    """Comments by other accounts (bots included) on the payload's exact anchor, as shown to the confirmer.

    The live lane compares this list again immediately before the POST, so a later change to
    these comments (added, removed or edited) needs a new confirmation."""
    rows = []
    for c in remote['comments']:
        if c.get('user', {}).get('login') != remote['principal'] and all(c.get(k) == payload[k] for k in ['commit_id', 'path', 'line', 'side']):
            plain = ' '.join(re.sub(r'<!--.*?-->|<[^>]+>', '', c.get('body') or '', flags=re.S).split())
            # The excerpt is for display; the digest of the complete body detects any edit.
            rows.append(dict(id=c['id'], login=c['user']['login'], html_url=c.get('html_url'), excerpt=plain[:300],
                             body_sha256=digest(c.get('body') or ''), label='Already raised by ' + c['user']['login']))
    return rows


def is_live(remote):
    return getattr(type(remote), 'mode', None) == LIVE_MODE
