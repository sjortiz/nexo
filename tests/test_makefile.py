"""Every documented Makefile target still resolves (CI runs lint, neo4j-test and test for real)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = (ROOT / "Makefile").read_text()
TARGETS = re.findall(r"^([a-z0-9-]+):.*## ", MAKEFILE, re.MULTILINE)
ARGS = {"ingest": ["FILE=corpus/memo.md"], "ask": ["Q=question"], "app": ["ARGS=models --check"]}


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
@pytest.mark.parametrize("target", TARGETS)
def test_target_dry_runs(target):
    # Drop what a calling `make test RUN=` exports, so each dry run sees only the Makefile's defaults.
    env = {k: v for k, v in os.environ.items() if k not in {"MAKEFLAGS", "MFLAGS", "MAKELEVEL", "RUN"}}
    result = subprocess.run(["make", "-n", target, *ARGS.get(target, [])], cwd=ROOT, env=env,
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    for path in re.findall(r"\b((?:deploy|examples|tests)/[\w./-]+\.(?:sh|py))\b", result.stdout):
        assert (ROOT / path).exists(), f"{target} uses missing {path}"
