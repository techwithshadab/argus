# Shared agent image. Build args select the requirements set and the agent module.
# AgentCore Runtime requires linux/arm64 images; docker compose builds for the host arch.
ARG PYTHON_IMAGE=ghcr.io/astral-sh/uv:python3.12-bookworm-slim
FROM ${PYTHON_IMAGE}
ARG REQS=requirements-strands.txt
ARG AGENT=watch
WORKDIR /app
COPY agents/${REQS} /app/requirements.txt
RUN uv pip install --system -r /app/requirements.txt
COPY agents/shared /app/shared
COPY agents/${AGENT}/app.py /app/app.py
COPY agents/entrypoint.sh /app/entrypoint.sh
ARG GIT_SHA=unknown
ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app AGENT_NAME=${AGENT} GIT_SHA=${GIT_SHA}
# Run as a non-root user (gap audit I20). A container that is compromised through
# an application bug should not also be root inside its own filesystem; the task
# roles are the real boundary on AWS, this is the layer under them.
RUN useradd --system --uid 10001 --create-home argus && chown -R argus:argus /app
USER argus
# 9000 = A2A protocol (AgentCore convention), 8080 = HTTP protocol (/invocations, /ping)
EXPOSE 9000 8080
CMD ["/app/entrypoint.sh"]
