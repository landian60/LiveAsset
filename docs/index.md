# Documentation

Start here for the mechanism, the host adapters, and the runnable examples.

| Want to... | Read |
| --- | --- |
| Understand what an intent asset contains, and call the core | [Core mechanism and host integration](architecture.md) |
| Install the Codex plugin, hooks, or MCP server | [LiveAsset for Codex and MCP](../integrations/codex-liveasset/README.md) |
| Run the offline learn / select / verify examples | [Examples](../examples/README.md) |
| Replay anonymized multi-turn routing cases | [Routing cases](../integrations/codex-liveasset/cases/README.md) |
| Validate an asset file | [Asset JSON Schema](../schema/asset.schema.json) |
| Run local checks or add fixtures | [Contributing](../CONTRIBUTING.md) |

## Suggested reading order

1. [README](../README.md) — what the method does and what is in the package.
2. [architecture.md](architecture.md) — schema, core API, host events, failure boundaries.
3. [examples/README.md](../examples/README.md) — run the three offline scripts.
4. The [integration README](../integrations/codex-liveasset/README.md) — only when wiring LiveAsset into a host.

## Conventions

- The canonical core is `src/liveasset/`: `situated_intents.py` for the mechanism, `prompts.py` for all model instruction text. The plugin carries generated copies of both at `integrations/codex-liveasset/vendor/liveasset/`. Never edit the generated copy; run `python tools/sync_liveasset_vendor.py` after changing either file.
- Prompt builders are re-exported from `situated_intents.py`, so public call signatures are unchanged. Exercise receipts hash the assembled prompt at runtime: keep the instruction strings byte-identical unless the change is intentional.
- `examples/` uses scripted model judgments. Add new examples under `examples/` and register them in `release/files.json`.
- Assets carry a `schema_version` format tag; see [asset.schema.json](../schema/asset.schema.json) and [report-asset.json](../examples/report-asset.json) for a small hand-authored example.
