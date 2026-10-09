# Bounded runtime input context (R05)

This independent participant-authored candidate augments the original agent's
runtime task context; it is not a task-specific solver or answer bank.

The original discovery policy omits large files and describes binary tables by
path. This candidate adds at most 4,096 bytes of input structure while preserving
the original complete task prompt, its byte limit, model, sampling settings,
tokens, retry/attempt policy, output guards and publication behavior. It does not
add a mandatory semantic review or a House request.

CSV/TSV information is explicitly the first logical record with **unknown header
status**, not an inferred schema. Parquet/PQT information contains schema fields
and metadata row counts, never table rows or statistics. Only mounted
`environment/data` inputs are eligible. Checkers, references, task IDs, metadata
cards, manifests, generated output and exception text are not new inputs.

The profiler is an isolated two-second, no-retry, credential-free local child.
It caps directories, entries, candidates, record/footer sizes and prompt bytes;
unsafe paths, canary-containing records and unavailable metadata are not used.
Missing metadata never removes a task or replaces the model's solving obligation.
Its overhead is charged to the original absolute model-call allowance.

The base engine is byte-frozen at
`1c85bae327299e6eeacf7f1c788dfd5bf93d8f3c67a45e723280f37aaa4bef3a`.
The CLI has only a solver-import change. No base tag or default branch is replaced.

Validation uses owned synthetic inputs. A deterministic information-consumer
client establishes a before/after control-flow witness, not real LLM performance.
Mandatory native Parquet tests use the original image's installed PyArrow.
Actual-image hash binding, nonroot/read-only/network-none/tmp/process limits and
all frozen test identities must pass before a unique candidate image is published.
No skipped test qualifies as success. Official quality remains unknown until an
admissible new organizer evaluation; fixtures and image checks are not a score.

Latest reviewed Track 1 contract revision:
https://github.com/Agenthon-2026/track1-coding-public/commit/3fa6e295601b18e0427502117a26ac4fcf32cdb2

The Final disallows inherited Docker volumes. Sampling is fixed; Track 1 reruns
check the submitted image/program, not identical stochastic model answers.
