#!/usr/bin/env python3
"""
Atlas Second Brain — REACTOR (Phase 3)
======================================
The missing cognition step: turns the append-only event spine into perception-driven
action PROPOSALS. For each unprocessed kernel.sqlite event it recalls relevant brain
memory, asks a LOCAL model (qwen2.5 via Ollama — no Claude) what to do, and routes the
proposal through the EXISTING gate.py (gate.request). It NEVER sends money or comms and
never bypasses send_effector — money/external_comms/irreversible/self_modify always park,
kill_switch parks everything. High-priority events get an LLM proposal; low-priority are
dispositioned by rule. Then the event is marked processed.

  reactor.py --dry-run            # show what it WOULD do, mark nothing
  reactor.py --limit 40 --llm 10  # one real pass (timer default)
"""
import sqlite3, json, sys, os, time, argparse, importlib.util
sys.path.insert(0, "/opt/app/mind")          # gate.py (stdlib-only)
KERNEL  = "/opt/app/mind/kernel.sqlite"
OLLAMA  = "http://localhost:11434"
MODEL   = "qwen2.5:7b"
LOG     = "/opt/app/secondbrain/logs/reactor.log"

# --- load brain_recall from the secondbrain engine ---
def _load_brain():
    spec = importlib.util.spec_from_file_location("brain", "/opt/app/secondbrain/brain.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m
brain = _load_brain()

# --- load the existing gate (propose-only enforcement lives here) ---
try:
    import gate as GATE
except Exception as e:
    GATE = None
    print(f"[reactor] WARN gate.py import failed ({e}); proposals will only be logged", file=sys.stderr)

import requests
def _llm(prompt, timeout=120):
    r = requests.post(f"{OLLAMA}/api/generate",
                      json={"model": MODEL, "prompt": prompt, "stream": False,
                            "options": {"temperature": 0.2}, "keep_alive": "5m"}, timeout=timeout)
    r.raise_for_status(); return r.json().get("response", "").strip()

PROPOSE_PROMPT = """You are Atlas's autonomous operations reactor. Given an inbound business EVENT and
relevant MEMORY from past work, decide the single best NEXT ACTION (or none).
Reply ONLY as compact JSON: {"action": "...", "domain": "money|comms|fleet|data|sales|ops",
"risk": "money|external_comms|irreversible|self_modify|none", "priority": 0-3, "reason": "<=20 words"}.
Rules: proposing is fine, you never execute. If it needs a human (payment, sending a message, anything
irreversible) set the matching risk so it parks for approval. If nothing is needed, action:"noop".

EVENT:
{event}

RELEVANT MEMORY:
{memory}

JSON:"""

def _disposition(ev, dry, do_llm):
    eid, ts, sense, subject, etype, prio, payload = ev
    pl = ""
    try:
        d = json.loads(payload or "{}")
        pl = " ".join(f"{k}={str(v)[:120]}" for k, v in d.items())
    except Exception:
        pl = (payload or "")[:200]
    summary = f"[{subject}] type={etype} prio={prio} sense={sense} :: {pl}"[:600]

    if prio is not None and prio <= 1 and do_llm:
        try:
            hits = brain.brain_recall(summary, k=4)
            mem = "\n".join(f"- ({h['score']}) {h['ref']}: {h['text'][:160]}" for h in hits) or "(none)"
        except Exception as e:
            mem = f"(recall failed: {e})"
        try:
            raw = _llm(PROPOSE_PROMPT.replace("{event}", summary).replace("{memory}", mem))
            js = raw[raw.find("{"): raw.rfind("}") + 1]
            prop = json.loads(js)
        except Exception as e:
            prop = {"action": "review", "domain": "data", "risk": "none", "priority": prio, "reason": f"parse fail: {e}"}
        verdict, reason = ("park", "no gate")
        if GATE and prop.get("action") not in ("noop", "", None):
            try:
                verdict, reason = GATE.gate(prop.get("domain", "data"), prop.get("action", "review"),
                                            prop.get("risk", "none"), 0)
                if not dry:
                    GATE.request("reactor", prop.get("domain", "data"), prop.get("action"),
                                 f"event:{eid}", json.dumps({"event": summary, "proposal": prop}),
                                 prop.get("risk", "none"), 0)
            except Exception as e:
                verdict, reason = "park", f"gate err: {e}"
        return {"eid": eid, "llm": True, "proposal": prop, "verdict": verdict, "gate_reason": reason}
    # low priority -> rule disposition, no LLM, no action
    return {"eid": eid, "llm": False, "proposal": {"action": "logged", "reason": "low-priority, no action"}}

def run(limit=40, llm_cap=10, dry=False):
    db = sqlite3.connect(KERNEL); db.execute("PRAGMA busy_timeout=5000")
    rows = db.execute(
        "SELECT id,ts,sense,subject,type,priority,payload FROM events "
        "WHERE processed=0 ORDER BY priority ASC, id DESC LIMIT ?", (limit,)).fetchall()
    print(f"[reactor] {len(rows)} unprocessed events this pass (dry={dry})")
    used = 0; results = []
    for ev in rows:
        do_llm = used < llm_cap
        r = _disposition(ev, dry, do_llm)
        if r.get("llm"): used += 1
        results.append(r)
        tag = ("PROPOSE:" + r["proposal"].get("action", "?") + f" [{r.get('verdict','-')}]") if r.get("llm") else "rule:logged"
        print(f"  ev#{r['eid']:>5}  {tag}")
        if not dry:
            db.execute("UPDATE events SET processed=1 WHERE id=?", (r["eid"],))
    if not dry:
        db.commit()
        with open(LOG, "a") as f:
            f.write(f"{time.strftime('%FT%T')} processed={len(rows)} llm={used} "
                    f"parked={sum(1 for r in results if r.get('verdict')=='park')}\n")
    db.close()
    print(f"[reactor] done: {len(rows)} handled, {used} LLM proposals, "
          f"{sum(1 for r in results if r.get('verdict')=='park')} parked for approval")

if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="reactor")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--llm", type=int, default=10)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    run(a.limit, a.llm, a.dry_run)
