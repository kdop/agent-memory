"""Verdict quality with the real model: the prompt measured, not felt.

The review prompt's checklist was tuned by hand to one model. This file runs
the 30 entries in `tests/data/verdict_set.json` through `OllamaReviewer` with
the neighbours and the offered tags from the file, grades each answer against
the expected verdict, rule, `duplicate_of` and `supersedes`, and prints one
table: case, passed of total, and each miss with what the model said. The
overall pass rate must stay at or above the `floor` stored in the file. Raise
the floor when the prompt improves; a change to the wording or the model that
lowers the rate fails here.

The real model runs under the `review` marker and only when
AGENT_MEMORY_REVIEW_URL names a server that answers in 2 seconds. The shape of
the data file is checked without a model, in every run.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from agent_memory.server import review as review_mod
from agent_memory.server.review import RULES, VERDICTS, OllamaReviewer
from test_review import _real_server

VERDICT_SET_PATH = Path(__file__).resolve().parent / "data" / "verdict_set.json"

CASES = (
    "clean-decision", "clean-lesson", "clean-preference", "diary", "git-fact", "no-why",
    "exact-repeat", "reworded-repeat", "reversal", "partial-repeat-more",
)
ENTRY_COUNT = 30
PER_CASE_MIN = 2


def load_verdict_set(path=VERDICT_SET_PATH):
    """Read the verdict set and check it is well formed.

    Returns `(entries, tags, floor)`. Each entry has `case` (one of `CASES`),
    `memory`, `neighbours` (each with an id and `review_status` verified) and
    `expected` with `verdict`, `rule`, `duplicate_of` and `supersedes`. A bad
    entry is a data error, so it raises rather than letting the test score
    against nothing."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    entries, tags, floor = data["entries"], data["tags"], data["floor"]
    rule_numbers = {n for n, _ in RULES}
    for n, e in enumerate(entries):
        if e["case"] not in CASES:
            raise ValueError(f"entry {n} has an unknown case {e['case']!r}")
        for key in ("content", "project", "type", "tags"):
            if key not in e["memory"]:
                raise ValueError(f"entry {n} memory lacks {key!r}")
        ids = [nb["id"] for nb in e["neighbours"]]
        if len(set(ids)) != len(ids):
            raise ValueError(f"entry {n} lists a neighbour id twice")
        for nb in e["neighbours"]:
            if nb.get("review_status") != review_mod.VERIFIED:
                raise ValueError(f"entry {n} neighbour {nb.get('id')} is not verified")
        x = e["expected"]
        if x["verdict"] not in VERDICTS:
            raise ValueError(f"entry {n} expects an unknown verdict {x['verdict']!r}")
        if x["rule"] is not None and x["rule"] not in rule_numbers:
            raise ValueError(f"entry {n} expects an unknown rule {x['rule']!r}")
        for key in ("duplicate_of", "supersedes"):
            if not isinstance(x[key], bool):
                raise ValueError(f"entry {n} expected.{key} must be true or false")
            if x[key] and not ids:
                raise ValueError(f"entry {n} expects {key} set but lists no neighbours")
    if not 0.0 <= floor <= 1.0:
        raise ValueError(f"floor must be between 0 and 1 (got {floor!r})")
    return entries, list(tags), float(floor)


def grade(expected: dict, verdict) -> list[str]:
    """What is wrong with the model's answer, as short lines; empty when it
    matches. The verdict must match; the rule must match when the file gives
    a number; `duplicate_of` and `supersedes` must be set when the file says
    true and unset when it says false."""
    if verdict is None:
        return ["no usable answer from the model"]
    wrong = []
    if verdict.verdict != expected["verdict"]:
        wrong.append(f"verdict {verdict.verdict}, expected {expected['verdict']}")
    if expected["rule"] is not None and verdict.rule != expected["rule"]:
        wrong.append(f"rule {verdict.rule}, expected {expected['rule']}")
    for key in ("duplicate_of", "supersedes"):
        got = getattr(verdict, key)
        if expected[key] and got is None:
            wrong.append(f"{key} unset, expected an id")
        elif not expected[key] and got is not None:
            wrong.append(f"{key} {got}, expected unset")
    return wrong


def test_verdict_set_is_well_formed():
    entries, tags, floor = load_verdict_set()
    assert len(entries) == ENTRY_COUNT
    assert tags and all(isinstance(t, str) and t for t in tags)
    counts = {case: sum(1 for e in entries if e["case"] == case) for case in CASES}
    assert all(n >= PER_CASE_MIN for n in counts.values()), counts


@pytest.mark.review
def test_verdict_quality(record_property):
    url = _real_server()
    if url is None:
        pytest.skip("AGENT_MEMORY_REVIEW_URL is unset or the server did not answer in 2 s")
    model = os.environ.get("AGENT_MEMORY_REVIEW_MODEL", "").strip() or review_mod.DEFAULT_MODEL
    # A long timeout: the first call may have to load the model into memory.
    reviewer = OllamaReviewer(url, model, timeout=180)
    entries, tags, floor = load_verdict_set()

    results = []  # (case, index, wrong lines, verdict, seconds)
    t0 = time.perf_counter()
    for n, e in enumerate(entries):
        t1 = time.perf_counter()
        verdict = reviewer.review(dict(e["memory"]), [dict(nb) for nb in e["neighbours"]], tags)
        results.append((e["case"], n, grade(e["expected"], verdict), verdict,
                        time.perf_counter() - t1))
    total_s = time.perf_counter() - t0

    passed = sum(1 for _, _, wrong, _, _ in results if not wrong)
    rate = passed / len(results)

    print(f"\n[review] {model} at {url}: {len(results)} entries in {total_s:.0f}s")
    print(f"{'case':<22} {'passed':>7}")
    for case in CASES:
        rows = [r for r in results if r[0] == case]
        ok = sum(1 for r in rows if not r[2])
        print(f"{case:<22} {ok:>3}/{len(rows)}")
        record_property(f"verdict {case}", f"{ok}/{len(rows)}")
    print(f"{'overall':<22} {passed:>3}/{len(results)}  rate {rate:.2f}  floor {floor:.2f}")
    record_property("verdict overall", f"{passed}/{len(results)} = {rate:.2f} (floor {floor:.2f})")
    for case, n, wrong, verdict, seconds in results:
        if wrong:
            said = verdict.as_dict() if verdict is not None else None
            print(f"  {case} #{n} failed ({seconds:.1f}s): {'; '.join(wrong)}\n"
                  f"    model said: {json.dumps(said)}")
            record_property(f"verdict {case} #{n}", "; ".join(wrong))

    assert rate >= floor, (
        f"verdict pass rate {rate:.2f} ({passed}/{len(results)}) is below the floor "
        f"{floor:.2f} in {VERDICT_SET_PATH.name}")
