"""Exercise actual image sources on owned fixtures, no selected-House call."""
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
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if fields[4] == path:
            return set(fields[5].split(","))
    return set()


def main():
    started = time.monotonic()
    source = Path("/validation")
    manifest = json.loads((source / "SOURCE_MANIFEST.json").read_text())
    original_cli = (source / "submission/agent.py").read_bytes()
    expected_cli = original_cli.replace(
        b"from engine import EndpointError, OpenAICompatibleClient, solve",
        b"from engine import EndpointError, solve\nfrom thinking_control import NonThinkingHouseClient as OpenAICompatibleClient")
    checks = {
        "native_linux_x86_64": platform.system()=="Linux" and platform.machine()=="x86_64",
        "python_3_13": sys.version_info[:2]==(3,13),
        "nonroot": os.getuid()==os.getgid()==65534,
        "readonly_root": "ro" in options("/"),
        "readonly_validation": "ro" in options("/validation"),
        "tmp_noexec_nosuid_nodev": {"rw","noexec","nosuid","nodev"} <= options("/tmp"),
        "tmp_64_mib": os.statvfs("/tmp").f_blocks*os.statvfs("/tmp").f_frsize==67108864,
        "nofile_1024": resource.getrlimit(resource.RLIMIT_NOFILE)==(1024,1024),
        "nproc_256": resource.getrlimit(resource.RLIMIT_NPROC)==(256,256),
        "cli_only_client_import_differs": Path("/agent/agent.py").read_bytes()==expected_cli,
        "review_strategy_absent": not Path("/agent/review_solver.py").exists(),
        "exact_original_core": sha("/agent/engine.py")=="1c85bae327299e6eeacf7f1c788dfd5bf93d8f3c67a45e723280f37aaa4bef3a",
    }
    mapping={"engine.py":"submission/engine.py",
             "agent.py":"candidate_nonthinking_base_r03/agent.py",
             "thinking_control.py":"candidate_nonthinking_base_r03/thinking_control.py"}
    image_sources={name:sha(Path("/agent")/name) for name in mapping}
    checks["image_matches_prepared_source"]=all(image_sources[n]==manifest["files"][p] for n,p in mapping.items())
    checks["all_prepared_inputs_match"]=all(sha(source/n)==v for n,v in manifest["files"].items())
    if not all(checks.values()):
        print("NONTHINKING_TEST_RESULT="+json.dumps({"status":"FAIL","checks":checks}))
        return 1
    with tempfile.TemporaryDirectory(prefix="nonthinking-native-",dir="/tmp") as raw:
        root=Path(raw)
        core=root/"release_2026-10-02_r00/submission"
        core.mkdir(parents=True)
        candidate=root/"candidate_nonthinking_base_r03"
        candidate.mkdir()
        shutil.copyfile("/agent/engine.py",core/"engine.py")
        for name in ("agent.py","thinking_control.py"):
            shutil.copyfile(Path("/agent")/name,candidate/name)
        for name in ("test_baseline_cli.py","test_thinking_control.py"):
            shutil.copyfile(source/"candidate_nonthinking_base_r03"/name,candidate/name)
        sys.path.insert(0,str(core))
        sys.path.insert(0,str(candidate))
        suite=unittest.defaultTestLoader.discover(str(candidate),pattern="test_*.py")
        logs=io.StringIO()
        result=unittest.TextTestRunner(stream=logs,verbosity=2).run(suite)
        report={"status":"PASS" if result.wasSuccessful() and result.testsRun==10 and not result.skipped else "FAIL",
                "checks":checks,"image_sources":image_sources,"tests_run":result.testsRun,
                "failures":result.failures,"errors":result.errors,"skips":result.skipped,
                "python":platform.python_version(),"machine":platform.machine(),
                "wall_seconds":time.monotonic()-started,"real_house_calls":0,
                "official_score":None,"real_task_performance_verified":False}
        print(logs.getvalue(),flush=True)
        print("NONTHINKING_TEST_RESULT="+json.dumps(report,sort_keys=True),flush=True)
        return 0 if report["status"]=="PASS" else 1


if __name__=="__main__":
    raise SystemExit(main())
