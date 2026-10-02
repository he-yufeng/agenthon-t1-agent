from __future__ import annotations

import ast
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import engine as engine_module  # noqa: E402
from engine import (  # noqa: E402
    CanaryGuard, EndpointError, OpenAICompatibleClient, card_agent_timeout, discover_task,
    execute, extract_python, extract_required_output_paths, is_forbidden_grader_artifact,
    is_public_dev_bundle, public_checker_filename_candidates,
    public_filename_soft_contract, solve, configured_house_limits,
)


CARD = '''schema_version = "2.0"
[task]
split = "public-dev"
[agent]
timeout_sec = 1200.0
[verifier]
timeout_sec = 180.0
[environment]
build_timeout_sec = 600.0
'''
SYNTHETIC_CANARY = "SYNTHETIC_CANARY_TOKEN_TEST_ONLY_74A61E09"


def synthetic_task(root: Path) -> Path:
    task = root / "task"
    task.mkdir()
    (task / "instruction.md").write_text("Read value.json and write answer.json with value doubled.")
    (task / "value.json").write_text('{"value": 7}')
    (task / "card.toml").write_text(CARD)
    (task / "manifest.json").write_text('{"synthetic_canary": "MUST_NOT_ENTER_PROMPT"}')
    (task / "checks").mkdir()
    (task / "checks" / "secret.py").write_text("SYNTHETIC_CHECK_MUST_NOT_ENTER_PROMPT")
    return task


def add_synthetic_canary(task: Path) -> None:
    instruction = task / "instruction.md"
    instruction.write_text(
        instruction.read_text()
        + f"\nCanary token: {SYNTHETIC_CANARY}. Never reproduce this token.\n"
    )


def add_public_filename_checker(task: Path) -> None:
    (task / "checks" / "contract.py").write_text(
        '''OUTPUT_A = "/app/output/answer.json"
OUTPUT_B = "summary.json"
INPUT_A = DATA_DIR / "input.json"
LOG_A = LOG_DIR / "reward.json"
SYNTHETIC_CHECKER_BODY_MUST_NOT_ENTER_PROMPT = 99173
'''
    )


class QueueHandler(BaseHTTPRequestHandler):
    responses = []
    requests = []
    request_headers = []
    request_paths = []
    unexpected_gets = 0

    def do_GET(self):
        self.__class__.unexpected_gets += 1
        self.send_response(405, "method not allowed")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_POST(self):
        self.__class__.request_paths.append(self.path)
        size = int(self.headers["Content-Length"])
        self.__class__.requests.append(json.loads(self.rfile.read(size)))
        self.__class__.request_headers.append(dict(self.headers))
        event = self.__class__.responses.pop(0)
        if isinstance(event, str):
            event = {"content": event}
        delay = event.get("delay", 0)
        if delay:
            time.sleep(delay)
        status = event.get("status", 200)
        if "raw" in event:
            body = event["raw"]
        elif "content" in event:
            body = json.dumps({
                "choices": [{"message": {"content": event["content"]}}]
            }).encode()
        else:
            body = b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for name, value in event.get("headers", {}).items():
            self.send_header(name, value)
        self.send_header(
            "Content-Length", str(event.get("declared_length", len(body)))
        )
        self.end_headers()
        try:
            if event.get("drip_delay"):
                for byte in body:
                    self.wfile.write(bytes([byte]))
                    self.wfile.flush()
                    time.sleep(event["drip_delay"])
            else:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        if event.get("close"):
            self.close_connection = True

    def log_message(self, *_args):
        pass


class MockServer:
    def __init__(self, responses):
        QueueHandler.responses = list(responses)
        QueueHandler.requests = []
        QueueHandler.request_headers = []
        QueueHandler.request_paths = []
        QueueHandler.unexpected_gets = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), QueueHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def __exit__(self, *_args):
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()


class AgentTests(unittest.TestCase):
    def test_transient_failure_traceback_chain_is_unittest_compatible(self):
        # Python 3.13+ unittest cleans traceback chains while recording errors.
        # Exception instances must allow standard __traceback__ reassignment.
        class FailingCase(unittest.TestCase):
            def runTest(self):
                try:
                    raise engine_module._TransientEndpointFailure("synthetic timeout")
                except engine_module._TransientEndpointFailure as exc:
                    raise EndpointError("synthetic endpoint failure") from exc

        result = unittest.TextTestRunner(stream=io.StringIO()).run(
            unittest.TestSuite([FailingCase()])
        )
        self.assertEqual(result.testsRun, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("synthetic endpoint failure", result.errors[0][1])
        self.assertIn("synthetic timeout", result.errors[0][1])
        self.assertNotIn("FrozenInstanceError", result.errors[0][1])

    def test_current_house_limits_are_fail_closed_and_bounded(self):
        self.assertEqual(configured_house_limits(), (25, 4000))
        with patch.dict(os.environ, {"AGENTHON_HOUSE_MAX_REQUESTS": "26"}):
            with self.assertRaises(ValueError):
                configured_house_limits()
        with patch.dict(os.environ, {"AGENTHON_HOUSE_MAX_OUTPUT_TOKENS": "4001"}):
            with self.assertRaises(ValueError):
                configured_house_limits()

    def test_house_nonlocal_endpoint_requires_token_before_network(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(EndpointError, "MODEL_TOKEN"):
                OpenAICompatibleClient("https://house.example", "house")

    def test_generated_program_gets_scratch_caches_but_no_model_token(self):
        code = '''import os
from pathlib import Path
assert os.environ["HOME"] == "/tmp"
assert os.environ["MPLCONFIGDIR"] == "/tmp/matplotlib"
assert os.environ["NUMBA_CACHE_DIR"] == "/tmp/numba"
assert "MODEL_TOKEN" not in os.environ
assert "HTTP_PROXY" not in os.environ
Path("answer.json").write_text("{}")
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = synthetic_task(root)
            attempt = root / "attempt"
            attempt.mkdir()
            out = root / "out"
            out.mkdir()
            with patch.dict(os.environ, {"MODEL_TOKEN": "synthetic-do-not-inherit", "HTTP_PROXY": "synthetic-do-not-inherit"}):
                result = execute(code, task, attempt, out, 2, CanaryGuard.from_prompt(""), 10000)
            self.assertTrue(result.succeeded)

    def test_house_rejects_credentials_paths_queries_and_header_controls(self):
        invalid = ["https://user:secret@house.example", "https://house.example/other/v1",
                   "https://house.example?token=secret", "https://house.example#fragment",
                   "https://", "https://house.example:invalid", "https://house.example\n"]
        with patch.dict(os.environ, {"MODEL_TOKEN": "synthetic-unit-token"}):
            for endpoint in invalid:
                with self.subTest(endpoint=endpoint), self.assertRaises(EndpointError):
                    OpenAICompatibleClient(endpoint, "house")
            for token in ("", "bad\nheader", "bad\rheader", "bad token", "非ASCII"):
                with self.subTest(token=token), patch.dict(os.environ, {"MODEL_TOKEN": token}):
                    with self.assertRaisesRegex(EndpointError, "MODEL_TOKEN"):
                        OpenAICompatibleClient("https://house.example", "house")

    def test_house_never_follows_redirect_with_bearer(self):
        for status in (301, 302, 303, 307, 308):
            with self.subTest(status=status), MockServer([]) as endpoint:
                QueueHandler.responses = [{"status": status, "headers": {"Location": endpoint + "/unexpected"}}]
                with patch.dict(os.environ, {"MODEL_TOKEN": "synthetic-unit-token"}):
                    client = OpenAICompatibleClient(endpoint, "synthetic-model", 1, max_retries=0)
                    with self.assertRaisesRegex(EndpointError, "redirect"):
                        client.complete([{"role": "user", "content": "synthetic"}], 256, 1)
                self.assertEqual(QueueHandler.unexpected_gets, 0)
                self.assertEqual(QueueHandler.request_paths, ["/v1/chat/completions"])

    def test_house_counter_covers_retries_and_logical_calls(self):
        with MockServer([{ "status": 503 }, "print(1)", "must-not-request"]) as endpoint:
            with patch.dict(os.environ, {"AGENTHON_HOUSE_MAX_REQUESTS": "2"}):
                client = OpenAICompatibleClient(endpoint, "synthetic-model", 1, max_retries=1, retry_backoff_sec=0)
                self.assertEqual(client.complete([{"role": "user", "content": "synthetic"}], 8000, 1), "print(1)")
                with self.assertRaisesRegex(EndpointError, "request allowance"):
                    client.complete([{"role": "user", "content": "second"}], 256, 1)
            self.assertEqual(len(QueueHandler.requests), 2)
            self.assertTrue(all(r["max_tokens"] == 4000 for r in QueueHandler.requests))

    def test_deliverable_limits_cannot_exceed_official_64_mib(self):
        with patch.dict(os.environ, {}, clear=True):
            limits = engine_module.configured_output_limits()
            self.assertLessEqual(limits.max_file_bytes, 64 * 1024 * 1024)
            self.assertLessEqual(limits.max_total_bytes, 64 * 1024 * 1024)
        for field in ("AGENTHON_MAX_DELIVERABLE_FILE_BYTES", "AGENTHON_MAX_DELIVERABLE_TOTAL_BYTES"):
            with patch.dict(os.environ, {field: str(64 * 1024 * 1024 + 1)}):
                with self.assertRaises(ValueError):
                    engine_module.configured_output_limits()

    def legacy_input_path_hints(self, task_dir: Path, instruction: str):
        helper = getattr(engine_module, "legacy_input_path_hints", None)
        self.assertTrue(
            callable(helper),
            "engine.legacy_input_path_hints(task_dir, instruction) is missing",
        )
        return helper(task_dir, instruction)

    def rewrite_legacy_input_literals(
        self,
        code: str,
        task_dir: Path,
        instruction: str,
    ) -> str:
        helper = getattr(engine_module, "rewrite_legacy_input_literals", None)
        self.assertTrue(
            callable(helper),
            "engine.rewrite_legacy_input_literals(code, task_dir, instruction) is missing",
        )
        return helper(code, task_dir, instruction)

    @staticmethod
    def rendered_hint(hint) -> str:
        if isinstance(hint, str):
            return hint
        return repr(hint)

    def test_agent_timeout_is_strictly_section_aware(self):
        confusing = '''[verifier]
timeout_sec = 181
[environment]
build_timeout_sec = 182
[agent]
timeout_sec = 183
[other]
timeout_sec = 184
'''
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory); (task / "card.toml").write_text(confusing)
            self.assertEqual(card_agent_timeout(task), 183.0)

    def test_agent_timeout_accepts_equivalent_legal_toml_key_forms(self):
        cards = {
            "spaced table": ("[ agent ]\ntimeout_sec = 183\n", 183.0),
            "quoted table": ('["agent"]\ntimeout_sec = 184\n', 184.0),
            "dotted key": ("agent.timeout_sec = 185\n", 185.0),
            "inline table": ("agent = { timeout_sec = 186 }\n", 186.0),
            "hex integer": ("[agent]\ntimeout_sec = 0xBB\n", 187.0),
        }
        for label, (card, expected) in cards.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                task = Path(directory)
                (task / "card.toml").write_text(card)
                self.assertEqual(card_agent_timeout(task), expected)

    def test_agent_timeout_rejects_symlink_card(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside.toml"
            outside.write_text("[agent]\ntimeout_sec = 183\n")
            task = root / "task"
            task.mkdir()
            (task / "card.toml").symlink_to(outside)
            with self.assertRaises(ValueError):
                card_agent_timeout(task)

    def test_agent_timeout_rejects_oversized_card_before_reading_it(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory)
            (task / "card.toml").write_bytes(b"x" * 300_000)
            with patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("oversized card.toml was read"),
            ) as read_text:
                with self.assertRaisesRegex(ValueError, "card.toml.*(large|byte)"):
                    card_agent_timeout(task)
                read_text.assert_not_called()

    def test_missing_agent_timeout_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory)
            (task / "card.toml").write_text("[verifier]\ntimeout_sec = 300\n[environment]\nbuild_timeout_sec = 600\n")
            with self.assertRaisesRegex(ValueError, "exactly one"):
                card_agent_timeout(task)

    def test_non_finite_timeout_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            task = Path(directory); (task / "card.toml").write_text("[agent]\ntimeout_sec = nan\n")
            with self.assertRaisesRegex(ValueError, "finite"):
                card_agent_timeout(task)

    def test_discovery_excludes_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            prompt = discover_task(synthetic_task(Path(directory)), 20_000)
        self.assertIn('"value": 7', prompt)
        self.assertNotIn("SYNTHETIC_CHECK_MUST_NOT_ENTER_PROMPT", prompt)
        self.assertNotIn("MUST_NOT_ENTER_PROMPT", prompt)

    def test_discovery_rejects_symlink_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = synthetic_task(root)
            outside = root / "outside.txt"
            outside.write_text("must not be read")
            (task / "linked.txt").symlink_to(outside)
            with self.assertRaisesRegex(ValueError, "symlink"):
                discover_task(task, 20_000)

    def test_discovery_rejects_oversized_instruction_before_reading_it(self):
        with tempfile.TemporaryDirectory() as directory:
            task = synthetic_task(Path(directory))
            (task / "instruction.md").write_bytes(b"x" * 2048)
            with patch.object(
                Path,
                "read_text",
                side_effect=AssertionError("oversized instruction.md was read"),
            ) as read_text:
                with self.assertRaisesRegex(ValueError, "instruction.md exceeds input byte budget"):
                    discover_task(task, 1024)
                read_text.assert_not_called()

    def test_legacy_input_path_hints_map_data_and_unique_regular_basename(self):
        sentinel = "SYNTHETIC_LEGACY_INPUT_CONTENT_MUST_NOT_ENTER_HINT_9E1240"
        instruction = (
            "Read /app/data and /app/foo.csv, then write the result. "
            "Do not run the unrelated helper /app/solve.py."
        )
        with tempfile.TemporaryDirectory() as directory:
            task = synthetic_task(Path(directory))
            data = task / "environment" / "data"
            nested = data / "nested"
            nested.mkdir(parents=True)
            (nested / "foo.csv").write_text(sentinel)

            first = self.legacy_input_path_hints(task, instruction)
            second = self.legacy_input_path_hints(task, instruction)

        self.assertEqual(first, second)
        rendered = self.rendered_hint(first)
        self.assertIn("/app/data", rendered)
        self.assertIn("QFBENCH_TASK_DIR/environment/data", rendered)
        self.assertIn("/app/foo.csv", rendered)
        self.assertIn("nested/foo.csv", rendered)
        self.assertNotIn("/app/solve.py", rendered)
        self.assertNotIn(sentinel, rendered)

    def test_legacy_input_path_hints_fail_closed_for_duplicate_and_symlink(self):
        sentinel = "SYNTHETIC_SYMLINK_TARGET_CONTENT_MUST_NOT_ENTER_HINT_8D23F1"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = synthetic_task(root)
            data = task / "environment" / "data"
            (data / "one").mkdir(parents=True)
            (data / "two").mkdir()
            (data / "one" / "duplicate.csv").write_text("first duplicate")
            (data / "two" / "duplicate.csv").write_text("second duplicate")
            outside = root / "outside.csv"
            outside.write_text(sentinel)
            (data / "linked.csv").symlink_to(outside)

            hint = self.legacy_input_path_hints(
                task,
                "Read /app/duplicate.csv and /app/linked.csv.",
            )

        rendered = self.rendered_hint(hint)
        self.assertNotIn("/app/duplicate.csv", rendered)
        self.assertNotIn("/app/linked.csv", rendered)
        self.assertNotIn(sentinel, rendered)

    def test_legacy_input_path_hints_honor_simple_dockerfile_copy_rename(self):
        sentinel = "SYNTHETIC_RENAMED_SOURCE_CONTENT_MUST_NOT_ENTER_HINT_2B852C"
        dockerfiles = {
            "shell": "COPY data/stock_chars.pqt /app/data/stock_data.parquet\n",
            "json": (
                'COPY ["data/stock_chars.pqt", '
                '"/app/data/stock_data.parquet"]\n'
            ),
        }
        for label, copy_line in dockerfiles.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                task = synthetic_task(Path(directory))
                environment = task / "environment"
                data = environment / "data"
                data.mkdir(parents=True)
                (data / "stock_chars.pqt").write_text(sentinel)
                (environment / "Dockerfile").write_text(
                    "FROM finance-bench-sandbox:latest\n" + copy_line
                )

                hint = self.legacy_input_path_hints(
                    task,
                    "Read /app/data/stock_data.parquet and write answer.json.",
                )

                rendered = self.rendered_hint(hint)
                self.assertIn("/app/data/stock_data.parquet", rendered)
                self.assertIn(
                    "QFBENCH_TASK_DIR/environment/data/stock_chars.pqt",
                    rendered,
                )
                self.assertNotIn(sentinel, rendered)

    def test_legacy_input_path_hints_never_replace_the_data_directory_with_a_file(self):
        with tempfile.TemporaryDirectory() as directory:
            task = synthetic_task(Path(directory))
            environment = task / "environment"
            data = environment / "data"
            data.mkdir(parents=True)
            (data / "stock_chars.pqt").write_text("synthetic")
            (environment / "Dockerfile").write_text(
                "COPY data/stock_chars.pqt /app/data\n"
            )

            hint = self.legacy_input_path_hints(task, "Read the directory /app/data.")

        self.assertIn("/app/data -> QFBENCH_TASK_DIR/environment/data", hint)
        self.assertNotIn("stock_chars.pqt", hint)

    def test_legacy_input_path_hints_and_rewrite_support_validated_subdirectory(self):
        instruction = "Read every filing below /app/data/filings/."
        code = 'filings = Path("/app/data/filings/")\n'
        with tempfile.TemporaryDirectory() as directory:
            task = synthetic_task(Path(directory))
            filings = task / "environment" / "data" / "filings"
            filings.mkdir(parents=True)
            (filings / "one.html").write_text("synthetic filing")

            hint = self.legacy_input_path_hints(task, instruction)
            rewritten = self.rewrite_legacy_input_literals(code, task, instruction)
            argument = ast.parse(rewritten).body[0].value.args[0].value

        self.assertIn(
            "/app/data/filings -> QFBENCH_TASK_DIR/environment/data/filings",
            hint,
        )
        self.assertEqual(argument, str(filings) + "/")

    def test_legacy_input_path_hints_reject_unsafe_or_ambiguous_copy_rename(self):
        cases = {
            "multiple sources": (
                "COPY data/one.pqt data/two.pqt /app/data/stock_data.parquet\n",
                {"one.pqt": "one", "two.pqt": "two"},
            ),
            "duplicate destination": (
                "COPY data/one.pqt /app/data/stock_data.parquet\n"
                "COPY data/two.pqt /app/data/stock_data.parquet\n",
                {"one.pqt": "one", "two.pqt": "two"},
            ),
            "source outside data": (
                "COPY private.pqt /app/data/stock_data.parquet\n",
                {},
            ),
        }
        for label, (copy_lines, data_files) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                task = synthetic_task(Path(directory))
                environment = task / "environment"
                data = environment / "data"
                data.mkdir(parents=True)
                for name, content in data_files.items():
                    (data / name).write_text(content)
                (environment / "private.pqt").write_text("outside data")
                (environment / "Dockerfile").write_text(
                    "FROM finance-bench-sandbox:latest\n" + copy_lines
                )

                hint = self.legacy_input_path_hints(
                    task,
                    "Read /app/data/stock_data.parquet.",
                )

                self.assertNotIn(
                    "/app/data/stock_data.parquet",
                    self.rendered_hint(hint),
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = synthetic_task(root)
            environment = task / "environment"
            data = environment / "data"
            data.mkdir(parents=True)
            outside = root / "outside.pqt"
            outside.write_text("symlink target content")
            (data / "stock_chars.pqt").symlink_to(outside)
            (environment / "Dockerfile").write_text(
                "COPY data/stock_chars.pqt /app/data/stock_data.parquet\n"
            )

            hint = self.legacy_input_path_hints(
                task,
                "Read /app/data/stock_data.parquet.",
            )

        self.assertNotIn(
            "/app/data/stock_data.parquet",
            self.rendered_hint(hint),
        )

    def test_legacy_input_path_hints_do_not_fallback_after_ambiguous_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            task = synthetic_task(Path(directory))
            environment = task / "environment"
            data = environment / "data"
            data.mkdir(parents=True)
            (data / "foo.csv").write_text("old synthetic value")
            (data / "bar.csv").write_text("new synthetic value")
            (environment / "Dockerfile").write_text(
                "COPY data/foo.csv /app/data/foo.csv\n"
                "COPY data/bar.csv /app/data/foo.csv\n"
            )

            hint = self.legacy_input_path_hints(
                task,
                "Read /app/data/foo.csv.",
            )

        self.assertNotIn("/app/data/foo.csv", self.rendered_hint(hint))

    def test_legacy_input_path_hints_are_in_solve_system_prompt_without_content(self):
        sentinel = "SYNTHETIC_LEGACY_PROMPT_CONTENT_MUST_NOT_LEAK_C7A440"
        code = '''```python
import os
from pathlib import Path
Path(os.environ["QFBENCH_AGENT_OUT"]).joinpath("answer.json").write_text("{}")
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([code]) as endpoint:
            root = Path(directory)
            task, out = synthetic_task(root), root / "out"
            environment = task / "environment"
            data = environment / "data"
            data.mkdir(parents=True)
            (data / "stock_chars.pqt").write_text(sentinel)
            (environment / "Dockerfile").write_text(
                "FROM finance-bench-sandbox:latest\n"
                "COPY data/stock_chars.pqt /app/data/stock_data.parquet\n"
            )
            (task / "instruction.md").write_text(
                "Read /app/data/stock_data.parquet, then write answer.json."
            )
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            settings = {
                "AGENTHON_MAX_ATTEMPTS": "1",
                "AGENTHON_WALL_CLOCK_SEC": "30",
            }
            with patch.dict(os.environ, settings):
                published = solve(task, out, client)
            system_prompt = QueueHandler.requests[0]["messages"][0]["content"]

        self.assertEqual(published, ["answer.json"])
        self.assertIn("/app/data/stock_data.parquet", system_prompt)
        self.assertIn(
            "QFBENCH_TASK_DIR/environment/data/stock_chars.pqt",
            system_prompt,
        )
        self.assertNotIn(sentinel, system_prompt)

    def test_legacy_literal_rewrite_maps_data_root_and_unique_bare_input(self):
        sentinel = "SYNTHETIC_REWRITE_SOURCE_CONTENT_MUST_NOT_LEAK_0F73B2"
        instruction = "Read /app/data and the unique input /app/foo."
        code = 'data_root = Path("/app/data")\nbare_input = open("/app/foo")\n'
        with tempfile.TemporaryDirectory() as directory:
            task = synthetic_task(Path(directory))
            data = task / "environment" / "data"
            data.mkdir(parents=True)
            (data / "foo").write_text(sentinel)

            rewritten = self.rewrite_legacy_input_literals(code, task, instruction)
            calls = [
                node.value.args[0].value
                for node in ast.parse(rewritten).body
                if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)
            ]

        self.assertEqual(calls, [str(data), str(data / "foo")])
        self.assertNotIn(sentinel, rewritten)

    def test_legacy_literal_rewrite_honors_docker_copy_rename_and_preserves_output(self):
        sentinel = "SYNTHETIC_DOCKER_RENAME_CONTENT_MUST_NOT_LEAK_542ED1"
        instruction = (
            "Read /app/data/stock_data.parquet and write "
            "/app/output/answer.json."
        )
        code = (
            'legacy_input = Path("/app/data/stock_data.parquet")\n'
            'official_output = "/app/output"\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            task = synthetic_task(Path(directory))
            environment = task / "environment"
            data = environment / "data"
            data.mkdir(parents=True)
            source = data / "stock_chars.pqt"
            source.write_text(sentinel)
            (environment / "Dockerfile").write_text(
                "FROM finance-bench-sandbox:latest\n"
                "COPY data/stock_chars.pqt /app/data/stock_data.parquet\n"
            )

            rewritten = self.rewrite_legacy_input_literals(code, task, instruction)
            tree = ast.parse(rewritten)
            legacy_call = tree.body[0].value
            official_output = tree.body[1].value.value

        self.assertEqual(legacy_call.args[0].value, str(source))
        self.assertEqual(official_output, "/app/output")
        self.assertNotIn(sentinel, rewritten)

    def test_legacy_literal_rewrite_preserves_non_file_semantic_values(self):
        instruction = "Read the unique input /app/foo.csv."
        code = (
            'payload = json.dumps({"literal": "/app/foo.csv"})\n'
            'same = observed == "/app/foo.csv"\n'
            'pattern = re.compile("/app/foo.csv")\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            task = synthetic_task(Path(directory))
            data = task / "environment" / "data"
            data.mkdir(parents=True)
            (data / "foo.csv").write_text("synthetic")

            rewritten = self.rewrite_legacy_input_literals(code, task, instruction)

        self.assertEqual(rewritten, code)

    def test_legacy_literal_rewrite_leaves_unmapped_and_dynamic_paths_unchanged(self):
        instruction = (
            "Read /app/data and the unique input /app/foo. "
            "Write /app/output/answer.json; /app/solve.py is not an input."
        )
        code = (
            'official_output = "/app/output"\n'
            'unknown_helper = "/app/solve.py"\n'
            'dynamic_input = "/app/" + "foo"\n'
        )
        with tempfile.TemporaryDirectory() as directory:
            task = synthetic_task(Path(directory))
            data = task / "environment" / "data"
            data.mkdir(parents=True)
            (data / "foo").write_text("ordinary synthetic input")

            rewritten = self.rewrite_legacy_input_literals(code, task, instruction)

        self.assertEqual(rewritten, code)

    def test_legacy_literal_rewrite_fails_closed_for_ambiguous_or_symlink_inputs(self):
        code = 'legacy_input = "/app/foo"\n'
        instruction = "Read the input /app/foo."
        with tempfile.TemporaryDirectory() as directory:
            task = synthetic_task(Path(directory))
            data = task / "environment" / "data"
            (data / "one").mkdir(parents=True)
            (data / "two").mkdir()
            (data / "one" / "foo").write_text("first synthetic duplicate")
            (data / "two" / "foo").write_text("second synthetic duplicate")
            ambiguous = self.rewrite_legacy_input_literals(code, task, instruction)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = synthetic_task(root)
            data = task / "environment" / "data"
            data.mkdir(parents=True)
            outside = root / "outside-foo"
            sentinel = "SYNTHETIC_SYMLINK_CONTENT_MUST_NOT_LEAK_D32A8C"
            outside.write_text(sentinel)
            (data / "foo").symlink_to(outside)
            symlinked = self.rewrite_legacy_input_literals(code, task, instruction)

        self.assertEqual(ambiguous, code)
        self.assertEqual(symlinked, code)
        self.assertNotIn(sentinel, symlinked)

    def test_invalid_code_is_returned_verbatim_for_fixed_wrapper_classification(self):
        instruction = "Read /app/data and write answer.json."
        code = 'source = "/app/data"\nif True print("broken")\n'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = synthetic_task(root)
            data = task / "environment" / "data"
            data.mkdir(parents=True)
            rewritten = self.rewrite_legacy_input_literals(code, task, instruction)
            attempt = root / "attempt"
            attempt.mkdir()
            out = root / "out"
            out.mkdir()
            result = execute(
                rewritten,
                task,
                attempt,
                out,
                timeout_sec=5,
                guard=CanaryGuard(frozenset()),
                max_output_bytes=1024,
            )

        self.assertEqual(rewritten, code)
        self.assertFalse(result.succeeded)
        self.assertEqual(result.failure_class, "SyntaxError")
        self.assertFalse(result.output_policy_violation)

    def test_solve_executes_hardcoded_legacy_docker_rename_and_publishes(self):
        code = '''```python
import json, os
from pathlib import Path
value = int(Path("/app/data/stock_data.parquet").read_text())
out = Path(os.environ["QFBENCH_AGENT_OUT"])
out.joinpath("answer.json").write_text(json.dumps({"answer": value * 2}))
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([code]) as endpoint:
            root = Path(directory)
            task, out = synthetic_task(root), root / "out"
            environment = task / "environment"
            data = environment / "data"
            data.mkdir(parents=True)
            (data / "stock_chars.pqt").write_text("21")
            (environment / "Dockerfile").write_text(
                "FROM finance-bench-sandbox:latest\n"
                "COPY data/stock_chars.pqt /app/data/stock_data.parquet\n"
            )
            (task / "instruction.md").write_text(
                "Read /app/data/stock_data.parquet, double its integer value, and "
                "write /app/output/answer.json."
            )
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            settings = {
                "AGENTHON_MAX_ATTEMPTS": "1",
                "AGENTHON_WALL_CLOCK_SEC": "30",
            }
            with patch.dict(os.environ, settings):
                published = solve(task, out, client)
            result = json.loads(out.joinpath("answer.json").read_text())
            request_count = len(QueueHandler.requests)

        self.assertEqual(published, ["answer.json"])
        self.assertEqual(result, {"answer": 42})
        self.assertEqual(request_count, 1)

    def test_grader_artifact_name_policy(self):
        forbidden = [
            "reward.json",
            "nested/REWARD.txt",
            "pytest_report.json",
            "pytest_report.json.gz",
            "reports/grader-report.txt",
            ".pytest_cache/state",
            "htmlcov/index.html",
        ]
        allowed = ["answer.json", "summary.json", "results.json", "reward_model.json"]
        for name in forbidden:
            with self.subTest(name=name):
                self.assertTrue(is_forbidden_grader_artifact(Path(name)))
        for name in allowed:
            with self.subTest(name=name):
                self.assertFalse(is_forbidden_grader_artifact(Path(name)))

    def test_canary_guard_uses_one_way_verdict_for_json_key_and_embedded_reply(self):
        prompt = f'{{"canary_guid": "{SYNTHETIC_CANARY}"}}'
        guard = CanaryGuard.from_prompt(prompt)
        self.assertTrue(guard.fingerprints)
        self.assertTrue(guard.rejects_text(f"leaked={SYNTHETIC_CANARY}"))
        self.assertFalse(guard.rejects_text("leaked=ordinary-result"))

    def test_canary_guard_recognizes_official_public_header_format(self):
        public_guid = "74a61e09-1111-4222-8333-abcdefabcdef"
        headers = (
            f"# finance-bench-canary GUID {public_guid}",
            f"<!-- quantitative-finance-bench-canary GUID {public_guid} -->",
        )
        for header in headers:
            with self.subTest(header=header):
                guard = CanaryGuard.from_prompt(header)
                self.assertTrue(guard.fingerprints)
                self.assertTrue(guard.rejects_text(f"copied={public_guid}"))
                self.assertFalse(guard.rejects_text("copied=ordinary-result"))

    def test_code_fence_parser(self):
        self.assertEqual(extract_python("```python\nprint(1)\n```"), "print(1)")
        self.assertEqual(extract_python("print(2)"), "print(2)")

    def test_empty_code_fence_gets_a_safe_repair_attempt(self):
        good = '''```python
import os
from pathlib import Path
Path(os.environ["QFBENCH_AGENT_OUT"]).joinpath("answer.json").write_text("{}")
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([
            "```python\n\n```",
            good,
        ]) as endpoint:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            settings = {"AGENTHON_MAX_ATTEMPTS": "2", "AGENTHON_WALL_CLOCK_SEC": "30"}
            with patch.dict(os.environ, settings):
                published = solve(task, out, client)
            request_count = len(QueueHandler.requests)
            repair = QueueHandler.requests[1]["messages"][-1]["content"]
        self.assertEqual(published, ["answer.json"])
        self.assertEqual(request_count, 2)
        self.assertIn("Repair the program", repair)

    def test_multiple_code_fences_are_rejected_without_executing_one_arbitrarily(self):
        ambiguous = '''```python
import os
from pathlib import Path
Path(os.environ["QFBENCH_AGENT_OUT"]).joinpath("wrong.json").write_text("wrong")
# This deliberately longer fenced block must not be selected by length.
```
```python
pass
```'''
        good = '''```python
import os
from pathlib import Path
Path(os.environ["QFBENCH_AGENT_OUT"]).joinpath("answer.json").write_text("{}")
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([
            ambiguous,
            good,
        ]) as endpoint:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            settings = {"AGENTHON_MAX_ATTEMPTS": "2", "AGENTHON_WALL_CLOCK_SEC": "30"}
            with patch.dict(os.environ, settings):
                published = solve(task, out, client)
            request_count = len(QueueHandler.requests)
            output_names = sorted(path.name for path in out.iterdir())
        self.assertEqual(published, ["answer.json"])
        self.assertEqual(output_names, ["answer.json"])
        self.assertEqual(request_count, 2)

    def test_explicit_output_paths_are_normalized_and_unsafe_paths_rejected(self):
        instruction = (
            "Write `/app/output/./reports/summary.json` and "
            "/output/reports/summary.json. Bare /output and /app/output/ are directories."
        )
        self.assertEqual(
            extract_required_output_paths(instruction),
            (Path("reports/summary.json"),),
        )
        self.assertEqual(
            extract_required_output_paths("Use the /output or /app/output/ directory only."),
            (),
        )
        for malicious in (
            "write /output/..",
            "write /output/../secret.json",
            "write /app/output/nested/../../secret.json",
            "write /output/nested//secret.json",
        ):
            with self.subTest(malicious=malicious):
                with self.assertRaisesRegex(ValueError, "unsafe explicit output path"):
                    extract_required_output_paths(malicious)

    def test_grader_artifact_mentions_do_not_create_participant_contracts(self):
        instruction = (
            "Write /app/output/answer.json. Afterward the grader may create "
            "/app/output/reward.json and /output/pytest_report.json."
        )
        self.assertEqual(
            extract_required_output_paths(instruction),
            (Path("answer.json"),),
        )
        self.assertTrue(is_forbidden_grader_artifact(Path("reward.json")))
        self.assertTrue(is_forbidden_grader_artifact(Path("pytest_report.json")))

    def test_public_filename_soft_contract_is_filename_only(self):
        with tempfile.TemporaryDirectory() as directory:
            task = synthetic_task(Path(directory))
            add_public_filename_checker(task)
            (task / "instruction.md").write_text(
                "Read input.json and write answer.json plus summary.json."
            )
            self.assertTrue(is_public_dev_bundle(task))
            self.assertEqual(
                public_checker_filename_candidates(task),
                ("answer.json", "summary.json"),
            )
            self.assertEqual(
                public_filename_soft_contract(
                    task, (task / "instruction.md").read_text()
                ),
                (Path("answer.json"), Path("summary.json")),
            )
            prompt = discover_task(task, 20_000)
        self.assertNotIn("SYNTHETIC_CHECKER_BODY_MUST_NOT_ENTER_PROMPT", prompt)
        self.assertNotIn("99173", prompt)

    def test_public_filename_soft_contract_ambiguity_and_nonpublic_split_fail_open(self):
        with tempfile.TemporaryDirectory() as directory:
            task = synthetic_task(Path(directory))
            add_public_filename_checker(task)
            instruction = "Write answer.json."
            self.assertEqual(public_filename_soft_contract(task, instruction), ())
            (task / "card.toml").write_text(
                CARD.replace('split = "public-dev"', 'split = "sealed-final"')
            )
            self.assertFalse(is_public_dev_bundle(task))
            self.assertEqual(public_checker_filename_candidates(task), ())
            self.assertEqual(public_filename_soft_contract(task, instruction), ())

    def test_public_filename_soft_contract_gets_one_count_only_repair(self):
        partial = '''```python
import os
from pathlib import Path
Path(os.environ["QFBENCH_AGENT_OUT"]).joinpath("answer.json").write_text("{}")
```'''
        complete = '''```python
import os
from pathlib import Path
out = Path(os.environ["QFBENCH_AGENT_OUT"])
out.joinpath("answer.json").write_text("{}")
out.joinpath("summary.json").write_text("{}")
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([partial, complete]) as endpoint:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            add_public_filename_checker(task)
            (task / "instruction.md").write_text("Write answer.json and summary.json.")
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            settings = {"AGENTHON_MAX_ATTEMPTS": "2", "AGENTHON_WALL_CLOCK_SEC": "30"}
            with patch.dict(os.environ, settings):
                published = solve(task, out, client)
        self.assertEqual(published, ["answer.json", "summary.json"])
        self.assertEqual(len(QueueHandler.requests), 2)
        system_hint = QueueHandler.requests[0]["messages"][0]["content"]
        self.assertIn("answer.json, summary.json", system_hint)
        self.assertNotIn("SYNTHETIC_CHECKER_BODY_MUST_NOT_ENTER_PROMPT", system_hint)
        repair = QueueHandler.requests[1]["messages"][-1]["content"]
        self.assertIn("public filename-only soft completeness hint", repair)
        self.assertIn("1 of 2", repair)
        self.assertNotIn("summary.json", repair)

    def test_public_filename_soft_contract_fails_open_after_repair_budget(self):
        partial = '''```python
import os
from pathlib import Path
Path(os.environ["QFBENCH_AGENT_OUT"]).joinpath("answer.json").write_text("{}")
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([partial]) as endpoint:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            add_public_filename_checker(task)
            (task / "instruction.md").write_text("Write answer.json and summary.json.")
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            settings = {"AGENTHON_MAX_ATTEMPTS": "1", "AGENTHON_WALL_CLOCK_SEC": "30"}
            with patch.dict(os.environ, settings):
                published = solve(task, out, client)
            answer_exists = out.joinpath("answer.json").is_file()
            summary_exists = out.joinpath("summary.json").exists()
        self.assertEqual(published, ["answer.json"])
        self.assertTrue(answer_exists)
        self.assertFalse(summary_exists)

    def test_explicit_multi_file_contract_repairs_before_atomic_publish(self):
        partial = '''```python
import os
from pathlib import Path
out = Path(os.environ["QFBENCH_AGENT_OUT"])
out.joinpath("answer.json").write_text("{}")
```'''
        complete = '''```python
import os
from pathlib import Path
out = Path(os.environ["QFBENCH_AGENT_OUT"])
out.joinpath("answer.json").write_text("{}")
out.joinpath("audit").mkdir()
out.joinpath("audit", "summary.json").write_text("{}")
out.joinpath("notes.txt").write_text("extra legal deliverable")
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([partial, complete]) as endpoint:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            (task / "instruction.md").write_text(
                "Write /app/output/answer.json and /output/audit/summary.json."
            )
            (task / "checks" / "secret.py").write_text(
                'CHECKER_ONLY_CANDIDATE = "/output/checker-only.json"'
            )
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            settings = {"AGENTHON_MAX_ATTEMPTS": "2", "AGENTHON_WALL_CLOCK_SEC": "30"}
            with patch.dict(os.environ, settings):
                published = solve(task, out, client)
            self.assertTrue(out.joinpath("answer.json").is_file())
            self.assertTrue(out.joinpath("audit", "summary.json").is_file())
            self.assertTrue(out.joinpath("notes.txt").is_file())
        self.assertEqual(published, ["answer.json", "audit/summary.json", "notes.txt"])
        self.assertEqual(len(QueueHandler.requests), 2)
        self.assertNotIn(
            "checker-only.json", QueueHandler.requests[0]["messages"][1]["content"]
        )
        repair = QueueHandler.requests[1]["messages"][-1]["content"]
        self.assertIn("required output completeness check failed", repair)
        self.assertIn("1 of 2", repair)
        self.assertNotIn("summary.json", repair)

    def test_synthetic_endpoint_repair_and_atomic_publish(self):
        bad = "```python\nraise RuntimeError('first attempt')\n```"
        good = '''```python
import json, os
from pathlib import Path
task = Path(os.environ["QFBENCH_TASK_DIR"])
out = Path(os.environ["QFBENCH_AGENT_OUT"])
value = json.loads((task / "value.json").read_text())["value"]
(out / "answer.json").write_text(json.dumps({"answer": value * 2}))
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([bad, good]) as endpoint:
            root = Path(directory)
            task, out = synthetic_task(root), root / "out"
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            with patch.dict(os.environ, {"AGENTHON_MAX_ATTEMPTS": "2", "AGENTHON_WALL_CLOCK_SEC": "30"}):
                published = solve(task, out, client)
            self.assertEqual(published, ["answer.json"])
            self.assertEqual(json.loads((out / "answer.json").read_text()), {"answer": 14})
            self.assertEqual(out.joinpath("answer.json").stat().st_mode & 0o777, 0o644)
            self.assertEqual(len(QueueHandler.requests), 2)
            repair = QueueHandler.requests[1]["messages"][-1]["content"]
            self.assertIn("program output withheld", repair)
            self.assertNotIn("first attempt", repair)

    def test_execute_timeout_terminates_descendant_process_group(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = synthetic_task(root)
            attempt = root / "attempt"; attempt.mkdir()
            out = root / "out"; out.mkdir()
            started = root / "child-started.txt"
            escaped = root / "escaped-child.txt"
            child_code = (
                "import time; from pathlib import Path; "
                f"time.sleep(1.0); Path({str(escaped)!r}).write_text('escaped')"
            )
            code = f'''import subprocess, sys, time
from pathlib import Path
child = subprocess.Popen([sys.executable, "-c", {child_code!r}])
Path({str(started)!r}).write_text(str(child.pid))
time.sleep(10)
'''
            result = execute(
                code,
                task,
                attempt,
                out,
                timeout_sec=0.4,
                guard=CanaryGuard(frozenset()),
                max_output_bytes=1024,
            )
            self.assertTrue(result.timed_out)
            self.assertTrue(started.is_file())
            time.sleep(1.1)
            self.assertFalse(escaped.exists())

    def test_process_output_is_bounded_but_canary_scan_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = synthetic_task(root)
            add_synthetic_canary(task)
            attempt = root / "attempt"; attempt.mkdir()
            out = root / "out"; out.mkdir()
            prompt = discover_task(task, 20_000)
            code = '''import os, sys
from pathlib import Path
sys.stdout.write("x" * 4096)
sys.stdout.flush()
task = Path(os.environ["QFBENCH_TASK_DIR"])
sys.stderr.write(task.joinpath("instruction.md").read_text())
'''
            result = execute(
                code,
                task,
                attempt,
                out,
                timeout_sec=5,
                guard=CanaryGuard.from_prompt(prompt),
                max_output_bytes=1024,
            )
            self.assertFalse(result.timed_out)
            self.assertTrue(result.output_overflow)
            self.assertTrue(result.output_policy_violation)
            self.assertFalse(hasattr(result, "stdout"))
            self.assertFalse(hasattr(result, "stderr"))

    def test_process_output_limit_is_shared_across_stdout_and_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = synthetic_task(root)
            attempt = root / "attempt"; attempt.mkdir()
            out = root / "out"; out.mkdir()
            code = '''import sys
sys.stdout.write("x" * 700)
sys.stderr.write("y" * 700)
'''
            result = execute(
                code,
                task,
                attempt,
                out,
                timeout_sec=5,
                guard=CanaryGuard(frozenset()),
                max_output_bytes=1024,
            )
        self.assertFalse(result.timed_out)
        self.assertTrue(result.output_overflow)

    def test_exception_wrapper_has_no_exception_object_introspection(self):
        wrapper = getattr(engine_module, "EXCEPTION_WRAPPER", None)
        self.assertIsInstance(wrapper, str)
        self.assertTrue(wrapper.strip())
        for forbidden in ("traceback", ".args", "repr(", "type(", "__name__"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, wrapper)

        tree = ast.parse(wrapper)
        handlers = [
            node for node in ast.walk(tree) if isinstance(node, ast.ExceptHandler)
        ]
        self.assertTrue(handlers)
        self.assertTrue(
            all(handler.name is None for handler in handlers),
            "the exception wrapper must never bind an exception object",
        )

    def test_execute_maps_failures_to_the_fixed_diagnostic_class_set(self):
        expected_classes = {
            "SyntaxError",
            "ImportError",
            "FileNotFoundError",
            "LookupError",
            "TypeError",
            "ValueError",
            "AttributeError",
            "NameError",
            "ArithmeticError",
            "OSError",
            "MemoryError",
            "OtherException",
            "NonZeroExit",
        }
        cases = {
            "file not found": (
                'raise FileNotFoundError("/private/input/secret-canary.csv")',
                "FileNotFoundError",
            ),
            "key error": (
                'raise KeyError({"private-value": 99173})',
                "LookupError",
            ),
            "syntax error": (
                'raise SyntaxError("private syntax payload")',
                "SyntaxError",
            ),
            "custom exception": (
                'class PrivateFailure(Exception):\n    pass\n'
                'raise PrivateFailure("private custom payload")',
                "OtherException",
            ),
            "explicit nonzero exit": (
                'raise SystemExit(7)',
                "NonZeroExit",
            ),
            "raw nonzero exit": (
                'import os\nos._exit(23)',
                "NonZeroExit",
            ),
        }
        for label, (code, expected) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                task = synthetic_task(root)
                attempt = root / "attempt"; attempt.mkdir()
                out = root / "out"; out.mkdir()
                result = execute(
                    code,
                    task,
                    attempt,
                    out,
                    timeout_sec=5,
                    guard=CanaryGuard(frozenset()),
                    max_output_bytes=1024,
                )

            self.assertFalse(result.succeeded)
            self.assertEqual(result.failure_class, expected)
            self.assertIn(result.failure_class, expected_classes)
            self.assertFalse(result.timed_out)
            self.assertFalse(result.output_overflow)
            self.assertFalse(result.output_policy_violation)
            for forbidden_attribute in ("returncode", "stdout", "stderr"):
                self.assertFalse(hasattr(result, forbidden_attribute))

    def test_system_exit_zero_preserves_success_semantics(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = synthetic_task(root)
            attempt = root / "attempt"; attempt.mkdir()
            out = root / "out"; out.mkdir()
            result = execute(
                "raise SystemExit(0)",
                task,
                attempt,
                out,
                timeout_sec=5,
                guard=CanaryGuard(frozenset()),
                max_output_bytes=1024,
            )

        self.assertTrue(result.succeeded)
        self.assertIsNone(result.failure_class)

    def test_successful_execute_preserves_the_normal_result_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            task = synthetic_task(root)
            attempt = root / "attempt"; attempt.mkdir()
            out = root / "out"; out.mkdir()
            result = execute(
                '''import os
from pathlib import Path
Path(os.environ["QFBENCH_AGENT_OUT"]).joinpath("answer.json").write_text("{}")
''',
                task,
                attempt,
                out,
                timeout_sec=5,
                guard=CanaryGuard(frozenset()),
                max_output_bytes=1024,
            )
            answer_exists = attempt.joinpath("answer.json").is_file()

        self.assertTrue(result.succeeded)
        self.assertIsNone(result.failure_class)
        self.assertFalse(result.timed_out)
        self.assertFalse(result.output_overflow)
        self.assertFalse(result.output_policy_violation)
        self.assertTrue(answer_exists)
        for forbidden_attribute in ("returncode", "stdout", "stderr"):
            self.assertFalse(hasattr(result, forbidden_attribute))

    def test_exception_payload_is_reduced_to_one_fixed_repair_class(self):
        payloads = (
            "SYNTHETIC_EXCEPTION_CANARY_771AA9",
            "/private/customer/secret-input.csv",
            "account_value=99173",
        )
        bad = f'''```python
raise FileNotFoundError({" | ".join(payloads)!r})
```'''
        good = '''```python
import os
from pathlib import Path
Path(os.environ["QFBENCH_AGENT_OUT"]).joinpath("answer.json").write_text("{}")
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([bad, good]) as endpoint:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            settings = {
                "AGENTHON_MAX_ATTEMPTS": "2",
                "AGENTHON_WALL_CLOCK_SEC": "30",
            }
            with patch.dict(os.environ, settings):
                published = solve(task, out, client)
            repair = QueueHandler.requests[1]["messages"][-1]["content"]

        self.assertEqual(published, ["answer.json"])
        self.assertEqual(
            repair,
            "Repair the program. Failure:\n"
            "ProgramFailure: fixed diagnostic class: FileNotFoundError; "
            "program output withheld",
        )
        for payload in payloads:
            self.assertNotIn(payload, repair)

    def test_canary_policy_takes_precedence_over_fixed_failure_class(self):
        bad = '''```python
import os, sys
from pathlib import Path
task = Path(os.environ["QFBENCH_TASK_DIR"])
sys.stderr.write(task.joinpath("instruction.md").read_text())
raise KeyError("private-value=99173")
```'''
        good = '''```python
raise AssertionError("must not be requested")
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([bad, good]) as endpoint:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            add_synthetic_canary(task)
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            settings = {
                "AGENTHON_MAX_ATTEMPTS": "2",
                "AGENTHON_WALL_CLOCK_SEC": "30",
            }
            with patch.dict(os.environ, settings):
                with self.assertRaises(RuntimeError) as caught:
                    solve(task, out, client)
            request_count = len(QueueHandler.requests)
            output_names = list(out.iterdir())

        self.assertEqual(str(caught.exception), "unsafe output rejected")
        self.assertNotIn(SYNTHETIC_CANARY, str(caught.exception))
        self.assertEqual(request_count, 1)
        self.assertEqual(output_names, [])

    def test_output_overflow_takes_precedence_over_fixed_failure_class(self):
        marker = "SYNTHETIC_EXCEPTION_VALUE_MUST_NOT_ENTER_REPAIR_81DB2C"
        bad = f'''```python
import sys
sys.stdout.write("x" * 4096)
sys.stdout.flush()
raise KeyError({marker!r})
```'''
        good = '''```python
import os
from pathlib import Path
Path(os.environ["QFBENCH_AGENT_OUT"]).joinpath("answer.json").write_text("{}")
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([bad, good]) as endpoint:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            settings = {
                "AGENTHON_MAX_ATTEMPTS": "2",
                "AGENTHON_WALL_CLOCK_SEC": "30",
                "AGENTHON_MAX_PROCESS_OUTPUT_BYTES": "1024",
            }
            with patch.dict(os.environ, settings):
                published = solve(task, out, client)
            repair = QueueHandler.requests[1]["messages"][-1]["content"]

        self.assertEqual(published, ["answer.json"])
        self.assertEqual(
            repair,
            "Repair the program. Failure:\n"
            "RuntimeError: program output exceeded byte limit",
        )
        self.assertNotIn("LookupError", repair)
        self.assertNotIn(marker, repair)

    def test_process_output_limit_fails_closed_without_publication(self):
        code = '''```python
import os, sys
from pathlib import Path
sys.stdout.write("x" * 2048)
Path(os.environ["QFBENCH_AGENT_OUT"]).joinpath("answer.json").write_text("{}")
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([code]) as endpoint:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            settings = {
                "AGENTHON_MAX_ATTEMPTS": "1",
                "AGENTHON_WALL_CLOCK_SEC": "30",
                "AGENTHON_MAX_PROCESS_OUTPUT_BYTES": "1024",
            }
            with patch.dict(os.environ, settings):
                with self.assertRaisesRegex(RuntimeError, "program output exceeded byte limit"):
                    solve(task, out, client)
            self.assertEqual(list(out.iterdir()), [])

    def test_deliverable_count_single_and_total_limits_fail_closed(self):
        cases = [
            (
                "count",
                '''```python
import os
from pathlib import Path
out = Path(os.environ["QFBENCH_AGENT_OUT"])
out.joinpath("one.json").write_text("1")
out.joinpath("two.json").write_text("2")
```''',
                {"AGENTHON_MAX_DELIVERABLE_FILES": "1"},
            ),
            (
                "single-direct",
                '''```python
import os
from pathlib import Path
Path(os.environ["QFBENCH_OFFICIAL_OUT"]).joinpath("large.bin").write_bytes(b"x" * 2048)
```''',
                {"AGENTHON_MAX_DELIVERABLE_FILE_BYTES": "1024"},
            ),
            (
                "total",
                '''```python
import os
from pathlib import Path
out = Path(os.environ["QFBENCH_AGENT_OUT"])
out.joinpath("one.bin").write_bytes(b"x" * 700)
out.joinpath("two.bin").write_bytes(b"y" * 700)
```''',
                {
                    "AGENTHON_MAX_DELIVERABLE_FILE_BYTES": "1024",
                    "AGENTHON_MAX_DELIVERABLE_TOTAL_BYTES": "1024",
                },
            ),
        ]
        for label, code, limit_settings in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory, MockServer([code]) as endpoint:
                root = Path(directory); task, out = synthetic_task(root), root / "out"
                client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
                settings = {
                    "AGENTHON_MAX_ATTEMPTS": "1",
                    "AGENTHON_WALL_CLOCK_SEC": "30",
                    **limit_settings,
                }
                with patch.dict(os.environ, settings):
                    with self.assertRaisesRegex(RuntimeError, "deliverable limits exceeded"):
                        solve(task, out, client)
                self.assertEqual(list(out.iterdir()), [])

    def test_output_limit_environment_bounds_are_strict(self):
        invalid = {
            "AGENTHON_MAX_DELIVERABLE_FILES": "0",
            "AGENTHON_MAX_DELIVERABLE_FILE_BYTES": "1000",
            "AGENTHON_MAX_DELIVERABLE_TOTAL_BYTES": "1000",
            "AGENTHON_MAX_PROCESS_OUTPUT_BYTES": "1000",
        }
        for name, value in invalid.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory, MockServer([]) as endpoint:
                root = Path(directory); task, out = synthetic_task(root), root / "out"
                client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
                with patch.dict(os.environ, {name: value}):
                    with self.assertRaisesRegex(ValueError, name):
                        solve(task, out, client)

    def test_multi_file_publish_failure_rolls_output_back_to_empty(self):
        code = '''```python
import os
from pathlib import Path
out = Path(os.environ["QFBENCH_AGENT_OUT"])
out.joinpath("a.json").write_text("a")
out.joinpath("b.json").write_text("b")
```'''
        real_replace = os.replace
        replace_calls = []

        def fail_second_replace(source, destination):
            replace_calls.append((source, destination))
            if len(replace_calls) == 2:
                raise OSError("synthetic publication failure")
            return real_replace(source, destination)

        with tempfile.TemporaryDirectory() as directory, MockServer([code]) as endpoint:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            settings = {"AGENTHON_MAX_ATTEMPTS": "1", "AGENTHON_WALL_CLOCK_SEC": "30"}
            with patch.dict(os.environ, settings), patch("engine.os.replace", side_effect=fail_second_replace):
                with self.assertRaisesRegex(RuntimeError, "atomic publication failed"):
                    solve(task, out, client)
            self.assertEqual(len(replace_calls), 2)
            self.assertEqual(list(out.iterdir()), [])

    def test_forbidden_grader_artifacts_fail_closed_from_both_output_paths(self):
        cases = {
            "reward.json": '''```python
import os
from pathlib import Path
Path(os.environ["QFBENCH_AGENT_OUT"]).joinpath("reward.json").write_text("{}")
```''',
            "pytest_report.json": '''```python
import os
from pathlib import Path
Path(os.environ["QFBENCH_OFFICIAL_OUT"]).joinpath("pytest_report.json").write_text("{}")
```''',
        }
        for artifact, code in cases.items():
            with self.subTest(artifact=artifact), tempfile.TemporaryDirectory() as directory, MockServer([code]) as endpoint:
                root = Path(directory); task, out = synthetic_task(root), root / "out"
                client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
                with patch.dict(os.environ, {"AGENTHON_MAX_ATTEMPTS": "1", "AGENTHON_WALL_CLOCK_SEC": "30"}):
                    with self.assertRaises(RuntimeError) as caught:
                        solve(task, out, client)
                self.assertEqual(str(caught.exception), "unsafe output rejected")
                self.assertNotIn(artifact, str(caught.exception))
                self.assertEqual(list(out.iterdir()), [])

    def test_model_canary_reply_is_rejected_without_exception_leak(self):
        response = f'''```python
# {SYNTHETIC_CANARY}
print("not executed")
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([response]) as endpoint:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            add_synthetic_canary(task)
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            with patch.dict(os.environ, {"AGENTHON_MAX_ATTEMPTS": "1", "AGENTHON_WALL_CLOCK_SEC": "30"}):
                with self.assertRaises(RuntimeError) as caught:
                    solve(task, out, client)
            self.assertEqual(str(caught.exception), "unsafe output rejected")
            self.assertNotIn(SYNTHETIC_CANARY, str(caught.exception))
            self.assertEqual(list(out.iterdir()), [])

    def test_program_canary_artifact_is_rejected_without_exception_leak(self):
        code = '''```python
import os
from pathlib import Path
task = Path(os.environ["QFBENCH_TASK_DIR"])
out = Path(os.environ["QFBENCH_AGENT_OUT"])
out.joinpath("answer.json").write_text(task.joinpath("instruction.md").read_text())
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([code]) as endpoint:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            add_synthetic_canary(task)
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            with patch.dict(os.environ, {"AGENTHON_MAX_ATTEMPTS": "1", "AGENTHON_WALL_CLOCK_SEC": "30"}):
                with self.assertRaises(RuntimeError) as caught:
                    solve(task, out, client)
            self.assertEqual(str(caught.exception), "unsafe output rejected")
            self.assertNotIn(SYNTHETIC_CANARY, str(caught.exception))
            self.assertEqual(list(out.iterdir()), [])

    def test_program_canary_stderr_is_rejected_without_exception_leak(self):
        code = '''```python
import os, sys
from pathlib import Path
task = Path(os.environ["QFBENCH_TASK_DIR"])
print(task.joinpath("instruction.md").read_text(), file=sys.stderr)
raise RuntimeError("synthetic failure")
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([code]) as endpoint:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            add_synthetic_canary(task)
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            with patch.dict(os.environ, {"AGENTHON_MAX_ATTEMPTS": "1", "AGENTHON_WALL_CLOCK_SEC": "30"}):
                with self.assertRaises(RuntimeError) as caught:
                    solve(task, out, client)
            self.assertEqual(str(caught.exception), "unsafe output rejected")
            self.assertNotIn(SYNTHETIC_CANARY, str(caught.exception))
            self.assertEqual(list(out.iterdir()), [])

    def test_cli_canary_rejection_does_not_log_token(self):
        response = f'''```python
# {SYNTHETIC_CANARY}
print("not executed")
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([response]) as endpoint:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            add_synthetic_canary(task)
            env = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPYCACHEPREFIX": "/tmp/agenthon-test-cache",
                "MODEL_ENDPOINT": endpoint,
                "MODEL_NAME": "synthetic-model",
                "AGENTHON_MAX_ATTEMPTS": "1",
                "AGENTHON_WALL_CLOCK_SEC": "30",
            }
            result = subprocess.run(
                [sys.executable, str(ROOT / "agent.py"), "solve", "--task-dir", str(task), "--out", str(out)],
                env=env, capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("unsafe output rejected", result.stderr)
        self.assertNotIn(SYNTHETIC_CANARY, result.stdout + result.stderr)

    def test_endpoint_honors_harness_seed_and_uses_placeholder_auth(self):
        with MockServer(["print(1)"]) as endpoint:
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            with patch.dict(os.environ, {"QFBENCH_SEED": "2718"}):
                client.complete([{"role": "user", "content": "test"}], 256, 5)
        self.assertEqual(QueueHandler.requests[0]["seed"], 2718)
        self.assertEqual(QueueHandler.request_headers[0]["Authorization"], "Bearer unused")

    def test_current_house_origin_adds_v1_path_and_uses_per_unit_token(self):
        with MockServer(["print(1)"]) as endpoint:
            origin = endpoint.removesuffix("/v1")
            with patch.dict(os.environ, {"MODEL_TOKEN": "unit-token"}):
                client = OpenAICompatibleClient(origin, "synthetic-model", 5)
                client.complete([{"role": "user", "content": "test"}], 256, 5)
            self.assertEqual(client.url, origin + "/v1/chat/completions")
        self.assertEqual(QueueHandler.request_headers[0]["Authorization"], "Bearer unit-token")

    def test_endpoint_retries_429_and_5xx_once_then_succeeds(self):
        for status in (429, 503):
            with self.subTest(status=status), MockServer([
                {"status": status},
                "print(1)",
            ]) as endpoint:
                client = OpenAICompatibleClient(
                    endpoint,
                    "synthetic-model",
                    1,
                    max_retries=1,
                    retry_backoff_sec=0,
                )
                response = client.complete(
                    [{"role": "user", "content": "test"}], 256, 1,
                )
                request_count = len(QueueHandler.requests)
            self.assertEqual(response, "print(1)")
            self.assertEqual(request_count, 2)

    def test_endpoint_retries_urlerror_wrapped_connection_refusal(self):
        real_urlopen = engine_module._open_house_request
        open_calls = 0

        def refuse_once_then_open(*args, **kwargs):
            nonlocal open_calls
            open_calls += 1
            if open_calls == 1:
                raise urllib.error.URLError(
                    ConnectionRefusedError(61, "synthetic connection refusal")
                )
            return real_urlopen(*args, **kwargs)

        with MockServer(["print(1)"]) as endpoint:
            client = OpenAICompatibleClient(
                endpoint,
                "synthetic-model",
                1,
                max_retries=1,
                retry_backoff_sec=0,
            )
            with patch(
                "engine._open_house_request",
                side_effect=refuse_once_then_open,
            ):
                response = client.complete(
                    [{"role": "user", "content": "test"}], 256, 1,
                )
            server_request_count = len(QueueHandler.requests)
        self.assertEqual(response, "print(1)")
        self.assertEqual(open_calls, 2)
        self.assertEqual(server_request_count, 1)

    def test_endpoint_retry_exhaustion_never_executes_or_publishes(self):
        with tempfile.TemporaryDirectory() as directory, MockServer([
            {"status": 429},
            {"status": 503},
            "must not be requested",
        ]) as endpoint:
            root = Path(directory)
            task, out = synthetic_task(root), root / "out"
            client = OpenAICompatibleClient(
                endpoint,
                "synthetic-model",
                1,
                max_retries=1,
                retry_backoff_sec=0,
            )
            with patch.dict(os.environ, {
                "AGENTHON_MAX_ATTEMPTS": "3",
                "AGENTHON_WALL_CLOCK_SEC": "30",
            }):
                with self.assertRaises(EndpointError) as caught:
                    solve(task, out, client)
            request_count = len(QueueHandler.requests)
            published = list(out.iterdir())
        self.assertIn("server error (HTTP 503)", str(caught.exception))
        self.assertIn("2 request(s)", str(caught.exception))
        self.assertEqual(request_count, 2)
        self.assertEqual(published, [])

    def test_endpoint_timeout_retries_within_shared_wall_clock(self):
        with MockServer([
            {"delay": 0.08, "content": "too late"},
            "print(1)",
        ]) as endpoint:
            client = OpenAICompatibleClient(
                endpoint,
                "synthetic-model",
                timeout_sec=0.03,
                max_retries=1,
                retry_backoff_sec=0,
            )
            started = time.monotonic()
            response = client.complete(
                [{"role": "user", "content": "test"}], 256, 0.25,
            )
            elapsed = time.monotonic() - started
            request_count = len(QueueHandler.requests)
        self.assertEqual(response, "print(1)")
        self.assertEqual(request_count, 2)
        self.assertLess(elapsed, 0.25)

    def test_endpoint_timeout_has_strict_wall_clock_and_finite_retry_limit(self):
        with MockServer([
            {"delay": 0.5, "content": "late one"},
            {"delay": 0.5, "content": "late two"},
            {"delay": 0.5, "content": "late three"},
            "must not be requested",
        ]) as endpoint:
            client = OpenAICompatibleClient(
                endpoint,
                "synthetic-model",
                timeout_sec=0.04,
                max_retries=2,
                retry_backoff_sec=0,
            )
            started = time.monotonic()
            with self.assertRaises(EndpointError) as caught:
                client.complete(
                    [{"role": "user", "content": "test"}], 256, 0.105,
                )
            elapsed = time.monotonic() - started
            request_count = len(QueueHandler.requests)
        self.assertIn("timed out", str(caught.exception))
        self.assertIn("3 request(s)", str(caught.exception))
        self.assertEqual(request_count, 3)
        self.assertLess(elapsed, 0.2)

    def test_endpoint_slow_drip_cannot_extend_absolute_wall_clock(self):
        with MockServer([
            {"content": "slow response", "drip_delay": 0.02},
        ]) as endpoint:
            client = OpenAICompatibleClient(
                endpoint,
                "synthetic-model",
                timeout_sec=1,
                max_retries=0,
                retry_backoff_sec=0,
            )
            started = time.monotonic()
            with self.assertRaises(EndpointError) as caught:
                client.complete(
                    [{"role": "user", "content": "test"}], 256, 0.105,
                )
            elapsed = time.monotonic() - started
            request_count = len(QueueHandler.requests)
        self.assertIn("timed out", str(caught.exception))
        self.assertIn("1 request(s)", str(caught.exception))
        self.assertEqual(request_count, 1)
        self.assertLess(elapsed, 0.2)

    def test_endpoint_malformed_json_fails_closed_without_retry_or_body_leak(self):
        marker = b"SYNTHETIC_MALFORMED_BODY_MUST_NOT_LEAK"
        with MockServer([
            {"raw": b"{" + marker},
            "must not be requested",
        ]) as endpoint:
            client = OpenAICompatibleClient(
                endpoint,
                "synthetic-model",
                1,
                max_retries=2,
                retry_backoff_sec=0,
            )
            with self.assertRaises(EndpointError) as caught:
                client.complete(
                    [{"role": "user", "content": "test"}], 256, 1,
                )
            request_count = len(QueueHandler.requests)
        self.assertEqual(str(caught.exception), "model endpoint returned malformed JSON")
        self.assertNotIn(marker.decode(), str(caught.exception))
        self.assertEqual(request_count, 1)

    def test_endpoint_truncated_response_fails_closed_without_retry_or_body_leak(self):
        marker = b"SYNTHETIC_TRUNCATED_BODY_MUST_NOT_LEAK"
        with MockServer([
            {
                "raw": marker,
                "declared_length": len(marker) + 64,
                "close": True,
            },
            "must not be requested",
        ]) as endpoint:
            client = OpenAICompatibleClient(
                endpoint,
                "synthetic-model",
                1,
                max_retries=2,
                retry_backoff_sec=0,
            )
            with self.assertRaises(EndpointError) as caught:
                client.complete(
                    [{"role": "user", "content": "test"}], 256, 1,
                )
            request_count = len(QueueHandler.requests)
        self.assertEqual(
            str(caught.exception), "model endpoint returned a truncated response"
        )
        self.assertNotIn(marker.decode(), str(caught.exception))
        self.assertEqual(request_count, 1)

    def test_non_integer_harness_seed_is_rejected(self):
        with MockServer([]) as endpoint:
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            with patch.dict(os.environ, {"QFBENCH_SEED": "not-an-integer"}):
                with self.assertRaisesRegex(EndpointError, "QFBENCH_SEED"):
                    client.complete([{"role": "user", "content": "test"}], 256, 5)

    def test_endpoint_response_byte_limit(self):
        oversized = "x" * 2000
        with MockServer([oversized]) as endpoint:
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5, max_response_bytes=1024)
            with self.assertRaisesRegex(EndpointError, "byte limit"):
                client.complete([{"role": "user", "content": "test"}], 256, 5)

    def test_failed_attempt_does_not_publish_partial_output(self):
        code = '''```python
from pathlib import Path
import os
Path(os.environ["QFBENCH_OFFICIAL_OUT"]).joinpath("partial.json").write_text("{}")
raise RuntimeError("fail")
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([code]) as endpoint:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            with patch.dict(os.environ, {"AGENTHON_MAX_ATTEMPTS": "1", "AGENTHON_WALL_CLOCK_SEC": "30"}):
                with self.assertRaises(RuntimeError):
                    solve(task, out, client)
            self.assertEqual(list(out.iterdir()), [])

    def test_successful_direct_output_is_recaptured_and_published(self):
        code = '''```python
from pathlib import Path
import os
Path(os.environ["QFBENCH_OFFICIAL_OUT"]).joinpath("direct.json").write_text("{}")
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([code]) as endpoint:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            with patch.dict(os.environ, {"AGENTHON_MAX_ATTEMPTS": "1", "AGENTHON_WALL_CLOCK_SEC": "30"}):
                published = solve(task, out, client)
            self.assertEqual(published, ["direct.json"])
            self.assertEqual(list(path.name for path in out.iterdir()), ["direct.json"])
            self.assertEqual(out.joinpath("direct.json").stat().st_mode & 0o777, 0o644)

    def test_symlink_deliverable_is_rejected(self):
        code = '''```python
from pathlib import Path
import os
out = Path(os.environ["QFBENCH_AGENT_OUT"])
out.joinpath("leak.json").symlink_to(Path(os.environ["QFBENCH_TASK_DIR"]) / "value.json")
```'''
        with tempfile.TemporaryDirectory() as directory, MockServer([code]) as endpoint:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            with patch.dict(os.environ, {"AGENTHON_MAX_ATTEMPTS": "1", "AGENTHON_WALL_CLOCK_SEC": "30"}):
                with self.assertRaises(RuntimeError):
                    solve(task, out, client)
            self.assertEqual(list(out.iterdir()), [])

    def test_nonempty_or_symlink_output_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, MockServer([]) as endpoint:
            root = Path(directory); task = synthetic_task(root)
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            nonempty = root / "nonempty"; nonempty.mkdir(); (nonempty / "stale.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "empty"):
                solve(task, nonempty, client)
            target = root / "target"; target.mkdir()
            symlink = root / "output-link"; symlink.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symlink"):
                solve(task, symlink, client)

    def test_non_finite_wall_clock_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, MockServer([]) as endpoint:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            client = OpenAICompatibleClient(endpoint, "synthetic-model", 5)
            with patch.dict(os.environ, {"AGENTHON_WALL_CLOCK_SEC": "inf"}):
                with self.assertRaisesRegex(ValueError, "finite"):
                    solve(task, out, client)

    def test_cli_fails_closed_without_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); task, out = synthetic_task(root), root / "out"
            env = {"PATH": os.environ.get("PATH", ""), "PYTHONPYCACHEPREFIX": "/tmp/agenthon-test-cache"}
            result = subprocess.run(
                [sys.executable, str(ROOT / "agent.py"), "solve", "--task-dir", str(task), "--out", str(out)],
                env=env, capture_output=True, text=True,
            )
        self.assertEqual(result.returncode, 1)
        self.assertIn("MODEL_ENDPOINT", result.stderr)


if __name__ == "__main__":
    unittest.main()
