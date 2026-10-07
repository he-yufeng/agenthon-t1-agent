"""Own-data end-to-end path-composition reproduction, no model/network/checker."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parent


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run(engine):
    if Path("/app/data/prices.csv").exists():
        raise RuntimeError("Unexpected host input path; do not touch it")
    with tempfile.TemporaryDirectory(prefix="agenthon-join-r04-") as temporary:
        root = Path(temporary)
        task, output = root / "task", root / "out"
        data = task / "environment" / "data"
        data.mkdir(parents=True)
        (task / "instruction.md").write_text(
            "Read /app/data/prices.csv and write /output/result.json with total of value column.\n")
        (task / "card.toml").write_text("[agent]\ntimeout_sec = 60\n")
        (data / "prices.csv").write_text("value\n1\n2\n3\n")
        program = (
            "import csv,json,os\nfrom pathlib import Path\n"
            "p = os.path.join('/app/data', 'prices.csv')\n"
            "with open(p) as handle:\n"
            "    total = sum(float(row['value']) for row in csv.DictReader(handle))\n"
            "(Path(os.environ['QFBENCH_AGENT_OUT'])/'result.json').write_text(json.dumps({'total':total}))\n")

        class FixedClient:
            calls = 0

            def complete(self, messages, max_tokens, timeout_sec):
                self.calls += 1
                return program

        client = FixedClient()
        mapping = engine._legacy_input_path_map(task, (task / "instruction.md").read_text())
        transformed = engine.rewrite_legacy_input_literals(program, task, (task / "instruction.md").read_text())
        try:
            files = engine.solve(task, output, client)
            value = json.loads((output / "result.json").read_text())
            return {"status": "COMPLETED", "model_calls_are_fixed_local_stubs": client.calls,
                    "published_files": files, "actual_total": value["total"], "expected_total": 6.0,
                    "validated_legacy_directory_mapping": "/app/data" in mapping,
                    "program_transformed": transformed != program,
                    "real_house_calls": 0}
        except RuntimeError as exc:
            return {"status": "FAILED", "model_calls_are_fixed_local_stubs": client.calls,
                    "published_files": list(output.iterdir()), "fixed_error": str(exc),
                    "validated_legacy_directory_mapping": "/app/data" in mapping,
                    "program_transformed": transformed != program,
                    "real_house_calls": 0}


if __name__ == "__main__":
    original = load(ROOT / "engine_original.py", "join_original")
    print(json.dumps(run(original), default=str))
