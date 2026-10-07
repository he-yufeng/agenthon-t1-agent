FROM ghcr.io/he-yufeng/agenthon-t1-agent@sha256:0f74d9bdd86d8ed19e7387d3bd2144c1a956602a41390ec70df55f1a6e86da8e
COPY --chmod=0644 submission/engine.py /agent/engine.py
LABEL org.opencontainers.image.description="Participant-owned Agenthon T1 legacy path composition repair candidate"
