#!/usr/bin/env python3
"""Small resumable routing checks. Scripted mode tests state transitions; model mode tests selection too."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
from types import SimpleNamespace
import uuid


class ScriptedModel:
    def __init__(self, asset):
        self.asset = asset
        self.expected = {}
        self.calls = 0

    async def chat(self, messages):
        self.calls += 1
        expected = self.expected
        selected = expected.get("selected", [])
        reply = {"selected": [{"situation_id": s["id"], "reason": "Scripted state-transition fixture."}
                               for s in self.asset["situations"] if any(i["id"] in selected for i in s["intents"])]}
        if '"bound_task"' in messages[0]["content"]:
            reply["relation"] = "continue" if expected.get("same_task_as") else "new"
        return SimpleNamespace(content=json.dumps(reply))


async def run(args):
    package = Path(__file__).resolve().parents[1]
    output = args.out.resolve()
    output.mkdir(parents=True, exist_ok=True)
    frozen = output / "source"
    if not frozen.exists():
        staging = output / ("source-" + uuid.uuid4().hex)
        shutil.copytree(package, staging, ignore=shutil.ignore_patterns(".venv", "__pycache__", ".DS_Store"))
        staging.rename(frozen)
    sys.path.insert(0, str(frozen / "scripts"))
    import liveasset as la

    manifest = {"mode": args.mode, "cases": args.cases, "model": args.model, "effort": args.effort,
                "source": {str(p.relative_to(frozen)): hashlib.sha256(p.read_bytes()).hexdigest()
                           for p in frozen.rglob("*") if p.is_file() and "__pycache__" not in p.parts}}
    saved = la.read(output / "manifest.json")
    if saved is not None and saved != manifest:
        raise ValueError("run arguments or frozen source differ; use a new output directory")
    la.save(output / "manifest.json", manifest)
    fixture = la.read(frozen / "cases/daily-routing.json")
    base_asset = la.read(frozen / "cases/assets.json")
    cases = [case for case in fixture["cases"] if not args.cases or case["id"] in args.cases]
    if args.cases and set(args.cases) != {c["id"] for c in cases}:
        raise ValueError("unknown case id")
    summary = {"mode": args.mode, "semantic_model_test": args.mode == "model", "cases": [], "new_learning_calls": 0}
    for case in cases:
        folder = output / case["id"]
        store = folder / "store"
        store.mkdir(parents=True, exist_ok=True)
        if not (store / "asset.json").exists():
            la.save(store / "asset.json", base_asset)
        model = (ScriptedModel(base_asset) if args.mode == "scripted" else
                 la.CodexModel(folder / "model-calls", model=args.model, effort=args.effort))
        service = la.Service(store, model)
        results = {}
        case_result = {"id": case["id"], "kind": case["kind"], "events": []}
        for event in case["events"]:
            target = folder / "events" / (event["id"] + ".json")
            previous = la.read(target)
            if previous is not None:
                if "result" in previous:
                    results[event["id"]] = previous["result"]
                case_result["events"].append(previous)
                continue
            la.save(output / "status.json", {"status": "running", "case": case["id"], "event": event["id"]})
            if event.get("operation") == "replace_asset":
                asset = service.asset()
                asset["revision"] = event["revision"]
                asset["situations"] = [s for s in asset["situations"] if s["id"] not in event["remove_situations"]]
                la.save(store / "asset.json", asset)
                record = {"event": event["id"], "operation": "replace_test_asset", "passed": True}
            else:
                if args.mode == "scripted":
                    model.asset = service.asset()
                    model.expected = event["expect"]
                kwargs = {k: event[k] for k in ("context", "session_id", "scope", "reroute", "replay", "event_id") if k in event}
                kwargs.setdefault("scope", "anonymous-project")
                if "task_from" in event:
                    kwargs["task_id"] = results[event["task_from"]]["task_id"]
                asset_before = (store / "asset.json").read_bytes()
                binding_before = (store / "routing.json").read_bytes() if (store / "routing.json").exists() else None
                started = time.monotonic()
                while True:
                    try:
                        result = await service.match(event["request"], **kwargs)
                        break
                    except (OSError, TimeoutError, RuntimeError) as exc:
                        la.save(folder / "failures" / (uuid.uuid4().hex + ".json"), {"event": event["id"], "error": str(exc)})
                        la.save(output / "status.json", {"status": "recovering", "case": case["id"], "event": event["id"], "error": str(exc)})
                        await asyncio.sleep(2)
                expected = event["expect"]
                checks = {"asset_unchanged": asset_before == (store / "asset.json").read_bytes()}
                if "selected" in expected:
                    checks["selection"] = set(result["selected_ids"]) == set(expected["selected"])
                for key in ("action", "source", "asset_revision"):
                    if key in expected:
                        checks[key] = result["routing"][key] == expected[key]
                for key in ("calls", "duplicate_event"):
                    if key in expected:
                        checks[key] = result.get(key) == expected[key]
                if "same_task_as" in expected:
                    checks["same_task"] = result["task_id"] == results[expected["same_task_as"]]["task_id"]
                if "different_task_from" in expected:
                    checks["different_task"] = result["task_id"] != results[expected["different_task_from"]]["task_id"]
                if expected.get("binding_unchanged"):
                    checks["binding_unchanged"] = binding_before == (store / "routing.json").read_bytes()
                if "context_contains" in expected:
                    checks["context"] = expected["context_contains"] in result["actor_context"]
                record = {"event": event["id"], "checks": checks, "passed": all(checks.values()),
                          "seconds": round(time.monotonic() - started, 3), "result": result}
                results[event["id"]] = result
            la.save(target, record)
            case_result["events"].append(record)
            print(case["id"], event["id"], "PASS" if record["passed"] else "FAIL", flush=True)
        case_result["passed"] = all(e["passed"] for e in case_result["events"])
        summary["cases"].append(case_result)
    summary["passed"] = all(c["passed"] for c in summary["cases"])
    summary["model_calls"] = sum(e.get("result", {}).get("calls", 0) for c in summary["cases"] for e in c["events"])
    la.save(output / "results.json", summary)
    la.save(output / "status.json", {"status": "completed", "passed": summary["passed"], "model_calls": summary["model_calls"]})
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["scripted", "model"], default="scripted")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cases", nargs="*", default=[])
    parser.add_argument("--model", default="gpt-6.1-sol")
    parser.add_argument("--effort", default="high")
    raise SystemExit(asyncio.run(run(parser.parse_args())))
