FROM python:3.12-slim
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV PYTHONUNBUFFERED=1 \
    BRAIN_BASE=/data \
    BRAIN_DB=/data/store/brain.db \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=8790

RUN mkdir -p /data/store /data/logs /data/proposals

EXPOSE 8790

# Streamable-HTTP transport so other containers / a web UI can reach it.
# For stdio (Claude Desktop, Cursor) run mcp_server.py directly on the host instead.
CMD ["python3", "mcp_server.py", "--http"]
