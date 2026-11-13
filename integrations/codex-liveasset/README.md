# LiveAsset for Codex and MCP

This adapter connects the LiveAsset core to an agent's user messages, tool observations, and candidate replies. Codex hooks provide automatic invocation. Other harnesses can call the same operations over MCP.

The core implementation is copied into `vendor/liveasset/` for standalone installation. In the source repository, edit `src/liveasset/` and run `python tools/sync_liveasset_vendor.py`; do not edit the generated copy. The asset schema and model prompts are shared with the core.

## Local setup

Use Python 3.11+ and a POSIX environment such as macOS or Linux. The supplied model backend also needs Codex CLI and either a usable Codex login or a configured Responses-compatible API endpoint. Native Windows support is untested.

From this plugin directory:

```bash
bash scripts/setup.sh /path/to/python3.12
export LIVEASSET_MODEL=your-available-model
export LIVEASSET_EFFORT=low
codex plugin marketplace add /absolute/path/to/codex-liveasset
codex plugin add liveasset@liveasset-local
```

The local marketplace metadata is included. Review and trust the hook definitions in Codex `/hooks`, then test in a new chat. Installation alone does not establish hook trust. For current marketplace and hook behavior, see [OpenAI's plugin packaging documentation](https://developers.openai.com/plugins/build/plugins).

If the installed package's hooks do not load, the explicit registrar provides a fallback:

```bash
.venv/bin/python scripts/install_hooks.py --model your-available-model --effort low
```

Use the fallback only after checking that the package hooks are absent, to avoid two registrations. It backs up `hooks.json`, preserves other hooks, and registers `UserPromptSubmit`, `PostToolUse`, and `Stop`. Repeating registration from the same path replaces that installation's entries. `--config-home` selects an isolated configuration directory for testing. `--store` fixes the store location, and `--codex` fixes the CLI executable. Review and trust the resulting definitions in `/hooks`.

API routing is optional. Set `LIVEASSET_API_ENV` to a local file containing `OPENAI_API_KEY` and `OPENAI_BASE_URL`; the endpoint must use HTTPS, except for a loopback HTTP proxy. The registrar accepts `--api-env /path/to/api.env` and stores only that path. Keep the file outside source control. Available models depend on the configured login or endpoint.

| Environment variable | Purpose |
| --- | --- |
| `LIVEASSET_MODEL`, `LIVEASSET_EFFORT` | Model and reasoning effort; current defaults are `gpt-6.1-sol` and `high`. |
| `LIVEASSET_API_ENV` | Optional local API configuration file. |
| `LIVEASSET_AUTH` | Optional Codex login file; otherwise `~/.codex/auth.json`. |
| `LIVEASSET_CODEX` | Codex executable; otherwise `codex` on PATH. |
| `LIVEASSET_PYTHON` | Python used by `scripts/run.sh`. |
| `LIVEASSET_STORE` | Explicit asset and execution-state directory. |

Without an explicit store, the adapter uses `${PLUGIN_DATA}/store`, falling back to `~/.local/share/liveasset/store`. Model inputs, outputs, and check records are saved there. Model calls run in temporary homes with the required login or API configuration and with plugins, hooks, memory, and tools disabled by the adapter.

This package supports manual local installation. Publishing a source repository is separate from admission to a plugin directory; consult the current [plugin submission requirements](https://developers.openai.com/plugins/deploy/submission) before submitting a hook-dependent package.

## What happens during a task

`UserPromptSubmit` matches each substantive user message against the stored situations and their intent explanations. A recognized short continuation can reuse an unchanged task selection. When it matches again, the model also determines whether the goal continues or a new task begins.

`PostToolUse` supplies new visible tool evidence to matching while keeping the explicit current task ID. All situations can be reconsidered, including a subtask revealed by the material. It retains the latest six observations, at most 2,000 characters of output and 1,000 characters of input each, with truncation marked. Repeated observations are skipped. Selected intents are injected again only if their selection or asset version changes.

`Stop` checks the candidate reply against the current selected asset. It returns unmet requirements for revision while the number of met items improves, within the core limit. It cannot retract already streamed text. A host that requires checking before display must buffer candidate replies.

None of these hooks learns or updates an asset. Hosted tools that do not emit local hook events are outside this automatic observation path. The current checker reads supplied text evidence; it does not render Office documents or inspect arbitrary files automatically.

## MCP for other harnesses

The server provides four tools: `match_intents`, `check_delivery`, `inspect_intents`, and `learn_intents`. After running `scripts/setup.sh`, configure a stdio client:

```json
{
  "mcpServers": {
    "liveasset": {
      "command": "/absolute/path/to/codex-liveasset/.venv/bin/python",
      "args": ["/absolute/path/to/codex-liveasset/scripts/mcp_server.py"],
      "env": {
        "LIVEASSET_STORE": "/absolute/path/to/personal-store",
        "LIVEASSET_MODEL": "your-available-model"
      }
    }
  }
}
```

MCP makes these calls available; the host must invoke them at user input, material context changes, and candidate delivery. The supplied MCP server still uses Codex CLI for model calls. To use another model transport, call the core or construct `Service(store, model=...)` with an object implementing `async chat(messages)` and a text `.content` response.

## Task identity and matching inputs

A stable chat ID lets the service remember what work is active. A task ID lets a caller explicitly resume the same work, including from another chat. Similar tasks can reuse the same asset while retaining different task IDs.

| `match_intents` argument | Meaning |
| --- | --- |
| `request` | Current user message. Required. |
| `context` | Current task context, such as observed materials. Supply the full current context when updating it. |
| `session_id` | Stable chat identifier. A chat may contain several tasks over time. |
| `scope` | Project boundary. Use a consistent value; Codex hooks use the working directory. |
| `task_id` | Explicitly resume a known task in the same scope. Omit it when a new goal may begin. |
| `reroute` | Force matching after a relevant context change. |
| `event_id` | Identifier for one input. Identical repeats reuse the receipt; changed input with the same ID is rejected. |
| `replay` | Read a saved task snapshot without changing live bindings. Requires `task_id`. |

Calls with only `request` still work without saving a task binding. Passing `task_id` fixes task identity; the matcher may update its selection but cannot create a replacement task. Unknown or cross-scope IDs are rejected. A chat link used as a reference does not itself establish resumption; an ambiguous multi-task chat is not attached by guessing its latest task.

Returns include `task_id`, `task_context`, `routing`, `selected_ids`, `actor_context`, and `asset_sha256`. Pass the same task ID and scope to `check_delivery` to check the saved selection and task context without another match. Route changed context first if that selection should be updated. For an earlier event's replay, also provide the original `session_id` and `event_id`.

Bindings and asset snapshots live in `routing.json` and `asset-snapshots/`; matching does not write `asset.json`. The store serializes binding updates. It does not schedule an executor or recover an execution directory, branch, or external operation.

## Learning and failure handling

Learn only after the user explicitly asks to learn a particular interaction into LiveAsset. First review whether it contains reusable corrections and visible before/after evidence. A chat reference, finished reply, or delivery check is not authorization to learn.

MCP requires `learn_intents(..., user_requested=true)`. The CLI requires `learn --user-requested`. This flag records the caller's assertion of the user request; it does not authenticate a human. The core learner still makes assumptions about corrected or accepted outputs. Learned practices need review, especially when the history itself contains mistakes or no accepted result.

Invalid matcher judgments raise an error and preserve the previous binding. They cannot be treated as a valid empty selection or a successful delivery check. A hook failure records “not verified” and lets the host continue. A valid empty selection is allowed, and a check only covers the selected asset and visible evidence.

All facts in a selected intent remain in the injected text. The checker assesses applicability using the current object, instructions and materials. Earlier `scope=user|task` values are preserved but are not a physical fact filter. Current instructions take precedence over historical values.

## Examples and tests

See [cases/README.md](cases/README.md) for anonymized multi-turn inputs, expected task relationships, scripted replay, and real-model replay. Run the adapter's unit and stdio protocol tests with:

```bash
.venv/bin/python -m unittest discover -s . -p 'test_*.py' -v
```

The tests use temporary stores and simulated model responses; they do not install hooks in your real configuration or learn from personal chats. The package contains no credentials, personal assets, or raw user transcripts.

## License

This adapter and its bundled LiveAsset core use the [MIT License](LICENSE). Third-party dependencies retain their own licenses.
