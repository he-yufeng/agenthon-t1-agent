FROM python:3.13-slim@sha256:16f75ad0fbc6c4883a8afd63b2d700c3cf68ccffc1aaeca5304ca0a3a908451f

LABEL qfbench2.interface_version="2.0" \
      org.opencontainers.image.source="track1-coding-public@db74b8dd9ca8e8de05cb87437e99da43f2d277b9" \
      org.opencontainers.image.description="Participant-owned Agenthon Track 1 API agent"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLCONFIGDIR=/tmp/matplotlib

COPY base/requirements-sandbox.lock \
     base/requirements-sandbox.lock.sha256 \
     /tmp/agenthon-deps/
RUN mkdir /tmp/agenthon-deps/wheels \
    && python -m pip download --only-binary=:all: --no-deps \
       -r /tmp/agenthon-deps/requirements-sandbox.lock \
       -d /tmp/agenthon-deps/wheels \
    && cd /tmp/agenthon-deps/wheels \
    && sha256sum -c ../requirements-sandbox.lock.sha256 \
    && python -m pip install --no-index --no-deps --find-links=. \
       -r ../requirements-sandbox.lock \
    && python -m pip check \
    && rm -rf /tmp/agenthon-deps

COPY base/smoke_imports.py /tmp/agenthon-smoke-imports.py
RUN python /tmp/agenthon-smoke-imports.py \
    && rm /tmp/agenthon-smoke-imports.py \
    && mkdir -p /app/data /app/output /output /logs/verifier

WORKDIR /agent
COPY agent.py engine.py /agent/

ENTRYPOINT ["python", "/agent/agent.py"]
CMD ["solve", "--task-dir", "/input", "--out", "/app/output"]
