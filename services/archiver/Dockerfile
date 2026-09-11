# Positions archiver: expired daily partitions -> Parquet (S3 or a directory) -> dropped.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY services/archiver/requirements.txt .
RUN uv pip install --system -r requirements.txt
COPY services/archiver/archive.py .
ENV PYTHONUNBUFFERED=1
# Run as a non-root user (gap audit I20). A container that is compromised through
# an application bug should not also be root inside its own filesystem; the task
# roles are the real boundary on AWS, this is the layer under them.
RUN useradd --system --uid 10001 --create-home argus && chown -R argus:argus /app
USER argus
CMD ["python", "archive.py"]
