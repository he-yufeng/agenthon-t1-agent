FROM ghcr.io/he-yufeng/agenthon-t1-agent@sha256:0f74d9bdd86d8ed19e7387d3bd2144c1a956602a41390ec70df55f1a6e86da8e
COPY --chmod=0644 submission/agent.py /agent/agent.py
COPY --chmod=0644 input_context_r05/input_profile_r05.py /agent/input_profile_r05.py
LABEL org.opencontainers.image.description="Participant-authored bounded runtime input context; no task answers"
