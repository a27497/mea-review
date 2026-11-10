"""Pure GitHub review-comment wire contract. No transport/authentication capability."""
import re
from ..contract import fields,text,path

API_VERSION='2026-03-10'


def comment_request(target,payload):
    fields(target,'repository number');fields(payload,'commit_id path line side body')
    if not isinstance(target['repository'],str) or not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',target['repository']) or type(target['number']) is not int or target['number']<1:raise ValueError('GITHUB_TARGET_INVALID')
    if not isinstance(payload['commit_id'],str) or not re.fullmatch(r'[0-9a-f]{40}',payload['commit_id']):raise ValueError('GITHUB_COMMIT_ID_INVALID')
    path(payload['path']);text(payload['body'],8000)
    if not payload['body'].strip() or type(payload['line']) is not int or payload['line']<1 or payload['side'] not in {'LEFT','RIGHT'}:raise ValueError('GITHUB_LINE_COMMENT_INVALID')
    return dict(method='POST',api_origin='https://api.github.com',path=f'/repos/{target["repository"]}/pulls/{target["number"]}/comments',
                headers={'Accept':'application/vnd.github+json','X-GitHub-Api-Version':API_VERSION},json=dict(payload))
