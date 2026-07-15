#!/usr/bin/env python3
"""File-based patcher (atlas-01 convention: backup + py_compile + auto-revert).

Adds the blueprint-critic HARD_GATE additions to /opt/app/mind/gate.py:
  {"money","external_comms","irreversible","self_modify"}
    -> + "calendar", "memory_write"

Run on atlas-01:  python3 patch_gate_hardgate.py [--dry-run]
"""
import py_compile
import shutil
import sys
import time

TARGET = "/opt/app/mind/gate.py"
OLD = 'HARD_GATE = {"money", "external_comms", "irreversible", "self_modify"}'
NEW = 'HARD_GATE = {"money", "external_comms", "irreversible", "self_modify", "calendar", "memory_write"}'


def main():
    dry = "--dry-run" in sys.argv
    src = open(TARGET, encoding="utf-8").read()
    if NEW in src:
        print("already patched — nothing to do")
        return 0
    if OLD not in src:
        print(f"ERROR: expected HARD_GATE line not found in {TARGET}; refusing to patch")
        return 1
    if dry:
        print("dry-run OK: would patch HARD_GATE (+calendar, +memory_write)")
        return 0
    backup = f"{TARGET}.bak-cerebras-{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(TARGET, backup)
    print(f"backup: {backup}")
    open(TARGET, "w", encoding="utf-8").write(src.replace(OLD, NEW, 1))
    try:
        py_compile.compile(TARGET, doraise=True)
    except py_compile.PyCompileError as e:
        shutil.copy2(backup, TARGET)
        print(f"COMPILE FAILED — auto-reverted from backup: {e}")
        return 1
    print("patched + compiled OK: HARD_GATE now includes calendar, memory_write")
    return 0


if __name__ == "__main__":
    sys.exit(main())
