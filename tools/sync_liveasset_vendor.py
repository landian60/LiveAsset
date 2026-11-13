#!/usr/bin/env python3
"""Keep the standalone plugin's core identical to src; never edit vendor by hand."""
import argparse
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
CORE_FILES = ("__init__.py", "situated_intents.py", "prompts.py")


def sync(root=ROOT, *, check=False):
    source = root / "src/liveasset"
    target = root / "integrations/codex-liveasset/vendor/liveasset"
    stale = [name for name in CORE_FILES
             if not (target / name).is_file() or (target / name).read_bytes() != (source / name).read_bytes()]
    obsolete = target / "preference_feedback.py"
    if check:
        if stale or obsolete.exists():
            raise ValueError("Plugin core differs from src; run tools/sync_liveasset_vendor.py")
        return
    target.mkdir(parents=True, exist_ok=True)
    for name in stale:
        shutil.copy2(source / name, target / name)
    # This generated module was only needed for _readable, now owned by the core.
    obsolete.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    sync(check=args.check)
    print("Plugin core matches src.")
