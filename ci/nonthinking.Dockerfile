FROM ghcr.io/he-yufeng/agenthon-t1-agent@sha256:0f74d9bdd86d8ed19e7387d3bd2144c1a956602a41390ec70df55f1a6e86da8e
COPY candidate_nonthinking_base_r03/agent.py candidate_nonthinking_base_r03/thinking_control.py /agent/
LABEL org.opencontainers.image.source="https://github.com/he-yufeng/agenthon-t1-agent" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.description="Single approved thinking option over unchanged original solver; quality unverified"
