"""Validate the actual review image with owned synthetic fixtures, no House call."""
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
    mapping = {'agent.py':'candidate_review_r01/agent.py',
               'engine.py':'submission/engine.py',
               'review_solver.py':'candidate_review_r01/review_solver.py'}
    image_sources = {name: sha(Path('/agent') / name) for name in mapping}
    checks['image_matches_prepared_source'] = all(
        image_sources[name] == manifest['files'][prepared] for name, prepared in mapping.items())
    checks['all_prepared_inputs_match'] = all(sha(source / name) == expected
                                             for name, expected in manifest['files'].items())
    if not all(checks.values()):
        print('REVIEW_TEST_RESULT=' + json.dumps({'status':'FAIL', 'checks':checks}))
        return 1
    with tempfile.TemporaryDirectory(prefix='review-native-', dir='/tmp') as raw:
        root = Path(raw)
        core = root / 'submission'
        core.mkdir()
        candidate = root / 'candidate_review_r01'
        candidate.mkdir()
        # Every runtime module/CLI below comes from /agent, not /validation.
        shutil.copyfile('/agent/engine.py', core / 'engine.py')
        for name in ('agent.py', 'review_solver.py'):
            shutil.copyfile(Path('/agent') / name, candidate / name)
        shutil.copyfile(source / 'candidate_review_r01/test_review_solver.py',
                        candidate / 'test_review_solver.py')
        sys.path.insert(0, str(core))
        sys.path.insert(0, str(candidate))
        suite = unittest.defaultTestLoader.discover(str(candidate), pattern='test_review_solver.py')
        logs = io.StringIO()
        result = unittest.TextTestRunner(stream=logs, verbosity=2).run(suite)
        report = {'status':'PASS' if result.wasSuccessful() and result.testsRun == 22
                  and not result.skipped else 'FAIL', 'checks':checks,
                  'image_sources':image_sources, 'tests_run':result.testsRun,
                  'failures':result.failures, 'errors':result.errors, 'skips':result.skipped,
                  'python':platform.python_version(), 'machine':platform.machine(),
                  'wall_seconds':time.monotonic()-started, 'real_house_calls':0,
                  'official_score':None, 'real_task_performance_verified':False}
        print(logs.getvalue(), flush=True)
        print('REVIEW_TEST_RESULT=' + json.dumps(report, sort_keys=True), flush=True)
        return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
