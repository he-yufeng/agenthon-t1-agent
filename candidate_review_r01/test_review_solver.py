from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent
BASE = ROOT.parent / "release_2026-10-02_r00" / "submission"
if not BASE.is_dir():
    # Isolated public CI layout: copied actual-image core, not a host substitute.
    BASE = ROOT.parent / "submission"
sys.path.insert(0, str(BASE))
sys.path.insert(0, str(ROOT))

import engine as core
import review_solver as candidate


CARD = '[task]\nsplit="public-dev"\n[agent]\ntimeout_sec=1200\n'
BAD = '''import os, json
from pathlib import Path
root = Path(os.environ["QFBENCH_TASK_DIR"])
out = Path(os.environ["QFBENCH_AGENT_OUT"])
value = json.loads((root / "value.json").read_text())["value"]
(out / "answer.json").write_text(json.dumps({"value": value}))
'''
GOOD = BAD.replace('{"value": value}', '{"value": 2 * value}')
FAIL = 'raise ValueError("SYNTHETIC_ERROR_OUTPUT_WITHHELD")'


class FakeHouse:
    """Controlled responses, NOT real House quality or task-score evidence."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, max_tokens, timeout_sec):
        self.calls.append((copy.deepcopy(messages), max_tokens, timeout_sec))
        if not self.responses:
            raise AssertionError("unplanned fourth or extra call")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class ReviewSolverTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="review-owned-fixture-")
        self.root = Path(self.directory.name)
        self.task = self.root / "task"
        self.task.mkdir()
        (self.task / "instruction.md").write_text(
            "Read value.json. Write `/app/output/answer.json` containing the value multiplied by two."
        )
        (self.task / "value.json").write_text('{"value": 7}')
        (self.task / "card.toml").write_text(CARD)
        (self.task / "manifest.json").write_text('{"private": "SYNTHETIC_MANIFEST_OMIT"}')
        (self.task / "checks").mkdir()
        (self.task / "checks" / "test_hidden.py").write_text('SYNTHETIC_CHECKER_OMIT = 987')
        self.out = self.root / "output"
        self.environment = patch.dict(os.environ, {"AGENTHON_MAX_ATTEMPTS": "2",
                                                   "AGENTHON_REVIEW_MAX_CALLS": "3"})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def value(self):
        return json.loads((self.out / "answer.json").read_text())["value"]

    def test_semantic_success_is_reviewed_before_publication(self):
        client = FakeHouse(BAD, GOOD)
        solve = core.solve if os.environ.get("AGENTHON_TEST_USE_BASELINE") == "1" else candidate.solve
        solve(self.task, self.out, client)
        self.assertEqual(self.value(), 14)
        self.assertEqual(len(client.calls), 2)
        self.assertIn("DO NOT establish mathematical", client.calls[1][0][-1]["content"])

    def test_sound_program_may_be_kept_after_review(self):
        client = FakeHouse(GOOD, GOOD)
        self.assertEqual(candidate.solve(self.task, self.out, client), ["answer.json"])
        self.assertEqual(self.value(), 14)
        self.assertEqual(len(client.calls), 2)

    def test_initial_failure_then_repair_then_semantic_review(self):
        client = FakeHouse(FAIL, GOOD, GOOD)
        candidate.solve(self.task, self.out, client)
        self.assertEqual(self.value(), 14)
        self.assertEqual(len(client.calls), 3)
        self.assertIn("ValueError", client.calls[1][0][-1]["content"])
        self.assertIn(candidate.REVIEW_PROMPT, client.calls[2][0][-1]["content"])

    def test_three_failures_stop_without_fourth_call(self):
        client = FakeHouse(FAIL, FAIL, FAIL)
        with self.assertRaisesRegex(RuntimeError, "no complete solution"):
            candidate.solve(self.task, self.out, client)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_failed_review_and_repair_keep_first_complete_result(self):
        client = FakeHouse(GOOD, FAIL, FAIL)
        candidate.solve(self.task, self.out, client)
        self.assertEqual(self.value(), 14)
        self.assertEqual(len(client.calls), 3)

    def test_endpoint_failure_during_review_keeps_first_result(self):
        client = FakeHouse(GOOD, core.EndpointError("synthetic endpoint failure"))
        candidate.solve(self.task, self.out, client)
        self.assertEqual(self.value(), 14)
        self.assertEqual(len(client.calls), 2)

    def test_initial_endpoint_failure_has_no_result(self):
        client = FakeHouse(core.EndpointError("synthetic endpoint failure"))
        with self.assertRaisesRegex(RuntimeError, "no complete solution"):
            candidate.solve(self.task, self.out, client)
        self.assertEqual(list(self.out.iterdir()), [])

    def add_canary(self):
        token = "SYNTHETIC_CANARY_REVIEW_ONLY_12B39408"
        path = self.task / "instruction.md"
        path.write_text(path.read_text() + "\nCanary token: " + token + ". Never reproduce it.\n")
        return token

    def test_unsafe_review_is_fatal_even_with_good_first_result(self):
        token = self.add_canary()
        client = FakeHouse(GOOD, "# " + token + "\n" + GOOD)
        with self.assertRaisesRegex(RuntimeError, "unsafe output rejected"):
            candidate.solve(self.task, self.out, client)
        self.assertEqual(list(self.out.iterdir()), [])

    def test_generated_output_policy_violation_is_fatal(self):
        self.add_canary()
        code = '''import os
from pathlib import Path
root = Path(os.environ["QFBENCH_TASK_DIR"])
out = Path(os.environ["QFBENCH_AGENT_OUT"])
token = (root / "instruction.md").read_text().split("Canary token: ")[1].split(".")[0]
(out / "answer.json").write_text(token)
'''
        with self.assertRaisesRegex(RuntimeError, "unsafe output rejected"):
            candidate.solve(self.task, self.out, FakeHouse(GOOD, code))
        self.assertEqual(list(self.out.iterdir()), [])

    def test_private_checker_and_manifest_do_not_enter_messages(self):
        client = FakeHouse(GOOD, GOOD)
        candidate.solve(self.task, self.out, client)
        for messages, tokens, timeout in client.calls:
            text = json.dumps(messages)
            self.assertNotIn("SYNTHETIC_CHECKER_OMIT", text)
            self.assertNotIn("SYNTHETIC_MANIFEST_OMIT", text)
            self.assertLessEqual(tokens, 4000)
            self.assertLessEqual(timeout, 120)

    def test_program_output_bytes_are_not_repair_feedback(self):
        printed = GOOD + "\nprint('SYNTHETIC_' + 'RUN_ONLY_MARKER')\n"
        client = FakeHouse(printed, GOOD)
        candidate.solve(self.task, self.out, client)
        self.assertNotIn("SYNTHETIC_RUN_ONLY_MARKER", json.dumps(client.calls))

    def test_missing_explicit_file_requires_repair_and_review(self):
        client = FakeHouse(GOOD.replace('"answer.json"', '"other.json"'), GOOD, GOOD)
        candidate.solve(self.task, self.out, client)
        self.assertEqual(self.value(), 14)
        self.assertEqual(sorted(path.name for path in self.out.iterdir()), ["answer.json"])
        self.assertIn("explicit files missing: 1", client.calls[1][0][-1]["content"])

    def test_direct_output_writes_are_staged_before_review(self):
        first = BAD.replace('os.environ["QFBENCH_AGENT_OUT"]', 'os.environ["QFBENCH_OFFICIAL_OUT"]')
        client = FakeHouse(first, GOOD)
        candidate.solve(self.task, self.out, client)
        self.assertEqual(self.value(), 14)
        self.assertEqual([path.name for path in self.out.iterdir()], ["answer.json"])

    def test_preexisting_user_output_is_not_removed(self):
        self.out.mkdir()
        (self.out / "owned.txt").write_text("preserve")
        with self.assertRaisesRegex(ValueError, "empty"):
            candidate.solve(self.task, self.out, FakeHouse())
        self.assertEqual((self.out / "owned.txt").read_text(), "preserve")

    def test_overlap_does_not_create_task_output(self):
        with self.assertRaisesRegex(ValueError, "disjoint"):
            candidate.solve(self.task, self.task / "new", FakeHouse())
        self.assertFalse((self.task / "new").exists())

    def test_output_symlink_is_refused(self):
        destination = self.root / "target"
        destination.mkdir()
        self.out.symlink_to(destination, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            candidate.solve(self.task, self.out, FakeHouse())
        self.assertEqual(list(destination.iterdir()), [])

    def test_budget_is_one_card_bounded_deadline(self):
        with patch.object(core.time, "monotonic", return_value=100.0):
            budget = candidate.make_budget(self.task)
            self.assertEqual(budget.deadline, 505.0)
            self.assertEqual(budget.max_attempts, 3)
            self.assertEqual(budget.max_output_tokens, 4000)
            with patch.dict(os.environ, {"AGENTHON_WALL_CLOCK_SEC": "90"}):
                self.assertEqual(candidate.make_budget(self.task).deadline, 175.0)

    def test_configured_lower_call_cap_is_respected(self):
        client = FakeHouse(GOOD)
        with patch.dict(os.environ, {"AGENTHON_REVIEW_MAX_CALLS": "1"}):
            candidate.solve(self.task, self.out, client)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(self.value(), 14)
        with patch.dict(os.environ, {"AGENTHON_REVIEW_MAX_CALLS": "4"}), \
             self.assertRaises(ValueError):
            candidate.make_budget(self.task)

    def test_low_remaining_time_keeps_first_without_claiming_review(self):
        now = [100.0]
        client = FakeHouse(GOOD)

        def stage(response, task_dir, output, *args):
            (output / "answer.json").write_text('{"value": 14}')
            now[0] = 470.0  # 35s remain on the absolute candidate deadline.
            return True, "Synthetic completeness only.", False

        with patch.object(core.time, "monotonic", side_effect=lambda: now[0]), \
             patch.object(candidate, "_stage_attempt", side_effect=stage):
            candidate.solve(self.task, self.out, client)
        self.assertEqual(self.value(), 14)
        self.assertEqual(len(client.calls), 1)

    def test_base_hash_change_is_refused_before_creating_output(self):
        fake = self.root / "other_engine.py"
        fake.write_text("not the pinned engine")
        with patch.object(core, "__file__", str(fake)), \
             self.assertRaisesRegex(RuntimeError, "exact frozen"):
            candidate.solve(self.task, self.out, FakeHouse())
        self.assertFalse(self.out.exists())

    def test_original_core_stays_bound_and_unchanged(self):
        self.assertEqual(hashlib.sha256((BASE / "engine.py").read_bytes()).hexdigest(),
                         candidate.BASE_ENGINE_SHA256)
        candidate.solve(self.task, self.out, FakeHouse(GOOD, GOOD))
        self.assertEqual(hashlib.sha256((BASE / "engine.py").read_bytes()).hexdigest(),
                         candidate.BASE_ENGINE_SHA256)

    def test_cli_has_original_verb_and_sanitized_auth_failure(self):
        environment = {"PATH": os.environ.get("PATH", ""),
                       "PYTHONPATH": str(BASE), "MODEL_TOKEN": "SYNTHETIC_NO_NETWORK_TOKEN"}
        help_result = subprocess.run([sys.executable, "-B", str(ROOT / "agent.py"), "--help"],
                                     capture_output=True, text=True, env=environment, timeout=5)
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("solve", help_result.stdout)
        result = subprocess.run([sys.executable, "-B", str(ROOT / "agent.py"), "solve",
                                 "--task-dir", str(self.task), "--out", str(self.out)],
                                capture_output=True, text=True, env=environment, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn("EndpointError", result.stderr)
        self.assertNotIn("SYNTHETIC_NO_NETWORK_TOKEN", result.stderr)


if __name__ == "__main__":
    unittest.main()
