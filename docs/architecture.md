# Core mechanism and host integration

## What an asset contains

An asset has a `schema_version` format tag, an `asset_id`, an `owner_id`, a `revision`, and a list of `situations`. A situation is a recurring kind of user work; it may contain several intents.

| Stored field | Meaning and current use |
| --- | --- |
| `situation` | Description of the request and purpose of a group. Read by the matcher. |
| `queries` | Requests associated with that situation during learning. |
| `intents[].id` | Stable identifier for an intent. There is no separate intent-name field. |
| `intents[].intent_explanation` | When and why the user wants the requirements. Read by matching, injection, and checking. |
| `intents[].requirements` | Individually checkable expectations. Conditions belong in their text. |
| `intents[].facts` | Historical information with `fact`, `quote`, and `episode`; earlier assets may also carry `scope`. |
| `practices` | Retained situation-level field. Earlier v2 assets missing it are normalized to an empty list in memory. |

[report-asset.json](../examples/report-asset.json) contains one situation and two intents. It is manually authored test data, not an extraction from a user account.

## Core API

The core is [situated_intents.py](../src/liveasset/situated_intents.py) plus [prompts.py](../src/liveasset/prompts.py), which holds every model instruction string verbatim and re-exports the prompt builders. It uses the Python standard library. A model adapter must expose `async chat(messages)` and return an object with a string `.content` containing the requested JSON. The caller supplies model credentials, timeouts, and transport retries.

| Function or class | Purpose |
| --- | --- |
| `new_situated_asset(asset_id, owner_id)` | Create an empty asset. |
| `validate_asset(asset)` | Validate structure and return a normalized copy. |
| `extract(model, asset, dialogue, episode=..., log_dir=...)` | Process corrections incrementally and return a candidate asset plus its receipt. |
| `match(model, asset, request, task_context=..., log_dir=...)` | Choose situations and expand their intents into `selected_ids` and `actor_context`. |
| `IntentProgress(asset, selected_ids)` | Track checks for the supplied selection. |
| `IntentProgress.check(...)` | Check visible output, supplied text artifacts and optional saved task context. |

The [offline example](../examples/quickstart.py) shows the actual call signatures. `match` returns a status as well as the selection: callers must handle `status="failed"` rather than interpret it as “nothing applies.” The local service raises an error for that case. A valid empty selection is allowed. `retry_empty=False` avoids asking again after a valid empty selection, as used by the local adapter.

Each checker item is a requirement (`r1`, `r2`, ...) or fact (`f1`, `f2`, ...). The model returns `met`, `unmet`, or `not_applicable` for each. Missing judgments are retried once and may remain unknown. The returned `complete`, `revise`, and `continuation_message` fields let the host decide what to do next. A completed check concerns the selected asset and supplied evidence, not every possible aspect of task success.

## When the local adapter matches

Matching must follow changes in the user's work. [Service.match](../integrations/codex-liveasset/scripts/liveasset.py) stores the current task and selection so a short continuation can reuse them, while a substantive new message or new tool evidence can trigger matching again.

| Host event | Implemented behavior |
| --- | --- |
| User submits a message | The Codex `UserPromptSubmit` hook matches the message and relevant saved context. A recognized short continuation reuses an unchanged binding. |
| A local tool returns new evidence | `PostToolUse` passes the observation and explicit current task ID back to matching. All situations are eligible for reconsideration. |
| Agent offers a candidate reply | `Stop` checks the current selection. It asks for revision while unmet items remain and the count of met items improves, within the core continuation limit. |
| User explicitly requests learning | `learn_intents` or the CLI learning command extracts a candidate asset. None of the three hooks triggers learning. |

MCP exposes the same service operations, but an MCP client must arrange their invocation. MCP alone does not observe all user turns or tool results. The current server's model backend is `CodexModel`; using a different host through MCP still requires Codex CLI. To use a different model transport, supply a model object to the core or `Service`.

## What the stored task identity does

`session_id` identifies a chat, `task_id` identifies the continuing work, and `scope` separates project contexts. Passing a known `task_id` explicitly resumes that work within its scope; omitting it lets the matcher decide whether the current chat has moved to a new goal. Similarity alone does not make two tasks identical.

The service saves the task's initial request, latest substantive request, supplied context, selection, and asset snapshot. It does not restore an execution directory, restart tools, or schedule an agent. Duplicate `event_id` values return the recorded result only if the input is unchanged. `replay=true` reads a saved snapshot without rolling back the live state. These records support consistent matching across calls, while the host retains responsibility for executing and recovering the task.

## Boundaries and failure handling

The core reports invalid model judgments in its receipts. The local service rejects unusable matching results and preserves the previous binding. A Codex hook failure records an error and lets the host continue with an explicit “not verified” message. A timeout or unavailable checker does not certify the answer.

Matching receives situation descriptions and intent explanations, not requirements or facts. After selection, injection includes the selected intents' historical facts. Applicability is then decided from the current object, instructions and visible evidence; `facts[].scope` is preserved but is not an implemented pre-injection filter. Older facts must not override the current request.

The local store includes model inputs, outputs, task contexts and check records. Model calls send that material to the configured model provider. The plugin does not bundle credentials or personal assets. Keep the store and credential files outside a public checkout.

Learning is gated by an explicit user request in the adapter. The flag records the caller's assertion of that request; it is not identity authentication. The core learner still makes assumptions about corrected or accepted outputs. Neither this package nor the routing fixtures establishes robustness to erroneous corrections or long-term preference drift.
