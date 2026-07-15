#!/usr/bin/env python3
"""
Atlas Second Brain — learning layer  (the self-improving, non-Claude ML part)
=============================================================================
Runs entirely on CPU over the embeddings already in brain.db. No GPU, no Claude.

  topics              unsupervised theme discovery (MiniBatchKMeans over nomic vectors)
                      + top TF-IDF terms per theme  -> writes facts table + topics.md
  classify-train      supervised priority/intent classifier (SGD) trained on
                      gate approve/deny + outcome labels  (Phase 3 data)
  classify <text>     predict priority/intent for a new signal

The loop that makes it "feed itself":
  every new embedded chunk -> topics re-clustered nightly -> emergent themes become
  durable facts -> the priority classifier learns from each gate decision -> retrieval
  and triage get sharper over time, with zero API spend.
"""
import os, sys, json, sqlite3, time, argparse, pickle, struct
import numpy as np
import sqlite_vec

BASE = "/opt/app/secondbrain"
DB   = os.environ.get("BRAIN_DB", f"{BASE}/store/brain.db")
MODELS = f"{BASE}/store"

def _db():
    c = sqlite3.connect(DB); c.enable_load_extension(True); sqlite_vec.load(c); c.enable_load_extension(False)
    return c

def _load_vectors(limit=None):
    """Return (ids, X[n,768], texts, projects) for embedded chunks."""
    c = _db()
    q = ("SELECT c.id, c.text, c.project, v.emb FROM chunks c "
         "JOIN vec_chunks v ON v.rowid=c.id WHERE c.embedded=1")
    if limit: q += f" LIMIT {int(limit)}"
    ids, texts, projs, vecs = [], [], [], []
    for cid, text, proj, emb in c.execute(q):
        ids.append(cid); texts.append(text or ""); projs.append(proj or "")
        vecs.append(np.frombuffer(emb, dtype=np.float32))
    c.close()
    if not vecs: return [], np.zeros((0,768)), [], []
    return ids, np.vstack(vecs), texts, projs

# ---------------------------------------------------------------------------
def topics(k=16, write_facts=True):
    from sklearn.cluster import MiniBatchKMeans
    from sklearn.feature_extraction.text import TfidfVectorizer
    ids, X, texts, projs = _load_vectors()
    if len(ids) < k*3:
        print(f"[topics] only {len(ids)} embedded chunks — need >= {k*3}; embed more first."); return
    print(f"[topics] clustering {len(ids)} vectors into {k} themes ...")
    km = MiniBatchKMeans(n_clusters=k, random_state=0, n_init=3, batch_size=256)
    labels = km.fit_predict(X)
    # top terms per cluster via TF-IDF on the member texts
    tf = TfidfVectorizer(max_features=4000, stop_words="english", token_pattern=r"[A-Za-z][A-Za-z0-9_.-]{2,}")
    M = tf.fit_transform(texts); vocab = np.array(tf.get_feature_names_out())
    out = []
    c = _db()
    for cl in range(k):
        idx = np.where(labels==cl)[0]
        if len(idx)==0: continue
        centroid = np.asarray(M[idx].mean(axis=0)).ravel()
        top = vocab[centroid.argsort()[::-1][:8]]
        # dominant project for the theme
        pj = {}
        for i in idx: pj[projs[i]] = pj.get(projs[i],0)+1
        dom = max(pj, key=pj.get)
        theme = ", ".join(top)
        out.append({"theme": theme, "size": int(len(idx)), "project": dom})
        if write_facts:
            fact = f"Recurring theme ({len(idx)} chunks, mostly {dom}): {theme}"
            c.execute("INSERT OR REPLACE INTO facts(fact,topic,weight,provenance,created) VALUES(?,?,?,?,?)",
                      (fact, f"cluster-{cl}", float(len(idx)), "learn.topics", time.time()))
    c.commit(); c.close()
    out.sort(key=lambda r:-r["size"])
    with open(f"{BASE}/store/topics.md","w") as f:
        f.write(f"# Atlas Brain — discovered themes ({len(ids)} chunks, {k} clusters)\n\n")
        for r in out: f.write(f"- **{r['size']}** [{r['project']}] — {r['theme']}\n")
    print(f"[topics] {len(out)} themes -> {BASE}/store/topics.md  (+facts)")
    for r in out[:10]: print(f"  {r['size']:5d} [{r['project'][:24]:24s}] {r['theme']}")

# ---------------------------------------------------------------------------
def classify_train():
    """Train an SGD priority/intent classifier from labels in the 'labels' table.
    labels(chunk_id, label) is populated by the Phase-3 reactor from gate outcomes."""
    from sklearn.linear_model import SGDClassifier
    c = _db()
    try:
        rows = c.execute("SELECT l.chunk_id,l.label,v.emb FROM labels l JOIN vec_chunks v ON v.rowid=l.chunk_id").fetchall()
    except sqlite3.OperationalError:
        print("[classify-train] no 'labels' table yet — Phase 3 reactor must populate gate outcomes first."); return
    if len(rows) < 30:
        print(f"[classify-train] only {len(rows)} labels — need >=30. Accumulating from gate decisions."); return
    X = np.vstack([np.frombuffer(e,dtype=np.float32) for _,_,e in rows]); y=[l for _,l,_ in rows]
    clf = SGDClassifier(loss="log_loss"); clf.fit(X,y)
    pickle.dump((clf,), open(f"{MODELS}/priority_clf.pkl","wb"))
    print(f"[classify-train] trained on {len(y)} labels, classes={list(clf.classes_)}")

def classify(text):
    import requests
    if not os.path.exists(f"{MODELS}/priority_clf.pkl"):
        print("no classifier yet (Phase 3+)"); return
    (clf,) = pickle.load(open(f"{MODELS}/priority_clf.pkl","rb"))
    emb = requests.post("http://localhost:11434/api/embeddings",
                        json={"model":"nomic-embed-text","prompt":"search_query: "+text}).json()["embedding"]
    pred = clf.predict([np.array(emb,dtype=np.float32)])[0]
    print(f"predicted: {pred}")

# ---------------------------------------------------------------------------
if __name__ == "__main__":
    ap = argparse.ArgumentParser(prog="learn")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p=sub.add_parser("topics"); p.add_argument("-k",type=int,default=16)
    sub.add_parser("classify-train")
    p=sub.add_parser("classify"); p.add_argument("text")
    a=ap.parse_args()
    if a.cmd=="topics": topics(a.k)
    elif a.cmd=="classify-train": classify_train()
    elif a.cmd=="classify": classify(a.text)
