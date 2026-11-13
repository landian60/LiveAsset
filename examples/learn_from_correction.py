#!/usr/bin/env python3
"""Learn an intent from a correction that never states the desired result.

Offline demo: every model reply below is scripted. No login, no API key, no network,
no personal data. All names and notes here are synthetic.

Run from the repository root:
    PYTHONPATH=src python3 examples/learn_from_correction.py
"""
import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from liveasset import situated_intents as si

REQUEST = "Write the contributor list for the project note."
FIRST_DRAFT = "Contributors: ALICE CHEN, Bob Diaz, CAROL NG."
# The correction names what is wrong, not what the result should have been.
CORRECTION = "Not the wording - the names are inconsistent."
REVISED = "Contributors: Alice Chen, Bob Diaz, Carol Ng."

NEXT_REQUEST = "Write the contributor list for next month's note."
NEXT_BAD = "Contributors: DANA WU, Eli Park, FARAH KHAN."
NEXT_GOOD = "Contributors: Dana Wu, Eli Park, Farah Khan."

REQUIREMENT = "Write every contributor name in the same display form across the whole list."


class ScriptedModel:
    """Return the JSON a model would have produced, in call order."""

    def __init__(self, replies):
        self.replies = iter(replies)

    async def chat(self, messages):
        return SimpleNamespace(content=json.dumps(next(self.replies)))


def banner(text):
    print(f"\n{text}\n{'-' * len(text)}")


async def main():
    asset = si.new_situated_asset("demo-correction", "demo-user")
    trace = [
        {"role": "user", "content": REQUEST},
        {"role": "assistant", "content": FIRST_DRAFT},
        {"role": "user", "content": CORRECTION},
        {"role": "assistant", "content": REVISED},
    ]
    replies = [
        # 1. Incremental extraction: one correction step, compared against both results.
        {
            "situation_id": "contributor-list",
            "query": REQUEST,
            "intents": [{
                "id": "consistent-names",
                "intent_explanation": (
                    "When a contributor list is written for a project note, the user wants names "
                    "rendered in one display form so the list reads as a single edited list."),
                "requirements": [REQUIREMENT],
                "facts": [],
            }],
            "changes": [{"op": "add", "intent_id": "consistent-names",
                         "reason": "The accepted revision made the mixed-case names consistent."}],
        },
        # 2. A situation-level practice retained from the accepted end of the work.
        {"practices": ["Keep the display form of names consistent throughout a contributor list."]},
        # 3. A situation description, written once the corrections are done.
        {"situations": [{"situation_id": "contributor-list",
                         "situation": "When writing a contributor list for a project note, keep name presentation consistent.",
                         "merged": []}]},
        # 4. A later, different task: situation selection.
        {"selected": [{"situation_id": "contributor-list",
                       "reason": "Another contributor list for the same kind of note."}]},
        # 5-6. Delivery checks for the bad draft and its revision.
        {"nodes": [{"intent_id": "consistent-names",
                    "items": [{"id": "r1", "status": "unmet",
                               "reason": "Names mix uppercase and title case."}]}]},
        {"nodes": [{"intent_id": "consistent-names",
                    "items": [{"id": "r1", "status": "met",
                               "reason": "All three names use the same form."}]}]},
    ]
    model = ScriptedModel(replies)

    with tempfile.TemporaryDirectory(prefix="liveasset-learn-") as temporary:
        logs = Path(temporary)
        banner("STEP 1  A correction that names the problem, not the fix")
        print(f"  request    : {REQUEST}")
        print(f"  first draft: {FIRST_DRAFT}")
        print(f'  user says  : "{CORRECTION}"')
        print(f"  revised    : {REVISED}")

        learned, receipt = await si.extract(model, asset, trace,
                                            episode="demo-correction", log_dir=logs / "learn")
        banner("STEP 2  What was learned (from both results, not from the words alone)")
        print(f"  status: {receipt['status']} | model calls: {receipt['calls']} | "
              f"revision: {asset['revision']} -> {learned['revision']}")
        for situation in learned["situations"]:
            print(f"  situation [{situation['id']}]: {situation['situation']}")
            for intent in situation["intents"]:
                print(f"    intent  [{intent['id']}]: {intent['intent_explanation']}")
                for requirement in intent["requirements"]:
                    print(f"      - check: {requirement}")
            for practice in situation["practices"]:
                print(f"    practice: {practice}")

        banner("STEP 3  A later task - the learned requirement is now in force")
        matched = await si.match(model, learned, NEXT_REQUEST, task_context={},
                                 log_dir=logs / "match", retry_empty=False)
        print(f"  request : {NEXT_REQUEST}")
        print(f"  selected: {matched['selected_ids']}")
        progress = si.IntentProgress(learned, matched["selected_ids"])
        for label, draft in (("bad draft", NEXT_BAD), ("revised", NEXT_GOOD)):
            result = await progress.check(
                model, output=draft,
                visible_trace=[{"role": "user", "content": NEXT_REQUEST},
                               {"role": "assistant", "content": draft}],
                artifacts=None, log_path=logs / f"check-{label.replace(' ', '-')}.json")
            verdict = "BLOCKED -> revise" if result["revise"] else "DELIVERED"
            print(f"  {label:10s}: {draft}")
            print(f"  {'':10s}  {verdict} (complete={result['complete']})")
            if result["revise"]:
                print(f"  {'':10s}  reason: {result['continuation_message'].splitlines()[-1].strip()}")

    print("\nAll model replies were scripted; nothing here measures model quality.")
    print("Next: examples/switch_situations.py (selection) and examples/quickstart.py (verification).")


if __name__ == "__main__":
    asyncio.run(main())
