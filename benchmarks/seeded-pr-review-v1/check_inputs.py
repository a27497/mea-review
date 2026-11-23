"""Run pinned upstream tests and independent regression assertions in private exports."""

import argparse
import io
import os
import subprocess
import tarfile
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from freeze import digest, git, read, write


def check(root, row, spec):
    case = row['case']
    repo = root / spec['clone']
    output = root / 'regression' / case
    output.mkdir(parents=True, exist_ok=True)
    python = root / 'deps/humanize-venv/bin/python'
    js_deps = root / 'deps/luxon'
    env = {'PATH': os.defpath + ':/usr/local/bin', 'HOME': str(output), 'TZ': 'UTC',
           'LC_ALL': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTEST_DISABLE_PLUGIN_AUTOLOAD': '1'}
    commands = read(root / 'assertions' / f'{case}.json')
    python_case = spec['clone'] == 'humanize-upstream'
    lock = root / 'deps/humanize.lock' if python_case else js_deps / 'package-lock.json'
    results = []

    def run(label, argv, checkout, expected=0, expected_text=None):
        log = subprocess.run(argv, cwd=checkout, env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        path = output / f'{label}.log'
        path.write_bytes(log.stdout)
        matched = log.returncode == expected and (expected_text is None or expected_text.encode() in log.stdout)
        results.append(dict(label=label, command=list(map(str, argv)), exit_code=log.returncode, expected=expected,
                            marker_matches=matched, log_sha256=digest(log.stdout)))

    phases = [('clean', row['clean'], 0)]
    if row['injected']:
        phases += [('injected', row['head'], 1), ('restored', row['clean'], 0)]
    for label, commit, assertion_exit in phases:
        with tempfile.TemporaryDirectory(dir=root) as temp:
            checkout = Path(temp)
            archive = git(repo, 'archive', '--format=tar', commit)
            with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
                tar.extractall(checkout, filter='data')
            if python_case:
                env['PYTHONPATH'] = str(checkout / 'src')
                (checkout / 'src/humanize/_version.py').write_text('__version__ = "0.0.0+local"\n')
                upstream = [python, '-m', 'pytest', '-q', '-c', '/dev/null', '-p', 'no:cacheprovider',
                            *commands['upstream']]
                custom = [python, root / 'assertions' / f'{case}.py']
            else:
                if (checkout / 'package-lock.json').read_bytes() != lock.read_bytes():
                    raise ValueError('LUXON_LOCK_MISMATCH')
                (checkout / 'node_modules').symlink_to(js_deps / 'node_modules')
                jest = checkout / 'node_modules/.bin/jest'
                upstream = [jest, '--runInBand', '--no-cache', '--runTestsByPath', *commands['upstream']]
                test_path = 'test/pilot-only.test.js'
                (checkout / test_path).write_bytes((root / 'assertions' / f'{case}.js').read_bytes())
                custom = [jest, '--runInBand', '--no-cache', '--runTestsByPath', test_path]
            if label != 'injected':
                run(label + '-upstream', upstream, checkout)
            assertion = root / 'assertions' / (case + ('.py' if python_case else '.js'))
            run(label + '-assertion', custom, checkout, assertion_exit,
                commands['failure_marker'] if assertion_exit else commands['pass_marker'])
    write(output / 'receipt.json', dict(case=case, lock_sha256=digest(lock.read_bytes()),
                                       assertion_sha256=digest(assertion.read_bytes()), phases=phases,
                                       python_generated_version_shim=python_case,
                                       status='PASS' if all(r['marker_matches'] for r in results) else 'FAIL',
                                       results=results))
    return all(r['marker_matches'] for r in results)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('private', type=Path)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.private.resolve(strict=True)
    plan = {spec['case']: spec for spec in read(root / 'sample-plan.json')}
    with ThreadPoolExecutor(max_workers=4) as executor:
        pending = [executor.submit(check, root, row, plan[row['case']]) for row in read(root / 'freeze.json')]
    return 0 if all([f.result() for f in pending]) else 1


if __name__ == '__main__':
    raise SystemExit(main())
