#!/usr/bin/env python3
"""
Atlas Second Brain — engine
===========================
Isolated, additive knowledge layer for a self-hosted stack. Ingests your Claude
transcript corpus + your own scripts/signals, scrubs credentials, embeds with
the LOCAL nomic-embed-text (Ollama), stores in a single sqlite-vec file, and
answers brain_recall(query) — all without touching any existing service.

Storage : /opt/app/secondbrain/store/brain.db   (sqlite + sqlite-vec)
Embed   : http://localhost:11434  nomic-embed-text (768-dim, local, free)
Source  : READ-ONLY. Never writes back to the corpus or any live state.

Subcommands:
  init                         create schema (idempotent)
  ingest-transcripts <dir>     walk *.jsonl Claude sessions -> scrubbed chunks
  ingest-files <dir>           walk code/docs (.py .md .sh .yml ...) -> chunks
  embed [--limit N --batch B]  embed all un-embedded chunks (local nomic)
  recall "<query>" [-k N]      KNN semantic search over the brain
  stats                        counts + coverage
"""
import os, sys, json, re, sqlite3, hashlib, time, glob, argparse, struct
import sqlite_vec
import requests

BASE        = os.environ.get("BRAIN_BASE", "/opt/app/secondbrain")
DB_PATH     = os.environ.get("BRAIN_DB", f"{BASE}/store/brain.db")
# Embedding endpoint failover: point at one or more Ollama hosts running
# nomic-embed-text. A single always-on host is fine; the list lets you add a
# faster GPU box as a primary with a CPU box as fallback so a dead endpoint
# can't hang recall.
_OLLAMA_HOSTS = [h for h in [os.environ.get("OLLAMA_URL"),
                             os.environ.get("OLLAMA_URL_FALLBACK"),
                             "http://localhost:11434"]
                 if h]
OLLAMA      = _OLLAMA_HOSTS[0]
EMBED_MODEL = "nomic-embed-text"
DIM         = 768
MAX_CHARS   = 1600          # ~400 tokens per chunk
MIN_CHARS   = 40            # drop trivially short chunks

# Source-aware down-weight (hybrid recall): large code/boilerplate 'file' chunks
# from a bulk code-ingestion project can drown out business content. Multiply their
# RRF contribution by BRAIN_CODE_WEIGHT for business queries; skip the penalty when
# the query itself is code-intent so code retrieval stays UNCHANGED.
CODE_WEIGHT = float(os.environ.get("BRAIN_CODE_WEIGHT", "0.35"))
_CODE_INTENT = re.compile(r'(?i)\b(code|css|js|php|python|script|functie|bug|error|config)\b')

# ----------------------------------------------------------------------------
# CREDENTIAL SCRUB  (mandatory before any text is embedded/stored)
# Strategy: remove SECRETS (keys/tokens/passwords/JWT/PEM), but KEEP IPs,
# hostnames and emails — those are operational knowledge the brain needs and
# the DB never leaves your host. Tune SCRUB_KEEP_PII=0 to also strip emails.
# ----------------------------------------------------------------------------
KEEP_PII = os.environ.get("SCRUB_KEEP_PII", "1") == "1"
_SCRUB = [
    # PEM private key blocks (do first, multi-line)
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----', re.S), '<PRIVATE_KEY>'),
    # labelled secrets:  password: xxx  DB_PASSWORD=xxx  "token": "xxx"  bearer xxx
    # NOTE: [A-Za-z_]* prefix catches DB_PASSWORD / MYSQL_ROOT_PASSWORD / etc. (the '_' blocks a plain \b)
    (re.compile(r'(?i)([A-Za-z_]*(?:password|passwd|passphrase|pwd|secret|api[_-]?key|apikey|access[_-]?token|auth[_-]?token|token|bearer|client[_-]?secret|private[_-]?key))\b\s*["\']?\s*(?:is|=|:)\s*["\']?([^\s"\'<>]{3,})'), '\\1=<REDACTED>'),
    # credentials embedded in URLs / connection strings:  scheme://user:PASS@host
    (re.compile(r'(://[^/\s:@]+:)([^@\s/]{3,})(@)'), '\\1<REDACTED>\\3'),
    # user@host/PASS or user:PASS pairs with a service-style password
    (re.compile(r'([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+[/:])([A-Za-z0-9][A-Za-z0-9._@#%!-]{5,})'), '\\1<REDACTED>'),
    # generic "<Word>20YY!" password shapes (common "BrandNameYear!" convention).
    # Add your own org-specific literal password patterns here if you have
    # known legacy strings you want force-scrubbed on top of the generic rule.
    (re.compile(r'\b[A-Za-z][A-Za-z0-9_.-]{2,}20\d\d[!@#%]'), '<SECRET>'),
    # vendor key shapes
    (re.compile(r'\bgsk_[A-Za-z0-9]{20,}'), '<GROQ_KEY>'),
    (re.compile(r'\bsk-ant-[A-Za-z0-9_-]{20,}'), '<ANTHROPIC_KEY>'),
    (re.compile(r'\bsk-[A-Za-z0-9]{20,}'), '<OPENAI_KEY>'),
    (re.compile(r'\bAKIA[0-9A-Z]{16}\b'), '<AWS_KEY>'),
    (re.compile(r'\bghp_[A-Za-z0-9]{30,}'), '<GH_TOKEN>'),
    (re.compile(r'\bgithub_pat_[A-Za-z0-9_]{30,}'), '<GH_PAT>'),
    (re.compile(r'\bxox[baprs]-[A-Za-z0-9-]{10,}'), '<SLACK_TOKEN>'),
    # JWT
    (re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}'), '<JWT>'),
]
_SCRUB_PII = [
    (re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b'), '<EMAIL>'),
]
def scrub(text: str) -> str:
    if not text:
        return ""
    for pat, repl in _SCRUB:
        text = pat.sub(repl, text)
    if not KEEP_PII:
        for pat, repl in _SCRUB_PII:
            text = pat.sub(repl, text)
    return text

# ----------------------------------------------------------------------------
# DB
# ----------------------------------------------------------------------------
def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn

def init_schema():
    conn = db()
    conn.execute("""CREATE TABLE IF NOT EXISTS chunks(
        id        INTEGER PRIMARY KEY,
        sha       TEXT UNIQUE,           -- dedup key
        source    TEXT,                  -- 'transcript' | 'file'
        project   TEXT,
        path      TEXT,
        ref       TEXT,                  -- session id / symbol
        ts        TEXT,
        role      TEXT,
        text      TEXT,
        embedded  INTEGER DEFAULT 0,
        created   REAL
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_chunks_emb ON chunks(embedded)")
    conn.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(emb float[{DIM}])")
    # learning layer: durable facts distilled by the consolidate loop (Phase 4)
    conn.execute("""CREATE TABLE IF NOT EXISTS facts(
        id INTEGER PRIMARY KEY, fact TEXT UNIQUE, topic TEXT, weight REAL DEFAULT 1.0,
        provenance TEXT, created REAL)""")
    # lexical channel (hybrid recall): FTS5 over chunks.text.
    # external-content => stores ONLY the BM25 index, NOT a 2nd copy of the text
    # (disk-safe on a near-full disk). Triggers keep it in sync with chunks.
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5("
                 "text, content='chunks', content_rowid='id', tokenize='unicode61')")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
        INSERT INTO fts_chunks(rowid, text) VALUES (new.id, new.text); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
        INSERT INTO fts_chunks(fts_chunks, rowid, text) VALUES('delete', old.id, old.text); END""")
    conn.execute("""CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
        INSERT INTO fts_chunks(fts_chunks, rowid, text) VALUES('delete', old.id, old.text);
        INSERT INTO fts_chunks(rowid, text) VALUES (new.id, new.text); END""")
    conn.commit(); conn.close()
    print(f"[init] schema ready at {DB_PATH}")

def build_fts():
    """(Re)build the FTS5 lexical index from existing chunks. Idempotent.
    Needed once for a DB that pre-dates the FTS table; triggers keep it fresh after."""
    init_schema()  # ensures fts_chunks + triggers exist
    conn = db()
    conn.execute("INSERT INTO fts_chunks(fts_chunks) VALUES('rebuild')")
    conn.commit()
    n = conn.execute("SELECT count(*) FROM fts_chunks").fetchone()[0]
    conn.close()
    print(f"[build-fts] lexical index rebuilt: {n} rows")

# ----------------------------------------------------------------------------
# chunk helpers
# ----------------------------------------------------------------------------
def _sha(*parts) -> str:
    return hashlib.sha1("\x1f".join(p or "" for p in parts).encode("utf-8", "ignore")).hexdigest()

def _segments(text: str):
    """Split a long blob into <= MAX_CHARS segments on paragraph boundaries."""
    text = text.strip()
    if len(text) <= MAX_CHARS:
        if len(text) >= MIN_CHARS:
            yield text
        return
    buf = ""
    for para in re.split(r'\n\s*\n', text):
        if len(buf) + len(para) + 2 > MAX_CHARS:
            if len(buf) >= MIN_CHARS:
                yield buf.strip()
            buf = para
        else:
            buf += "\n\n" + para
    if len(buf.strip()) >= MIN_CHARS:
        yield buf.strip()

def _stage(conn, source, project, path, ref, ts, role, text):
    text = scrub(text)
    n = 0
    for seg in _segments(text):
        sha = _sha(source, path, ref, seg[:120], str(n))
        try:
            conn.execute(
                "INSERT OR IGNORE INTO chunks(sha,source,project,path,ref,ts,role,text,created)"
                " VALUES(?,?,?,?,?,?,?,?,?)",
                (sha, source, project, path, ref, ts, role, seg, time.time()))
            if conn.total_changes:
                n += 1
        except Exception:
            pass
    return n

# ----------------------------------------------------------------------------
# transcript extraction  (Claude Code .jsonl: one JSON event per line)
# ----------------------------------------------------------------------------
def _event_text(ev):
    """Reduce a transcript event to signal text + role. Tool spam collapsed."""
    role = ev.get("type") or (ev.get("message") or {}).get("role") or ""
    msg = ev.get("message") or ev
    content = msg.get("content") if isinstance(msg, dict) else None
    out = []
    if isinstance(content, str):
        out.append(content)
    elif isinstance(content, list):
        for b in content:
            if not isinstance(b, dict):
                continue
            t = b.get("type")
            if t == "text" and b.get("text"):
                out.append(b["text"])
            elif t == "tool_use":
                inp = b.get("input") or {}
                desc = inp.get("description") or inp.get("command") or inp.get("file_path") or ""
                out.append(f"[tool:{b.get('name','?')}] {str(desc)[:160]}")
            elif t == "tool_result":
                c = b.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text","") for x in c if isinstance(x, dict))
                c = str(c or "")
                # keep only head+tail of tool output
                if len(c) > 300:
                    c = c[:200] + " … " + c[-80:]
                out.append(f"[result] {c}")
    return role, "\n".join(s for s in out if s).strip()

def ingest_transcripts(root):
    conn = db()
    files = glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True)
    print(f"[ingest-transcripts] {len(files)} session files under {root}")
    total = 0
    for i, fp in enumerate(files):
        project = os.path.basename(os.path.dirname(fp))
        sess = os.path.basename(fp).replace(".jsonl", "")
        # group consecutive events into exchanges around user turns
        buf, last_ts = [], ""
        try:
            with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    ts = ev.get("timestamp") or ev.get("ts") or ""
                    role, txt = _event_text(ev)
                    if not txt:
                        continue
                    last_ts = ts or last_ts
                    buf.append(f"{role}: {txt}")
                    if sum(len(x) for x in buf) > MAX_CHARS:
                        total += _stage(conn, "transcript", project, fp, sess, last_ts, "exchange", "\n".join(buf))
                        buf = []
        except Exception as e:
            print(f"  ! {fp}: {e}")
            continue
        if buf:
            total += _stage(conn, "transcript", project, fp, sess, last_ts, "exchange", "\n".join(buf))
        if (i+1) % 100 == 0:
            conn.commit(); print(f"  .. {i+1}/{len(files)} files, {total} chunks")
    conn.commit(); conn.close()
    print(f"[ingest-transcripts] staged {total} new chunks")

# ----------------------------------------------------------------------------
# file/code ingestion  (understand-Atlas corpus)
# ----------------------------------------------------------------------------
FILE_GLOBS = ("*.py","*.sh","*.md","*.markdown","*.txt","*.rst","*.yml","*.yaml","*.toml","*.ini","*.cfg","*.conf","*.env","*.properties","*.json","*.jsonl","*.xml","*.csv","*.tsv","*.sql","*.js","*.mjs","*.cjs","*.ts","*.tsx","*.jsx","*.vue","*.svelte","*.html","*.htm","*.css","*.scss","*.sass","*.less","*.ps1","*.psm1","*.bat","*.cmd","*.go","*.rs","*.java","*.kt","*.c","*.h","*.cpp","*.hpp","*.cs","*.rb","*.php","*.pl","*.lua","*.r","*.R","*.swift","*.scala","*.service","*.timer","*.socket","*.ipynb","*.tf","*.hcl","*.gitignore","*.editorconfig")
def ingest_files(root):
    conn = db()
    n_files = total = 0
    for dirpath, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in (".git","node_modules","venv","__pycache__","vendor",".obsidian")]
        for name in names:
            if not any(glob.fnmatch.fnmatch(name, g) for g in FILE_GLOBS):
                continue
            fp = os.path.join(dirpath, name)
            try:
                if os.path.getsize(fp) > 400_000:
                    continue
                with open(fp, "r", encoding="utf-8", errors="ignore") as fh:
                    txt = fh.read()
            except Exception:
                continue
            n_files += 1
            total += _stage(conn, "file", os.path.basename(root), fp, name, "", "code", txt)
    conn.commit(); conn.close()
    print(f"[ingest-files] {n_files} files -> staged {total} new chunks")

# ----------------------------------------------------------------------------
# embedding  (local nomic-embed-text via Ollama)
# ----------------------------------------------------------------------------
import numpy as np
def _l2norm(v):
    a = np.asarray(v, dtype=np.float32); n = float(np.linalg.norm(a))
    return (a / n) if n else a   # unit vector -> L2 ranking == cosine ranking

EMBED_CAP = 6000  # chars (~1500 tok); nomic-embed-text ctx ~2048 tok

def _embed_one(text, prefix="search_document: "):
    """Single embed (queries). Fails over across mesh hosts with a SHORT timeout so a dead
    endpoint can't hang recall for 120s (the MET-down bug). Returns a unit-normalized list."""
    last = None
    for _host in _OLLAMA_HOSTS:
        try:
            r = requests.post(f"{_host}/api/embeddings",
                              json={"model": EMBED_MODEL, "prompt": prefix + text, "keep_alive": "10m"},
                              timeout=20)
            r.raise_for_status()
            return _l2norm(r.json()["embedding"]).tolist()
        except Exception as e:
            last = e
            continue
    raise RuntimeError(f"all embed hosts failed: {last}")

def _embed_batch(texts, prefix="search_document: "):
    """Batch embed via /api/embed (one model-load per call). Falls back to single endpoint."""
    try:
        r = requests.post(f"{OLLAMA}/api/embed",
                          json={"model": EMBED_MODEL, "input": [prefix + (t or "").replace(chr(0), " ")[:EMBED_CAP] for t in texts], "keep_alive": "10m"},
                          timeout=600)
        if r.status_code == 404:
            raise RuntimeError("no /api/embed")
        r.raise_for_status()
        return [_l2norm(e) for e in r.json().get("embeddings", [])]
    except Exception:
        return [_l2norm(_emb_raw(t, prefix)) for t in texts]

def _emb_raw(text, prefix):
    r = requests.post(f"{OLLAMA}/api/embeddings",
                      json={"model": EMBED_MODEL, "prompt": prefix + (text or "").replace(chr(0), " ")[:EMBED_CAP], "keep_alive": "10m"}, timeout=120)
    r.raise_for_status(); return r.json()["embedding"]

def embed(limit=None, batch=48):
    conn = db()
    q = "SELECT id,text FROM chunks WHERE embedded=0 ORDER BY id"
    if limit:
        q += f" LIMIT {int(limit)}"
    rows = conn.execute(q).fetchall()
    print(f"[embed] {len(rows)} chunks to embed via {EMBED_MODEL} (batch={batch})")
    done = 0; t0 = time.time()
    for i in range(0, len(rows), batch):
        grp = rows[i:i+batch]
        try:
            vecs = _embed_batch([t for _, t in grp])
            for (cid, _), v in zip(grp, vecs):
                conn.execute("INSERT OR REPLACE INTO vec_chunks(rowid,emb) VALUES(?,?)",
                             (cid, sqlite_vec.serialize_float32(v)))
                conn.execute("UPDATE chunks SET embedded=1 WHERE id=?", (cid,))
            conn.commit(); done += len(grp)
            rate = done/(time.time()-t0+1e-9)
            print(f"  .. {done}/{len(rows)}  {rate:.1f}/s")
        except Exception as e:
            # batch failed: retry each chunk solo; mark truly-unembeddable ones embedded=-1
            bad = 0
            for cid, t in grp:
                try:
                    v = _l2norm(_emb_raw(t or "", "search_document: "))
                    conn.execute("INSERT OR REPLACE INTO vec_chunks(rowid,emb) VALUES(?,?)",
                                 (cid, sqlite_vec.serialize_float32(v)))
                    conn.execute("UPDATE chunks SET embedded=1 WHERE id=?", (cid,))
                    done += 1
                except Exception:
                    conn.execute("UPDATE chunks SET embedded=-1 WHERE id=?", (cid,))
                    bad += 1
            conn.commit()
            print(f"  ~ batch@{i} fell back per-chunk: {bad} unembeddable (embedded=-1)")
    conn.commit(); conn.close()
    print(f"[embed] embedded {done} chunks in {time.time()-t0:.0f}s")

# ----------------------------------------------------------------------------
# recall  (the one public function the rest of Atlas calls)
# ----------------------------------------------------------------------------
def brain_recall(query, k=6):
    conn = db()
    qv = _embed_one(query, prefix="search_query: ")
    rows = conn.execute("""
        SELECT c.project, c.source, c.path, c.ref, c.ts, c.text, v.distance
        FROM vec_chunks v JOIN chunks c ON c.id = v.rowid
        WHERE v.emb MATCH ? AND k = ?
        ORDER BY v.distance
    """, (sqlite_vec.serialize_float32(qv), k)).fetchall()
    conn.close()
    # unit vectors -> L2 distance d relates to cosine sim by cos = 1 - d^2/2
    return [{"project":p,"source":s,"path":pa,"ref":rf,"ts":ts,
             "score":round(1-(d*d)/2,3),"text":tx} for (p,s,pa,rf,ts,tx,d) in rows]

def _fts_sanitize(q):
    """Turn arbitrary user text into a safe FTS5 MATCH string: alphanumeric
    terms, each quoted (neutralizes FTS5 operators), joined by OR for recall.
    Identifiers like INV/2026/00037 tokenize to inv/2026/00037 and stay findable."""
    terms = [t for t in re.findall(r"[A-Za-z0-9_]+", (q or "")) if len(t) > 1]
    return " OR ".join(f'"{t}"' for t in terms)

def brain_recall_hybrid(query, k=6, pool=50, rrf_k=60):
    """Hybrid recall: dense KNN + FTS5 BM25 fused by reciprocal-rank-fusion.
    Same dict shape as brain_recall (cosine 'score') plus 'rrf' and 'via'.
    Fixes the dense-only blind spot for business identifiers (invoice #, IBAN,
    container names, paths). Degrades to dense-only if the lexical channel is
    empty/unavailable, so it is a safe drop-in for brain_recall."""
    conn = db()
    qv = _embed_one(query, prefix="search_query: ")
    qblob = sqlite_vec.serialize_float32(qv)
    # dense channel
    dense = conn.execute(
        "SELECT v.rowid, v.distance FROM vec_chunks v WHERE v.emb MATCH ? AND k = ? ORDER BY v.distance",
        (qblob, pool)).fetchall()
    dense_rank = {cid: i for i, (cid, _) in enumerate(dense)}
    dense_cos  = {cid: 1-(d*d)/2 for cid, d in dense}
    # lexical channel (best-effort: never let a bad MATCH break recall)
    lex_rank = {}
    m = _fts_sanitize(query)
    if m:
        try:
            lex = conn.execute(
                "SELECT rowid FROM fts_chunks WHERE fts_chunks MATCH ? ORDER BY rank LIMIT ?",
                (m, pool)).fetchall()
            lex_rank = {cid: i for i, (cid,) in enumerate(lex)}
        except Exception:
            lex_rank = {}
    # reciprocal-rank fusion
    ids = set(dense_rank) | set(lex_rank)
    # source-aware penalty: down-weight code/boilerplate 'file' chunks for
    # business queries only. code-intent queries get NO penalty (weight=1.0).
    code_intent = bool(_CODE_INTENT.search(query or ""))
    penalty = {}
    if not code_intent and ids and CODE_WEIGHT != 1.0:
        ph = ",".join("?" * len(ids))
        for cid, proj, src in conn.execute(
                f"SELECT id,project,source FROM chunks WHERE id IN ({ph})", tuple(ids)):
            if src == "file":
                penalty[cid] = CODE_WEIGHT
    fused = {}
    for cid in ids:
        s = 0.0
        if cid in dense_rank: s += 1.0/(rrf_k + dense_rank[cid])
        if cid in lex_rank:   s += 1.0/(rrf_k + lex_rank[cid])
        fused[cid] = s * penalty.get(cid, 1.0)
    top = sorted(ids, key=lambda c: fused[c], reverse=True)[:k]
    # materialize, preserving the cosine 'score' contract for downstream callers
    out = []
    for cid in top:
        row = conn.execute("SELECT project,source,path,ref,ts,text FROM chunks WHERE id=?", (cid,)).fetchone()
        if not row:
            continue
        p, s, pa, rf, ts, tx = row
        cos = dense_cos.get(cid)
        if cos is None:
            try:
                d = conn.execute("SELECT vec_distance_L2(emb, ?) FROM vec_chunks WHERE rowid=?",
                                 (qblob, cid)).fetchone()
                cos = 1-(d[0]*d[0])/2 if d and d[0] is not None else 0.0
            except Exception:
                cos = 0.0
        via = "both" if (cid in dense_rank and cid in lex_rank) else ("dense" if cid in dense_rank else "lexical")
        out.append({"project":p,"source":s,"path":pa,"ref":rf,"ts":ts,
                    "score":round(cos,3),"rrf":round(fused[cid],4),"via":via,"text":tx})
    conn.close()
    return out

def rescrub():
    """Re-apply the (hardened) scrub to every staged chunk in place. Run before embed."""
    conn = db()
    rows = conn.execute("SELECT id,text FROM chunks").fetchall()
    changed = 0
    for cid, text in rows:
        s = scrub(text)
        if s != text:
            conn.execute("UPDATE chunks SET text=? WHERE id=?", (s, cid))
            changed += 1
    conn.commit(); conn.close()
    print(f"[rescrub] re-scrubbed {changed}/{len(rows)} chunks")

def stats():
    conn = db()
    tot = conn.execute("SELECT count(*) FROM chunks").fetchone()[0]
    emb = conn.execute("SELECT count(*) FROM chunks WHERE embedded=1").fetchone()[0]
    bysrc = conn.execute("SELECT source,count(*) FROM chunks GROUP BY source").fetchall()
    proj = conn.execute("SELECT project,count(*) c FROM chunks GROUP BY project ORDER BY c DESC LIMIT 10").fetchall()
    facts = conn.execute("SELECT count(*) FROM facts").fetchone()[0]
    conn.close()
    print(f"chunks: {tot}  embedded: {emb}  facts: {facts}")
    print("by source:", dict(bysrc))
    print("top projects:", proj)

# ----------------------------------------------------------------------------
def ingest_kernel(kernel="/opt/app/mind/kernel.sqlite"):
    """Ingest the operator's REAL comms (email/whatsapp/bank) from the mind event-spine
    so recall answers operator questions, not only agent-session transcripts."""
    import sqlite3 as _sq, json as _json
    conn = db()
    k = _sq.connect(f"file:{kernel}?mode=ro", uri=True)
    rows = k.execute("SELECT id, ts, sense, subject, payload FROM events "
                     "WHERE sense IN ('email','whatsapp','bank') ORDER BY id").fetchall()
    total = 0
    for eid, ts, sense, subject, payload in rows:
        try: p = _json.loads(payload or "{}")
        except Exception: p = {}
        if sense == "email":
            ref = p.get("from_name") or p.get("from") or "?"
            txt = f"[E-mail van {ref}] Onderwerp: {p.get('subject','')}"
        elif sense == "whatsapp":
            ref = p.get("from") or "?"
            txt = f"[WhatsApp - {ref}] {p.get('text','')}"
        else:  # bank
            ref = p.get("counterparty") or "bank"
            txt = f"[Bank {p.get('date','')}] {p.get('amount','')} {p.get('currency','EUR')} - {ref}: {p.get('info','')}"
        if txt.strip():
            total += _stage(conn, "comms", sense, f"kernel-{eid}", str(ref)[:80], ts, sense, txt)
    conn.commit(); conn.close(); k.close()
    print(f"[ingest-kernel] {len(rows)} events -> staged {total} new chunks")

# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(prog="brain")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    sub.add_parser("build-fts")
    p = sub.add_parser("ingest-transcripts"); p.add_argument("dir")
    p = sub.add_parser("ingest-files"); p.add_argument("dir")
    p = sub.add_parser("ingest-kernel"); p.add_argument("--kernel", default="/opt/app/mind/kernel.sqlite")
    p = sub.add_parser("embed"); p.add_argument("--limit", type=int); p.add_argument("--batch", type=int, default=64)
    p = sub.add_parser("recall"); p.add_argument("query"); p.add_argument("-k", type=int, default=6)
    p.add_argument("--hybrid", action="store_true", help="dense + FTS5 lexical, RRF-fused")
    sub.add_parser("rescrub")
    sub.add_parser("stats")
    a = ap.parse_args()
    if a.cmd == "init": init_schema()
    elif a.cmd == "build-fts": build_fts()
    elif a.cmd == "rescrub": rescrub()
    elif a.cmd == "ingest-transcripts": ingest_transcripts(a.dir)
    elif a.cmd == "ingest-files": ingest_files(a.dir)
    elif a.cmd == "ingest-kernel": ingest_kernel(a.kernel)
    elif a.cmd == "embed": embed(a.limit, a.batch)
    elif a.cmd == "recall":
        fn = brain_recall_hybrid if a.hybrid else brain_recall
        for i, r in enumerate(fn(a.query, a.k), 1):
            tag = f"  via={r['via']} rrf={r['rrf']}" if "via" in r else ""
            print(f"\n#{i}  score={r['score']}{tag}  [{r['source']}/{r['project']}]  {r['ref']}")
            print("   " + r["text"][:500].replace("\n", "\n   "))
    elif a.cmd == "stats": stats()

if __name__ == "__main__":
    main()
