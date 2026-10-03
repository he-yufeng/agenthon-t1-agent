#!/usr/bin/env python3
"""Candidate CLI; package beside the exact frozen participant engine (MIT)."""

from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path

from engine import EndpointError, OpenAICompatibleClient
from review_solver import solve, verify_base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.py")
    parser.add_argument("verb", choices=["solve"])
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        verify_base()
        timeout = float(os.environ.get("AGENTHON_HTTP_TIMEOUT_SEC", "120"))
        if not math.isfinite(timeout) or not 1 <= timeout <= 600:
            raise ValueError("invalid endpoint timeout")
        client = OpenAICompatibleClient(
            os.environ.get("MODEL_ENDPOINT", ""), os.environ.get("MODEL_NAME", ""), timeout,
            int(os.environ.get("AGENTHON_MAX_RESPONSE_BYTES", "2000000")),
            int(os.environ.get("AGENTHON_ENDPOINT_MAX_RETRIES", "1")),
            float(os.environ.get("AGENTHON_ENDPOINT_RETRY_BACKOFF_SEC", "0.25")),
        )
        solve(args.task_dir, args.out, client)
        return 0
    except (EndpointError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        # No endpoint, response body, task output or token in CLI diagnostics.
        print("agenthon-review: " + type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
