# Positions archiver: expired daily partitions -> Parquet (S3 or a directory) -> dropped.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY services/archiver/requirements.txt .
RUN uv pip install --system -r requirements.txt
COPY services/archiver/archive.py .
ENV PYTHONUNBUFFERED=1
CMD ["python", "archive.py"]
