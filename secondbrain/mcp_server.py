#!/usr/bin/env python3
# SecondBrain — (c) 2026 Atlas SHB. MIT License; see LICENSE.
"""SecondBrain MCP server — expose your brain as tools to any MCP host.

Add this server to Claude Desktop, Open WebUI, LibreChat, Cursor, or any MCP client,
and the assistant gains long-term, local, privacy-scrubbed memory + recall.

Run (stdio transport, the default MCP wiring):
    pip install "mcp[cli]"
    python3 mcp_server.py

Example Claude Desktop / MCP host config:
    {
      "mcpServers": {
        "secondbrain": {
          "command": "python3",
          "args": ["/opt/secondbrain/mcp_server.py"],
          "env": { "BRAIN_HOME": "/opt/secondbrain/data",
                   "OLLAMA_URL": "http://localhost:11434" }
        }
      }
    }

Tools exposed: recall · remember · ingest_path · stats.
Config is the same env as the engine (BRAIN_HOME, OLLAMA_URL, …).
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import brain

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.server.transport_security import TransportSecuritySettings
except Exception:
    sys.stderr.write("SecondBrain MCP needs the MCP SDK:  pip install \"mcp[cli]\"\n")
    raise

# Allow reverse-proxied / container access (host.docker.internal, LAN) over Streamable HTTP.
# The brain endpoint itself should be bound to a private interface / put behind auth.
_SECURITY = TransportSecuritySettings(enable_dns_rebinding_protection=False)
# stateless_http + json_response = broadest compatibility with web-UI MCP clients
# (e.g. Open WebUI) that don't hold an SSE session.
mcp = FastMCP("secondbrain", transport_security=_SECURITY,
              stateless_http=True, json_response=True)
# Streamable-HTTP bind (used with `--http`); the transport all web UIs consume natively.
mcp.settings.host = os.environ.get("MCP_HOST", "127.0.0.1")
mcp.settings.port = int(os.environ.get("MCP_PORT", "8790"))


@mcp.tool()
def recall(query: str, k: int = 6, hybrid: bool = True) -> list:
    """Search long-term memory (the Second Brain). Returns the top-k most relevant
    stored snippets with their source, project, timestamp and relevance score.
    Use this to remember prior decisions, facts, documents, emails, code, and context.
    `hybrid=True` fuses semantic + exact-keyword search (best for names/IDs/paths)."""
    fn = brain.brain_recall_hybrid if hybrid else brain.brain_recall
    return fn(query, k)


@mcp.tool()
def remember(text: str, project: str = "note", ref: str = "manual") -> str:
    """Store a note/fact in long-term memory. Credentials are scrubbed automatically.
    It becomes searchable immediately (lexical) and semantically after the next embed."""
    conn = brain.db()
    n = brain._stage(conn, "note", project, "", ref, "", "note", text)
    conn.commit(); conn.close()
    try:
        brain.embed(limit=64)
    except Exception:
        pass
    return f"stored {n} new chunk(s)"


@mcp.tool()
def ingest_path(path: str) -> str:
    """Index a local directory of files (code/docs/notes) into memory, then embed."""
    p = os.path.expanduser(path)
    if not os.path.isdir(p):
        return f"not a directory: {p}"
    brain.ingest_files(p)
    try:
        brain.embed(limit=20000)
    except Exception:
        pass
    return f"ingested + embedded from {p}"


@mcp.tool()
def stats() -> str:
    """Report memory coverage: total chunks, embedded count, and sources."""
    conn = brain.db()
    tot = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    emb = conn.execute("SELECT count(*) FROM chunks WHERE embedded=1").fetchone()[0]
    bysrc = dict(conn.execute("SELECT source,count(*) FROM chunks GROUP BY source").fetchall())
    conn.close()
    return f"chunks={tot} embedded={emb} by_source={bysrc}"


if __name__ == "__main__":
    # stdio (Claude Desktop, Cursor):   python3 mcp_server.py
    # streamable-http (Open WebUI, LibreChat, any web UI):  python3 mcp_server.py --http
    if "--http" in sys.argv:
        mcp.run(transport="streamable-http")   # serves at http://MCP_HOST:MCP_PORT/mcp
    else:
        mcp.run()
