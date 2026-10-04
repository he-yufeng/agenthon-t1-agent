#!/usr/bin/env python3
"""Official public CLI adapter: solve --task-dir PATH --out PATH."""

from __future__ import annotations

import argparse
import math
import os
import sys
from pathlib import Path

from engine import EndpointError, solve
from thinking_control import NonThinkingHouseClient as OpenAICompatibleClient


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(prog="agent.py")
    value.add_argument("verb", choices=["solve"])
    value.add_argument("--task-dir", type=Path, required=True)
    value.add_argument("--out", type=Path, required=True)
    return value


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        timeout = float(os.environ.get("AGENTHON_HTTP_TIMEOUT_SEC", "120"))
        if not math.isfinite(timeout) or not 1 <= timeout <= 600:
            raise ValueError("AGENTHON_HTTP_TIMEOUT_SEC must be finite and between 1 and 600")
        max_response_bytes = int(os.environ.get("AGENTHON_MAX_RESPONSE_BYTES", "2000000"))
        max_retries = int(os.environ.get("AGENTHON_ENDPOINT_MAX_RETRIES", "1"))
        retry_backoff = float(os.environ.get("AGENTHON_ENDPOINT_RETRY_BACKOFF_SEC", "0.25"))
        client = OpenAICompatibleClient(
            os.environ.get("MODEL_ENDPOINT", ""), os.environ.get("MODEL_NAME", ""), timeout,
            max_response_bytes, max_retries, retry_backoff,
        )
        solve(args.task_dir, args.out, client)
        return 0
    except (EndpointError, OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"agenthon-t1: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
