#!/usr/bin/env python3
"""
Atlas Second Brain — SELF-PROMPT loop
=====================================
The brain prompts ITSELF. Pulls discovered themes (facts), recent high-priority events,
and open roadmap tasks, then asks a LOCAL model (qwen2.5 — no Claude) for the most
important things Atlas should do next. Writes them as PARKED proposals (vault + file) and
through the gate. Never executes. This is the autonomous "what should I work on" cycle.

  selfprompt.py            # one cycle -> proposals/ + vault note
"""
import sqlite3, json, sys, os, time, importlib.util
sys.path.insert(0, "/opt/app/mind")
BASE   = "/opt/app/secondbrain"
KERNEL = "/opt/app/mind/kernel.sqlite"
VAULT  = "/opt/app/brain"
OLLAMA = "http://localhost:11434"
MODEL  = "qwen2.5:7b"

spec = importlib.util.spec_from_file_location("brain", f"{BASE}/brain.py")
brain = importlib.util.module_from_spec(spec); spec.loader.exec_module(brain)
try:
    import gate as GATE
except Exception:
    GATE = None
import requests

def _llm(prompt, timeout=180):
    r = requests.post(f"{OLLAMA}/api/generate",
                      json={"model": MODEL, "prompt": prompt, "stream": False,
                            "options": {"temperature": 0.3}, "keep_alive": "5m"}, timeout=timeout)
    r.raise_for_status(); return r.json().get("response", "").strip()

def _context():
    bd = sqlite3.connect(f"{BASE}/store/brain.db")
    facts = [f for (f,) in bd.execute("SELECT fact FROM facts ORDER BY weight DESC LIMIT 12").fetchall()]
    bd.close()
    k = sqlite3.connect(KERNEL)
    evs = k.execute("SELECT subject,type,payload FROM events WHERE priority<=1 "
                    "ORDER BY id DESC LIMIT 12").fetchall()
    k.close()
    tasks = []
    try:
        t = json.load(open(f"{BASE}/tasks.json"))
        tasks = [f"{x['id']} [{x['status']}] {x['title']}" for x in t.get("tasks", [])
                 if x.get("status") not in ("done",)]
    except Exception:
        pass
    return facts, evs, tasks

PROMPT = """You are an autonomous business operations brain for a small self-hosted business/personal stack.
Based ONLY on the context below, list the 3 MOST IMPORTANT things Atlas should do next.
Be concrete and specific to THIS business. For each: one line, start with a verb, and mark
[NEEDS-HUMAN] if it involves money, sending a message, or anything irreversible.

DISCOVERED THEMES:
{facts}

RECENT HIGH-PRIORITY SIGNALS:
{events}

OPEN ROADMAP TASKS:
{tasks}

The 3 next actions:"""

def cycle():
    facts, evs, tasks = _context()
    ev_lines = []
    for subj, typ, pl in evs:
        try:
            d = json.loads(pl or "{}"); s = " ".join(f"{k}={str(v)[:60]}" for k, v in list(d.items())[:4])
        except Exception:
            s = (pl or "")[:80]
        ev_lines.append(f"- {subj} ({typ}): {s}")
    prompt = (PROMPT.replace("{facts}", "\n".join(f"- {x}" for x in facts) or "(none yet)")
                    .replace("{events}", "\n".join(ev_lines) or "(none)")
                    .replace("{tasks}", "\n".join(f"- {x}" for x in tasks) or "(none)"))
    out = _llm(prompt)
    day = time.strftime("%Y-%m-%d %H:%M")
    note = f"# Atlas self-prompt — {day}\n\n_Local model ({MODEL}), no Claude. Proposals only — parked for review._\n\n{out}\n"
    os.makedirs(f"{BASE}/proposals", exist_ok=True)
    open(f"{BASE}/proposals/selfprompt-{time.strftime('%Y%m%d-%H%M')}.md", "w").write(note)
    # surface in the operator's Obsidian vault
    open(f"{VAULT}/Self-Prompt-Latest.md", "w").write(note)
    # park each proposed line through the gate so human-needing ones queue for approval
    if GATE:
        for line in [l.strip("-* ").strip() for l in out.splitlines() if l.strip() and l.strip()[0:1].isalnum() or "[NEEDS-HUMAN]" in l]:
            if len(line) < 6:
                continue
            risk = "external_comms" if "[NEEDS-HUMAN]" in line else "none"
            try:
                GATE.request("selfprompt", "ops", line[:120], "self", json.dumps({"line": line}), risk, 0)
            except Exception:
                pass
    print(note[:800])

if __name__ == "__main__":
    cycle()
