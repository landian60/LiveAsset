#!/usr/bin/env python3
"""Explicit hook registration for Codex CLI installations that omit bundled plugin hooks."""
import argparse
import json
import os
from pathlib import Path
import shlex
import sys
import time

from liveasset import ROOT, read, save

parser = argparse.ArgumentParser()
parser.add_argument("--config-home", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))))
parser.add_argument("--plugin-root", type=Path, default=ROOT)
parser.add_argument("--api-env", type=Path, help="Existing local model route credentials; values are never copied into hooks.json")
parser.add_argument("--model")
parser.add_argument("--effort")
parser.add_argument("--codex", help="Codex executable used for isolated matcher/checker calls")
parser.add_argument("--store", type=Path)
args = parser.parse_args()
root = args.plugin_root.resolve()
config = args.config_home.resolve()
path = config / "hooks.json"
data = read(path, {"hooks": {}})
if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
    raise SystemExit("Existing hooks.json has an unsupported shape; no changes written")
if path.exists():
    backup = path.with_name(f"hooks.json.backup-{time.time_ns()}")
    backup.write_bytes(path.read_bytes())
environment = ["env", "PLUGIN_ROOT=" + str(root),
    "PLUGIN_DATA=" + str(config / "plugins/data/liveasset"), "LIVEASSET_PYTHON=" + sys.executable,
    *(["LIVEASSET_API_ENV=" + str(args.api_env.resolve())] if args.api_env else []),
    *(["LIVEASSET_STORE=" + str(args.store.resolve())] if args.store else []),
    *(["LIVEASSET_MODEL=" + args.model] if args.model else []),
    *(["LIVEASSET_EFFORT=" + args.effort] if args.effort else [])]
if args.codex:
    environment.append("LIVEASSET_CODEX=" + args.codex)
command = " ".join(shlex.quote(x) for x in environment + ["bash", str(root / "scripts/run.sh"), "hook"])
for event in ["UserPromptSubmit", "PostToolUse", "Stop"]:
    entry = {"hooks": [{"type": "command", "command": command, "timeout": 600,
                         "statusMessage": "LiveAsset " + event}]}
    entries = data.setdefault("hooks", {}).setdefault(event, [])
    # Replace this installation's previous command while leaving all other hooks intact.
    for group in entries:
        group["hooks"] = [h for h in group.get("hooks", []) if str(root / "scripts/run.sh") not in h.get("command", "")]
    entries[:] = [g for g in entries if g.get("hooks")]
    if entry not in entries:
        entries.append(entry)
save(path, data)
print(json.dumps({"hooks_file": str(path), "events": ["UserPromptSubmit", "PostToolUse", "Stop"],
                  "next": "Review and trust these hooks in Codex /hooks before using them."}))
