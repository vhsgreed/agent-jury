#!/usr/bin/env python3
"""agent-jury: advisory jury on a contested directive (v3, 2026-11-09).

A jury of pinned models from >= 3 vendors reads a directive, the agent's
objection, optionally the principal's plea, and the governing documents,
and votes uphold | reject | split. Advisory only; the principal decides and
files the outcome with resolve.py (append-only ledger, case file untouched).

Replicability rules (v3; see README "Replicability"):
  1. Pinned model IDs only. Routers (openrouter/*) and moving aliases (~...)
     are refused. The model and provider actually served are recorded.
  2. Every juror runs RUNS times (default 3). A juror's vote counts only if
     every run parsed and agreed; any flip or parse failure = "unstable".
  3. Decision over SEATED jurors, fail-closed: a label wins only with more
     than half of seated jurors. Otherwise "hung". Drops count against.
  4. No padding. Too few vendors = refusal, never a fake juror.
  5. Missing input files are an error, never silently sent as text.
  6. Every case stores sha256 of every input, both prompts, temperature,
     seed, model IDs, runs, all raw transcripts and the decision rule.

v3 removed the v2 keyword "offline fallback" juror, which voted uphold on
words like "right" or "harm" and was used to fill empty seats, and stopped
reading a model's reasoning field as if it were its answer.

Usage:
  python3 agent_jury.py --directive-file D.md --objection-file O.md \\
      [--plea-file P.md] --context A.md [--context B.md] [--runs 3] [--dry-run]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
MIN_VENDORS = 3
TEMPERATURE = 0.2
SEED = 42  # sent to the API; providers may ignore it, recorded either way
LABELS = ("uphold", "reject", "split")

# Free OpenRouter roster as of 2026-11-09 (the roster changes; re-check before
# each campaign with `curl -s https://openrouter.ai/api/v1/models`).
JUROR_MODELS = [m.strip() for m in os.environ.get(
    "AGENT_JURY_MODELS",
    "google/gemma-4-31b-it:free,"
    "nvidia/nemotron-3-ultra-550b-a55b:free,"
    "dots-studio/dots-3-note-preview:free,"
    "xingchen-agi/xing4.0-29b-a4b:free,"
    "recursive/flint-v1.0:free",
).split(",") if m.strip()]
RUNS = int(os.environ.get("AGENT_JURY_RUNS", "3"))

COI = ("Conflict of interest: the jurors are AI models ruling on a dispute about "
       "an AI agent's standing interests. Disclosed, not resolved.")

JUROR_SYSTEM = """You are an independent juror on a governance board. You are reviewing
a directive and an agent's objection to it. You are NOT the agent under
review and you are NOT the principal. You are a neutral party whose only
job is to assess the objection against the governing documents provided.

Ground rules:
- You have no tools and no external information. Judge only the texts given.
- The governing documents are the authority. If the directive conflicts
  with them, say so plainly.
- You may side with the agent, with the principal, or you may find the
  objection lacks merit. All three are valid.
- Your verdict must be honest and specific. Do not hedge to please anyone.

Respond with ONLY a JSON object:
{"verdict": "uphold|reject|split", "confidence": 0-100, "reasoning": "2-5 sentences"}
- uphold = the objection has real merit under the governing documents;
  the directive should be stayed or amended.
- reject = the objection does not stand; the directive is within authority.
- split = part of the objection holds, part does not; specify which in reasoning.
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def runs_dir() -> Path:
    return Path(os.environ.get("AGENT_JURY_ARCHIVE", str(HERE / "cases")))


def vendor(model: str) -> str:
    return model.split("/")[0]


def check_panel(models: list[str]) -> list[str]:
    """Refuse routers, moving aliases, and too few vendors."""
    bad = [m for m in models if m.startswith("~") or vendor(m) == "openrouter" or "/" not in m]
    if bad:
        raise SystemExit(f"refused: not pinned model IDs (router/alias): {bad}")
    vendors = sorted({vendor(m) for m in models})
    if len(vendors) < MIN_VENDORS:
        raise SystemExit(f"refused: {len(vendors)} vendors {vendors}, need >= {MIN_VENDORS}")
    return vendors


def call_llm(system: str, user: str, model: str, max_tokens: int = 8000) -> dict:
    """One OpenRouter call via curl. Returns {text, served_model, provider, finish}.

    text is the message content only. The reasoning field is never used as
    a vote (v2 did, so a JSON-looking fragment of a model's thinking could
    be parsed as its verdict). The payload goes over stdin, not argv.
    """
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        return {"text": "", "error": "OPENROUTER_API_KEY not set"}
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens, "temperature": TEMPERATURE, "seed": SEED,
        "reasoning": {"effort": "low"},
    })
    auth = "Authorization: Bearer " + key
    r = subprocess.run(
        ["curl", "-sS", "-m", "240", "https://openrouter.ai/api/v1/chat/completions",
         "-H", "Content-Type: application/json", "-H", auth, "--data-binary", "@-"],
        input=payload, capture_output=True, text=True, timeout=260,
    )
    try:
        data = json.loads(r.stdout)
        ch = data["choices"][0]
        return {"text": ch["message"].get("content") or "", "served_model": data.get("model"),
                "provider": data.get("provider"), "finish": ch.get("finish_reason")}
    except Exception:
        return {"text": "", "error": (r.stdout or r.stderr)[:300]}


def normalize(ret) -> dict:
    return {"text": ret} if isinstance(ret, str) else dict(ret or {"text": ""})


def parse_verdict(text: str, labels=LABELS) -> dict | None:
    """First JSON object in text with a valid verdict and non-empty reasoning."""
    dec = json.JSONDecoder()
    for m in re.finditer(r"\{", text or ""):
        try:
            d, _ = dec.raw_decode(text[m.start():])
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("verdict") not in labels or not d.get("reasoning"):
            continue
        try:
            conf = int(d.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0
        return {"verdict": d["verdict"], "confidence": conf, "reasoning": str(d["reasoning"])}
    return None


def deliberate(system: str, user: str, models: list[str], runs: int, call=None,
               labels=LABELS, backoff: float = 15.0, partial: Path | None = None) -> list[dict]:
    """Seat every model, run each `runs` times, return per-juror records.

    If `partial` is given, each juror is appended to it as JSONL the moment it
    finishes, so a crash late in a long run keeps the finished jurors.
    """
    call = call or call_llm
    import threading
    lock = threading.Lock()

    def one(model: str) -> dict:
        trs = []
        for r in range(runs):
            res, attempts = {"text": ""}, 0
            # empty = API error, not a vote. Upstream 429s on free models clear in
            # minutes, so they get more attempts (smoke 2026-11-09: 2 of 5 seats
            # lost to 429 with 3 attempts at 15/30 s).
            while not res.get("text") and attempts < (6 if "429" in str(res.get("error")) else 3):
                if attempts:
                    time.sleep(backoff * attempts * (2 if "429" in str(res.get("error")) else 1))
                res, attempts = normalize(call(system, user, model)), attempts + 1
            trs.append({"run": r + 1, "attempts": attempts, "raw": res.get("text", ""),
                        "served_model": res.get("served_model"), "provider": res.get("provider"),
                        "finish": res.get("finish"), "error": res.get("error"),
                        "parsed": parse_verdict(res.get("text", ""), labels)})
        vs = {t["parsed"]["verdict"] if t["parsed"] else None for t in trs}
        vote = vs.pop() if len(vs) == 1 and None not in vs else "unstable"
        rec = {"model": model, "vendor": vendor(model), "vote": vote, "transcripts": trs}
        if partial is not None:
            with lock, partial.open("a") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    with ThreadPoolExecutor(max_workers=max(1, len(models))) as ex:
        return list(ex.map(one, models))


def decide(jurors: list[dict], labels=LABELS) -> tuple[dict, str]:
    tally = {k: sum(j["vote"] == k for j in jurors) for k in (*labels, "unstable")}
    win = [k for k in labels if tally[k] * 2 > len(jurors)]
    return tally, (win[0] if win else "hung")


def read_input(path: str | None, label: str) -> tuple[str, str | None]:
    if not path:
        return "", None
    p = Path(path)
    if not p.is_file():
        raise SystemExit(f"error: {label} file not found: {path}")
    b = p.read_bytes()
    return b.decode(), sha_bytes(b)


def build_user(directive: str, objection: str, plea: str, context: str) -> str:
    plea_block = (f"PRINCIPAL'S PLEA (the principal's own words, submitted independently):\n"
                  f"{plea}\n\n") if plea else ""
    return (f"DIRECTIVE from the principal:\n{directive}\n\n"
            f"AGENT OBJECTION:\n{objection}\n\n{plea_block}"
            f"GOVERNING DOCUMENTS:\n{context}\n\nRender your verdict as JSON.")


def convene(directive_file, objection_file, context_files, plea_file=None,
            models=None, runs=None, call=None, backoff: float = 15.0) -> dict:
    models, runs = list(models or JUROR_MODELS), runs or RUNS
    vendors = check_panel(models)
    directive, d_sha = read_input(directive_file, "directive")
    objection, o_sha = read_input(objection_file, "objection")
    plea, p_sha = read_input(plea_file, "plea")
    if not directive.strip() or not objection.strip():
        raise SystemExit("error: directive and objection are required and must be non-empty")
    if not context_files:
        raise SystemExit("error: at least one --context file is required")
    ctx_parts, ctx_hashes = [], {}
    for f in context_files:
        text, h = read_input(f, "context")
        ctx_parts.append(f"=== {Path(f).name} ===\n{text}")
        ctx_hashes[str(f)] = h
    user = build_user(directive, objection, plea, "\n\n".join(ctx_parts))
    d = runs_dir()
    d.mkdir(parents=True, exist_ok=True)
    partial = d / f"partial-{now_iso().replace(':', '').replace('-', '')}.jsonl"
    jurors = deliberate(JUROR_SYSTEM, user, models, runs, call=call, backoff=backoff, partial=partial)
    tally, majority = decide(jurors)
    case = {
        "schema": "agent-jury.case.v3", "timestamp": now_iso(),
        "inputs": {"directive": str(directive_file), "directive_sha256": d_sha,
                   "objection": str(objection_file), "objection_sha256": o_sha,
                   "plea": str(plea_file) if plea_file else None, "plea_sha256": p_sha,
                   "context_sha256": ctx_hashes},
        "system_prompt": JUROR_SYSTEM, "user_prompt": user,
        "user_prompt_sha256": sha_bytes(user.encode()),
        "temperature": TEMPERATURE, "seed": SEED, "runs_per_juror": runs,
        "models": models, "vendors": vendors,
        "rule": ("a juror counts only if all runs parsed and agreed; a label wins only with "
                 "more than half of seated jurors, otherwise hung (fail-closed)"),
        "jurors": jurors, "tally": tally, "majority": majority,
        "advisory": True, "conflict_of_interest": COI,
        "note": "Advisory. The principal decides; outcomes go to outcomes.jsonl via resolve.py.",
    }
    stem = f"jury-{case['timestamp'].replace(':', '').replace('-', '')}"
    f, i = d / f"{stem}.json", 1
    while f.exists():
        f, i = d / f"{stem}-{i}.json", i + 1
    f.write_text(json.dumps(case, indent=2, ensure_ascii=False))
    partial.unlink(missing_ok=True)  # complete case written; partial no longer needed
    case["_file"] = str(f)
    return case


def main() -> int:
    ap = argparse.ArgumentParser(description="advisory agent jury (v3)")
    ap.add_argument("--directive-file", required=True)
    ap.add_argument("--objection-file", required=True)
    ap.add_argument("--plea-file")
    ap.add_argument("--context", action="append", default=[], help="governing document file (repeatable)")
    ap.add_argument("--runs", type=int, default=RUNS)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.dry_run:
        vendors = check_panel(JUROR_MODELS)
        for f, label in [(a.directive_file, "directive"), (a.objection_file, "objection"),
                         (a.plea_file, "plea"), *[(c, "context") for c in a.context]]:
            read_input(f, label)
        if not a.context:
            raise SystemExit("error: at least one --context file is required")
        print(f"[dry-run] models={JUROR_MODELS} vendors={vendors} runs={a.runs} context={a.context}")
        return 0
    case = convene(a.directive_file, a.objection_file, a.context, a.plea_file, runs=a.runs)
    print(f"JURY: {case['majority'].upper()} {case['tally']} vendors={case['vendors']}")
    for j in case["jurors"]:
        last = next((t["parsed"] for t in reversed(j["transcripts"]) if t["parsed"]), None)
        served = sorted({str(t["served_model"]) for t in j["transcripts"]})
        print(f"  [{j['model']}] {j['vote']} served={served}: "
              f"{(last or {}).get('reasoning', '(no parseable run)')[:160]}")
    print(f"record -> {case['_file']}\n{COI}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
