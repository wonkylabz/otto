#!/usr/bin/env python3
"""One-off backfill: collapse the audit trail's two model namespaces into canonical model ids.

A pool entry has a LABEL (`name`, operator-editable) and an id (`model`, what the server
serves). Every Claude path recorded the id; every local path and `gateway.last()` recorded the
label — so `audit.model` and `audit.verdict_model` held two names for one model and `scorecard`
could not join a judge to an executor. `gateway.model_id` + `audit._audit` fix that going
forward; this fixes the rows already written.

Resolution is EVIDENCE-ONLY: a label resolves through the live pool (or
`gateway._RETIRED_MODEL_IDS`) or it is left exactly as it is and reported. Guessing what a
deleted pool entry used to serve would put a fabricated attribution in an immutable trail, which
is worse than a label that no longer joins.

    ./.venv/bin/python migrate_model_ids.py           # dry run: report only
    ./.venv/bin/python migrate_model_ids.py --apply   # back up the DB, then rewrite

Idempotent — a second --apply is a no-op. Rewrites only the `model`/`verdict_model` keys inside
each row's JSON `data` blob (adding `model_entry` where the label differed from the id); no row
is added, removed, or otherwise touched.
"""
import json
import shutil
import sys
import time

import audit
import config
import gateway

_KEYS = ("model", "verdict_model")


def plan(cfg=None):
    """(changes, unresolved) — changes is [(rowid, new_data_json, [(key, old, new)])]."""
    cfg = cfg or gateway.load()
    conn = audit._audit_conn()
    try:
        rows = conn.execute("SELECT id, data FROM audit ORDER BY id ASC").fetchall()
    finally:
        conn.close()
    changes, unresolved = [], {}
    for row in rows:
        data = json.loads(row["data"])
        moves = []
        for key in _KEYS:
            label = data.get(key)
            if not label:
                continue
            canonical = gateway.model_id(label, cfg)
            if canonical == label:
                # Either already canonical, or a RETIRED entry's label whose id died with the
                # entry. Nothing on the row tells the two apart — a retired model's id is
                # legitimately absent from the current pool too — so this reports the set and
                # classifies none of it. Guessing is what the whole migration exists to avoid.
                if not any(m.get("model") == label for m in cfg.get("pool", [])):
                    unresolved[label] = unresolved.get(label, 0) + 1
                continue
            data[key] = canonical
            moves.append((key, label, canonical))
            if key == "model":
                data["model_entry"] = label
        if moves:
            changes.append((row["id"], json.dumps(data), moves))
    return changes, unresolved


def main(argv):
    apply = "--apply" in argv
    changes, unresolved = plan()
    tally = {}
    for _rid, _d, moves in changes:
        for key, old, new in moves:
            tally[(key, old, new)] = tally.get((key, old, new), 0) + 1
    print(f"{len(changes)} audit row(s) to rewrite")
    for (key, old, new), n in sorted(tally.items(), key=lambda kv: -kv[1]):
        print(f"  {n:5}  {key:13} {old!r} -> {new!r}")
    if unresolved:
        print("\nnot matched by any current pool entry (left unchanged — already an id, OR a")
        print("retired entry's label; the row cannot say which):")
        for label, n in sorted(unresolved.items(), key=lambda kv: -kv[1]):
            print(f"  {n:5}  {label!r}")
        print("  -> for any that IS a retired label, put its real id in")
        print("     gateway._RETIRED_MODEL_IDS and re-run. Leave the rest alone.")
    if not changes:
        print("\nnothing to do.")
        return 0
    if not apply:
        print("\ndry run — re-run with --apply to write.")
        return 0
    backup = f"{config.DB_PATH}.pre-model-ids.{time.strftime('%Y%m%dT%H%M%S')}"
    shutil.copy2(config.DB_PATH, backup)
    print(f"\nbacked up {config.DB_PATH} -> {backup}")
    conn = audit._audit_conn()
    try:
        with conn:      # one transaction: a partial rewrite is not a state worth having
            conn.executemany("UPDATE audit SET data = ? WHERE id = ?",
                             [(d, rid) for rid, d, _m in changes])
    finally:
        conn.close()
    print(f"rewrote {len(changes)} row(s)")
    left, _ = plan()
    print(f"re-plan after apply: {len(left)} row(s) (0 = idempotent)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
