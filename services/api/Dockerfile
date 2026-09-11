FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY services/api/requirements.txt .
RUN uv pip install --system -r requirements.txt
COPY services/api/*.py /app/
ENV PYTHONUNBUFFERED=1
# Run as a non-root user (gap audit I20). A container that is compromised through
# an application bug should not also be root inside its own filesystem; the task
# roles are the real boundary on AWS, this is the layer under them.
RUN useradd --system --uid 10001 --create-home argus && chown -R argus:argus /app
USER argus
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/health')"
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
