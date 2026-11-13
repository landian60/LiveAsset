#!/usr/bin/env python3
"""Run the five benchmark cases used in the paper's evaluation.

Each case in benchmark-cases/ keeps the original task interaction, the intent
asset learned from it, and six follow-up requests. Model replies are scripted
from the case's expected selections; replace ScriptedModel with your own model
adapter to test semantic selection.

Run from the repository root:
    PYTHONPATH=src python3 examples/run_benchmark_cases.py
"""
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from liveasset import situated_intents as si

CASES_DIR = Path(__file__).with_name("benchmark-cases")


class ScriptedModel:
    """Return the JSON a model would have produced, in call order."""

    def __init__(self, replies):
        self.replies = iter(replies)

    async def chat(self, messages):
        return SimpleNamespace(content=json.dumps(next(self.replies)))


def scripted_replies(asset, queries):
    """Translate expected intent IDs into the situation-level match reply."""
    replies = []
    for query in queries:
        wanted = query["expect_selected"]
        if wanted is None:  # this case leaves the selection deliberately open
            selected = [{"situation_id": s["id"], "reason": "open judgement"}
                        for s in asset["situations"]]
        else:
            wanted = set(wanted)
            selected = [{"situation_id": s["id"], "reason": "scripted selection"}
                        for s in asset["situations"]
                        if any(i["id"] in wanted for i in s["intents"])]
        replies.append({"selected": selected})
    return replies


async def run_case(path, logs):
    case = json.loads(path.read_text())
    asset = si.validate_asset(case["asset"])
    model = ScriptedModel(scripted_replies(asset, case["queries"]))

    print(f"\n{case['benchmark']}  |  {case['id']}")
    print("=" * 68)
    print(f"  {case['focus']}")
    print(f"  asset: {len(asset['situations'])} situation(s), "
          f"{sum(len(s['intents']) for s in asset['situations'])} intent(s), "
          f"{len(case['history'])} interaction rows")

    ok = True
    for index, query in enumerate(case["queries"], 1):
        matched = await si.match(model, asset, query["request"], task_context={},
                                 log_dir=logs / f"{case['id']}-{index}", retry_empty=False)
        got = matched["selected_ids"]
        print(f"\n  {index}. [{query['lane']}] {query['request']}")
        if query["expect_selected"] is None:
            print(f"     OPEN | selected: {got or '(none)'}")
        else:
            passed = got == query["expect_selected"]
            ok &= passed
            print(f"     {'PASS' if passed else 'FAIL'} | expected: {query['expect_selected'] or '(none)'}"
                  f" | selected: {got or '(none)'}")
        print(f"     {query['note']}")
        if matched["status"] == "failed":
            print("     match did not return a usable selection")
            ok = False
    return ok


async def main():
    paths = sorted(CASES_DIR.glob("*.json"))
    if not paths:
        raise SystemExit(f"no cases found in {CASES_DIR}")
    print("FIVE BENCHMARK CASES")
    print("Offline, scripted model replies, no credentials needed.")
    with tempfile.TemporaryDirectory(prefix="liveasset-bench-") as temporary:
        logs = Path(temporary)
        results = [await run_case(path, logs) for path in paths]
    cases = [json.loads(p.read_text()) for p in paths]
    total = sum(len(c["queries"]) for c in cases)
    opened = sum(1 for c in cases for q in c["queries"] if q["expect_selected"] is None)
    print("\n" + "=" * 68)
    print(f"{len(paths)} cases, {total} requests ({opened} open judgements), "
          f"{sum(results)}/{len(results)} cases passed")
    print("Replace ScriptedModel with your own model to test semantic selection.")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
