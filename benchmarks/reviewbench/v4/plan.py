"""M3 pre-registration checks: exclusion list, dev/holdout split and PLAN.json binding. No I/O beyond files."""
import hashlib
import json
from pathlib import Path

PLAN_SCHEMA = 'mea-m3-plan-v1'
EXCLUSIONS_SCHEMA = 'mea-m3-exclusions-v1'
SETS = ('dev', 'holdout')
MIN_HOLDOUT = 30
PERMISSIVE = {'MIT', 'BSD-2-Clause', 'BSD-3-Clause', 'Apache-2.0', 'ISC', '0BSD', 'Unlicense', 'MIT-0', 'PSF-2.0'}
LANG_EXT = ('.py', '.js', '.ts', '.tsx', '.jsx', '.mjs', '.cjs', '.mts', '.cts')


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def ref(sample):
    return f"{sample['repository']}#{sample['number']}"


def excluded_reason(sample, exclusions):
    """Why a sample is excluded, or None. Repository names compare case-insensitively."""
    if exclusions.get('schema') != EXCLUSIONS_SCHEMA:
        raise ValueError('EXCLUSIONS_SCHEMA')
    repos = {r.lower() for r in exclusions['repositories']}
    prs = {p.lower() for p in exclusions['pull_requests']}
    if ref(sample).lower() in prs:
        return 'EXCLUDED_PULL_REQUEST'
    if sample['repository'].lower() in repos:
        return 'EXCLUDED_REPOSITORY'
    return None


def check_samples(samples, exclusions):
    """Raise on any excluded sample, duplicate PR, cross-set repository or admission failure."""
    seen, repo_set = {}, {}
    for name in SETS:
        for s in samples[name]:
            why = excluded_reason(s, exclusions)
            if why:
                raise ValueError(f'{why}:{ref(s)}')
            key = ref(s).lower()
            if key in seen:
                raise ValueError(f'DUPLICATE_SAMPLE:{ref(s)}')
            seen[key] = name
            other = repo_set.setdefault(s['repository'].lower(), name)
            if other != name:
                raise ValueError(f'REPOSITORY_IN_BOTH_SETS:{s["repository"]}')
            if s['license'] not in PERMISSIVE:
                raise ValueError(f'LICENSE:{ref(s)}')
            if not (s['changed_files'] <= 15 and s['changed_lines'] <= 500):
                raise ValueError(f'SIZE_BOUND:{ref(s)}')
            if not s['gold_defects'] >= 1:
                raise ValueError(f'NO_GOLD:{ref(s)}')
            if len(s['pin']) != 40:
                raise ValueError(f'PIN:{ref(s)}')
    if len(samples['holdout']) < MIN_HOLDOUT:
        raise ValueError('HOLDOUT_TOO_SMALL')
    return True


def load_plan(path, expected_sha256=None):
    raw = Path(path).read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if expected_sha256 is not None and actual != expected_sha256:
        raise ValueError('PLAN_SHA256_MISMATCH')
    plan = json.loads(raw)
    if plan.get('schema') != PLAN_SCHEMA:
        raise ValueError('PLAN_SCHEMA')
    return plan, actual


def holdout_admission(plan, plan_sha256, approval):
    """A holdout run needs a frozen plan and a coordinator approval naming this exact plan digest."""
    if plan.get('status') != 'FROZEN':
        raise ValueError('PLAN_NOT_FROZEN')
    if not isinstance(approval, dict) or approval.get('plan_sha256') != plan_sha256 or not approval.get('coordinator_reply'):
        raise ValueError('HOLDOUT_APPROVAL_REQUIRED')
    return True
