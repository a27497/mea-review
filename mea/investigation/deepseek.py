"""Controlled real DeepSeek transport for the provider worker. One POST per request, no retry.

The key is read from a private env file only by the explicit live CLI. It is never
journaled: wire.call redacts the exact secret and Bearer/sk- patterns from every receipt.
"""
import http.client
import os
import ssl
import stat
import threading
import time
from pathlib import Path

from . import wire
from .real_protocol import REQUEST_SECONDS

CREDENTIALS = Path('~/.config/mea/deepseek.env').expanduser()
VARIABLE = 'DEEPSEEK_API_KEY'
# Collect waits for the socket timeout plus this grace before calling the outcome unknown.
GRACE_SECONDS = 5


def read_key(path=CREDENTIALS):
    """Return DEEPSEEK_API_KEY from a private owner-only file. Errors never include the value."""
    path = Path(path)
    try:
        info = path.lstat()
    except OSError:
        raise ValueError('CREDENTIAL_FILE_MISSING') from None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('CREDENTIAL_FILE_NOT_REGULAR')
    if info.st_mode & 0o077 or info.st_uid != os.getuid():
        raise ValueError('CREDENTIAL_FILE_PERMISSIONS')
    if info.st_size > 4096:
        raise ValueError('CREDENTIAL_FILE_SIZE')
    value = None
    for line in path.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#') or '=' not in line:
            continue
        name, raw = line.split('=', 1)
        if name.strip().removeprefix('export ').strip() == VARIABLE:
            value = raw.strip().strip('"').strip("'")
    if not value:
        raise ValueError('CREDENTIAL_EMPTY')
    if any(c.isspace() for c in value) or len(value) > 512:
        raise ValueError('CREDENTIAL_FORMAT')
    return value


def connect(host, port, timeout):
    return http.client.HTTPSConnection(host, port, timeout=timeout, context=ssl.create_default_context())


class Sender:
    """Broker sender interface: start launches one POST, collect waits for its receipt.

    `validate` is the task's own request validator (prompt, first-turn and tool profile).
    A cancelled or overdue request is reported as uncertain and is never resent.
    """

    def __init__(self, secret, *, validate=None, connect=connect, clock=time.monotonic):
        self._secret = secret
        self.validate = validate
        self.connect = connect
        self.clock = clock
        self.calls = 0

    def start(self, body):
        if self.validate is None:
            raise ValueError('LIVE_VALIDATOR_REQUIRED')
        box = {}
        self.calls += 1

        def run():
            try:
                box['value'] = wire.call(body, self._secret, connect=self.connect, validate=self.validate)
            except Exception as exc:  # A validator or adapter fault is a launch outcome, not a retry.
                box['value'] = dict(error='LAUNCH_RESULT_UNCERTAIN', http_posts_attempted='UNKNOWN',
                                    diagnostics=[dict(stage='TRANSPORT', code=type(exc).__name__, path='$')])
        thread = threading.Thread(target=run, name='mea-deepseek-request', daemon=True)
        thread.start()
        return thread, box

    def collect(self, handle, seconds, cancelled, heartbeat):
        thread, box = handle
        deadline = self.clock() + max(seconds, REQUEST_SECONDS) + GRACE_SECONDS
        while thread.is_alive():
            heartbeat()
            if cancelled():
                return dict(error='LIVE_CANCELLED_UNCERTAIN', http_posts_attempted='UNKNOWN')
            if self.clock() >= deadline:
                return dict(error='LIVE_DEADLINE_UNCERTAIN', http_posts_attempted='UNKNOWN')
            thread.join(0.05)
        heartbeat()
        return box['value']
