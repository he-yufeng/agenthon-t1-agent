"""Run the frozen synthetic suite using source copied from the actual image."""
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import resource
import shutil
import sys
import tempfile
import time
import unittest


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def options(path):
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        fields = line.split()
        if fields[4] == path:
            return set(fields[5].split(','))
    return set()


def test_ids(suite):
    result = []
    for item in suite:
        result.extend(test_ids(item) if isinstance(item, unittest.TestSuite) else [item.id()])
    return result


def main():
    started = time.monotonic()
    source = Path('/validation')
    manifest = json.loads((source / 'SOURCE_MANIFEST.json').read_text())
    checks = {
        'native_linux_x86_64': platform.system() == 'Linux' and platform.machine() == 'x86_64',
        'python_3_13': sys.version_info[:2] == (3, 13),
        'nonroot': os.getuid() == os.getgid() == 65534,
        'readonly_root': 'ro' in options('/'),
        'readonly_validation': 'ro' in options('/validation'),
        'tmp_noexec_nosuid_nodev': {'rw', 'noexec', 'nosuid', 'nodev'} <= options('/tmp'),
        'tmp_64_mib': os.statvfs('/tmp').f_blocks * os.statvfs('/tmp').f_frsize == 67108864,
        'nofile_1024': resource.getrlimit(resource.RLIMIT_NOFILE) == (1024, 1024),
        'nproc_256': resource.getrlimit(resource.RLIMIT_NPROC) == (256, 256),
    }
    image_sources = {name: sha(Path('/agent') / name) for name in ('agent.py', 'engine.py')}
    checks['image_matches_prepared_source'] = all(
        image_sources[name] == manifest['files']['submission/' + name] for name in image_sources)
    checks['all_prepared_inputs_match'] = all(sha(source / name) == expected
        for name, expected in manifest['files'].items())
    if not all(checks.values()):
        print('NATIVE_TEST_RESULT=' + json.dumps({'status':'FAIL', 'checks':checks}))
        return 1
    with tempfile.TemporaryDirectory(prefix='agenthon-native-', dir='/tmp') as raw:
        root = Path(raw)
        submission = root / 'submission'
        submission.mkdir()
        for name in ('agent.py', 'engine.py'):
            shutil.copyfile(Path('/agent') / name, submission / name)
        shutil.copytree(source / 'submission/tests', submission / 'tests')
        for name in ('Dockerfile', '.dockerignore'):
            shutil.copyfile(source / 'submission' / name, submission / name)
        shutil.copytree(source / 'diagnostics', root / 'diagnostics')
        sys.path.insert(0, str(submission))
        suite = unittest.defaultTestLoader.discover(str(submission / 'tests'))
        identities = test_ids(suite)
        logs = io.StringIO()
        result = unittest.TextTestRunner(stream=logs, verbosity=2).run(suite)
        report = {'status':'PASS' if result.wasSuccessful() and result.testsRun == 87
            and not result.skipped else 'FAIL', 'checks':checks,
            'image_sources':image_sources, 'tests_run':result.testsRun,
            'tests':identities, 'failures':result.failures, 'errors':result.errors,
            'skips':result.skipped, 'python':platform.python_version(),
            'machine':platform.machine(), 'wall_seconds':time.monotonic()-started,
            'official_score':None}
        print(logs.getvalue(), flush=True)
        print('NATIVE_TEST_RESULT=' + json.dumps(report, sort_keys=True), flush=True)
        return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
