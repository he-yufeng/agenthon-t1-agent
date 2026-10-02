"""Record actual native-test evidence and registry digest after a successful push."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

lines = Path('native-test.log').read_text().splitlines()
summaries = [json.loads(line.split('=',1)[1]) for line in lines if line.startswith('NATIVE_TEST_RESULT=')]
if len(summaries) != 1 or summaries[0]['status'] != 'PASS':
    raise SystemExit('Native image tests lack exactly one successful result')
image = json.loads(Path('image-inspect.json').read_text())[0]
digests = [ref for ref in image['RepoDigests'] if ref.startswith('ghcr.io/he-yufeng/agenthon-t1-agent@sha256:')]
if len(digests) != 1:
    raise SystemExit('Published image lacks one matching registry digest')
report = {'schema':'agenthon-native-release/v1', 'created_at_utc':datetime.now(timezone.utc).isoformat(),
    'source_commit':os.environ['GITHUB_SHA'], 'repository':os.environ['GITHUB_REPOSITORY'],
    'run_id':os.environ['GITHUB_RUN_ID'], 'image_id':image['Id'], 'registry_reference':digests[0],
    'architecture':image['Os']+'/'+image['Architecture'], 'native_tests':summaries[0],
    'native_log_sha256':hashlib.sha256(Path('native-test.log').read_bytes()).hexdigest(),
    'source_manifest_sha256':hashlib.sha256(Path('SOURCE_MANIFEST.json').read_bytes()).hexdigest(),
    'anonymous_pull_verified':False, 'official_score':None, 'new_spend_cny':0}
with Path('release-receipt.json').open('x') as handle:
    json.dump(report, handle, indent=2, sort_keys=True)
    handle.write('\n')
