# Runnable examples

Four scripts cover the three stages of the method and the five benchmark cases.

Everything here runs offline. Model replies are scripted inside each script, so no login, API key or network is needed.

## Run them

From the repository root, with Python 3.11+:

```bash
PYTHONPATH=src python3 examples/learn_from_correction.py
PYTHONPATH=src python3 examples/switch_situations.py
PYTHONPATH=src python3 examples/quickstart.py
PYTHONPATH=src python3 examples/run_benchmark_cases.py
```

When the package is installed (`python3 -m pip install .`), `PYTHONPATH=src` is not needed.

| Script | Stage | Output |
| --- | --- | --- |
| [`learn_from_correction.py`](learn_from_correction.py) | **Learn → Select → Verify** | A user correction that only says what is wrong (`"the names are inconsistent"`) yields an intent asset. The script prints the learned situation, requirement, and practice, then shows that requirement blocking a bad draft on a *later, different* task and passing the revision. This is the full loop in one file. |
| [`switch_situations.py`](switch_situations.py) | **Select** | One asset with two situations, three requests. The weekly-report request injects the reporting intents, the meeting request injects the decision intents, and an unrelated translation request injects nothing. Prints the selected IDs and the exact text the actor would receive. |
| [`quickstart.py`](quickstart.py) | **Verify** | The smallest possible check: an incorrect weekly-report draft is sent back for a complete revision, and the corrected draft passes. Uses the hand-authored [`report-asset.json`](report-asset.json). |
| [`run_benchmark_cases.py`](run_benchmark_cases.py) | **Select across tasks** | Five cases from the benchmarks used in the paper, each with the original interaction, the learned asset and six follow-up requests. See [benchmark-cases/](benchmark-cases/README.md). |

`workday-asset.json` is the hand-authored two-situation asset used by `switch_situations.py`. Asset files can be checked against [`asset.schema.json`](../schema/asset.schema.json).

## Next steps

- Change a scripted reply to `unmet` / `met` and watch the delivery decision flip.
- Add a third situation to `workday-asset.json` and a matching scripted reply to `switch_situations.py`.
- Replace `ScriptedModel` with an object exposing `async chat(messages)` returning `.content`, and point the same calls at a real model. See [architecture.md](../docs/architecture.md) for the core API.
- For longer, multi-turn sequences with tool events and task boundaries, replay the [routing cases](../integrations/codex-liveasset/cases/README.md) through the plugin entry points.

When adding an example, keep it offline and register the new file in `release/files.json` so it ships with the package. See [CONTRIBUTING.md](../CONTRIBUTING.md).
