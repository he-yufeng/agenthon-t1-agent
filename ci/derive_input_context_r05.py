"""Generate or verify the explicit public-source manifest, never task content."""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "submission/agent.py", "submission/engine.py", "LICENSE",
    "input_context_r05/input_profile_r05.py", "input_context_r05/test_input_profile.py",
    "input_context_r05/test_parquet_native.py", "input_context_r05/PUBLIC_PROTOCOL.md",
    "ci/input_context_r05.Dockerfile", "ci/input_context_r05.Dockerfile.dockerignore", "ci/derive_input_context_r05.py",
    "ci/test_input_context_r05_image.py", "ci/record_input_context_r05.py", ".github/workflows/publish.yml",
)


def derive():
    hashes = {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in FILES}
    if hashes["submission/engine.py"] != "1c85bae327299e6eeacf7f1c788dfd5bf93d8f3c67a45e723280f37aaa4bef3a":
        raise RuntimeError("frozen original engine changed")
    return {"schema": "input-context-r05-public-source/v1", "files": hashes,
            "standard_library_tests": 28, "mandatory_native_parquet_tests": 1,
            "real_house_quality_known": False,
            "base_image": "ghcr.io/he-yufeng/agenthon-t1-agent@sha256:0f74d9bdd86d8ed19e7387d3bd2144c1a956602a41390ec70df55f1a6e86da8e"}


if __name__ == "__main__":
    expected = derive()
    path = ROOT / "R05_SOURCE_MANIFEST.json"
    if sys.argv[1:] == ["--write"]:
        path.write_text(json.dumps(expected, indent=2) + "\n", encoding="utf-8")
        print("R05_MANIFEST_CREATED", len(FILES))
    elif sys.argv[1:] == ["--verify"]:
        if json.loads(path.read_text()) != expected:
            raise RuntimeError("source manifest mismatch")
        print("R05_MANIFEST_VERIFIED", len(FILES))
    else:
        raise SystemExit("Use --write or --verify")
