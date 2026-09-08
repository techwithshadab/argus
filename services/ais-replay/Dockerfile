FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY services/ais-replay/requirements.txt .
RUN uv pip install --system -r requirements.txt
COPY data /app/data
COPY services/ais-replay/replay.py services/ais-replay/network.py services/ais-replay/aisstatic.py services/ais-replay/datakey.py services/ais-replay/areas.py ./
ENV PYTHONUNBUFFERED=1
CMD ["python", "replay.py"]
