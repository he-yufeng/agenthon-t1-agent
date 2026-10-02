# Agenthon T1 participant agent

A participant-authored coding harness for Agenthon 2026 Track 1. It asks the
organizer-provided House model for Python code, executes the program under a
shared time budget, and publishes the required outputs after local checks.

The current implementation includes bounded model requests, code repair,
runtime input-path handling, restricted output publication, and an absolute
HTTP response-header deadline. It contains no competition task data, stored
unit answers, private evaluation assets, account details or model credentials.

## Build and invocation

```sh
docker build --platform linux/amd64 -t agenthon-native:tested submission
docker build --network none -t agenthon-publish:tested -f ci/publish.Dockerfile .
```

The image accepts `solve --task-dir /input --out /app/output`. The official
runtime supplies `MODEL_ENDPOINT`, `MODEL_NAME`, `MODEL_TOKEN`, and proxy
settings. Dependencies are baked into the image using hash-checked wheels.

The publication workflow builds on native Linux amd64, runs all 87 synthetic
tests under a read-only/non-root/64 MiB noexec scratch profile, and publishes
only after success. Local tests establish engineering behavior; competition
performance requires an actual evaluated submission. No official score or
award is claimed by this repository.

## Resources and licensing

Official contract references:

- https://github.com/Agenthon-2026/track1-coding-public/tree/84049dbc066cc02cf39c2cc18105c2f92284978d
- https://github.com/Agenthon-2026/Agenthon2026-public/tree/50fb2dc2b39c70f4cf81fcd269943782eddfaed0

Participant code is MIT-licensed. Third-party dependencies retain their own
licenses and package metadata. The pinned financial sandbox dependency list
was derived from the official public kit; no task data are redistributed.
The license for participant code is also included in the published image.
