#!/usr/bin/env python3
"""One asset, three requests: situation selection is what scopes the intents.

Offline demo: every model reply below is scripted. No login, no API key, no network,
no personal data. The asset in workday-asset.json is synthetic and hand-authored.

Run from the repository root:
    PYTHONPATH=src python3 examples/switch_situations.py
"""
import asyncio
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from liveasset import situated_intents as si

REQUESTS = [
    ("weekly review report", "Write this week's review summary. Two reviewers agreed on 18 of 20 judgments."),
    ("planning meeting notes", "Summarize the planning meeting and list the decisions we made."),
    ("unrelated work", "Translate this short paragraph into French."),
]


class ScriptedModel:
    """Return the JSON a model would have produced, in call order."""

    def __init__(self, replies):
        self.replies = iter(replies)

    async def chat(self, messages):
        return SimpleNamespace(content=json.dumps(next(self.replies)))


async def main():
    asset = si.validate_asset(
        json.loads(Path(__file__).with_name("workday-asset.json").read_text()))
    print("ONE ASSET, TWO SITUATIONS")
    print("=" * 60)
    for situation in asset["situations"]:
        print(f"  [{situation['id']}] {situation['situation']}")
        for intent in situation["intents"]:
            print(f"      intent: {intent['id']}")

    replies = [
        {"selected": [{"situation_id": "weekly-report",
                       "reason": "A weekly report about review agreement."}]},
        {"selected": [{"situation_id": "meeting-notes",
                       "reason": "A meeting write-up with decisions to record."}]},
        {"selected": []},  # no situation applies, so nothing is injected
    ]
    model = ScriptedModel(replies)

    with tempfile.TemporaryDirectory(prefix="liveasset-select-") as temporary:
        logs = Path(temporary)
        for index, (label, request) in enumerate(REQUESTS, 1):
            matched = await si.match(model, asset, request, task_context={},
                                     log_dir=logs / f"match-{index}", retry_empty=False)
            print(f"\n{index}. {label}")
            print(f"   request : {request}")
            print(f"   status  : {matched['status']}")
            print(f"   selected: {matched['selected_ids'] or '(none)'}")
            context = str(matched["actor_context"] or "")
            # The first lines are the same general scope note on every task; fold them for readability.
            general = ("- A line in parentheses", "- Adapt the practices", "- 'Known' lines", "- A Known line")
            shown = [line for line in context.splitlines()
                     if line.strip() and not line.startswith(general)]
            if shown:
                print(f"   injected| (general scope note omitted; full text is in actor_context)")
                for line in shown:
                    print(f"   injected| {line}")
            else:
                print("   injected| (nothing - no situation applies, so no intent is imposed)")

    print("\nAll model replies were scripted. Only the situation description and intent")
    print("explanations are read during selection; requirements and facts are shown after it.")


if __name__ == "__main__":
    asyncio.run(main())
