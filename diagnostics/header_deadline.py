#!/usr/bin/env python3
"""Observe real loopback response-header timing without changing production code.

The server deliberately sends a finite, slowly completed header. This tests the
deadline boundary before response.read1(), not the already-covered body drip.
Never uses a real model, credential, proxy, or external network destination.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import platform
import threading
import time
from unittest.mock import patch
import sys

PROTOCOL = {
    "version": "agenthon-header-deadline-observation/v1",
    "shared_budget_seconds": 0.5,
    "per_attempt_seconds": 0.5,
    "max_retries": 0,
    "header_chunks": 12,
    "header_chunk_delay_seconds": 0.1,
    "handler_settle_cap_seconds": 3.0,
    "wall_excess_reporting_tolerance_seconds": 0.1,
}


class Events:
    def __init__(self):
        self.start = time.monotonic()
        self.lock = threading.Lock()
        self.rows = []
        self.requests = 0
        self.handler_done = threading.Event()

    def add(self, name, **values):
        elapsed = time.monotonic() - self.start
        with self.lock:
            self.rows.append({"event": name, "seconds": elapsed, **values})


def load_engine(path):
    name = "header_diagnostic_engine"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def observe(engine):
    events = Events()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            events.requests += 1
            events.add("server_request_headers_received")
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if self.path != "/v1/chat/completions" or size > 8192:
                    raise ValueError("unexpected synthetic request")
                self.rfile.read(size)  # Never persist the request body or headers.
                events.add("server_request_body_read", byte_count=size)
                body = json.dumps({"choices": [{"message": {"content": "synthetic"}}]}).encode()
                prefix = b"HTTP/1.0 200 OK\r\nContent-Length: " + str(len(body)).encode() + b"\r\nX-Synthetic-Drip: "
                self.wfile.write(prefix)
                self.wfile.flush()
                events.add("server_incomplete_response_headers_started")
                for index in range(PROTOCOL["header_chunks"]):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    events.add("server_header_byte_sent", index=index)
                    time.sleep(PROTOCOL["header_chunk_delay_seconds"])
                self.wfile.write(b"\r\n\r\n")
                self.wfile.flush()
                events.add("server_response_headers_completed")
                self.wfile.write(body)
                self.wfile.flush()
                events.add("server_response_body_sent", byte_count=len(body))
            except (BrokenPipeError, ConnectionResetError) as exc:
                events.add("server_peer_closed", error_type=type(exc).__name__)
            except Exception as exc:
                events.add("server_error", error_type=type(exc).__name__)
            finally:
                events.add("server_handler_finished")
                events.handler_done.set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    real_open = engine._open_house_request

    def observed_open(request, timeout):
        # Observe the actual production blocking open call, not a fake transport.
        events.add("client_open_enter", socket_timeout=timeout)
        try:
            response = real_open(request, timeout)
        except Exception as exc:
            events.add("client_open_error", error_type=type(exc).__name__)
            raise
        events.add("client_open_returned_response_headers")
        return response

    result = {"returned_content": False, "endpoint_error_type": None}
    thread.start()
    try:
        # Remove inherited credential/proxy configuration; no existing secret is read.
        with patch.dict(os.environ, {"MODEL_TOKEN": "synthetic-header-diagnostic", "QFBENCH_SEED": "0"}, clear=True), \
             patch.object(engine, "_open_house_request", observed_open):
            client = engine.OpenAICompatibleClient(
                f"http://127.0.0.1:{server.server_port}", "synthetic", PROTOCOL["per_attempt_seconds"],
                max_retries=PROTOCOL["max_retries"], retry_backoff_sec=0)
            start = time.monotonic()
            events.add("client_complete_enter", shared_budget=PROTOCOL["shared_budget_seconds"])
            try:
                content = client.complete([{"role": "user", "content": "synthetic"}], 1,
                                          PROTOCOL["shared_budget_seconds"])
                result["returned_content"] = bool(content)
            except engine.EndpointError as exc:
                result["endpoint_error_type"] = type(exc).__name__
                result["endpoint_error_category"] = str(exc)  # Production controlled text only.
            result["client_wall_seconds"] = time.monotonic() - start
            events.add("client_complete_returned", success=result["returned_content"])
            result["attempted_requests"] = client.requests_attempted
        result["server_handler_settled"] = events.handler_done.wait(PROTOCOL["handler_settle_cap_seconds"])
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    result["server_received_requests"] = events.requests
    result["timeline"] = sorted(events.rows, key=lambda row: row["seconds"])
    result["observed_wall_budget_excess"] = result["client_wall_seconds"] > (
        PROTOCOL["shared_budget_seconds"] + PROTOCOL["wall_excess_reporting_tolerance_seconds"])
    result["header_phase_completed_before_open_return"] = any(
        row["event"] == "server_response_headers_completed" for row in events.rows) and any(
        row["event"] == "client_open_returned_response_headers" for row in events.rows)
    result["scope"] = "one instrumented real loopback header-drip observation; not full suite or House API"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, required=True)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.receipt.exists():
        raise FileExistsError("refusing to overwrite any observation")
    started = args.receipt.with_suffix(".started.json")
    engine_hash = hashlib.sha256(args.engine.read_bytes()).hexdigest()
    script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    with started.open("x") as handle:
        json.dump({"started_at_utc": datetime.now(timezone.utc).isoformat(), "pid": os.getpid(),
                   "protocol": PROTOCOL, "engine_sha256": engine_hash, "script_sha256": script_hash}, handle, indent=2)
    engine = load_engine(args.engine)
    report = {"protocol": PROTOCOL, "scope": "local synthetic HTTP only",
        "engine_sha256": engine_hash, "script_sha256": script_hash,
        "python_version": platform.python_version(), "machine": platform.machine(),
        "observation": observe(engine), "verified_at_utc": datetime.now(timezone.utc).isoformat(),
        "official_score": None, "full_suite_pass_claimed": False, "real_model_requests": 0,
        "new_spend_cny": 0}
    if hashlib.sha256(args.engine.read_bytes()).hexdigest() != engine_hash or hashlib.sha256(Path(__file__).read_bytes()).hexdigest() != script_hash:
        raise RuntimeError("source changed during observation")
    with args.receipt.open("x") as handle:
        json.dump(report, handle, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps(report, indent=2))
    return 0  # Collection success, NOT a deadline/full-suite pass.


if __name__ == "__main__":
    raise SystemExit(main())
