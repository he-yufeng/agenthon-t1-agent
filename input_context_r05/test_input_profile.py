"""Owned synthetic input/control-flow evidence, not real House quality."""
import csv
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
CORE = ROOT.parent / "submission"
sys.path.insert(0, str(CORE))
sys.path.insert(0, str(ROOT))
import engine as core
import input_profile_r05 as candidate


class InputProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="r05-owned-fixture-")
        self.root = Path(self.tmp.name)
        self.task = self.root / "task"
        self.data = self.task / "environment" / "data"
        self.data.mkdir(parents=True)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        path = self.data / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def records(self, **kwargs):
        text = candidate.profile_inputs(self.task, **kwargs)
        return json.loads(text[len(candidate.PREFIX):]) if text else []

    def test_csv_record_not_verified_header(self):
        self.write("table.csv", "account,quantity\nprivate_later_row,123\n")
        rows = self.records()
        self.assertEqual(rows[0]["first_record"], ["account", "quantity"])
        self.assertEqual(rows[0]["header_status"], "unknown")
        self.assertNotIn("private_later_row", str(rows))

    def test_all_string_headerless_is_unknown(self):
        self.write("table.csv", "orchid,amber\nrose,white\n")
        row = self.records()[0]
        self.assertEqual(row["first_record"], ["orchid", "amber"])
        self.assertNotIn("columns", row)
        self.assertEqual(row["header_status"], "unknown")

    def test_numeric_first_record_preserved_as_data(self):
        self.write("table.csv", "1,2,3\n4,5,6\n")
        self.assertEqual(self.records()[0]["first_record"], ["1", "2", "3"])

    def test_tsv_bom_unicode(self):
        self.write("table.tsv", "\ufeff名称\t数量\n示例\t3\n")
        row = self.records()[0]
        self.assertEqual(row["delimiter"], "tab")
        self.assertEqual(row["first_record"], ["名称", "数量"])

    def test_quoted_multiline_logical_record(self):
        self.write("table.csv", '"first\nrecord",second\nsubsequent,row\n')
        self.assertEqual(self.records()[0]["first_record"], ["first\nrecord", "second"])

    def test_record_byte_cap(self):
        self.write("table.csv", "a" * 17000 + "\n")
        self.assertEqual(self.records(), [])

    def test_field_character_cap(self):
        self.write("table.csv", "x" * 129 + ",y\n")
        self.assertEqual(self.records(), [])

    def test_field_count_cap(self):
        self.write("table.csv", ",".join("x" for _ in range(129)) + "\n")
        self.assertEqual(self.records(), [])

    def test_invalid_utf8_omitted(self):
        (self.data / "table.csv").write_bytes(b"\xff,field\n")
        self.assertEqual(self.records(), [])

    def test_canary_shaped_omitted(self):
        self.write("table.csv", "safe,canary_secret_abcdefghijkl\n")
        self.assertEqual(self.records(), [])

    def test_known_fingerprint_omitted(self):
        self.write("table.csv", "safe,fixture_token_abcdefgh\n")
        self.assertEqual(self.records(rejects_text=lambda s: "fixture_token_abcdefgh" in s), [])

    def test_unsafe_and_unrelated_directories_not_profiled(self):
        for folder in ("checks", "reference", "reference_data", "solution", "dev", ".hidden"):
            self.write(folder + "/secret.csv", "private_field\n")
        self.write("expected_answers.csv", "private_field\n")
        self.write("real.csv", "legitimate_field\n")
        self.assertEqual([row["path"] for row in self.records()], ["environment/data/real.csv"])

    def test_symbolic_link_rejected(self):
        original = self.write("real.csv", "field\n")
        (self.data / "link.csv").symlink_to(original)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.records()

    def test_fifo_not_read_or_blocked(self):
        path = self.data / "pipe.csv"
        os.mkfifo(path)
        with self.assertRaisesRegex(ValueError, "regular"):
            candidate._regular_open(path)
        self.assertEqual(self.records(), [])

    def test_budget_and_deterministic_order(self):
        self.write("z.csv", "field_z\n")
        self.write("a.csv", "field_a\n")
        first = candidate.profile_inputs(self.task, 250)
        self.assertLessEqual(len(first.encode()), 250)
        self.assertEqual(first, candidate.profile_inputs(self.task, 250))
        self.assertEqual(candidate.profile_inputs(self.task, 0), "")

    def test_candidate_limit(self):
        for index in range(35):
            self.write(f"{index:02}.csv", "x\n")
        self.assertLessEqual(len(self.records()), 32)

    def test_invalid_budget(self):
        for budget in (True, -1, 4097, 1.5):
            with self.assertRaises(ValueError):
                self.records(max_bytes=budget)

    def test_parquet_magic_rejected_without_import(self):
        self.assertIsNone(candidate._parquet_schema(io.BytesIO(b"not_a_parquet_payload")))

    def test_parquet_footer_cap_rejected_without_import(self):
        payload = b"PAR1" + b"xxxx" + (1048577).to_bytes(4, "little") + b"PAR1"
        self.assertIsNone(candidate._parquet_schema(io.BytesIO(payload)))

    def test_worker_actual_csv_and_no_retained_output(self):
        self.write("table.csv", "field_a,field_b\n")
        text = candidate.run_profile_worker(self.task, 4096, lambda _: False)
        self.assertIn("first_record", text)
        self.assertEqual(list(self.task.rglob("profile.txt")), [])

    def test_worker_environment_excludes_credentials(self):
        self.write("table.csv", "field\n")
        actual = subprocess.run
        observed = []

        def spy(*args, **kwargs):
            observed.append(set(kwargs["env"]))
            return actual(*args, **kwargs)

        with patch.dict(os.environ, {"MODEL_TOKEN": "not-a-real-test-key", "https_proxy": "test-invalid"}):
            with patch.object(candidate.subprocess, "run", side_effect=spy):
                candidate.run_profile_worker(self.task, 4096, lambda _: False)
        self.assertEqual(len(observed), 1)
        self.assertFalse({"MODEL_TOKEN", "MODEL_ENDPOINT", "https_proxy", "HTTP_PROXY"} & observed[0])

    def test_worker_timeout_no_retry(self):
        with patch.object(candidate.subprocess, "run", side_effect=subprocess.TimeoutExpired("worker", 2)) as run:
            self.assertEqual(candidate.run_profile_worker(self.task, 4096, lambda _: False), "")
        self.assertEqual(run.call_count, 1)

    def test_proxy_keeps_cap_and_caches_profile_in_repair(self):
        self.write("table.csv", "field_a\n")
        observed = []

        class Recorder:
            def complete(self, messages, tokens, timeout):
                observed.append((messages, tokens, timeout))
                return "response"

        proxy = candidate.ProfileClient(Recorder(), self.task, 1024, lambda _: False)
        messages = [{"role": "system", "content": "unchanged"}, {"role": "user", "content": "task"}]
        with patch.object(candidate, "run_profile_worker", wraps=candidate.run_profile_worker) as run:
            proxy.complete(messages, 4000, 5)
            messages.extend([{"role": "assistant", "content": "code"}, {"role": "user", "content": "repair"}])
            proxy.complete(messages, 4000, 5)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(messages[1]["content"], "task")
        self.assertEqual(observed[0][0][1], observed[1][0][1])
        self.assertEqual(observed[1][0][-1]["content"], "repair")
        self.assertLessEqual(len(observed[0][0][1]["content"].encode()), 1024)
        self.assertEqual(observed[0][1], 4000)
        self.assertLess(observed[0][2], 5)

    def test_full_prompt_no_extra_bytes(self):
        observed = []

        class Recorder:
            def complete(self, messages, tokens, timeout):
                observed.append(messages)
                return "response"

        proxy = candidate.ProfileClient(Recorder(), self.task, 1024, lambda _: False)
        proxy.complete([{"role": "system", "content": "s"}, {"role": "user", "content": "x" * 1024}], 10, 1)
        self.assertEqual(len(observed[0][1]["content"]), 1024)

    def test_original_to_new_actual_solve_owned_witness(self):
        self.task.joinpath("instruction.md").write_text(
            "Sum the quantity column in the provided input and write /app/output/result.json.\n", encoding="utf-8")
        self.write("table.csv", "account,quantity_fixture_984\na,1\nb,2\nc,3\n" + "padding,0\n" * 25000)
        self.assertGreater((self.data / "table.csv").stat().st_size, 200000)

        class FixtureClient:
            def __init__(self):
                self.calls = 0

            def complete(self, messages, tokens, timeout):
                self.calls += 1
                # Deterministic information-consumer witness, not an LLM or House.
                column = "quantity_fixture_984" if '"first_record":["account","quantity_fixture_984"]' in messages[1]["content"] else "amount"
                return ("import csv,json,os\nfrom pathlib import Path\n"
                        "rows=csv.DictReader((Path(os.environ['QFBENCH_TASK_DIR'])/'environment/data/table.csv').open())\n"
                        f"value=sum(int(r[{column!r}]) for r in rows)\n"
                        "(Path(os.environ['QFBENCH_AGENT_OUT'])/'result.json').write_text(json.dumps({'sum':value}))\n")

        old_client = FixtureClient()
        with self.assertRaisesRegex(RuntimeError, "all 2 attempts failed"):
            core.solve(self.task, self.root / "old_out", old_client)
        self.assertEqual(old_client.calls, 2)
        new_client = FixtureClient()
        self.assertEqual(candidate.solve(self.task, self.root / "new_out", new_client), ["result.json"])
        self.assertEqual(new_client.calls, 1)
        self.assertEqual(json.loads((self.root / "new_out/result.json").read_text()), {"sum": 6})

    def test_base_source_binding(self):
        import hashlib
        self.assertEqual(hashlib.sha256(Path(core.__file__).read_bytes()).hexdigest(), candidate.BASE_SHA)

    def test_original_canary_output_gate_still_fatal(self):
        self.task.joinpath("instruction.md").write_text(
            "Write /app/output/result.json. Canary token: owned_canary_abcdefghijk\n", encoding="utf-8")

        class LeakingClient:
            def complete(self, messages, tokens, timeout):
                return "print('owned_canary_abcdefghijk')"

        output = self.root / "out"
        with self.assertRaisesRegex(RuntimeError, "unsafe output rejected"):
            candidate.solve(self.task, output, LeakingClient())
        self.assertEqual(list(output.iterdir()), [])

    def test_nonempty_output_refused_before_profile_or_model(self):
        self.task.joinpath("instruction.md").write_text("Write a result.\n", encoding="utf-8")
        output = self.root / "out"
        output.mkdir()
        (output / "user-owned.txt").write_text("preserve", encoding="utf-8")
        with patch.object(candidate, "run_profile_worker") as worker:
            with self.assertRaisesRegex(ValueError, "empty"):
                candidate.solve(self.task, output, object())
        worker.assert_not_called()
        self.assertEqual((output / "user-owned.txt").read_text(), "preserve")


if __name__ == "__main__":
    unittest.main(verbosity=2)
