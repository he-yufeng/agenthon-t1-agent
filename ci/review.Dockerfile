FROM ghcr.io/he-yufeng/agenthon-t1-agent@sha256:0f74d9bdd86d8ed19e7387d3bd2144c1a956602a41390ec70df55f1a6e86da8e
COPY candidate_review_r01/agent.py candidate_review_r01/review_solver.py /agent/
LABEL org.opencontainers.image.source="https://github.com/he-yufeng/agenthon-t1-agent" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.description="Generic same-House review candidate; not a verified score improvement"
