FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY services/ais-replay/requirements.txt .
RUN uv pip install --system -r requirements.txt
COPY data /app/data
COPY services/ais-replay/replay.py services/ais-replay/network.py services/ais-replay/aisstatic.py services/ais-replay/datakey.py services/ais-replay/areas.py services/ais-replay/aisvalidate.py services/ais-replay/feed_metrics.py ./
ENV PYTHONUNBUFFERED=1
# Run as a non-root user (gap audit I20). A container that is compromised through
# an application bug should not also be root inside its own filesystem; the task
# roles are the real boundary on AWS, this is the layer under them.
RUN useradd --system --uid 10001 --create-home argus && chown -R argus:argus /app
USER argus
CMD ["python", "replay.py"]
