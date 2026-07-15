#!/usr/bin/env python3
"""
Atlas Cerebras — Second-Brain self-improvement COMPANY loop
===========================================================
Reframes the operator's "improve the website every day" loop onto the SECOND BRAIN
itself ("Atlas Cerebras"). The brain runs like an 8-department company:
  - Amazon single-threaded-owner  -> each dept = 1 owner of 1 metric, runs DAILY
  - Shape Up 6-week bets          -> operator re-bets department focus every 6 weeks
  - EOS Rocks (quarterly)         -> 3-7 quarterly priorities in ROADMAP.md
Powered by CEREBRAS (free, fast) on the daily grind — NEVER Claude. Every department
emits concrete, bounded proposals routed through the EXISTING gate.py (propose-only:
money / external comms / irreversible / self-modify always PARK). Compounds via
ROADMAP.md + backlog.md + git history (the "Ralph loop" file-memory pattern).
Claude is reserved for the weekly board-review only — the daily loop costs 0 Claude tokens.

  brain_cerebras_company.py --dry-run        # reason + print, write NOTHING, no gate, no git
  brain_cerebras_company.py                  # one real daily cycle (cron default)
  brain_cerebras_company.py --dept radar     # run a single department by key
  brain_cerebras_company.py --limit-depts 3  # run only first N (cost guard)
"""
import os, sys, json, time, sqlite3, hashlib, argparse, subprocess, importlib.util
import urllib.request

BASE   = "/opt/app/secondbrain"
MIND   = "/opt/app/mind"
KERNEL = f"{MIND}/kernel.sqlite"
STORE  = f"{BASE}/store/brain.db"
VAULT  = "/opt/app/brain"
SECRETS = "/opt/app/router-secrets.env"
BACKLOG = f"{BASE}/backlog.md"
PROPOSALS = f"{BASE}/proposals"
LOGDIR = f"{BASE}/logs"

CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"
CEREBRAS_MODELS = ["gemma-4-31b", "gpt-oss-120b", "zai-glm-4.7"]   # gemma = clean direct output; others are reasoning-dump fallbacks
OLLAMA_URL = "http://localhost:11434/api/generate"                 # last-resort local fallback
OLLAMA_MODEL = "qwen2.5:7b"

# ── The 8 departments: the website loop, reframed onto the brain ──────────────────
# (website concern) -> (brain concern)
DEPARTMENTS = [
    {"key": "corpus", "name": "Corpus Expansion",
     "charter": "Owns knowledge COVERAGE. (was: add/expand website pages.) Find thin or "
                "missing knowledge domains in the brain and propose specific sources to ingest "
                "so Atlas's memory covers what the business actually needs."},
    {"key": "retrieval", "name": "Retrieval Quality",
     "charter": "Owns whether recall returns the RIGHT knowledge. (was: conversion/CRO.) Propose "
                "canary queries to test, and concrete FTS5+RRF / embedding tweaks where recall is weak."},
    {"key": "synthesis", "name": "Synthesis & Distillation",
     "charter": "Owns turning raw transcripts into durable, reusable FACTS/THEMES. (was: blogging/copy.) "
                "Propose which recent raw material to distill into high-weight facts the brain should keep."},
    {"key": "radar", "name": "Market & Tech Radar",
     "charter": "Owns AWARENESS of the outside world. (was: competitor + tech + SEO tracking.) Propose the "
                "specific new AI models, tools, competitors, and tech shifts to capture as fresh brain knowledge today."},
    {"key": "health", "name": "Brain Health",
     "charter": "Owns reliability. (was: uptime/perf/broken-links.) Propose fixes for stale embeddings, "
                "DB/WAL issues, failed ingests, freshness gaps — one concrete health action."},
    {"key": "value", "name": "Decision Value",
     "charter": "Owns whether the brain's advice actually MOVED the business. (was: revenue ops.) Propose how to "
                "score whether past self-prompt proposals were adopted and helped Atlas — feed that back as a signal."},
    {"key": "scorecard", "name": "Scorecard",
     "charter": "The EOS Integrator. (was: analytics.) Propose the 3-5 metrics that best capture brain health this "
                "week (corpus size, recall hit-rate, proposals adopted, freshness) and flag any anomaly."},
    {"key": "governance", "name": "Scrub & Governance",
     "charter": "Owns TRUST. (was: terms/privacy/compliance.) Propose one concrete check that the credential-scrub, "
                "no-fake-data rule, and memory hygiene are holding — the brain's 'privacy & terms' equivalent."},
    {"key": "automation", "name": "Automation Engineering",
     "charter": "Owns OPERATOR LEVERAGE. Study the mined most-repeated-asks table and the current tool inventory "
                "(both injected below). Propose ONE new script/automation for the biggest ask that has NO adequate "
                "tool yet, or ONE concrete optimization of an existing tool that's causing repeated manual work. "
                "Never propose what already exists. Tag [CLAUDE] if building it needs real code judgment."},
]

SYSTEM = (
    "You are a single-threaded owner (Amazon STO model) of ONE department inside the "
    "Second Brain — a local-model-first memory/learning layer for a small self-hosted "
    "business/personal stack. You improve the BRAIN ITSELF, not a website. "
    "Work backwards from impact: every proposal must plausibly make the brain more useful to the business. "
    "Output 1-3 CONCRETE, BOUNDED actions doable in one day. Each line starts with a verb. "
    "TAG each action with the CHEAPEST tier that can do it WITHOUT losing quality:\n"
    "  [LOCAL] a free/local model can do it well — drafting, research, auditing, classification, retrieval, notes.\n"
    "  [CLAUDE] needs stronger judgment — cross-source synthesis, code review, tricky verification, planning.\n"
    "  [HUMAN] touches money, sends external comms, or is irreversible.\n"
    "DEFAULT to [LOCAL]; only escalate when a lower tier would genuinely lose quality. Most work is [LOCAL]. "
    "No fake data, no invented facts. Be specific to THIS business, terse, one tagged action per line."
)

def _secret(name):
    try:
        for ln in open(SECRETS):
            ln = ln.strip()
            if ln.startswith(name + "="):
                return ln.split("=", 1)[1].strip()
    except Exception:
        pass
    return os.environ.get(name, "")

def _post_json(url, payload, headers, timeout):
    data = json.dumps(payload).encode()
    h = dict(headers)
    # Cerebras fronts with Cloudflare, which 403s the default Python-urllib UA. Send a real one.
    h.setdefault("User-Agent", "curl/8.5.0")
    req = urllib.request.Request(url, data=data, headers=h, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

ROUTER_URL = os.environ.get("ROUTER_URL", "http://localhost:8888/v1/chat/completions")   # your own local model mesh/router, if any
ROUTER_MODELS = ["local-fast-1", "local-fast-2"]   # names of models your router exposes

def _cerebras(prompt, tries=3):
    """Cerebras (free, fast) with retry — it cold-bursts 403/429 on first hit."""
    key = _secret("CEREBRAS_API_KEY")
    if not key:
        return None, None
    for model in CEREBRAS_MODELS:
        for attempt in range(tries):
            try:
                d = _post_json(CEREBRAS_URL,
                    {"model": model,
                     "messages": [{"role": "system", "content": SYSTEM},
                                  {"role": "user", "content": prompt}],
                     "max_tokens": 900, "temperature": 0.4},
                    {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}, 45)
                msg = d["choices"][0]["message"]
                txt = (msg.get("content") or msg.get("reasoning") or "").strip()
                if txt:
                    return txt, f"cerebras/{model}"
            except Exception as e:
                sys.stderr.write(f"[engine] cerebras/{model} try{attempt+1}: {e}\n")
                time.sleep(1.5 * (attempt + 1))
    return None, None

def _router(prompt):
    """Fallback: your own local model router/mesh (no Claude)."""
    for model in ROUTER_MODELS:
        try:
            d = _post_json(ROUTER_URL,
                {"model": model, "stream": False, "max_tokens": 500,
                 "messages": [{"role": "system", "content": SYSTEM},
                              {"role": "user", "content": prompt}]},
                {"Content-Type": "application/json"}, 200)
            txt = (d["choices"][0]["message"].get("content") or "").strip()
            if txt:
                return txt, f"router/{model}"
        except Exception as e:
            sys.stderr.write(f"[engine] router/{model}: {e}\n")
    return None, None

def _engine(prompt, timeout=60):
    """Cerebras first (free/fast), then the free local mesh. NEVER Claude. Returns (text, engine)."""
    txt, eng = _cerebras(prompt)
    if txt:
        return txt, eng
    txt, eng = _router(prompt)
    if txt:
        return txt, eng
    return "[engine unavailable]", "none"

def _context():
    facts, events, tasks = [], [], []
    try:
        bd = sqlite3.connect(STORE)
        facts = [f for (f,) in bd.execute(
            "SELECT fact FROM facts ORDER BY weight DESC LIMIT 12").fetchall()]
        bd.close()
    except Exception:
        pass
    try:
        k = sqlite3.connect(KERNEL)
        for subj, typ, pl in k.execute(
                "SELECT subject,type,payload FROM events WHERE priority<=1 "
                "ORDER BY id DESC LIMIT 10").fetchall():
            events.append(f"- {subj} ({typ})")
        k.close()
    except Exception:
        pass
    try:
        t = json.load(open(f"{BASE}/tasks.json"))
        tasks = [f"{x['id']} [{x['status']}] {x['title']}" for x in t.get("tasks", [])
                 if x.get("status") not in ("done",)][:10]
    except Exception:
        pass
    # yesterday's backlog tail = the Ralph memory that makes it COMPOUND
    recent_backlog = []
    try:
        recent_backlog = open(BACKLOG).read().splitlines()[-15:]
    except Exception:
        pass
    return facts, events, tasks, recent_backlog

def _real_metrics():
    """Ground the Scorecard in REAL measured numbers, not model guesses."""
    m = {}
    try:
        d = sqlite3.connect(STORE)
        m["corpus_chunks"] = d.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        m["facts"] = d.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        d.close()
    except Exception:
        pass
    try:
        sys.path.insert(0, MIND)
        import gate as _g
        m["gate_pending"] = len(_g.pending())
    except Exception:
        pass
    try:
        m["backlog_items"] = sum(1 for l in open(BACKLOG) if l.startswith("- ["))
    except Exception:
        m["backlog_items"] = 0
    return m

def _mk_prompt(dept, ctx):
    facts, events, tasks, backlog = ctx
    return (f"DEPARTMENT: {dept['name']}\nCHARTER: {dept['charter']}\n\n"
            f"BRAIN'S TOP FACTS:\n" + ("\n".join(f"- {x}" for x in facts) or "(none yet)") +
            f"\n\nRECENT SIGNALS:\n" + ("\n".join(events) or "(none)") +
            f"\n\nOPEN ROADMAP TASKS:\n" + ("\n".join(f"- {x}" for x in tasks) or "(none)") +
            f"\n\nWHAT WAS PROPOSED RECENTLY (do NOT repeat these):\n" +
            ("\n".join(backlog) or "(nothing yet)") +
            f"\n\nYour 1-3 concrete actions for {dept['name']} today:")

def _tier(line):
    u = line.upper()
    if "[HUMAN]" in u or "[NEEDS-HUMAN]" in u:
        return "HUMAN"
    if "[CLAUDE]" in u:
        return "CLAUDE"
    return "LOCAL"

def _automation_context():
    """Real data for the Automation dept: the mined-asks table + live tool inventory."""
    mined = ""
    try:
        mined = open(f"{BASE}/mined_asks.md").read()[:3500]
    except Exception:
        mined = "(mined_asks.md missing — propose re-mining the transcript corpus)"
    inv = []
    try:
        r = subprocess.run(["bash", "-c",
            "ls /opt/app/cli/*.py /opt/app/cli/*.sh 2>/dev/null | xargs -n1 basename; "
            "echo '--- crons ---'; crontab -l 2>/dev/null | grep -c '^[0-9*]' "],
            capture_output=True, text=True, timeout=20)
        inv = r.stdout.strip().splitlines()
    except Exception:
        pass
    return mined + "\n\nCURRENT CLI TOOLS:\n" + "\n".join(f"- {x}" for x in inv[:40])

def run_department(dept, ctx, gate, dry, metrics=None):
    prompt = _mk_prompt(dept, ctx)
    if dept["key"] == "scorecard" and metrics:
        prompt += ("\n\nUSE THESE REAL, MEASURED NUMBERS (do NOT invent others):\n" +
                   "\n".join(f"- {k} = {v}" for k, v in metrics.items()))
    elif dept["key"] == "automation":
        prompt += "\n\nMINED ASKS + TOOL INVENTORY (ground truth):\n" + _automation_context()
    elif dept["key"] == "corpus":
        try:
            prompt += ("\n\nKNOWLEDGE-GAP INDEX (close top-down, propose status updates):\n" +
                       open(f"{BASE}/knowledge_gaps.md").read()[:2500])
        except Exception:
            pass
    txt, engine = _engine(prompt)
    lines = [l.strip("-*• \t") for l in txt.splitlines()
             if l.strip() and (l.strip()[0:1].isalnum() or "[" in l[:2])]
    lines = [l for l in lines if len(l) >= 8][:3]
    tiers = {"LOCAL": 0, "CLAUDE": 0, "HUMAN": 0}
    escalations = []      # [CLAUDE] items batched for the rare board-review — never a live Claude call here
    parked = 0
    for line in lines:
        t = _tier(line); tiers[t] += 1
        if t == "CLAUDE":
            escalations.append(f"[{dept['key']}] {line}")
        # HUMAN -> gate parks as external_comms; CLAUDE/LOCAL tracked (LOCAL is the loop's own lane)
        risk = "external_comms" if t == "HUMAN" else "none"
        if not dry and gate:
            try:
                gate.request("brain-company", "brain",
                             f"[{dept['key']}] {line[:110]}", "self",
                             json.dumps({"dept": dept["key"], "tier": t, "line": line}), risk, 0)
                parked += 1
            except Exception as e:
                sys.stderr.write(f"[gate] {dept['key']} failed: {e}\n")
    return {"dept": dept["name"], "key": dept["key"], "engine": engine,
            "actions": lines, "parked": parked, "tiers": tiers, "escalations": escalations}

def _load_gate():
    sys.path.insert(0, MIND)
    try:
        spec = importlib.util.spec_from_file_location("gate", f"{MIND}/gate.py")
        g = importlib.util.module_from_spec(spec); spec.loader.exec_module(g)
        return g
    except Exception as e:
        sys.stderr.write(f"[gate] import failed ({e}) — proposals will only be logged\n")
        return None

def _append_backlog(results, stamp):
    seen = ""
    try:
        seen = open(BACKLOG).read()
    except Exception:
        pass
    new = []
    for r in results:
        for a in r["actions"]:
            h = hashlib.sha1(a.lower().encode()).hexdigest()[:8]
            if h in seen:                      # dedupe across days
                continue
            new.append(f"- [{stamp}] ({r['key']}) {a}  <!--{h}-->")
    if new:
        with open(BACKLOG, "a") as f:
            f.write(f"\n## {stamp}\n" + "\n".join(new) + "\n")
    return len(new)

def _git_commit(stamp, n):
    try:
        subprocess.run(["git", "-C", BASE, "add", "backlog.md", "proposals"],
                       check=False, capture_output=True)
        subprocess.run(["git", "-C", BASE, "commit", "-m",
                        f"brain-company {stamp}: {n} new proposals"],
                       check=False, capture_output=True)
    except Exception:
        pass

def _notify(summary):
    try:
        subprocess.run(["python3", "/opt/app/notify/atlas_notify.py",
                        "--title", "Atlas Cerebras — brain company cycle",
                        "--message", summary], check=False, capture_output=True, timeout=20)
    except Exception:
        pass

CANARIES = [   # kept to 2 so the daily chore stays bounded; swap in queries you know should hit
    "example: a recurring client requirement you expect the brain to recall",
    "second brain retrieval architecture",
]

def run_chores():
    """SAFE auto-execute lane: read-only / reversible self-measurement ONLY.
    NEVER runs model-generated text, NEVER deletes, NEVER sends. Produces real numbers
    that ground the scorecard and self-heal-by-measurement. Each chore is isolated."""
    out = {"flags": []}
    import re
    # 1) HEALTH — embedding coverage from brain.py stats (read-only)
    try:
        r = subprocess.run(["python3", f"{BASE}/brain.py", "stats"],
                           capture_output=True, text=True, timeout=60)
        s = r.stdout
        ch = int(re.search(r"chunks:\s*(\d+)", s).group(1)) if re.search(r"chunks:\s*(\d+)", s) else 0
        em = int(re.search(r"embedded:\s*(\d+)", s).group(1)) if re.search(r"embedded:\s*(\d+)", s) else 0
        out["chunks"] = ch
        out["embed_pct"] = round(100 * em / ch, 1) if ch else 0
        if out["embed_pct"] < 85:
            out["flags"].append(f"embedding coverage {out['embed_pct']}% < 85%")
    except Exception as e:
        out["flags"].append(f"health chore failed: {e}")
    # 2) CANARY — fixed retrieval queries must return hits (read-only)
    hits = 0
    for q in CANARIES:
        try:
            r = subprocess.run(["python3", f"{BASE}/brain.py", "recall", "--hybrid", "-k", "3", q],
                               capture_output=True, text=True, timeout=60)
            if r.stdout and len(r.stdout.strip()) > 20:
                hits += 1
        except Exception:
            pass
    out["canary"] = f"{hits}/{len(CANARIES)}"
    if hits < len(CANARIES):
        out["flags"].append(f"canary retrieval only {hits}/{len(CANARIES)}")
    # 3) SCRUB — flag credential patterns in recent chunks (flag-only; NEVER prints secret / deletes)
    try:
        d = sqlite3.connect(STORE)
        schema = (d.execute("SELECT sql FROM sqlite_master WHERE name='chunks'").fetchone() or [""])[0]
        col = "text" if "text" in schema else ("content" if "content" in schema else "body")
        rows = d.execute(f"SELECT {col} FROM chunks ORDER BY id DESC LIMIT 500").fetchall()
        d.close()
        # keyword-adjacent AND raw token formats — a real ck_ WooCommerce key sat one line above
        # the matched keyword and slipped the old pattern (found in wave-review 2026-07-14, chunk 425010)
        pat = re.compile(r"(password\s*[=:]|api[_-]?key\s*[=:]|secret\s*[=:]|bearer\s+[A-Za-z0-9]{8}|-----BEGIN"
                         r"|\b(ck|cs)_[0-9a-fA-F]{32,64}\b|\bsk-[A-Za-z0-9]{20,}\b|\bwhsec_\w{20,}\b"
                         r"|\bghp_[A-Za-z0-9]{30,}\b|\bgsk_[A-Za-z0-9]{20,}\b)", re.I)
        leaks = sum(1 for (t,) in rows if t and pat.search(t))
        out["scrub_suspect"] = leaks
        if leaks:
            out["flags"].append(f"{leaks}/500 recent chunks match credential patterns — review scrub")
    except Exception as e:
        out["flags"].append(f"scrub chore failed: {e}")
    return out

def cycle(dry=False, only=None, limit=None):
    os.makedirs(PROPOSALS, exist_ok=True); os.makedirs(LOGDIR, exist_ok=True)
    ctx = _context()
    gate = None if dry else _load_gate()
    depts = DEPARTMENTS
    if only:
        depts = [d for d in DEPARTMENTS if d["key"] == only] or DEPARTMENTS
    if limit:
        depts = depts[:limit]
    stamp = time.strftime("%Y-%m-%d %H:%M")
    metrics = _real_metrics()
    chores = run_chores() if only is None else {}   # safe self-measure lane on full cycles only
    results = []
    for i, d in enumerate(depts):
        results.append(run_department(d, ctx, gate, dry, metrics))
        if i < len(depts) - 1:
            time.sleep(4)   # throttle: stay under Cerebras free-tier rate limit (keeps gemma as primary)

    # human-readable report -> proposals/ + Obsidian vault
    sc_line = "**Live scorecard (measured):** " + " · ".join(f"{k}={v}" for k, v in metrics.items())
    md = [f"# Atlas Cerebras — brain-company cycle {stamp}",
          "_Engine: Cerebras (free) · propose-only via gate · target = the Second Brain itself._",
          sc_line, ""]
    if chores:
        md.append("**Self-measured (safe auto-run chores):** " +
                  " · ".join(f"{k}={v}" for k, v in chores.items() if k != "flags"))
        if chores.get("flags"):
            md.append("⚠️ " + " ; ".join(chores["flags"]))
        md.append("")
    for r in results:
        md.append(f"## {r['dept']}  ·  _{r['engine']}_")
        md += [f"- {a}" for a in r["actions"]] or ["- (no proposal)"]
        md.append("")
    report = "\n".join(md)
    total = sum(len(r["actions"]) for r in results)
    parked = sum(r["parked"] for r in results)
    # tier split — the "push work down" scorecard. escalation_pct should SHRINK over time (learning).
    tt = {"LOCAL": 0, "CLAUDE": 0, "HUMAN": 0}
    for r in results:
        for k, v in r.get("tiers", {}).items():
            tt[k] += v
    escalations = [e for r in results for e in r.get("escalations", [])]
    esc_pct = round(100 * (tt["CLAUDE"] + tt["HUMAN"]) / total, 1) if total else 0
    tier_line = (f"**Tier routing:** LOCAL={tt['LOCAL']} · CLAUDE={tt['CLAUDE']} · HUMAN={tt['HUMAN']} "
                 f"· escalation={esc_pct}% (lower = more autonomous)")
    report = report.replace("\n## ", "\n" + tier_line + "\n\n## ", 1)

    if not dry:
        open(f"{PROPOSALS}/company-{time.strftime('%Y%m%d-%H%M')}.md", "w").write(report)
        open(f"{VAULT}/Brain-Company-Latest.md", "w").write(report)
        if chores:
            open(f"{BASE}/health.json", "w").write(json.dumps(
                {"stamp": stamp, "escalation_pct": esc_pct, "tiers": tt, **chores}, indent=2))
        if escalations:   # batch hard items for the weekly Claude board-review (NOT a live call)
            with open(f"{BASE}/claude_queue.md", "a") as f:
                f.write(f"\n## {stamp} (escalation {esc_pct}%)\n" +
                        "\n".join(f"- {e}" for e in escalations) + "\n")
        n_new = _append_backlog(results, stamp)
        _git_commit(stamp, n_new)
        flag_note = (" · ⚠️ " + "; ".join(chores["flags"])) if chores.get("flags") else " · health ok"
        _notify(f"{len(results)} depts · {total} proposals · esc {esc_pct}% "
                f"({len(escalations)} for Claude) · {n_new} new{flag_note}")
    print(report)
    print(f"\n[summary] depts={len(results)} proposals={total} tiers={tt} "
          f"escalation={esc_pct}% chores={'run' if chores else 'skipped'} dry={dry}")

def weekly(dry=False):
    """Board-review (EOS + Shape Up), on the free mesh. Distills the week into (a) the next
    6-week BET written to ROADMAP.md and (b) 2-3 durable high-weight FACTS written back into
    the brain — the compounding 'get smarter with time' step. 0 Claude tokens."""
    os.makedirs(LOGDIR, exist_ok=True)
    try:
        backlog = open(BACKLOG).read()[-6000:]
    except Exception:
        backlog = "(empty)"
    prompt = ("This is one week of proposals from the brain's 8 departments:\n\n" + backlog +
              "\n\nAct as the board (Amazon working-backwards + Shape Up bet):\n"
              "1) BET: the SINGLE most important 6-week focus for the brain — one line, start 'BET:'.\n"
              "2) LEARNINGS: 2-3 durable, reusable facts worth permanently remembering — each one line, start 'FACT:'.\n"
              "Concrete and specific to this business. No fluff.")
    txt, eng = _engine(prompt)
    stamp = time.strftime("%Y-%m-%d")
    bet = [l.strip() for l in txt.splitlines() if l.strip().upper().startswith("BET:")]
    facts = [l.split(":", 1)[1].strip() for l in txt.splitlines() if l.strip().upper().startswith("FACT:")]
    report = f"# Atlas Cerebras — board-review {stamp}  ·  _{eng}_\n\n{txt}\n"
    written = 0
    if not dry:
        with open(f"{BASE}/ROADMAP.md", "a") as f:
            f.write(f"\n\n## 6-week BET ({stamp}) — auto, board-review\n" +
                    ("\n".join(bet) if bet else txt[:300]) + "\n")
        try:
            d = sqlite3.connect(STORE)
            for fct in facts[:3]:
                if len(fct) < 8:
                    continue
                d.execute("INSERT OR IGNORE INTO facts(fact,topic,weight,provenance,created) "
                          "VALUES(?,?,?,?,?)", (fct[:400], "board-review", 2.0,
                                                "brain-company-weekly", time.time()))
                written += 1
            d.commit(); d.close()
        except Exception as e:
            sys.stderr.write(f"[weekly] facts write failed: {e}\n")
        open(f"{VAULT}/Brain-Board-Review-Latest.md", "w").write(report)
        _git_commit(f"{stamp} weekly board-review", written)
        _notify(f"Board review: 6-week bet set · {written} learnings written into the brain")
    print(report)
    print(f"\n[weekly] bet_lines={len(bet)} facts_written={written} dry={dry} engine={eng}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--dept", default=None, help="run one department by key")
    ap.add_argument("--limit-depts", type=int, default=None)
    ap.add_argument("--weekly", action="store_true", help="board-review: bet + learnings into the brain")
    ap.add_argument("--chores", action="store_true", help="run ONLY the safe read-only self-measure chores")
    a = ap.parse_args()
    if a.chores:
        c = run_chores()
        print(json.dumps(c, indent=2))
    elif a.weekly:
        weekly(dry=a.dry_run)
    else:
        cycle(dry=a.dry_run, only=a.dept, limit=a.limit_depts)
