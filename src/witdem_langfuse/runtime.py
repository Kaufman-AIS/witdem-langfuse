"""Locate the installed backfill runtime in the active Python environment."""

import os
import shutil
import sys
from pathlib import Path


def backfill_executable():
    override = os.environ.get("DUCKLE_EXECUTABLE")
    if override:
        return override
    name = "duckle.exe" if os.name == "nt" else "duckle"
    candidate = Path(sys.executable).parent / name
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    executable = shutil.which("duckle")
    if executable:
        return executable
    raise RuntimeError(
        "Backfill runtime is missing. Install witdem-langfuse with the [etl] extra "
        "in the Python environment running this command."
    )
