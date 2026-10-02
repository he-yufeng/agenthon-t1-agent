"""Check a fixed publication allowlist; no secret files or task data are scanned."""
import hashlib
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
manifest = json.loads((root / 'SOURCE_MANIFEST.json').read_text())
expected = set(manifest['files']) | {'SOURCE_MANIFEST.json'}
actual = {p.relative_to(root).as_posix() for p in root.rglob('*')
          if p.is_file() and '.git' not in p.parts}
if actual != expected:
    raise SystemExit(f'Publication file set differs: extra={sorted(actual-expected)}, missing={sorted(expected-actual)}')
for name, digest in manifest['files'].items():
    path = root / name
    if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise SystemExit(f'Publication source mismatch: {name}')
print(f'Frozen publication files: {len(actual)}; credential/task paths absent.')
