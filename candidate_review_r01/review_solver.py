"""Generic same-House review strategy over the frozen participant core (MIT)."""

from __future__ import annotations

import hashlib
import tempfile
import time
from pathlib import Path

import engine as core


BASE_ENGINE_SHA256 = "1c85bae327299e6eeacf7f1c788dfd5bf93d8f3c67a45e723280f37aaa4bef3a"
MAX_LOGICAL_CALLS = 3
MAX_UNIT_SECONDS = 420.0
PUBLICATION_RESERVE = 15.0
EXECUTION_RESERVE = 25.0
REVIEW_RESERVE = 45.0
MAX_CALL_SECONDS = 120.0
MAX_EXECUTION_SECONDS = 90.0

SYSTEM_PROMPT = (
    "Write one Python program that solves the supplied quantitative task. "
    "Read inputs from QFBENCH_TASK_DIR and write every required deliverable under "
    "QFBENCH_AGENT_OUT. Use environment variables rather than fixed container paths. "
    "Legacy /app/data normally means QFBENCH_TASK_DIR/environment/data; an input "
    "named /app/<filename> means the unique matching file under that directory. "
    "Inspect the actual allowed input schema in your program before computing. "
    "Follow the task's exact definitions, units, signs, timestamps and output schema. "
    "Include task-appropriate numerical and semantic self-checks; don't merely "
    "create plausible-looking output. Do not read checks or reference material, "
    "write a grader, install packages or use the network. Return one complete "
    "Python program only."
)

REVIEW_PROMPT = (
    "Review and, where needed, revise your program against the original problem. "
    "A successful program exit and complete files DO NOT establish mathematical "
    "correctness. Independently examine formulas and financial conventions, input "
    "schema and alignment, units/signs, time/as-of or look-ahead constraints, "
    "numerical stability/tolerances, and every requested output. Add useful "
    "task-specific self-checks and fix any genuine issue. Do not invent expected "
    "answers or make changes just for appearance. Return one complete Python "
    "program, including the original implementation if it is sound. No tests, "
    "reference data or generated output bytes are available in this feedback."
)


def verify_base() -> None:
    path = Path(core.__file__).resolve()
    if hashlib.sha256(path.read_bytes()).hexdigest() != BASE_ENGINE_SHA256:
        raise RuntimeError("candidate requires its exact frozen base engine")


def make_budget(task_dir: Path) -> core.Budget:
    original = core.make_budget(task_dir)
    return core.Budget(
        deadline=min(original.deadline, time.monotonic() + MAX_UNIT_SECONDS - PUBLICATION_RESERVE),
        max_attempts=core.bounded_int("AGENTHON_REVIEW_MAX_CALLS", MAX_LOGICAL_CALLS, 1, MAX_LOGICAL_CALLS),
        max_output_tokens=min(original.max_output_tokens, core.configured_house_limits()[1]),
    )


def _messages(task_dir: Path, task: str, instruction: str, soft_outputs: tuple[Path, ...]):
    hints = core.legacy_input_path_hints(task_dir, instruction)
    if soft_outputs:
        hints += (
            " Public-bundle filename-only output hints: "
            + ", ".join(path.name for path in soft_outputs)
            + ". These are instruction-aligned filenames, not checker results."
        )
    return [{"role": "system", "content": SYSTEM_PROMPT + hints},
            {"role": "user", "content": task}]


def _stage_attempt(response, task_dir, stage, out_dir, instruction,
                   required, soft, guard, limits, timeout_sec):
    if guard.rejects_text(response):
        raise core.OutputPolicyViolation(core.POLICY_REJECTION_MESSAGE)
    code = core.rewrite_legacy_input_literals(core.extract_python(response), task_dir, instruction)
    result = core.execute(code, task_dir, stage, out_dir, timeout_sec,
                          guard, limits.max_process_output_bytes)
    if result.output_policy_violation:
        raise core.OutputPolicyViolation(core.POLICY_REJECTION_MESSAGE)
    if result.timed_out:
        core.clear_generated_output(out_dir)
        return False, "Program execution timed out.", False
    if result.output_overflow:
        core.clear_generated_output(out_dir)
        return False, "Program output exceeded its byte limit.", False
    core.capture_direct_output(out_dir, stage, guard, limits)
    if not result.succeeded:
        return False, "Program failure class: " + result.failure_class + ". Output withheld.", False
    files = core.deliverables(stage, guard, limits)
    missing = core.missing_required_outputs(stage, required)
    if missing or not files:
        return False, (f"Program exited successfully; explicit files missing: {len(missing)}; "
                       "no complete deliverable set. Output withheld."), False
    soft_missing = core.missing_required_outputs(stage, soft)
    feedback = "Program exited successfully; explicit output completeness passed. "
    if soft_missing:
        feedback += f"Public filename-only soft hints missing: {len(soft_missing)}. "
    return True, feedback + "Generated outputs withheld; semantics not verified.", bool(soft_missing)


def solve(task_dir: Path, out_dir: Path, client: core.ModelClient) -> list[str]:
    verify_base()
    task_dir = task_dir.resolve(strict=True)
    if out_dir.is_symlink():
        raise ValueError("output directory must not be a symlink")
    resolved_out = out_dir.resolve()
    if task_dir == resolved_out or task_dir in resolved_out.parents or resolved_out in task_dir.parents:
        raise ValueError("task and output directories must be disjoint")
    out_dir.mkdir(parents=True, exist_ok=True)
    if any(out_dir.iterdir()):
        raise ValueError("output directory must be empty")
    budget = make_budget(task_dir)
    limits = core.configured_output_limits()
    task = core.discover_task(task_dir, core.bounded_int("AGENTHON_MAX_INPUT_BYTES", 200_000, 1024, 2_000_000))
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8", errors="replace")
    required = core.extract_required_output_paths(instruction)
    soft = core.public_filename_soft_contract(task_dir, instruction)
    guard = core.CanaryGuard.from_prompt(task)
    messages = _messages(task_dir, task, instruction, soft)
    best = None
    review_done = False
    last_feedback = "No complete solution."
    with tempfile.TemporaryDirectory(prefix="agenthon-review-") as workspace:
        for index in range(budget.max_attempts):
            remaining = budget.remaining()
            if remaining <= EXECUTION_RESERVE:
                break
            # Keep a usable first result if insufficient time remains for a
            # substantive review and execution. Never pretend it was reviewed.
            if best is not None and not review_done and remaining < REVIEW_RESERVE:
                break
            stage = Path(workspace) / f"attempt-{index + 1}"
            stage.mkdir()
            try:
                response = client.complete(messages, budget.max_output_tokens,
                                           min(MAX_CALL_SECONDS, remaining - EXECUTION_RESERVE))
            except core.EndpointError:
                last_feedback = "Model endpoint failure; no output inspected."
                break
            if guard.rejects_text(response):
                core.clear_generated_output(out_dir)
                raise RuntimeError(core.POLICY_REJECTION_MESSAGE)
            if best is not None:
                review_done = True
            execution_time = min(MAX_EXECUTION_SECONDS, budget.remaining())
            if index == 0:
                execution_time = min(execution_time, budget.remaining() - REVIEW_RESERVE)
            if execution_time <= 0:
                break
            try:
                complete, last_feedback, soft_missing = _stage_attempt(
                    response, task_dir, stage, out_dir, instruction,
                    required, soft, guard, limits, execution_time,
                )
            except core.OutputPolicyViolation:
                core.clear_generated_output(out_dir)
                raise RuntimeError(core.POLICY_REJECTION_MESSAGE) from None
            except OSError:
                core.clear_generated_output(out_dir)
                last_feedback = "Runtime I/O failure."
                complete, soft_missing = False, False
            except ValueError:
                core.clear_generated_output(out_dir)
                last_feedback = "Response does not contain exactly one executable program."
                complete, soft_missing = False, False
            except RuntimeError:
                core.clear_generated_output(out_dir)
                last_feedback = "Deliverable validation failed."
                complete, soft_missing = False, False
            if complete:
                best = stage
                if review_done and not soft_missing:
                    break
            messages.extend([
                {"role": "assistant", "content": response},
                {"role": "user", "content": (
                    last_feedback + "\n" +
                    (REVIEW_PROMPT if complete else
                     "Repair the complete program against the original problem. "
                     "Preserve input/output contracts and include appropriate self-checks. "
                     "Return one complete Python program only.")
                )},
            ])
        if best is None:
            raise RuntimeError("candidate has no complete solution: " + last_feedback)
        return core.publish_atomic(best, out_dir, guard, limits)
