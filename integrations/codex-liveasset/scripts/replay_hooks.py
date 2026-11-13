#!/usr/bin/env python3
"""Replay natural dialogue/tool fixtures through the real hook entry point and matcher."""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid


async def run(args):
    package = Path(__file__).resolve().parents[1]
    out = args.out.resolve()
    source = out / "source"
    if not source.exists():
        shutil.copytree(package, source, ignore=shutil.ignore_patterns(".venv", "__pycache__", ".DS_Store"))
    sys.path.insert(0, str(source / "scripts"))
    import liveasset as la
    manifest = {"model": os.environ.get("LIVEASSET_MODEL", "gpt-6.1-sol"),
                "effort": os.environ.get("LIVEASSET_EFFORT", "high"), "cases": args.cases,
                "source_sha256": hashlib.sha256((source / "scripts/liveasset.py").read_bytes()).hexdigest()}
    previous = la.read(out / "manifest.json")
    if previous is not None and previous != manifest:
        raise ValueError("Run configuration changed; use a new output directory")
    la.save(out / "manifest.json", manifest)
    fixture = la.read(source / "cases/supervision.json")
    results = []
    for case in fixture["cases"]:
        if args.cases and case["id"] not in args.cases:
            continue
        folder = out / case["id"]
        store = folder / "store"
        if not (store / "asset.json").exists():
            la.save(store / "asset.json", la.read(source / "cases/assets.json"))
        asset_hash = hashlib.sha256((store / "asset.json").read_bytes()).hexdigest()
        prior = {}
        for step in case["steps"]:
            target = folder / "events" / (step["id"] + ".json")
            saved = la.read(target)
            if saved:
                prior[step["id"]] = saved
                results.append(saved)
                continue
            event = {"cwd": "anonymous-workspace", **step["event"]}
            started = time.monotonic()
            while True:
                la.save(out / "status.json", {"status": "running", "case": case["id"], "step": step["id"]})
                try:
                    output = await la.hook(event, store)
                    break
                except (OSError, TimeoutError, RuntimeError) as exc:
                    la.save(folder / "failures" / (uuid.uuid4().hex + ".json"), {"step": step["id"], "error": str(exc)})
                    la.save(out / "status.json", {"status": "retrying_transport", "error": str(exc)})
                    await asyncio.sleep(2)
            state = la.read(store / "sessions" / la.digest(event["session_id"]) / "state.json")
            selected = state["selection"]
            expect = step["expect"]
            checks = {"selected": set(selected["selected_ids"]) == set(expect["selected"]),
                      "asset_unchanged": asset_hash == hashlib.sha256((store / "asset.json").read_bytes()).hexdigest()}
            for kind in ("same_task_as", "different_task_from"):
                if kind in expect:
                    same = selected["task_id"] == prior[expect[kind]]["selection"]["task_id"]
                    checks[kind] = same if kind == "same_task_as" else not same
            if "additional_context" in expect:
                checks["additional_context"] = bool(output.get("hookSpecificOutput", {}).get("additionalContext")) == expect["additional_context"]
            if "action" in expect:
                checks["action"] = selected["routing"]["action"] == expect["action"]
            saved = {"case": case["id"], "step": step["id"], "event": event, "output": output,
                     "selection": {k: v for k, v in selected.items() if k != "asset"},
                     "checks": checks, "passed": all(checks.values()), "seconds": round(time.monotonic() - started, 2)}
            la.save(target, saved)
            prior[step["id"]] = saved
            results.append(saved)
            print(case["id"], step["id"], "PASS" if saved["passed"] else "FAIL", flush=True)
    result = {"kind": "real-model hook-event replay; tool results are fixtures, not live tool executions",
              "passed": all(r["passed"] for r in results), "steps": len(results), "new_learning_calls": 0, "results": results}
    la.save(out / "results.json", result)
    la.save(out / "status.json", {"status": "completed", "passed": result["passed"], "steps": len(results)})
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cases", nargs="*", default=[])
    raise SystemExit(asyncio.run(run(parser.parse_args())))
