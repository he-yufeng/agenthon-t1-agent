"""Actual-image synthetic admission evidence; never an official quality score."""
import hashlib
import importlib.util
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
from unittest.mock import patch


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def mount_options(path):
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if fields[4] == path:
            return set(fields[5].split(","))
    return set()


def ids(suite):
    found = []
    for item in suite:
        found.extend(ids(item) if isinstance(item, unittest.TestSuite) else [item.id()])
    return found


def main():
    started = time.monotonic()
    source = Path("/validation")
    manifest = json.loads((source / "R05_SOURCE_MANIFEST.json").read_text())
    checks = {
        "linux_x86_64": platform.system() == "Linux" and platform.machine() == "x86_64",
        "python_3_13": sys.version_info[:2] == (3, 13),
        "nonroot": os.getuid() == os.getgid() == 65534,
        "readonly_root": "ro" in mount_options("/"),
        "readonly_validation": "ro" in mount_options("/validation"),
        "tmp_noexec_nosuid_nodev": {"rw", "noexec", "nosuid", "nodev"} <= mount_options("/tmp"),
        "tmp_64_mib": os.statvfs("/tmp").f_blocks * os.statvfs("/tmp").f_frsize == 67108864,
        "nofile1024": resource.getrlimit(resource.RLIMIT_NOFILE) == (1024, 1024),
        "nproc256": resource.getrlimit(resource.RLIMIT_NPROC) == (256, 256),
        "all_prepared_source_hashes": all(sha(source / name) == value for name, value in manifest["files"].items()),
        "original_image_engine": sha("/agent/engine.py") == manifest["files"]["submission/engine.py"],
        "actual_image_cli": sha("/agent/agent.py") == manifest["files"]["submission/agent.py"],
        "actual_image_profiler": sha("/agent/input_profile_r05.py") == manifest["files"]["input_context_r05/input_profile_r05.py"],
    }
    if not all(checks.values()):
        print("NATIVE_R05_RESULT=" + json.dumps({"status": "FAIL", "checks": checks}), flush=True)
        return 1
    with tempfile.TemporaryDirectory(prefix="r05-image-controls-", dir="/tmp") as raw:
        root = Path(raw)
        submission = root / "submission"
        folder = root / "input_context_r05"
        submission.mkdir()
        folder.mkdir()
        for name in ("engine.py", "agent.py"):
            shutil.copyfile(Path("/agent") / name, submission / name)
        shutil.copyfile("/agent/input_profile_r05.py", folder / "input_profile_r05.py")
        for name in ("test_input_profile.py", "test_parquet_native.py"):
            shutil.copyfile(source / "input_context_r05" / name, folder / name)
        sys.path[:0] = [str(submission), str(folder)]
        modules = []
        for name, count in (("test_input_profile", 28), ("test_parquet_native", 1)):
            spec = importlib.util.spec_from_file_location(name, folder / (name + ".py"))
            module = importlib.util.module_from_spec(spec)
            sys.modules[name] = module
            spec.loader.exec_module(module)
            suite = unittest.defaultTestLoader.loadTestsFromModule(module)
            if suite.countTestCases() != count:
                raise RuntimeError("frozen suite identity/count changed")
            modules.append(suite)
        suite = unittest.TestSuite(modules)
        identities = ids(suite)
        logs = io.StringIO()
        result = unittest.TextTestRunner(stream=logs, verbosity=2).run(suite)
        spec = importlib.util.spec_from_file_location("actual_image_cli_r05", "/agent/agent.py")
        cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cli)
        import input_profile_r05
        checks["actual_cli_solver_binding"] = cli.solve is input_profile_r05.solve
        owned = root / "cli_fixture"
        (owned / "environment/data").mkdir(parents=True)
        (owned / "instruction.md").write_text("Sum quantity in table.csv; write /app/output/result.json.\n")
        (owned / "environment/data/table.csv").write_text("account,quantity_cli_496\na,2\nb,5\n" + "padding,0\n" * 25000)
        out = root / "cli_out"

        class Client:
            calls = 0
            def complete(self, messages, max_tokens, timeout_sec):
                self.calls += 1
                if '"first_record":["account","quantity_cli_496"]' not in messages[1]["content"]:
                    raise RuntimeError("native CLI omitted eligible input context")
                return ("import os,csv,json\nfrom pathlib import Path\n"
                        "p=Path(os.environ['QFBENCH_TASK_DIR'])/'environment/data/table.csv'\n"
                        "v=sum(int(r['quantity_cli_496']) for r in csv.DictReader(p.open()))\n"
                        "(Path(os.environ['QFBENCH_AGENT_OUT'])/'result.json').write_text(json.dumps({'sum':v}))\n")

        client = Client()
        with patch.object(cli, "OpenAICompatibleClient", return_value=client):
            exit_code = cli.main(["solve", "--task-dir", str(owned), "--out", str(out)])
        checks["actual_cli_owned_solve"] = exit_code == 0 and client.calls == 1 and json.loads((out / "result.json").read_text()) == {"sum": 7}
        status = "PASS" if result.wasSuccessful() and result.testsRun == 29 and not result.skipped and all(checks.values()) else "FAIL"
        print(logs.getvalue(), flush=True)
        report = {"status": status, "checks": checks, "tests_run": result.testsRun,
                  "tests": identities, "failures": [(name.id(), trace) for name, trace in result.failures],
                  "errors": [(name.id(), trace) for name, trace in result.errors], "skips": result.skipped,
                  "python": platform.python_version(), "machine": platform.machine(),
                  "pyarrow": sys.modules["pyarrow"].__version__, "wall_seconds": time.monotonic() - started,
                  "real_house_calls": 0, "official_score": None,
                  "scope": "ACTUAL_IMAGE_29_OWNED_TESTS_AND_CLI_NOT_MODEL_QUALITY"}
        print("NATIVE_R05_RESULT=" + json.dumps(report, sort_keys=True), flush=True)
        return 0 if status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
