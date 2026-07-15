#!/bin/bash
# Atlas Second Brain — continuous self-feed (niced, locked, idempotent).
# Re-ingests Atlas code + any refreshed transcripts, embeds a bounded backlog.
exec 9>/opt/app/secondbrain/logs/.feed.lock
flock -n 9 || { echo "[feed] $(date -Is) busy, skip" >> /opt/app/secondbrain/logs/feed.log; exit 0; }
cd /opt/app/secondbrain
PY=./venv/bin/python3
{
  echo "[feed] $(date -Is) start"
  $PY brain.py ingest-files /opt/app/mind
  $PY brain.py ingest-files /opt/app/secondbrain/corpus  >/dev/null 2>&1 || true
  $PY brain.py ingest-transcripts /opt/app/secondbrain/corpus
  $PY brain.py ingest-kernel
  $PY brain.py rescrub
  nice -n 15 $PY brain.py embed --limit 6000 --batch 64
  echo "[feed] $(date -Is) done"
} >> logs/feed.log 2>&1
