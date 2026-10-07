"""Emit source-only manifest to stdout; application uses apply_patch."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
files = {}
for p in sorted(root.rglob('*')):
    if '.git' in p.parts or p == root / 'SOURCE_MANIFEST.json':
        continue
    if p.is_symlink():
        raise RuntimeError('Source symlink')
    if p.is_file():
        files[p.relative_to(root).as_posix()] = hashlib.sha256(p.read_bytes()).hexdigest()
print(json.dumps({
    'schema': 'agenthon-publication-source/v1',
    'scope': 'R04_CORE_JOIN_CORRECTNESS_ONLY_NOT_MODEL_QUALITY',
    'files': files,
    'candidate_engine_sha256': files['submission/engine.py'],
    'original_core_sha256': files['r04/engine_original.py'],
    'base_registry_digest': 'sha256:0f74d9bdd86d8ed19e7387d3bd2144c1a956602a41390ec70df55f1a6e86da8e',
    'expected_synthetic_tests': 105, 'original_unchanged_suite': 87,
    'new_regressions': 18, 'competition_task_data': False,
    'credential_files': False, 'participant_license': 'MIT', 'official_score': None,
}, indent=2))
