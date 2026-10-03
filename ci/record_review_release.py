"""Record a new candidate image only after native strategy validation and push."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

lines = Path('review-test.log').read_text().splitlines()
results = [json.loads(line.split('=',1)[1]) for line in lines if line.startswith('REVIEW_TEST_RESULT=')]
if len(results) != 1 or results[0]['status'] != 'PASS' or results[0]['tests_run'] != 22:
    raise SystemExit('Actual image needs exactly one successful 22-test strategy result')
image = json.loads(Path('image-inspect.json').read_text())[0]
digests = [ref for ref in image['RepoDigests'] if ref.startswith('ghcr.io/he-yufeng/agenthon-t1-agent@sha256:')]
if len(digests) != 1:
    raise SystemExit('Candidate image lacks one matching registry digest')
report = {'schema':'agenthon-review-native-release/v1', 'created_at_utc':datetime.now(timezone.utc).isoformat(),
          'source_commit':os.environ['GITHUB_SHA'], 'repository':os.environ['GITHUB_REPOSITORY'],
          'run_id':os.environ['GITHUB_RUN_ID'], 'image_id':image['Id'], 'registry_reference':digests[0],
          'architecture':image['Os']+'/'+image['Architecture'], 'native_tests':results[0],
          'base_registry_digest':'sha256:0f74d9bdd86d8ed19e7387d3bd2144c1a956602a41390ec70df55f1a6e86da8e',
          'source_manifest_sha256':hashlib.sha256(Path('SOURCE_MANIFEST.json').read_bytes()).hexdigest(),
          'anonymous_pull_verified':False, 'official_submission_performed':False,
          'official_score':None, 'new_service_spend_cny':0}
with Path('review-release-receipt.json').open('x') as handle:
    json.dump(report, handle, indent=2, sort_keys=True)
    handle.write('\n')
