"""Offline GitHub REST fixture: a self-owned synthetic repository served from memory."""
import base64
import difflib
import json

from mea.contract import git_blob_sha
from mea.github_input import Response, TransportError

OWNER, REPO, NUMBER = 'mea-owned', 'shop', 7
MERGE_BASE = '1' * 40
TARGET_BASE = '2' * 40
HEAD = '3' * 40
EARLY_HEAD = '4' * 40

BASE_FILES = {
    'shop/pricing.py': 'def total(items):\n    return sum(i.price for i in items)\n',
    'shop/cart.py': 'from shop.pricing import total\n\n\ndef checkout(cart):\n    return total(cart.items)\n',
    'tests/test_pricing.py': 'from shop.pricing import total\n\n\ndef test_total():\n    assert total([]) == 0\n',
    'web/format.ts': 'export function money(n: number) {\n  return n.toFixed(2);\n}\n',
    'web/view.ts': "import { money } from './format';\n\nexport const show = (n: number) => money(n);\n",
}
HEAD_FILES = dict(BASE_FILES)
HEAD_FILES['shop/pricing.py'] = ('def total(items, discount=None):\n    value = sum(i.price for i in items)\n'
                                 '    return value - discount\n')


def patch(before, after):
    rows = list(difflib.unified_diff(before.splitlines(keepends=True), after.splitlines(keepends=True)))
    return ''.join(rows[2:])


def contents(text):
    raw = text.encode() if isinstance(text, str) else text
    return dict(type='file', encoding='base64', size=len(raw), sha=git_blob_sha(raw),
                content=base64.b64encode(raw).decode())


class Fetcher:
    """Maps request paths to fixture responses; records every request."""

    def __init__(self, base=None, head=None, *, state='closed', merged=True, compare_files=None, overrides=None):
        self.base_files = dict(BASE_FILES if base is None else base)
        self.head_files = dict(HEAD_FILES if head is None else head)
        self.state = state
        self.merged = merged
        self.compare_files = compare_files
        self.overrides = overrides or {}
        self.calls = []

    def pull(self):
        return dict(number=NUMBER, state=self.state, merged=self.merged, title='Add discount',
                    body='Adds a discount argument.', head=dict(sha=HEAD), base=dict(sha=TARGET_BASE, ref='main'))

    def files(self):
        if self.compare_files is not None:
            return self.compare_files
        rows = []
        for path in sorted(set(self.base_files) | set(self.head_files)):
            before, after = self.base_files.get(path), self.head_files.get(path)
            if before == after:
                continue
            status = 'added' if before is None else 'removed' if after is None else 'modified'
            row = dict(filename=path, status=status, changes=1, patch=patch(before or '', after or ''))
            if after is not None:
                row['sha'] = git_blob_sha(after.encode())
            rows.append(row)
        return rows

    def routes(self):
        base = f'/repos/{OWNER}/{REPO}'
        result = {
            f'{base}/pulls/{NUMBER}': self.pull(),
            f'{base}/pulls/{NUMBER}/commits?per_page=100': [dict(sha=EARLY_HEAD), dict(sha=HEAD)],
            f'{base}/compare/{TARGET_BASE}...{HEAD}': dict(merge_base_commit=dict(sha=MERGE_BASE), files=self.files()),
            f'{base}/git/trees/{HEAD}?recursive=1': dict(truncated=False, tree=[
                dict(path=p, type='blob', size=len(t.encode()), sha=git_blob_sha(t.encode())) for p, t in self.head_files.items()]),
        }
        for ref, files in ((MERGE_BASE, self.base_files), (HEAD, self.head_files)):
            for path, text in files.items():
                result[f'{base}/contents/{path}?ref={ref}'] = contents(text)
        return result

    def get(self, path, accept):
        self.calls.append((path, accept))
        if path in self.overrides:
            value = self.overrides[path]
            if isinstance(value, Exception):
                raise value
            if isinstance(value, Response):
                return value
            return Response(200, json.dumps(value).encode())
        if path.startswith('/search/code'):
            return Response(200, json.dumps(dict(items=[dict(path='shop/cart.py')])).encode())
        routes = self.routes()
        if path not in routes:
            return Response(404, b'{"message":"Not Found"}')
        return Response(200, json.dumps(routes[path]).encode())


def transport(code='TimeoutError'):
    return TransportError(code)
