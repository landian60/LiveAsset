#!/usr/bin/env python3
"""Offline interface example with scripted model judgments, not a model evaluation."""
import asyncio
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

from liveasset import situated_intents as si


class ScriptedModel:
    def __init__(self, responses):
        self.responses = iter(responses)

    async def chat(self, messages):
        return SimpleNamespace(content=json.dumps(next(self.responses)))


async def main():
    asset = si.validate_asset(json.loads(Path(__file__).with_name("report-asset.json").read_text()))
    request = "Write this week's review summary. Two reviewers agreed on 18 of 20 judgments."
    drafts = ["The grader achieved 90% accuracy.",
              "The reviewers agreed on 18 of 20 judgments (90%). This is agreement; accuracy was not measured."]
    selection = {"selected": [{"situation_id": "weekly-report", "reason": "A weekly report of review results."}]}
    wrong = {"nodes": [
        {"intent_id": "report-counts", "items": [{"id": "r1", "status": "unmet", "reason": "Include the 18/20 counts."}]},
        {"intent_id": "metric-meaning", "items": [{"id": "r1", "status": "unmet", "reason": "The evidence measures agreement, not accuracy."}]}]}
    right = {"nodes": [
        {"intent_id": "report-counts", "items": [{"id": "r1", "status": "met", "reason": "The result reports 18 of 20."}]},
        {"intent_id": "metric-meaning", "items": [{"id": "r1", "status": "met", "reason": "The result distinguishes agreement from accuracy."}]}]}
    model = ScriptedModel([selection, wrong, right])
    with tempfile.TemporaryDirectory(prefix="liveasset-example-") as temporary:
        logs = Path(temporary)
        matched = await si.match(model, asset, request, task_context={}, log_dir=logs / "match", retry_empty=False)
        if matched["status"] == "failed":
            raise RuntimeError("Matching failed; this result cannot be treated as an empty selection")
        progress = si.IntentProgress(asset, matched["selected_ids"])
        print("OFFLINE EXAMPLE: model judgments are scripted. No personal store is read or written.\n")
        print(matched["actor_context"])
        results = []
        for index, output in enumerate(drafts, 1):
            result = await progress.check(model, output=output,
                visible_trace=[{"role": "user", "content": request}, {"role": "assistant", "content": output}],
                artifacts=None, log_path=logs / f"check-{index}.json")
            results.append(result)
            print(json.dumps({"draft": output, "complete": result["complete"], "revise": result["revise"],
                              "feedback": result.get("continuation_message")}, indent=2))
        assert results[0]["revise"] and not results[0]["complete"]
        assert results[1]["complete"] and not results[1]["revise"]


if __name__ == "__main__":
    asyncio.run(main())
