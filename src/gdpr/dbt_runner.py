"""Run dbt as a subprocess so DuckDB file locks are released between steps."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import DBT_DIR, WAREHOUSE


def run_dbt(*args: str, db_path: Path | None = None) -> None:
    env = {**os.environ, "DUCKDB_PATH": str(db_path or WAREHOUSE)}
    cmd = [sys.executable, "-m", "dbt.cli.main", *args,
           "--project-dir", str(DBT_DIR), "--profiles-dir", str(DBT_DIR)]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"dbt {' '.join(args)} failed:\n{proc.stdout[-4000:]}\n{proc.stderr[-2000:]}")
