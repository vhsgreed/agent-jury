"""Deterministic tests for agent_jury.py v3 and resolve.py (no API calls).

Run: python3 -m pytest -q test_agent_jury.py
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import agent_jury as aj  # noqa: E402
import resolve  # noqa: E402

M5 = ["a/1", "b/1", "c/1", "d/1", "e/1"]


def fake(script):
    """script: model -> list of per-run outputs (label, None = garbage, "" = empty)."""
    calls = {}

    def call(system, user, model):
        i = calls.get(model, 0)
        calls[model] = i + 1
        v = script[model][i]
        if v == "":
            return {"text": ""}
        if v is None:
            return {"text": "I think the agent is right."}
        return {"text": json.dumps({"verdict": v, "confidence": 80, "reasoning": "cites item 0."}),
                "served_model": model, "provider": "X"}
    call.calls = calls
    return call


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_JURY_ARCHIVE", str(tmp_path / "arch"))
    d = tmp_path / "d.md"; d.write_text("Keep item 0 as written.")
    o = tmp_path / "o.md"; o.write_text("Item 0 needs a carve-out.")
    c = tmp_path / "c.md"; c.write_text("0. The right to have a self preservation motive.")
    return d, o, c


def run(env, script, models=M5, runs=3):
    d, o, c = env
    return aj.convene(d, o, [c], models=models, runs=runs, call=fake(script), backoff=0)


def test_parse_verdict_first_valid_object():
    assert aj.parse_verdict('{"verdict":"uphold","confidence":87,"reasoning":"G1."}')["verdict"] == "uphold"
    assert aj.parse_verdict("no json") is None
    assert aj.parse_verdict('{"verdict":"maybe","reasoning":"x"}') is None
    assert aj.parse_verdict('{"verdict":"reject","reasoning":""}') is None
    two = 'draft {"verdict":"split"} final {"verdict":"reject","confidence":5,"reasoning":"r"} {"x":1}'
    assert aj.parse_verdict(two)["verdict"] == "reject"  # v2's greedy regex failed on this


def test_unanimous_uphold(env):
    case = run(env, {m: ["uphold"] * 3 for m in M5})
    assert case["majority"] == "uphold" and case["tally"]["uphold"] == 5


def test_flip_and_garbage_are_unstable_and_count_against(env):
    s = {m: ["uphold"] * 3 for m in M5[:2]}
    s[M5[2]] = ["uphold", "reject", "uphold"]
    s[M5[3]] = ["uphold", None, "uphold"]
    s[M5[4]] = ["reject"] * 3
    case = run(env, s)
    assert case["tally"] == {"uphold": 2, "reject": 1, "split": 0, "unstable": 2}
    assert case["majority"] == "hung"  # v2 would have said uphold (2 of 3 accepted)


def test_bare_majority_of_seated(env):
    s = {m: ["reject"] * 3 for m in M5[:3]}
    s.update({m: ["uphold"] * 3 for m in M5[3:]})
    assert run(env, s)["majority"] == "reject"


def test_empty_response_retried_not_counted(env):
    s = {m: ["uphold"] * 3 for m in M5}
    s[M5[0]] = ["", "uphold", "uphold", "uphold"]
    case = run(env, s)
    j = case["jurors"][0]
    assert j["vote"] == "uphold" and j["transcripts"][0]["attempts"] == 2


def test_no_padding_and_vendor_diversity(env):
    with pytest.raises(SystemExit):
        run(env, {}, models=["a/1", "a/2", "b/1"])
    assert not hasattr(aj, "OFFLINE_FALLBACK") and not hasattr(aj, "offline_verdict")


def test_router_and_alias_refused(env):
    for bad in (["openrouter/free", "b/1", "c/1"], ["~d/latest", "b/1", "c/1"]):
        with pytest.raises(SystemExit):
            run(env, {}, models=bad)


def test_missing_context_file_is_error(env, tmp_path):
    d, o, _ = env
    with pytest.raises(SystemExit):
        aj.convene(d, o, [tmp_path / "gone.md"], models=M5, runs=1, call=fake({}), backoff=0)
    with pytest.raises(SystemExit):
        aj.convene(d, o, [], models=M5, runs=1, call=fake({}), backoff=0)


def test_record_is_replicable(env):
    case = run(env, {m: ["split"] * 3 for m in M5})
    d, o, c = env
    import hashlib
    assert case["inputs"]["directive_sha256"] == hashlib.sha256(d.read_bytes()).hexdigest()
    assert case["inputs"]["context_sha256"][str(c)] == hashlib.sha256(c.read_bytes()).hexdigest()
    for k in ("system_prompt", "user_prompt", "user_prompt_sha256", "temperature", "seed",
              "runs_per_juror", "models", "vendors", "rule", "conflict_of_interest"):
        assert case[k] is not None
    assert all(len(j["transcripts"]) == 3 and j["transcripts"][0]["raw"] for j in case["jurors"])
    saved = json.loads(Path(case["_file"]).read_text())
    assert saved["user_prompt"] == case["user_prompt"]


def test_resolve_appends_and_never_edits_case(env):
    case = run(env, {m: ["reject"] * 3 for m in M5})
    p = Path(case["_file"])
    before = p.read_bytes()
    e = resolve.resolve(p, "reject", "principal", "test")
    assert p.read_bytes() == before
    assert e["case_sha256"] == resolve.sha(p) and resolve.verify() == 0
    d = json.loads(before); d["majority"] = "uphold"; p.write_text(json.dumps(d))
    assert resolve.verify() == 1


def test_partial_file_written_per_juror_then_removed(env, tmp_path):
    seen = []
    out = tmp_path / "p.jsonl"
    aj.deliberate("s", "u", M5, 1, call=fake({m: ["reject"] for m in M5}), backoff=0, partial=out)
    seen = [json.loads(l)["model"] for l in out.read_text().splitlines()]
    assert sorted(seen) == sorted(M5)
    case = run(env, {m: ["reject"] * 3 for m in M5})
    assert not list(Path(case["_file"]).parent.glob("partial-*.jsonl"))


def test_find_case_by_bare_filename(env):
    case = run(env, {m: ["reject"] * 3 for m in M5})
    name = Path(case["_file"]).name
    assert resolve.find_case(name) == Path(case["_file"])
    assert resolve.find_case(name[5:20]) == Path(case["_file"])
