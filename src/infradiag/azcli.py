"""Thin wrapper over the az CLI so we reuse the user's existing az login."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


class AzError(Exception):
    pass


def run_az(args: list[str], cwd: Path | None = None, timeout: int = 900):
    exe = shutil.which("az")
    if exe is None:
        raise AzError("az CLI not found on PATH")
    try:
        proc = subprocess.run(
            [exe, *args, "--only-show-errors", "-o", "json"],
            cwd=cwd, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise AzError(f"az {' '.join(args[:3])} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        raise AzError(proc.stderr.strip() or f"az exited with {proc.returncode}")
    out = proc.stdout.strip()
    return json.loads(out) if out else None
