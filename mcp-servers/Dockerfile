# One image, four servers: select with MCP_SERVER=ais|registry|geo|imagery
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
COPY mcp-servers/requirements.txt .
RUN uv pip install --system -r requirements.txt
COPY mcp-servers/ /app/
# VERSION sits next to common/ so serve.py and telemetry can read it
ENV PYTHONUNBUFFERED=1 MCP_SERVER=ais MCP_PORT=8000
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request,os;urllib.request.urlopen('http://127.0.0.1:%s/health'%os.environ['MCP_PORT'])"
CMD ["python", "serve.py"]
