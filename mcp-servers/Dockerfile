# One image, four servers: select with MCP_SERVER=ais|registry|geo|imagery
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY mcp-servers/requirements.txt .
RUN uv pip install --system -r requirements.txt
COPY mcp-servers/ /app/
# VERSION sits next to common/ so serve.py and telemetry can read it
ENV PYTHONUNBUFFERED=1 MCP_SERVER=ais MCP_PORT=8000
EXPOSE 8000
# Run as a non-root user (gap audit I20). A container that is compromised through
# an application bug should not also be root inside its own filesystem; the task
# roles are the real boundary on AWS, this is the layer under them.
RUN useradd --system --uid 10001 --create-home argus && chown -R argus:argus /app
USER argus
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/health'%os.environ['MCP_PORT'])"
CMD ["python", "serve.py"]
