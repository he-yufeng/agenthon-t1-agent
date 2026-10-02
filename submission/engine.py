"""Dependency-free core for the minimal participant-authored T1 API agent."""

from __future__ import annotations

import ast
import hashlib
import http.client
import json
import math
import os
import re
import shlex
import signal
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

EXCLUDED_PARTS = {"checks", "reference", "reference_data", "solution", ".git", "__pycache__"}
TEXT_SUFFIXES = {".md", ".txt", ".json", ".toml", ".csv", ".py", ".yaml", ".yml"}
FENCE = re.compile(r"```(?:python|py)?\s*\n(.*?)```", re.I | re.S)
POLICY_REJECTION_MESSAGE = "unsafe output rejected"
DELIVERABLE_LIMIT_MESSAGE = "deliverable limits exceeded"
PROCESS_OUTPUT_LIMIT_MESSAGE = "program output exceeded byte limit"
EXCEPTION_EXIT_CLASSES = {
    200: "SyntaxError",
    201: "ImportError",
    202: "FileNotFoundError",
    203: "LookupError",
    204: "TypeError",
    205: "ValueError",
    206: "AttributeError",
    207: "NameError",
    208: "ArithmeticError",
    209: "OSError",
    210: "MemoryError",
    211: "OtherException",
}
EXCEPTION_WRAPPER = r'''import os as _os
import runpy as _runpy
import sys as _sys
_exit = _os._exit
_run_path = _runpy.run_path
_SyntaxError = SyntaxError
_ImportError = ImportError
_FileNotFoundError = FileNotFoundError
_LookupError = LookupError
_TypeError = TypeError
_ValueError = ValueError
_AttributeError = AttributeError
_NameError = NameError
_ArithmeticError = ArithmeticError
_OSError = OSError
_MemoryError = MemoryError
_SystemExit = SystemExit
_Exception = Exception
_BaseException = BaseException
try:
    _run_path(_sys.argv[1], run_name="__main__")
except _SyntaxError:
    _exit(200)
except _ImportError:
    _exit(201)
except _FileNotFoundError:
    _exit(202)
except _LookupError:
    _exit(203)
except _TypeError:
    _exit(204)
except _ValueError:
    _exit(205)
except _AttributeError:
    _exit(206)
except _NameError:
    _exit(207)
except _ArithmeticError:
    _exit(208)
except _OSError:
    _exit(209)
except _MemoryError:
    _exit(210)
except _SystemExit:
    raise
except _Exception:
    _exit(211)
except _BaseException:
    _exit(211)
'''
FORBIDDEN_GRADER_FILENAMES = {
    ".coverage",
    "coverage.xml",
    "junit.xml",
    "pytest.xml",
}
FORBIDDEN_GRADER_STEMS = {
    "grader_report",
    "grader-report",
    "pytest_report",
    "pytest-report",
    "scorer_report",
    "scorer-report",
    "verifier_report",
    "verifier-report",
}
FORBIDDEN_GRADER_DIRECTORIES = {".pytest_cache", "htmlcov"}
CANARY_TOKEN_BYTES = re.compile(rb"[A-Za-z0-9][A-Za-z0-9_:/+=@-]{11,1023}")
CANARY_ASSIGNMENT = re.compile(
    r"""(?ix)
    \b(?:synthetic[\s_-]*)?canary(?:[\s_-]*(?:token|guid|value|secret))?\b
    [\"'`]?\s*(?:(?:is|equals?)\s+|[:=]\s*)
    [\"'`]?(?P<token>[A-Za-z0-9][A-Za-z0-9_:/+=@-]{11,255})
    """
)
# The frozen public T1 corpus uses headers such as
# ``quantitative-finance-bench-canary GUID <uuid>``.  That form has no ``:`` or
# ``=`` after GUID, so it is deliberately recognized separately from the more
# general assignment syntax above.  Keeping this UUID-shaped avoids turning
# ordinary prose after the words "canary GUID" into a false fingerprint.
CANARY_GUID_DECLARATION = re.compile(
    r"(?i)\bcanary[\s_-]+guid\b\s*(?:[:=]\s*)?[\"'`]?"
    r"(?P<token>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b"
)
CANARY_SHAPED = re.compile(
    r"(?i)\b(?:synthetic[_-])?canary(?:[_:-][A-Za-z0-9][A-Za-z0-9_:/+=@-]*)+\b"
)
EXPLICIT_OUTPUT_PATH = re.compile(
    r"(?<![\w./-])(?:/app/output|/output)"
    r"(?P<suffix>/[A-Za-z0-9._@+=/-]*)"
    r"(?![\w./@+=%:-])"
)
INSTRUCTION_FILENAME = re.compile(
    r"(?<![A-Za-z0-9_.-])(?P<name>[A-Za-z0-9_.-]+\.(?:json|parquet|csv|txt))"
    r"(?!(?:[A-Za-z0-9_-]|\.[A-Za-z0-9]))",
    re.I,
)
LEGACY_APP_PATH = re.compile(
    r"(?<![A-Za-z0-9_.-])(?P<path>/app/(?!output(?:/|\b))"
    r"[A-Za-z0-9_@+=.-]+(?:/[A-Za-z0-9_@+=.-]+)*/?)"
    r"(?![A-Za-z0-9_@+=./-])"
)
PUBLIC_CARD_SECTION = re.compile(rb"^\[([A-Za-z0-9_.-]+)\]$")
PUBLIC_CARD_SPLIT = re.compile(rb'''^split\s*=\s*["']([^"']+)["']$''')
PUBLIC_CHECKER_FILENAME = re.compile(
    rb'''["'](?P<path>[A-Za-z0-9_./{}-]+\.(?:json|parquet|csv|txt))["']''',
    re.I,
)
PUBLIC_CHECKER_NON_OUTPUT_LINE = re.compile(
    rb"(data|input|ref|reference|log)_(dir|path)"
    rb"|input_(json|csv|parquet|text)|/app/data|/input|/tests/reference|/logs"
    rb"|[^a-z0-9_](data|ref) */",
    re.I,
)
MAX_PUBLIC_CARD_BYTES = 262_144
MAX_CARD_BYTES = 262_144
MAX_PUBLIC_CHECKER_FILES = 128
MAX_PUBLIC_CHECKER_FILE_BYTES = 2_000_000
MAX_PUBLIC_CHECKER_TOTAL_BYTES = 8_000_000
MAX_PUBLIC_CHECKER_LINE_BYTES = 131_072
MAX_LEGACY_MAP_FILES = 2048
MAX_ENV_DOCKERFILE_BYTES = 262_144


class OutputPolicyViolation(RuntimeError):
    """Internal verdict with a deliberately detail-free public message."""


@dataclass(frozen=True)
class CanaryGuard:
    """One-way fingerprints derived only from the already-supplied task prompt."""

    fingerprints: frozenset[tuple[int, bytes]]

    @staticmethod
    def _fingerprint(token: bytes) -> tuple[int, bytes]:
        normalized = token.lower()
        return len(normalized), hashlib.sha256(normalized).digest()

    @classmethod
    def from_prompt(cls, prompt: str) -> CanaryGuard:
        tokens = [match.group("token") for match in CANARY_ASSIGNMENT.finditer(prompt)]
        tokens.extend(
            match.group("token") for match in CANARY_GUID_DECLARATION.finditer(prompt)
        )
        tokens.extend(match.group(0) for match in CANARY_SHAPED.finditer(prompt))
        fingerprints = {
            cls._fingerprint(token.encode("ascii"))
            for token in tokens
            if 12 <= len(token) <= 256
        }
        return cls(frozenset(fingerprints))

    def rejects_bytes(self, value: bytes) -> bool:
        if not self.fingerprints:
            return False
        lengths = {length for length, _digest in self.fingerprints}
        for match in CANARY_TOKEN_BYTES.finditer(value):
            candidate = match.group(0).lower()
            for length in lengths:
                if len(candidate) < length:
                    continue
                for start in range(len(candidate) - length + 1):
                    if self._fingerprint(candidate[start:start + length]) in self.fingerprints:
                        return True
        return False

    def rejects_text(self, value: str) -> bool:
        return self.rejects_bytes(value.encode("utf-8", errors="ignore"))

    def rejects_process_output(self, value: str | bytes | None) -> bool:
        if value is None:
            return False
        if isinstance(value, str):
            return self.rejects_text(value)
        return self.rejects_bytes(value)

    def rejects_file(self, path: Path) -> bool:
        if not self.fingerprints:
            return False
        overlap = b""
        with path.open("rb") as handle:
            while chunk := handle.read(1_048_576):
                value = overlap + chunk
                if self.rejects_bytes(value):
                    return True
                overlap = value[-255:]
        return False


def is_forbidden_grader_artifact(relative: Path) -> bool:
    folded_parts = [part.casefold() for part in relative.parts]
    if any(part in FORBIDDEN_GRADER_DIRECTORIES for part in folded_parts):
        return True
    name = folded_parts[-1]
    if name in FORBIDDEN_GRADER_FILENAMES or name == "reward" or name.startswith("reward."):
        return True
    return any(name == stem or name.startswith(stem + ".") for stem in FORBIDDEN_GRADER_STEMS)


def enforce_output_policy(relative: Path, source: Path, guard: CanaryGuard) -> None:
    if (
        is_forbidden_grader_artifact(relative)
        or guard.rejects_text(relative.as_posix())
        or guard.rejects_file(source)
    ):
        raise OutputPolicyViolation(POLICY_REJECTION_MESSAGE)


class ModelClient(Protocol):
    def complete(self, messages: list[dict[str, str]], max_tokens: int, timeout_sec: float) -> str: ...


class EndpointError(RuntimeError):
    pass


@dataclass
class _TransientEndpointFailure(Exception):
    """Internal retry verdict that never retains an endpoint body or URL."""

    reason: str


@dataclass(frozen=True)
class Budget:
    deadline: float
    max_attempts: int
    max_output_tokens: int

    def remaining(self) -> float:
        return max(0.0, self.deadline - time.monotonic())


@dataclass(frozen=True)
class OutputLimits:
    max_files: int
    max_file_bytes: int
    max_total_bytes: int
    max_process_output_bytes: int


@dataclass
class StreamVerdict:
    byte_count: int = 0
    overflow: bool = False
    policy_violation: bool = False
    read_failed: bool = False


@dataclass(frozen=True)
class ExecutionResult:
    succeeded: bool
    failure_class: str | None
    timed_out: bool
    output_overflow: bool
    output_policy_violation: bool


class _NoHouseRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise EndpointError("model endpoint redirect refused")


def _open_house_request(request, timeout):
    # Default ProxyHandler still honors the organizer-injected proxy variables.
    # Never follow a redirect with a per-unit bearer or a prompt body.
    return urllib.request.build_opener(_NoHouseRedirect()).open(request, timeout=timeout)


def _open_house_before_deadline(request, deadline):
    """Bound urllib's connection/header phase by elapsed time, not idle time.

    urllib's socket timeout alone restarts while a header is slowly arriving.
    Each admitted attempt gets one daemon worker; the client's per-unit request
    allowance bounds their number. A response arriving after cancellation is
    closed by its worker and cannot enter the generation/retry path.
    """
    ready = threading.Event()
    lock = threading.Lock()
    state = {"cancelled": False}

    def open_request():
        response = None
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("model endpoint timed out")
            response = _open_house_request(request, timeout=remaining)
        except BaseException as exc:
            with lock:
                if not state["cancelled"]:
                    state["error"] = exc
                elif isinstance(exc, urllib.error.HTTPError):
                    exc.close()
        else:
            with lock:
                if state["cancelled"]:
                    response.close()
                else:
                    state["response"] = response
        finally:
            ready.set()

    if deadline <= time.monotonic():
        raise TimeoutError("model endpoint timed out")
    worker = threading.Thread(target=open_request, daemon=True, name="house-header")
    worker.start()
    ready.wait(max(0.0, deadline - time.monotonic()))
    with lock:
        if not ready.is_set() or time.monotonic() >= deadline:
            state["cancelled"] = True
            response = state.pop("response", None)
            error = state.pop("error", None)
            if response is not None:
                response.close()
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            raise TimeoutError("model endpoint timed out")
        if "error" in state:
            raise state["error"]
        return state.pop("response")


class OpenAICompatibleClient:
    def __init__(self, endpoint: str, model: str, timeout_sec: float = 120.0,
                 max_response_bytes: int = 2_000_000, max_retries: int = 1,
                 retry_backoff_sec: float = 0.25):
        if not endpoint or not model:
            raise EndpointError("MODEL_ENDPOINT and MODEL_NAME are both required")
        if not endpoint.startswith(("http://", "https://")):
            raise EndpointError("MODEL_ENDPOINT must be an HTTP(S) URL")
        if not math.isfinite(timeout_sec) or timeout_sec <= 0:
            raise EndpointError("endpoint timeout must be finite and positive")
        if not 1024 <= max_response_bytes <= 8_000_000:
            raise EndpointError("endpoint response limit must be between 1024 and 8000000 bytes")
        if not isinstance(max_retries, int) or not 0 <= max_retries <= 2:
            raise EndpointError("endpoint retry count must be between 0 and 2")
        if not math.isfinite(retry_backoff_sec) or not 0 <= retry_backoff_sec <= 5:
            raise EndpointError("endpoint retry backoff must be finite and between 0 and 5 seconds")
        # Current public runtime injects the route origin (no path) and admits
        # only /v1/chat/completions. Keep compatibility with the earlier local
        # synthetic harness, which supplied an endpoint already ending in /v1.
        normalized_endpoint = endpoint.rstrip("/")
        try:
            parsed = urlparse(normalized_endpoint)
            if (not parsed.hostname or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment or parsed.params
                    or parsed.path not in ("", "/v1")
                    or any(ord(c) <= 32 or ord(c) >= 127 for c in endpoint)):
                raise ValueError("invalid endpoint")
            _ = parsed.port
        except ValueError:
            raise EndpointError("MODEL_ENDPOINT must be an origin or legacy /v1 URL without credentials or query") from None
        path = parsed.path
        self._legacy_loopback = (parsed.hostname in ("127.0.0.1", "::1")
                                 and path == "/v1" and os.environ.get("QFBENCH_NETWORK") != "restricted")
        self._auth_token()  # reject absent/malformed production credentials before any request
        self.url = normalized_endpoint + (
            "/chat/completions" if path.endswith("/v1") else "/v1/chat/completions"
        )
        self.model = model
        self.timeout_sec = timeout_sec
        self.max_response_bytes = max_response_bytes
        self.max_retries = max_retries
        self.retry_backoff_sec = retry_backoff_sec
        self.request_limit, self.output_token_limit = configured_house_limits()
        self.requests_attempted = 0

    def _auth_token(self):
        token = os.environ.get("MODEL_TOKEN")
        if token is None and self._legacy_loopback:
            token = "unused"  # backward-compatible loopback-only synthetic fixture
        if not token or not re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token):
            raise EndpointError("MODEL_TOKEN is required and must be a valid bearer value")
        return token

    def _complete_once(
        self,
        request: urllib.request.Request,
        request_deadline: float,
    ) -> str:
        try:
            initial_remaining = request_deadline - time.monotonic()
            if initial_remaining <= 0:
                raise _TransientEndpointFailure("model endpoint timed out")
            with _open_house_before_deadline(request, request_deadline) as response:
                declared_values = response.headers.get_all("Content-Length", [])
                declared: int | None = None
                if declared_values:
                    normalized = [
                        token.strip()
                        for value in declared_values
                        for token in value.split(",")
                    ]
                    if (
                        not normalized
                        or any(not token.isdecimal() for token in normalized)
                        or len(set(normalized)) != 1
                    ):
                        raise EndpointError("model endpoint returned an invalid Content-Length")
                    declared = int(normalized[0])
                    if declared > self.max_response_bytes:
                        raise EndpointError("model endpoint response exceeds byte limit")
                body = bytearray()
                target_bytes = (
                    declared if declared is not None else self.max_response_bytes + 1
                )
                while len(body) < target_bytes:
                    remaining = request_deadline - time.monotonic()
                    if remaining <= 0:
                        raise _TransientEndpointFailure("model endpoint timed out")
                    transport = getattr(
                        getattr(getattr(response, "fp", None), "raw", None),
                        "_sock",
                        None,
                    )
                    if transport is None:
                        raise EndpointError(
                            "model endpoint response transport cannot enforce a deadline"
                        )
                    transport.settimeout(remaining)
                    try:
                        chunk = response.read1(
                            min(65_536, target_bytes - len(body))
                        )
                    except http.client.IncompleteRead as exc:
                        raise EndpointError(
                            "model endpoint returned a truncated response"
                        ) from exc
                    if not chunk:
                        break
                    body.extend(chunk)
                if len(body) > self.max_response_bytes:
                    raise EndpointError("model endpoint response exceeds byte limit")
                if declared is not None and len(body) != declared:
                    raise EndpointError("model endpoint returned a truncated response")
        except urllib.error.HTTPError as exc:
            try:
                if 300 <= exc.code < 400:
                    raise EndpointError("model endpoint redirect refused")
                if exc.code == 429:
                    raise _TransientEndpointFailure("model endpoint rate limited (HTTP 429)")
                if 500 <= exc.code <= 599:
                    raise _TransientEndpointFailure(
                        f"model endpoint server error (HTTP {exc.code})"
                    )
                raise EndpointError(f"model endpoint rejected request (HTTP {exc.code})")
            finally:
                exc.close()
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            if isinstance(exc, urllib.error.URLError):
                if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                    raise _TransientEndpointFailure("model endpoint timed out") from exc
                if isinstance(exc.reason, ConnectionError):
                    raise _TransientEndpointFailure(
                        "model endpoint connection failed"
                    ) from exc
                raise EndpointError("model endpoint transport failure") from exc
            raise _TransientEndpointFailure("model endpoint timed out") from exc
        except ConnectionError as exc:
            raise _TransientEndpointFailure("model endpoint connection failed") from exc
        except OSError as exc:
            raise EndpointError("model endpoint transport failure") from exc

        try:
            value = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise EndpointError("model endpoint returned malformed JSON") from exc
        try:
            choices = value["choices"]
            if not isinstance(choices, list) or not choices:
                raise TypeError
            message = choices[0]["message"]
            if not isinstance(message, dict):
                raise TypeError
            content = message["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise EndpointError("model endpoint returned an invalid response schema") from exc
        if not isinstance(content, str) or not content.strip():
            raise EndpointError("model endpoint returned empty/non-text content")
        return content

    def complete(self, messages: list[dict[str, str]], max_tokens: int, timeout_sec: float) -> str:
        try:
            seed = int(os.environ.get("QFBENCH_SEED", "0"))
        except ValueError as exc:
            raise EndpointError("QFBENCH_SEED must be an integer") from exc
        if not math.isfinite(timeout_sec) or timeout_sec <= 0:
            raise EndpointError("model endpoint wall-clock budget must be finite and positive")
        if type(max_tokens) is not int or max_tokens < 1:
            raise EndpointError("model output token limit must be a positive integer")
        payload = json.dumps({
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "seed": seed,
            "max_tokens": min(max_tokens, self.output_token_limit),
        }).encode("utf-8")
        request = urllib.request.Request(
            self.url,
            data=payload,
            method="POST",
            headers={
                # Missing bearer is accepted only for the legacy literal
                # loopback /v1 synthetic fixture, never a production route.
                "Authorization": "Bearer " + self._auth_token(),
                "Content-Type": "application/json",
            },
        )
        deadline = time.monotonic() + timeout_sec
        total_requests = self.max_retries + 1
        for request_number in range(1, total_requests + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise EndpointError(
                    "model endpoint wall-clock budget exhausted before request"
                )
            try:
                if self.requests_attempted >= self.request_limit:
                    raise EndpointError("model endpoint request allowance exhausted")
                # Conservatively charge every transport attempt, including a
                # retry after an unknown admission outcome. No quota reset on
                # a subsequent complete() call within this unit/client.
                self.requests_attempted += 1
                request_deadline = min(
                    deadline,
                    time.monotonic() + self.timeout_sec,
                )
                content = self._complete_once(
                    request,
                    request_deadline,
                )
            except _TransientEndpointFailure as exc:
                remaining = deadline - time.monotonic()
                if request_number == total_requests:
                    raise EndpointError(
                        f"{exc.reason}; transient retry limit exhausted after "
                        f"{request_number} request(s)"
                    ) from exc
                delay = min(
                    self.retry_backoff_sec * (2 ** (request_number - 1)),
                    max(0.0, remaining),
                )
                if remaining <= delay:
                    raise EndpointError(
                        f"{exc.reason}; wall-clock budget exhausted after "
                        f"{request_number} request(s)"
                    ) from exc
                if delay:
                    time.sleep(delay)
                continue
            if time.monotonic() > deadline:
                raise EndpointError("model endpoint wall-clock budget exhausted after response")
            return content
        raise EndpointError("model endpoint retry state exhausted")


def bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def configured_output_limits() -> OutputLimits:
    return OutputLimits(
        max_files=bounded_int("AGENTHON_MAX_DELIVERABLE_FILES", 128, 1, 1000),
        max_file_bytes=bounded_int(
            "AGENTHON_MAX_DELIVERABLE_FILE_BYTES", 64 * 1024 * 1024, 1024, 64 * 1024 * 1024,
        ),
        max_total_bytes=bounded_int(
            "AGENTHON_MAX_DELIVERABLE_TOTAL_BYTES", 64 * 1024 * 1024, 1024, 64 * 1024 * 1024,
        ),
        max_process_output_bytes=bounded_int(
            "AGENTHON_MAX_PROCESS_OUTPUT_BYTES", 1_000_000, 1024, 8_000_000,
        ),
    )


def configured_house_limits() -> tuple[int, int]:
    """Current public Development allowance: 25 requests, 4000 output tokens."""
    requests = bounded_int("AGENTHON_HOUSE_MAX_REQUESTS", 25, 1, 25)
    output_tokens = bounded_int("AGENTHON_HOUSE_MAX_OUTPUT_TOKENS", 4000, 1, 4000)
    return requests, output_tokens


def _strip_toml_comment(line: str) -> str:
    """Remove a TOML comment while respecting quoted strings on the same line."""
    quote = ""
    escaped = False
    for index, character in enumerate(line):
        if quote == '"':
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
        elif quote == "'":
            if character == quote:
                quote = ""
        elif character in {'"', "'"}:
            quote = character
        elif character == "#":
            return line[:index]
    return line


def _limited_toml_number(token: str) -> object:
    """Parse the TOML numeric forms relevant to agent.timeout_sec."""
    value = token.strip()
    decimal = r"(?:0|[1-9](?:_?\d)*)"
    digits = r"\d(?:_?\d)*"
    if re.fullmatch(rf"[+-]?{decimal}", value):
        return int(value.replace("_", ""), 10)
    bases = {
        r"0x[0-9A-Fa-f](?:_?[0-9A-Fa-f])*": 16,
        r"0o[0-7](?:_?[0-7])*": 8,
        r"0b[01](?:_?[01])*": 2,
    }
    for pattern, base in bases.items():
        if re.fullmatch(pattern, value):
            return int(value.replace("_", "")[2:], base)
    finite_float = rf"[+-]?(?:{decimal}\.{digits}(?:[eE][+-]?{digits})?|{decimal}[eE][+-]?{digits})"
    if re.fullmatch(finite_float, value) or re.fullmatch(r"[+-]?(?:inf|nan)", value):
        return float(value.replace("_", ""))
    return value


def _agent_timeout_values(text: str) -> list[object]:
    """Extract the supported timeout surface identically on Python 3.9 and 3.13+."""
    section = ""
    values: list[object] = []
    agent_key = r'''(?:agent|"agent"|'agent')'''
    timeout_key = r'''(?:timeout_sec|"timeout_sec"|'timeout_sec')'''
    for raw_line in text.splitlines():
        line = _strip_toml_comment(raw_line).strip()
        if not line:
            continue
        if re.fullmatch(rf"\[\s*{agent_key}\s*\]", line):
            section = "agent"
            continue
        if line.startswith("[") and line.endswith("]"):
            section = "other"
            continue
        match = None
        if section == "agent":
            match = re.fullmatch(rf"{timeout_key}\s*=\s*(\S+)", line)
        elif not section:
            match = re.fullmatch(
                rf"{agent_key}\s*\.\s*{timeout_key}\s*=\s*(\S+)", line
            )
        if match:
            values.append(_limited_toml_number(match.group(1)))
            continue
        if not section:
            inline = re.fullmatch(rf"{agent_key}\s*=\s*\{{(.*)\}}", line)
            if inline:
                for item in re.finditer(
                    rf"(?:^|,)\s*{timeout_key}\s*=\s*([^,}}]+)",
                    inline.group(1),
                ):
                    values.append(_limited_toml_number(item.group(1)))
    return values


def card_agent_timeout(task_dir: Path) -> float:
    card = task_dir / "card.toml"
    if card.is_symlink():
        raise ValueError("card.toml must not be a symlink")
    if not card.exists():
        return 1800.0
    if not card.is_file():
        raise ValueError("card.toml must be a regular file")
    if card.stat().st_size > MAX_CARD_BYTES:
        raise ValueError("card.toml exceeds byte limit")
    try:
        text = card.read_bytes().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("card.toml must be valid UTF-8") from exc

    values = _agent_timeout_values(text)
    if len(values) != 1:
        raise ValueError(f"card.toml must have exactly one [agent].timeout_sec; found {len(values)}")
    if isinstance(values[0], bool) or not isinstance(values[0], (int, float)):
        raise ValueError("[agent].timeout_sec must be numeric")
    value = float(values[0])
    if not math.isfinite(value) or value <= 15:
        raise ValueError("agent timeout must be finite and exceed publication reserve")
    return value


def make_budget(task_dir: Path) -> Budget:
    card_limit = card_agent_timeout(task_dir)
    try:
        requested = float(os.environ.get("AGENTHON_WALL_CLOCK_SEC", card_limit))
    except ValueError as exc:
        raise ValueError("AGENTHON_WALL_CLOCK_SEC must be numeric") from exc
    if not math.isfinite(requested) or requested <= 15:
        raise ValueError("AGENTHON_WALL_CLOCK_SEC must be finite and exceed 15")
    wall_clock = min(requested, card_limit) - 15.0
    return Budget(
        deadline=time.monotonic() + wall_clock,
        max_attempts=bounded_int("AGENTHON_MAX_ATTEMPTS", 2, 1, 3),
        max_output_tokens=bounded_int("AGENTHON_MAX_OUTPUT_TOKENS", 4096, 256, 16384),
    )


def discover_task(task_dir: Path, max_bytes: int) -> str:
    instruction = task_dir / "instruction.md"
    if not instruction.is_file():
        raise FileNotFoundError("task directory must contain instruction.md")
    if instruction.is_symlink():
        raise ValueError("instruction.md must not be a symlink")
    remaining = max_bytes
    instruction_header = "# instruction.md\n"
    if instruction.stat().st_size > remaining - len(instruction_header.encode("utf-8")):
        raise ValueError("instruction.md exceeds input byte budget")
    chunks = [instruction_header + instruction.read_text(encoding="utf-8", errors="replace")]
    remaining -= len(chunks[0].encode("utf-8"))
    if remaining < 0:
        raise ValueError("instruction.md exceeds input byte budget")

    for path in sorted(task_dir.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"task input must not be a symlink: {path.relative_to(task_dir)}")
        if not path.is_file() or path == instruction:
            continue
        relative = path.relative_to(task_dir)
        if any(part in EXCLUDED_PARTS for part in relative.parts):
            continue
        if relative.as_posix() in {"card.toml", "manifest.json"}:
            chunks.append(f"\n# file: {relative.as_posix()} [contract metadata omitted]\n")
            continue
        size = path.stat().st_size
        header = f"\n# file: {relative.as_posix()} ({size} bytes)\n"
        if path.suffix.lower() in TEXT_SUFFIXES and size <= remaining:
            body = path.read_text(encoding="utf-8", errors="replace")
            addition = header + body
        else:
            addition = header + "[binary or omitted from prompt; read it from task_dir at runtime]\n"
        encoded = addition.encode("utf-8")
        if len(encoded) > remaining:
            continue
        chunks.append(addition)
        remaining -= len(encoded)
    return "".join(chunks)


def extract_python(text: str) -> str:
    matches = FENCE.findall(text)
    if len(matches) > 1:
        raise ValueError("model returned multiple executable code blocks")
    code = matches[0].strip() if matches else text.strip()
    if not code:
        raise ValueError("model returned no executable code")
    return code


def extract_required_output_paths(instruction: str) -> tuple[Path, ...]:
    """Return only unambiguous file paths explicitly rooted at an output mount.

    Bare output-directory mentions are intentionally not contracts.  Dot segments
    are normalized, while traversal and empty interior segments fail closed.  The
    deliberately narrow ASCII token grammar avoids treating prose or URLs as paths.
    Grader-owned artifacts may be mentioned while describing verification, but are
    never participant deliverables and therefore cannot create completeness duties.
    """
    required: set[Path] = set()
    for match in EXPLICIT_OUTPUT_PATH.finditer(instruction):
        suffix = match.group("suffix")
        if suffix == "/":
            continue
        raw = suffix[1:]
        if raw.endswith(".") and raw.rsplit("/", 1)[-1] not in {".", ".."}:
            # A full stop immediately after a path is ordinary prose punctuation.
            raw = raw[:-1]
        if not raw or raw.endswith("/"):
            continue
        parts = raw.split("/")
        if "" in parts or ".." in parts:
            raise ValueError("instruction contains an unsafe explicit output path")
        normalized = tuple(part for part in parts if part != ".")
        if not normalized:
            continue
        relative = Path(*normalized)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("instruction contains an unsafe explicit output path")
        if is_forbidden_grader_artifact(relative):
            continue
        required.add(relative)
    return tuple(sorted(required, key=lambda path: path.as_posix()))


def is_public_dev_bundle(task_dir: Path) -> bool:
    """Recognize only a strict public-dev card without retaining unrelated fields."""
    card = task_dir / "card.toml"
    try:
        if card.is_symlink() or not card.is_file() or card.stat().st_size > MAX_PUBLIC_CARD_BYTES:
            return False
        section = b""
        splits: list[bytes] = []
        with card.open("rb") as handle:
            for raw_line in handle:
                if len(raw_line) > MAX_PUBLIC_CHECKER_LINE_BYTES:
                    return False
                line = raw_line.split(b"#", 1)[0].strip()
                section_match = PUBLIC_CARD_SECTION.fullmatch(line)
                if section_match:
                    section = section_match.group(1)
                    continue
                if section == b"task":
                    split_match = PUBLIC_CARD_SPLIT.fullmatch(line)
                    if split_match:
                        splits.append(split_match.group(1))
        return splits == [b"public-dev"]
    except OSError:
        return False


def public_checker_filename_candidates(task_dir: Path) -> tuple[str, ...]:
    """Extract filename literals only from a verified public bundle's checker surface.

    This mirrors the public conformance floor's output-root filters. Checker bodies,
    assertions and values are neither returned nor retained. Any structural ambiguity
    disables the soft contract instead of producing a participant-side grader.
    """
    if not is_public_dev_bundle(task_dir):
        return ()
    checks = task_dir / "checks"
    try:
        if checks.is_symlink() or not checks.is_dir():
            return ()
        paths = sorted(checks.glob("*.py"))
        if not 1 <= len(paths) <= MAX_PUBLIC_CHECKER_FILES:
            return ()
        total_bytes = 0
        candidates: set[str] = set()
        for path in paths:
            if path.is_symlink() or not path.is_file():
                return ()
            size = path.stat().st_size
            total_bytes += size
            if (
                size > MAX_PUBLIC_CHECKER_FILE_BYTES
                or total_bytes > MAX_PUBLIC_CHECKER_TOTAL_BYTES
            ):
                return ()
            with path.open("rb") as handle:
                for line in handle:
                    if len(line) > MAX_PUBLIC_CHECKER_LINE_BYTES:
                        return ()
                    if PUBLIC_CHECKER_NON_OUTPUT_LINE.search(line):
                        continue
                    for match in PUBLIC_CHECKER_FILENAME.finditer(line):
                        value = match.group("path")
                        if not (
                            b"/" not in value
                            or value.startswith(b"/output/")
                            or value.startswith(b"/app/output/")
                            or value.startswith(b"{OUTPUT_DIR}/")
                        ):
                            continue
                        basename = value.rsplit(b"/", 1)[-1]
                        if b"{" in basename or b"}" in basename:
                            continue
                        name = basename.decode("ascii")
                        if not is_forbidden_grader_artifact(Path(name)):
                            candidates.add(name)
        return tuple(sorted(candidates))
    except (OSError, UnicodeError):
        return ()


def public_filename_soft_contract(task_dir: Path, instruction: str) -> tuple[Path, ...]:
    """Return unambiguous public filename hints, or fail open to no soft contract."""
    candidates = public_checker_filename_candidates(task_dir)
    if not candidates:
        return ()
    mentioned = {
        match.group("name")
        for match in INSTRUCTION_FILENAME.finditer(instruction)
        if not is_forbidden_grader_artifact(Path(match.group("name")))
    }
    if any(candidate not in mentioned for candidate in candidates):
        return ()
    return tuple(Path(candidate) for candidate in candidates)


def _dockerfile_input_copy_map(
    task_dir: Path,
    data_root: Path,
) -> dict[str, tuple[Path, ...]]:
    """Parse only simple COPY source/destination paths; never execute Dockerfile text."""
    dockerfile = task_dir / "environment" / "Dockerfile"
    try:
        if (
            dockerfile.is_symlink()
            or not dockerfile.is_file()
            or dockerfile.stat().st_size > MAX_ENV_DOCKERFILE_BYTES
        ):
            return {}
        text = dockerfile.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return {}

    destinations: dict[str, set[Path]] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        instruction_parts = line.split(None, 1)
        if len(instruction_parts) != 2 or instruction_parts[0].upper() != "COPY":
            continue
        payload = instruction_parts[1].strip()
        if payload.startswith("["):
            try:
                copy_values = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if (
                not isinstance(copy_values, list)
                or len(copy_values) != 2
                or any(not isinstance(value, str) for value in copy_values)
            ):
                continue
            source_text, destination_text = copy_values
        else:
            try:
                copy_values = shlex.split(payload, comments=True, posix=True)
            except ValueError:
                continue
            if len(copy_values) != 2:
                continue
            source_text, destination_text = copy_values
        source = Path(source_text)
        destination = Path(destination_text)
        if (
            source.is_absolute()
            or not destination.is_absolute()
            or not (
                destination.as_posix() == "/app"
                or destination.as_posix().startswith("/app/")
            )
            or destination.as_posix().startswith("/app/output")
            or ".." in source.parts
            or ".." in destination.parts
            or not source.parts
            or source.parts[0] != "data"
        ):
            continue
        relative_source = Path(*source.parts[1:])
        source_path = data_root / relative_source
        try:
            if source_path.is_symlink():
                continue
            if source_path.is_file():
                target = destination
                if destination_text.endswith("/"):
                    target = destination / source_path.name
                destinations.setdefault(target.as_posix(), set()).add(relative_source)
            elif source_path.is_dir():
                for child in sorted(source_path.rglob("*")):
                    if child.is_symlink():
                        return {}
                    if not child.is_file():
                        continue
                    relative_child = child.relative_to(source_path)
                    target = destination / relative_child
                    destinations.setdefault(target.as_posix(), set()).add(
                        child.relative_to(data_root)
                    )
        except OSError:
            continue
    return {
        destination: tuple(sorted(sources, key=lambda path: path.as_posix()))
        for destination, sources in destinations.items()
    }


def _legacy_input_path_map(task_dir: Path, instruction: str) -> dict[str, Path]:
    """Return validated legacy destination paths mapped to task-data-relative sources."""
    data_root = task_dir / "environment" / "data"
    try:
        if data_root.is_symlink() or not data_root.is_dir():
            return {}
        by_basename: dict[str, list[Path]] = {}
        count = 0
        for path in sorted(data_root.rglob("*")):
            if path.is_symlink():
                return {}
            if not path.is_file():
                continue
            count += 1
            if count > MAX_LEGACY_MAP_FILES:
                return {}
            by_basename.setdefault(path.name, []).append(path.relative_to(data_root))
    except OSError:
        return {}

    copy_map = _dockerfile_input_copy_map(task_dir, data_root)
    mappings: dict[str, Path] = {}
    if "/app/data" in instruction:
        mappings["/app/data"] = Path(".")
    for match in LEGACY_APP_PATH.finditer(instruction):
        legacy = match.group("path").rstrip("/.,;:")
        if legacy == "/app/data":
            continue
        source: Path | None = None
        if legacy in copy_map:
            copied_sources = copy_map[legacy]
            if len(copied_sources) != 1:
                # Multiple COPY sources for one destination are deliberately
                # unresolved; never fall back to a stale same-name data file.
                continue
            source = copied_sources[0]
        if source is None and legacy.startswith("/app/data/"):
            direct = Path(legacy.removeprefix("/app/data/"))
            candidate = data_root / direct
            try:
                if (
                    ".." not in direct.parts
                    and not candidate.is_symlink()
                    and (candidate.is_file() or candidate.is_dir())
                ):
                    source = direct
            except OSError:
                pass
        if source is None and legacy.count("/") == 2:
            name = legacy.rsplit("/", 1)[-1]
            candidates = by_basename.get(name, [])
            if len(candidates) == 1:
                source = candidates[0]
        if source is not None:
            mappings[legacy] = source
    return mappings


def legacy_input_path_hints(task_dir: Path, instruction: str) -> str:
    """Build a value-free map from stale /app input prose to mounted unit data.

    The public corpus mixes unit-image paths with the participant harness's /input
    mount. Only paths mentioned in the instruction and backed by a unique regular
    file beneath environment/data are mapped. File contents are never opened.
    """
    mappings = _legacy_input_path_map(task_dir, instruction)
    if not mappings:
        return ""
    lines = [
        " Validated legacy input path map for this task (paths only; no file contents):"
    ]
    for legacy, relative in sorted(mappings.items()):
        canonical = "QFBENCH_TASK_DIR/environment/data"
        if relative != Path("."):
            canonical += "/" + relative.as_posix()
        lines.append(f" {legacy} -> {canonical};")
    lines.append(
        " exact file mappings override the directory mapping; use "
        "Path(os.environ['QFBENCH_TASK_DIR']) with the mapped relative path."
    )
    return "".join(lines)


def rewrite_legacy_input_literals(code: str, task_dir: Path, instruction: str) -> str:
    """Rewrite validated literals only when passed directly to known file APIs."""
    mappings = _legacy_input_path_map(task_dir, instruction)
    if not mappings:
        return code
    data_root = task_dir / "environment" / "data"
    absolute = {
        legacy: str(data_root if relative == Path(".") else data_root / relative)
        for legacy, relative in mappings.items()
    }
    for legacy, relative in mappings.items():
        target = data_root if relative == Path(".") else data_root / relative
        try:
            if target.is_dir():
                absolute[legacy + "/"] = str(target) + "/"
        except OSError:
            continue

    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError):
        return code

    path_call_names = {
        "Path", "PurePath", "ZipFile", "TarFile", "access", "exists", "fromfile",
        "genfromtxt", "getsize", "glob", "iglob", "imread", "isdir", "isfile",
        "listdir", "load", "load_workbook", "loadtxt", "lstat", "memmap", "open",
        "read_csv", "read_excel", "read_feather", "read_fwf", "read_hdf", "read_json",
        "read_orc", "read_parquet", "read_pickle", "read_sas", "read_stata", "read_table",
        "read_text", "read_xml", "readlink", "scan_csv", "scan_parquet", "scandir",
        "stat", "walk",
    }
    path_keyword_names = {
        "file", "filename", "fname", "io", "name", "path", "path_or_buf",
        "filepath_or_buffer",
    }

    class LiteralRewriter(ast.NodeTransformer):
        changed = False

        @staticmethod
        def call_name(function: ast.expr):
            if isinstance(function, ast.Name):
                return function.id
            if isinstance(function, ast.Attribute):
                return function.attr
            return None

        def rewrite_argument(self, node: ast.expr) -> ast.expr:
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in absolute
            ):
                self.changed = True
                return ast.copy_location(ast.Constant(absolute[node.value]), node)
            return node

        def visit_Call(self, node: ast.Call) -> ast.AST:
            self.generic_visit(node)
            if self.call_name(node.func) not in path_call_names:
                return node
            if node.args:
                node.args[0] = self.rewrite_argument(node.args[0])
            for keyword in node.keywords:
                if keyword.arg in path_keyword_names:
                    keyword.value = self.rewrite_argument(keyword.value)
            return node

    rewriter = LiteralRewriter()
    rewritten = rewriter.visit(tree)
    if not rewriter.changed:
        return code
    ast.fix_missing_locations(rewritten)
    return ast.unparse(rewritten)


def missing_required_outputs(root: Path, required: tuple[Path, ...]) -> tuple[Path, ...]:
    """Report required paths that are absent, non-regular, or behind a symlink."""
    missing = []
    for relative in required:
        current = root
        safe = True
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                safe = False
                break
        if not safe or not current.is_file():
            missing.append(relative)
    return tuple(missing)


def _drain_process_stream(
    stream: BinaryIO,
    guard: CanaryGuard,
    max_bytes: int,
    verdict: StreamVerdict,
) -> None:
    """Drain a pipe with constant memory while scanning every byte for a canary."""
    overlap = b""
    try:
        while True:
            chunk = stream.read(65_536)
            if not chunk:
                break
            verdict.byte_count += len(chunk)
            if verdict.byte_count > max_bytes:
                verdict.overflow = True
            value = overlap + chunk
            if guard.rejects_bytes(value):
                verdict.policy_violation = True
            overlap = value[-255:]
    except Exception:
        verdict.read_failed = True
    finally:
        try:
            stream.close()
        except OSError:
            verdict.read_failed = True


def _terminate_process_group(process: subprocess.Popen) -> bool:
    """Terminate the isolated process group, including descendants of an exited leader."""
    cleanup_failed = False
    for group_signal in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, group_signal)
        except ProcessLookupError:
            pass
        except OSError:
            cleanup_failed = True
        if group_signal == signal.SIGTERM and process.poll() is None:
            try:
                process.wait(timeout=0.25)
            except subprocess.TimeoutExpired:
                pass
    if process.poll() is None:
        try:
            process.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            cleanup_failed = True
    return not cleanup_failed


def execute(
    code: str,
    task_dir: Path,
    attempt_dir: Path,
    official_out: Path,
    timeout_sec: float,
    guard: CanaryGuard,
    max_output_bytes: int,
) -> ExecutionResult:
    if os.name != "posix":
        raise OSError("isolated process groups require a POSIX runtime")
    script = attempt_dir / "solution.py"
    script.write_text(code, encoding="utf-8")
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": os.environ.get("PYTHONPATH", ""),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "QFBENCH_SEED": os.environ.get("QFBENCH_SEED", "0"),
        "QFBENCH_TASK_DIR": str(task_dir),
        "QFBENCH_AGENT_OUT": str(attempt_dir),
        "QFBENCH_OFFICIAL_OUT": str(official_out),
        # Non-root runtime has no writable home directory. These are public,
        # scratch-only settings; MODEL_TOKEN and proxy credentials stay out.
        "HOME": "/tmp",
        "XDG_CACHE_HOME": "/tmp/.cache",
        "MPLCONFIGDIR": "/tmp/matplotlib",
        "NUMBA_CACHE_DIR": "/tmp/numba",
    }
    process = subprocess.Popen(
        [sys.executable, "-c", EXCEPTION_WRAPPER, str(script)], cwd=attempt_dir, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:
        _terminate_process_group(process)
        raise OSError("process output pipes were not created")

    stdout_verdict = StreamVerdict()
    stderr_verdict = StreamVerdict()
    readers = [
        threading.Thread(
            target=_drain_process_stream,
            args=(process.stdout, guard, max_output_bytes, stdout_verdict),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_process_stream,
            args=(process.stderr, guard, max_output_bytes, stderr_verdict),
            daemon=True,
        ),
    ]
    started_readers = []
    timed_out = False
    cleanup_ok = True
    try:
        for reader in readers:
            reader.start()
            started_readers.append(reader)
        try:
            process.wait(timeout=max(0.05, timeout_sec))
        except subprocess.TimeoutExpired:
            timed_out = True
    finally:
        cleanup_ok = _terminate_process_group(process)
        for reader in started_readers:
            reader.join(timeout=2.0)

    if not cleanup_ok or any(reader.is_alive() for reader in started_readers):
        raise OSError("process tree cleanup failed")
    if stdout_verdict.read_failed or stderr_verdict.read_failed:
        raise OSError("bounded process output read failed")
    raw_returncode = process.returncode if process.returncode is not None else -1
    return ExecutionResult(
        succeeded=raw_returncode == 0,
        failure_class=(
            None
            if raw_returncode == 0
            else EXCEPTION_EXIT_CLASSES.get(raw_returncode, "NonZeroExit")
        ),
        timed_out=timed_out,
        output_overflow=(
            stdout_verdict.overflow
            or stderr_verdict.overflow
            or stdout_verdict.byte_count + stderr_verdict.byte_count > max_output_bytes
        ),
        output_policy_violation=(
            stdout_verdict.policy_violation or stderr_verdict.policy_violation
        ),
    )


def deliverables(
    root: Path,
    guard: CanaryGuard,
    limits: OutputLimits,
    exclude_solution: bool = True,
) -> list[Path]:
    files = []
    total_bytes = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("solution created a symlink; refusing publication")
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if "__pycache__" in relative.parts or (exclude_solution and path.name == "solution.py"):
            continue
        files.append(path)
        if len(files) > limits.max_files:
            raise RuntimeError(DELIVERABLE_LIMIT_MESSAGE)
        size = path.stat().st_size
        if size > limits.max_file_bytes:
            raise RuntimeError(DELIVERABLE_LIMIT_MESSAGE)
        total_bytes += size
        if total_bytes > limits.max_total_bytes:
            raise RuntimeError(DELIVERABLE_LIMIT_MESSAGE)
        enforce_output_policy(relative, path, guard)
    return sorted(files)


def clear_generated_output(out_dir: Path) -> None:
    """Remove only artifacts generated after this solver verified an empty output mount."""
    for path in list(out_dir.iterdir()):
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def capture_direct_output(
    out_dir: Path,
    attempt_dir: Path,
    guard: CanaryGuard,
    limits: OutputLimits,
) -> list[str]:
    """Move direct /app/output or /output writes back behind the publication gate."""
    try:
        files = deliverables(out_dir, guard, limits, exclude_solution=False)
        for source in files:
            if (attempt_dir / source.relative_to(out_dir)).exists():
                raise RuntimeError("duplicate staged/direct deliverable")
        captured = []
        for source in files:
            relative = source.relative_to(out_dir)
            destination = attempt_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
            captured.append(relative.as_posix())
        return captured
    finally:
        clear_generated_output(out_dir)


def publish_atomic(
    attempt_dir: Path,
    out_dir: Path,
    guard: CanaryGuard,
    limits: OutputLimits,
) -> list[str]:
    files = deliverables(attempt_dir, guard, limits)
    if not files:
        raise RuntimeError("solution exited successfully but produced no deliverables")
    staged = []
    published = []
    try:
        for source in files:
            relative = source.relative_to(attempt_dir)
            destination = out_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as handle:
                temporary = Path(handle.name)
            staged.append((temporary, destination, relative.as_posix()))
            shutil.copyfile(source, temporary)
            os.chmod(temporary, 0o644)
        for temporary, destination, relative in staged:
            os.replace(temporary, destination)
            published.append(relative)
    except OSError:
        for temporary, _destination, _relative in staged:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            clear_generated_output(out_dir)
        except OSError:
            raise RuntimeError("publication rollback failed") from None
        raise RuntimeError("atomic publication failed") from None
    for temporary, _destination, _relative in staged:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            try:
                clear_generated_output(out_dir)
            except OSError:
                raise RuntimeError("publication rollback failed") from None
            raise RuntimeError("atomic publication failed") from None
    return published


def solve(task_dir: Path, out_dir: Path, client: ModelClient) -> list[str]:
    task_dir = task_dir.resolve(strict=True)
    if out_dir.is_symlink():
        raise ValueError("output directory must not be a symlink")
    out_dir.mkdir(parents=True, exist_ok=True)
    resolved_out = out_dir.resolve()
    if task_dir == resolved_out or task_dir in resolved_out.parents or resolved_out in task_dir.parents:
        raise ValueError("task and output directories must be disjoint")
    if any(out_dir.iterdir()):
        raise ValueError("output directory must be empty")
    budget = make_budget(task_dir)
    _, house_output_tokens = configured_house_limits()
    budget = Budget(budget.deadline, budget.max_attempts, min(budget.max_output_tokens, house_output_tokens))
    max_input = bounded_int("AGENTHON_MAX_INPUT_BYTES", 200_000, 1024, 2_000_000)
    limits = configured_output_limits()
    task = discover_task(task_dir, max_input)
    instruction = (task_dir / "instruction.md").read_text(encoding="utf-8", errors="replace")
    required_outputs = extract_required_output_paths(instruction)
    soft_outputs = public_filename_soft_contract(task_dir, instruction)
    path_hint = legacy_input_path_hints(task_dir, instruction)
    guard = CanaryGuard.from_prompt(task)
    filename_hint = ""
    if soft_outputs:
        filename_hint = (
            " Public-bundle filename-only output hints: "
            + ", ".join(path.name for path in soft_outputs)
            + ". Treat these only as instruction-aligned filenames, not checker results."
        )
    messages = [{
        "role": "system",
        "content": (
            "Write one Python program that solves the supplied quantitative task. "
            "Read inputs from QFBENCH_TASK_DIR and write every required deliverable under "
            "QFBENCH_AGENT_OUT. The evaluation task root is QFBENCH_TASK_DIR: legacy task "
            "prose that says /app/data normally means QFBENCH_TASK_DIR/environment/data, and an "
            "input named /app/<filename> means the unique matching file under that data "
            "directory. Prefer environment variables over hard-coded container paths, walk "
            "the data directory rather than guessing an extension, and do not create helper "
            "scripts under /app. Do not use network access. Return Python code only."
            + path_hint
            + filename_hint
        ),
    }, {"role": "user", "content": task}]

    last_error = ""
    with tempfile.TemporaryDirectory(prefix="agenthon-attempts-") as workspace:
        for attempt in range(1, budget.max_attempts + 1):
            if budget.remaining() <= 1:
                raise TimeoutError("wall-clock budget exhausted before model call")
            response = client.complete(messages, budget.max_output_tokens, budget.remaining())
            attempt_dir = Path(workspace) / f"attempt-{attempt}"
            attempt_dir.mkdir()
            try:
                if guard.rejects_text(response):
                    raise OutputPolicyViolation(POLICY_REJECTION_MESSAGE)
                code = extract_python(response)
                code = rewrite_legacy_input_literals(code, task_dir, instruction)
                result = execute(
                    code,
                    task_dir,
                    attempt_dir,
                    out_dir,
                    budget.remaining(),
                    guard,
                    limits.max_process_output_bytes,
                )
                if result.output_policy_violation:
                    raise OutputPolicyViolation(POLICY_REJECTION_MESSAGE)
                if result.timed_out:
                    clear_generated_output(out_dir)
                    last_error = "TimeoutExpired: program execution timed out"
                elif result.output_overflow:
                    clear_generated_output(out_dir)
                    last_error = f"RuntimeError: {PROCESS_OUTPUT_LIMIT_MESSAGE}"
                else:
                    capture_direct_output(out_dir, attempt_dir, guard, limits)
                    if result.succeeded:
                        # Validate all staged files before returning a repair verdict so
                        # policy/size violations cannot hide behind a missing file.
                        deliverables(attempt_dir, guard, limits)
                        missing = missing_required_outputs(attempt_dir, required_outputs)
                        soft_missing = missing_required_outputs(attempt_dir, soft_outputs)
                        if not missing and (not soft_missing or attempt == budget.max_attempts):
                            return publish_atomic(attempt_dir, out_dir, guard, limits)
                        if missing:
                            last_error = (
                                "RuntimeError: required output completeness check failed; "
                                f"{len(missing)} of {len(required_outputs)} explicit required "
                                "files are missing or non-regular"
                            )
                        else:
                            last_error = (
                                "RuntimeError: public filename-only soft completeness hint; "
                                f"{len(soft_missing)} of {len(soft_outputs)} candidate files "
                                "are absent; this hint will fail open after the repair budget"
                            )
                    else:
                        last_error = (
                            "ProgramFailure: fixed diagnostic class: "
                            f"{result.failure_class}; program output withheld"
                        )
            except OutputPolicyViolation:
                try:
                    clear_generated_output(out_dir)
                finally:
                    raise RuntimeError(POLICY_REJECTION_MESSAGE) from None
            except OSError:
                try:
                    clear_generated_output(out_dir)
                except OSError:
                    raise RuntimeError("output cleanup failed") from None
                last_error = "OSError: runtime I/O failure"
            except RuntimeError as exc:
                try:
                    clear_generated_output(out_dir)
                except OSError:
                    raise RuntimeError("output cleanup failed") from None
                last_error = f"RuntimeError: {exc}"
            except ValueError:
                try:
                    clear_generated_output(out_dir)
                except OSError:
                    raise RuntimeError("output cleanup failed") from None
                last_error = "ValueError: model response did not contain exactly one executable program"
            messages.extend([
                {"role": "assistant", "content": response},
                {"role": "user", "content": "Repair the program. Failure:\n" + last_error},
            ])
    raise RuntimeError(f"all {budget.max_attempts} attempts failed: {last_error}")
