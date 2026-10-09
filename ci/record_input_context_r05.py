"""Record the single native admission and immutable published-image identity."""
import hashlib
import json
import os
from pathlib import Path

rows = [line.removeprefix("NATIVE_R05_RESULT=") for line in Path("native-r05.log").read_text().splitlines()
        if line.startswith("NATIVE_R05_RESULT=")]
if len(rows) != 1:
    raise RuntimeError("exactly one native result required")
native = json.loads(rows[0])
image = json.loads(Path("image-inspect-r05.json").read_text())[0]
if native["status"] != "PASS" or native["tests_run"] != 29 or native["skips"] or not all(native["checks"].values()):
    raise RuntimeError("native admission not proven")
if image["Config"].get("Volumes") not in (None, {}):
    raise RuntimeError("inherited volume forbidden for Final")
digests = image["RepoDigests"]
if len(digests) != 1 or not digests[0].startswith("ghcr.io/he-yufeng/agenthon-t1-agent@sha256:"):
    raise RuntimeError("published digest identity ambiguous")
report = {"schema": "r05-native-published-admission/v1", "commit": os.environ["GITHUB_SHA"],
          "run_id": os.environ["GITHUB_RUN_ID"], "image": digests[0], "image_id": image["Id"],
          "no_inherited_volume": True, "native_tests": 29,
          "source_manifest_sha256": hashlib.sha256(Path("R05_SOURCE_MANIFEST.json").read_bytes()).hexdigest(),
          "real_house_calls": 0, "official_score": None, "official_submission": False,
          "scope": "ACTUAL_IMAGE_ADMISSION_NOT_MODEL_QUALITY"}
Path("r05-release-receipt.json").write_text(json.dumps(report, indent=2) + "\n")
