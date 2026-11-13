# Multi-turn routing examples

These fixtures exercise task selection and continuation across messages and chats. `assets.json` contains hand-authored assets. `daily-routing.json` defines event sequences and expected results. `supervision.json` adds natural user and tool events through the hook entry point. They are engineering examples, not paper benchmark scores or newly learned personal assets.

The evaluation, repository-layout, and weekly-report cases are anonymized composites inspired by earlier interactions. Names, models, projects, counts, paths and chat IDs were replaced, and some event orders were combined. Handoff material and boundary cases are synthetic. The `codex://threads/...` references use fictional IDs. No source transcript is distributed.

| Case | Behavior to inspect |
| --- | --- |
| `evaluation-continuation` | Progress request, explicit continuation in another chat, then a report task. |
| `layout-correction` | Keep the user's correction when the task resumes. |
| `reference-and-noise` | Use an old report only as a reference; keep quoted mistakes out of the asset. |
| `context-change` | Reconsider situations when material reveals another subtask. |
| `asset-revision-and-replay` | Read an earlier snapshot without rolling back current state; handle repeated events. |
| `ambiguous-chat-reference` | Avoid guessing which task in a multi-task chat is being resumed. |

`supervision.json` has three additional sequences: a weekly report with shorter wording and a new reporting period; a vague request that becomes concrete after reading handoff material; and a layout correction followed by an unrelated translation task.

## Run an offline replay

From the plugin directory, after setting up its Python environment:

```bash
.venv/bin/python scripts/replay_cases.py --mode scripted --out /absolute/path/to/new-routing-run
```

Scripted mode reads the expected labels to generate fixture responses. It checks state transitions and interface behavior; it does not test semantic recognition.

## Run a model replay

Configure a model login or API route as described in the [plugin README](../README.md), then:

```bash
.venv/bin/python scripts/replay_cases.py --mode model --out /absolute/path/to/new-model-run --model your-available-model --effort low
.venv/bin/python scripts/replay_hooks.py --out /absolute/path/to/new-hook-run --cases handoff-context
```

Hook replay uses `LIVEASSET_MODEL` and `LIVEASSET_EFFORT`. In model mode, expected labels are read only by the comparison code, not included in model input. Hook replay passes fixed tool-result fixtures through the actual hook entry point; it does not execute the tools in those fixtures. A live host integration test is separate.

Use a new output directory after changing code or inputs. Existing event receipts allow an interrupted run with the same source and arguments to resume. Runs save a source snapshot, inputs, outputs and failures. Transport failures can retry; semantic mismatches remain recorded rather than being repeatedly sampled until they pass.

Each event may set the normal matching arguments. `task_from` refers to an earlier event's returned task ID. `expect` defines program-only assertions. The test-only `replace_asset` operation updates an isolated fixture store. Matching checks that the asset bytes remain unchanged.

Correction-noise cases currently check that routing does not learn or write an asset. They do not establish that the learner would extract a correct practice from a flawed correction. Add learning cases separately with explicitly authorized synthetic data.
