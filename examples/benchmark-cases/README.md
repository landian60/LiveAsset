# Five benchmark cases

Five learning cases from the benchmarks used in the paper. Each file keeps the task interaction, the intent asset learned from it, and the six follow-up requests written to inspect reuse. Requests and notes keep the original languages of the tasks.

| File | Benchmark | Task | What to look at |
| --- | --- | --- | --- |
| [`pi-rollout-review.json`](pi-rollout-review.json) | π-Bench | `Financier_task_003` | Four deliveries add comparison dimensions and a Meta Review module; the last correction asks for one consolidated meeting-ready report. |
| [`perma-wording.json`](perma-wording.json) | PERMA | `SD-Messaging-task-3` | Three deliveries narrow one wording preference: formal, courteous and brief, then routine rather than consequential. |
| [`vita-order.json`](vita-order.json) | Vita | `sub_E941775_5` | Three deliveries separate a lasting flavour dislike from the sweetness and destination of one order. |
| [`past-freeze-date.json`](past-freeze-date.json) | PAST-Bench | `SM03_fact_correction` | A correction retires an obsolete freeze date and confirms the official one; the value is scoped to one project. |
| [`skillevol-evidence.json`](skillevol-evidence.json) | SkillEvolBench | `E5-LS2` T1-T3 | A runnable generator must reproduce the saved artifact, and sources must directly support each comparison dimension. |

## Run them

From the repository root:

```bash
PYTHONPATH=src python3 examples/run_benchmark_cases.py
```

The script validates each asset, runs the requests through `match()`, and prints the expected and selected intents. Model replies are scripted from `expect_selected`, so a PASS confirms the asset and the matching path are wired correctly.

Two PAST-Bench requests have `expect_selected: null`: the case leaves the selection open for manual inspection of whether a task-scoped date is reused across projects. The script prints them as `OPEN`.

## Case file structure

| Field | Meaning |
| --- | --- |
| `benchmark`, `id`, `focus` | Source setting and what the case is about. |
| `asset` | The intent asset learned from the task, validated by `validate_asset()`. |
| `history` | User and assistant messages of the corrective interaction. |
| `queries[].lane` | `new_task` starts a fresh task; `session` continues within one chat. |
| `queries[].request` | A natural request, a continuation, or an unrelated request. |
| `queries[].expect_selected` | Intent IDs expected to be in force; `[]` means none; `null` means the selection is open. |
| `queries[].note` | The observation that request was written to check. |

## Test a real model

Replace `ScriptedModel` in `run_benchmark_cases.py` with any object exposing `async chat(messages)` that returns an object with a JSON string in `.content`, and build the match reply yourself:

```python
{"selected": [{"situation_id": "huadong-pd-gray-release-model-review", "reason": "..."}]}
```

Keep the `si.match(model, asset, request, task_context={}, log_dir=..., retry_empty=False)` calls unchanged. See [architecture.md](../../docs/architecture.md) for the core API and [examples/README.md](../README.md) for the smaller learn / select / verify scripts.
