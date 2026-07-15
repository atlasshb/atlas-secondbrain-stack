# 04 — The Second Brain (FTS5 + optional dense recall, RRF fusion)

The Second Brain is a single-file, self-hosted recall engine: point it at a corpus of
markdown/text, and it answers `recall("some question")` with the most relevant chunks —
no vector database service, no external API, no per-query cost. It runs entirely on
SQLite plus (optionally) a local embedding model.

This doc covers the core engine (`brain.py`), the credential-scrubbing that runs on
every chunk before it's stored, the MCP server that exposes recall to any MCP-aware
agent, and the autonomy loop (`learn.py` / `reactor.py` / `selfprompt.py`) that mines
the accumulated knowledge for gaps and drafts proposals.

## Prerequisites

- Python 3.10+, `pip install sqlite-vec requests numpy` (add `scikit-learn` for the
  learning layer, `mcp[cli]` for the MCP server)
- SQLite 3.38+ with extension loading enabled (bundled with modern CPython)
- Optional: a local embedding model server (e.g. [Ollama](https://ollama.com) running
  `nomic-embed-text`) if you want dense/semantic recall. **Not required** — FTS5-only
  (lexical) recall works standalone.

## Architecture

```mermaid
flowchart LR
    A[corpus/ *.md *.txt *.py ...] -->|ingest-files / ingest-transcripts| B[chunker]
    B --> C[(SQLite: chunks table)]
    C --> D[FTS5 virtual table\nlexical index]
    C -.optional.-> E[embed via Ollama]
    E -.optional.-> F[(sqlite-vec vec0\ndense vectors)]
    D --> G{RRF fusion}
    F -.-> G
    G --> H[brain.py recall / --hybrid]
    H --> I[MCP server\nmcp_server.py]
    I --> J[Any MCP-aware agent]
```

## Directory layout (generic)

```
/opt/app/secondbrain/
  brain.py            # engine: init, ingest, embed, recall, stats
  mcp_server.py        # MCP tool wrapper around brain.py
  learn.py             # topic clustering + priority classifier (CPU-only)
  reactor.py           # event -> recall -> local-LLM proposal -> gate
  selfprompt.py         # periodic "what should I do next" self-prompt
  feed.sh              # cron/timer glue: re-ingest + embed on a schedule
  corpus/               # YOUR markdown/text source files (gitignored, never shipped)
  store/
    brain.db            # SQLite (chunks + FTS5 + optional vec0) — gitignored
    topics.md            # discovered-theme report (learn.py output)
  logs/
```

Only `corpus/`, `store/`, and `logs/` are data — everything else is code. Keep data out
of version control (see the stack's top-level `.gitignore`).

## The pipeline

### 1. Corpus in

Anything text-shaped: markdown notes, code, config, exported chat transcripts, CSV/JSON
exports from other tools. Two built-in walkers:

```bash
# Walk a directory of code/docs/notes (globs: .py .md .sh .yml .json .csv ...)
python3 brain.py ingest-files ./corpus

# Walk newline-delimited-JSON session transcripts (Claude Code style *.jsonl),
# collapsing tool noise into short exchange summaries
python3 brain.py ingest-transcripts ./corpus/transcripts
```

Both commands are idempotent — re-running only stages *new* chunks (dedup is a SHA1 of
source + path + chunk position, `INSERT OR IGNORE`).

### 2. Chunking

Long text is split on paragraph boundaries into ~1600-character chunks (roughly 400
tokens) — small enough for good lexical/semantic precision, large enough to keep
context. Chunks under ~40 characters are dropped as noise.

### 3. Redaction (runs before anything is stored)

Every chunk is passed through a `scrub()` step *before* it touches disk. It targets
**secrets**, not general text:

- PEM private-key blocks → `<PRIVATE_KEY>`
- labelled secrets (`password=`, `api_key:`, `Authorization: Bearer ...`, etc.) → `<REDACTED>`
- credentials embedded in URLs (`scheme://user:PASS@host`) → password redacted, host kept
- known vendor key shapes (OpenAI `sk-`, Anthropic `sk-ant-`, AWS `AKIA...`, GitHub
  `ghp_`/`github_pat_`, Slack `xox...`, JWTs) → `<PROVIDER_KEY>`

By default IPs, hostnames, and email addresses are **kept** (`SCRUB_KEEP_PII=1`) because
they're operational knowledge the brain needs and the database is expected to stay
private/self-hosted. Set `SCRUB_KEEP_PII=0` to additionally strip email addresses if
you plan to widen access to the store. Run `python3 brain.py rescrub` any time you
tighten the patterns — it re-applies scrub to every already-staged chunk in place.

**This is a best-effort regex net, not a guarantee.** Treat `store/brain.db` as
containing sensitive operational data regardless, and never commit it.

### 4. Lexical index — SQLite FTS5 (always on)

An FTS5 virtual table (`fts_chunks`, `tokenize='unicode61'`) is kept in sync with the
`chunks` table via `AFTER INSERT/UPDATE/DELETE` triggers, so lexical search is always
current with zero extra ingestion step. This alone gives you working keyword recall —
exact identifiers, invoice numbers, filenames, error strings — with no embedding model
required at all.

```bash
python3 brain.py init         # idempotent schema create, includes fts_chunks
python3 brain.py build-fts    # one-time rebuild if you're migrating an older DB
```

### 5. Dense index — sqlite-vec (optional)

If you want semantic/"meaning" recall on top of exact keywords, embed the chunks with
a local model and store the vectors in a `vec0` virtual table (via the
[`sqlite-vec`](https://github.com/asg017/sqlite-vec) extension — a single SQLite
extension file, no separate vector DB service):

```bash
export OLLAMA_URL=http://<EMBED_HOST>:11434   # or any host serving an embeddings API
python3 brain.py embed --limit 6000 --batch 64
```

`embed` batches un-embedded chunks through `/api/embed` (falling back to per-chunk
`/api/embeddings` if the batch endpoint is unavailable), L2-normalizes each vector so
cosine similarity reduces to a simple L2 KNN, and writes it into `vec_chunks`. Chunks
that fail to embed after per-item retry are flagged `embedded=-1` and skipped, not
retried forever.

If you skip this step entirely, `brain.py recall` (dense-only) has nothing to query —
use `--hybrid` (below), which degrades gracefully to lexical-only when the dense
channel is empty.

### 6. Fusion — Reciprocal Rank Fusion (RRF)

`--hybrid` runs both channels and merges them with RRF instead of picking one:

```
score(chunk) = Σ 1 / (k + rank_in_channel)      (k=60, summed over dense + lexical)
```

Each channel contributes by *rank*, not raw score, so a chunk that's #1 in either
channel scores well even if the other channel misses it entirely — this is what
recovers exact identifiers (invoice numbers, container names, IBANs) that dense/semantic
search alone tends to miss, while still surfacing paraphrased/semantic matches that
plain keyword search would miss. Every result reports which channel(s) found it
(`via: dense | lexical | both`) so you can see fusion working.

Optionally, source-aware down-weighting demotes bulk code/boilerplate chunks for
non-code queries (`BRAIN_CODE_WEIGHT`, default `0.35`) so a large ingested codebase
doesn't drown out business content — code-intent queries (detected by a keyword regex)
get no penalty.

## The CLI, end to end

```bash
cd /opt/app/secondbrain
python3 brain.py init                              # 1. create schema
python3 brain.py ingest-files ./corpus              # 2. lexical index is live immediately
python3 brain.py embed --limit 6000 --batch 64      # 3. optional: add dense vectors
python3 brain.py recall "renewal date for the X contract" -k 6 --hybrid
python3 brain.py stats                              # coverage: chunks / embedded / by source
```

`recall` prints each hit's score, fusion channel, source/project, ref, and a text
preview. Drop `--hybrid` to query dense-only (requires embeddings) or use FTS5 directly
via `sqlite3 store/brain.db "SELECT * FROM fts_chunks WHERE fts_chunks MATCH '...'"` for
debugging.

**Verify it's working:**

```bash
python3 brain.py stats
# chunks: 1240  embedded: 1240  facts: 0
# by source: {'file': 1240}
python3 brain.py recall "test query that matches something you just ingested" --hybrid
```

## MCP server — expose recall to any agent

`mcp_server.py` wraps the engine as four MCP tools: `recall`, `remember`, `ingest_path`,
`stats`. Any MCP-aware host (Claude Desktop, Cursor, Open WebUI, LibreChat, custom
agents) can add it as a tool server and gain persistent, local, privacy-scrubbed memory.

```bash
pip install "mcp[cli]"
python3 mcp_server.py                 # stdio transport (Claude Desktop, Cursor)
python3 mcp_server.py --http          # streamable-HTTP, for web-UI MCP clients
```

Example MCP host config (stdio):

```json
{
  "mcpServers": {
    "secondbrain": {
      "command": "python3",
      "args": ["/opt/app/secondbrain/mcp_server.py"],
      "env": {
        "BRAIN_DB": "/opt/app/secondbrain/store/brain.db",
        "OLLAMA_URL": "http://<EMBED_HOST>:11434"
      }
    }
  }
}
```

For the HTTP transport, `MCP_HOST` / `MCP_PORT` control the bind address (default
`127.0.0.1:8790`) — put it behind your reverse proxy and auth layer if exposing beyond
localhost; the server itself does not implement authentication.

## The autonomy loop — mine gaps, propose actions

Three scripts turn the passive recall store into a small, local, non-LLM-API learning
loop. None of them call a hosted model API — they use a local model server (e.g. Ollama
running a small model) and plain scikit-learn, so the loop has no per-run cost.

```mermaid
flowchart LR
    Brain[(brain.db)] --> Learn[learn.py\ntopic clustering + classifier]
    Events[(event queue /\ntask backlog)] --> Reactor[reactor.py\nrecall + local-LLM proposal]
    Brain --> Reactor
    Learn -->|facts| Brain
    Reactor -->|proposal| Gate{approval gate}
    Brain --> Self[selfprompt.py\nperiodic self-review]
    Self -->|proposal| Gate
    Gate --> Human[human approves / rejects]
```

- **`learn.py topics -k 16`** — unsupervised theme discovery: clusters all embedded
  vectors (MiniBatchKMeans) and extracts top TF-IDF terms per cluster, writing durable
  `facts` rows plus a human-readable `store/topics.md`. Fully CPU, no LLM call.
- **`learn.py classify-train` / `classify "<text>"`** — an optional supervised
  priority/intent classifier (SGD, scikit-learn) trained on your own labelled outcomes,
  once you have ≥30 labels. Predicts a label for new incoming text.
- **`reactor.py`** — the "what do I do about this event" step. For each unprocessed
  item in your event queue (any append-only log of inbound signals — mail, tasks, bank
  lines, whatever you feed it), it calls `brain_recall()` for relevant context, asks a
  **local** model for a single proposed next action as strict JSON
  (`{"action","domain","risk","priority","reason"}`), and routes that proposal through
  an approval gate. It never executes anything itself, and never bypasses the gate:
  anything tagged `risk: money | external_comms | irreversible | self_modify` always
  parks for a human. Run `reactor.py --dry-run` first to see proposals without marking
  anything processed.
- **`selfprompt.py`** — a periodic "what should the system do next" cycle: it pulls the
  top discovered themes (`facts`), recent high-priority events, and open backlog items,
  asks the local model for the 3 most important next actions, and parks each one through
  the same gate. Purely a suggestion generator — it writes a markdown note, it never
  acts.

The loop only requires the pieces you actually run — `reactor.py` and `selfprompt.py`
expect an event queue and a gate/approval mechanism, which are yours to define (a
simple SQLite table and a request-log function are enough); swap in whatever fits your
stack. `learn.py` runs standalone against `brain.db` alone.

## Configuration reference

| Env var | Default | Purpose |
|---|---|---|
| `BRAIN_DB` | `/opt/app/secondbrain/store/brain.db` | SQLite file path |
| `OLLAMA_URL` | *(unset — falls back to hardcoded fallback hosts, override this)* | embedding endpoint |
| `SCRUB_KEEP_PII` | `1` | `0` also strips email addresses on scrub |
| `BRAIN_CODE_WEIGHT` | `0.35` | RRF down-weight for bulk code chunks on non-code queries |
| `MCP_HOST` / `MCP_PORT` | `127.0.0.1` / `8790` | MCP streamable-HTTP bind |

Set `OLLAMA_URL` explicitly in your `.env` — don't rely on any hardcoded fallback host
list in the source, those are placeholders for your own embedding server(s).
