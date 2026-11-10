#!/usr/bin/env python3
"""resolve.py: file the principal's OUTCOME of a jury case (v3, 2026-11-09).

v2 rewrote the case file in place to add the outcome, which changed the
hash that CHAIN.txt and any published copy commit to. v3 never touches the
case file: it appends one line to outcomes.jsonl in the archive, carrying
the case file's sha256, so the record of what the jury saw stays fixed and
the decision is bound to that exact record.

Usage:
    python3 resolve.py <case-file-or-fragment> --outcome uphold|reject|split|amended|superseded \\
        --by NAME [--note "..."]
    python3 resolve.py --verify      # rehash every case named in outcomes.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

OUTCOMES = ("uphold", "reject", "split", "amended", "superseded")


def archive() -> Path:
    return Path(os.environ.get("AGENT_JURY_ARCHIVE",
                               str(Path(__file__).resolve().parent / "cases")))


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def find_case(arg: str) -> Path:
    if Path(arg).is_file():
        return Path(arg)
    hits = sorted(h for h in archive().glob(f"*{Path(arg).name}*") if h.suffix == ".json" and h.name.startswith("jury-"))
    if len(hits) != 1:
        raise SystemExit(f"resolve: {'no' if not hits else 'ambiguous'} case matching {arg!r} "
                         f"in {archive()} {[h.name for h in hits]}")
    return hits[0]


def resolve(case: Path, outcome: str, by: str, note: str = "") -> dict:
    rec = json.loads(case.read_text())
    entry = {"action": "resolve", "case": case.name, "case_sha256": sha(case),
             "jury_majority": rec.get("majority"), "outcome": outcome, "by": by,
             "at": now_iso(), "note": note}
    archive().mkdir(parents=True, exist_ok=True)
    with (archive() / "outcomes.jsonl").open("a") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def verify() -> int:
    f = archive() / "outcomes.jsonl"
    if not f.exists():
        print("no outcomes filed")
        return 0
    bad = 0
    for line in f.read_text().splitlines():
        e = json.loads(line)
        p = archive() / e["case"]
        ok = p.is_file() and sha(p) == e["case_sha256"]
        bad += not ok
        print(f"{'OK ' if ok else 'BAD'} {e['case']} jury={e['jury_majority']} "
              f"outcome={e['outcome']} by {e['by']} at {e['at']}")
    return 1 if bad else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="file the outcome of a jury case")
    ap.add_argument("case", nargs="?")
    ap.add_argument("--outcome", choices=OUTCOMES)
    ap.add_argument("--by", default="principal")
    ap.add_argument("--note", default="")
    ap.add_argument("--verify", action="store_true")
    a = ap.parse_args()
    if a.verify:
        return verify()
    if not a.case or not a.outcome:
        ap.error("case and --outcome are required (or use --verify)")
    e = resolve(find_case(a.case), a.outcome, a.by, a.note)
    print(f"RESOLVED {e['case']}: jury said {e['jury_majority']}, {e['by']} decided "
          f"{e['outcome']}{' (' + a.note + ')' if a.note else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
