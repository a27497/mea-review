"""Remote outcome types shared by the Mock and the live writeback lane. No transport here."""
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


def is_live(remote):
    return getattr(type(remote), 'mode', None) == LIVE_MODE
