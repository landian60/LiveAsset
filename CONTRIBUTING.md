# Working on LiveAsset

The canonical core is `src/liveasset/situated_intents.py` plus `src/liveasset/prompts.py` (all model instruction text). The plugin carries generated copies of both so it can be installed on its own. After editing either file:

```bash
python tools/sync_liveasset_vendor.py
python tools/sync_liveasset_vendor.py --check
```

Use Python 3.11+ and install `.[mcp]` to run the core and adapter tests:

```bash
python -m pip install '.[mcp]'
python -m unittest discover -s tests -v
python -m unittest discover -s integrations/codex-liveasset -v
python examples/quickstart.py
```

These checks use scripted model responses and temporary stores. They do not need model credentials, change a personal asset, or register hooks in the real user configuration. The MCP protocol test starts a local stdio server in an empty temporary store.

For a semantic change, use the [model replay commands](integrations/codex-liveasset/cases/README.md) with an explicitly configured model and a new output directory. Save failed cases as well as successes. A scripted state-transition test cannot establish that the model recognizes a situation correctly.

Keep fixtures small and natural. Mark synthetic inputs and anonymized composites clearly. Store expected selections outside model prompts. Do not add actual chat IDs, account information, private files, credentials, or model-call traces to source fixtures. A correction-noise routing case can show that matching does not write an asset; it cannot prove that the learner would extract the right practice.

When changing behavior, explain what happens for the user and link the responsible function. Keep documentation fields and diagram stages consistent with the implementation. Readability improvements should preserve the existing asset schema, error handling, and public call signatures unless the change explicitly includes a migration.
