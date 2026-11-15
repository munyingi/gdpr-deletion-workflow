"""Paths, the data map and the keyed hash used for subject references."""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.environ.get("GDPR_DATA_DIR", ROOT / "data")).resolve()
RAW_DIR = DATA_DIR / "raw"
WAREHOUSE = DATA_DIR / "warehouse" / "warehouse.duckdb"
BACKUP_DIR = DATA_DIR / "backups"
AUDIT_DIR = DATA_DIR / "audit"
AUDIT_LOG = AUDIT_DIR / "erasure_log.jsonl"
SUPPRESSION_FILE = AUDIT_DIR / "suppression_list.json"
DBT_DIR = ROOT / "dbt"
PII_MAP = ROOT / "config" / "pii_map.yml"
KEY_FILE = Path(os.environ.get("GDPR_KEY_FILE", ROOT / ".secrets" / "gdpr_hmac.key"))

# dbt reads the warehouse path from this variable (see dbt/profiles.yml).
os.environ.setdefault("DUCKDB_PATH", str(WAREHOUSE))


@lru_cache
def load_map() -> dict:
    return yaml.safe_load(PII_MAP.read_text())


def _key() -> bytes:
    """Secret used for subject hashes. In production this lives in a KMS,
    never next to the data or the backups it protects."""
    env = os.environ.get("GDPR_HMAC_KEY")
    if env:
        return env.encode()
    if not KEY_FILE.exists():
        KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
        KEY_FILE.write_text(secrets.token_hex(32))
    return KEY_FILE.read_text().strip().encode()


def subject_hash(value: str | None) -> str | None:
    """Keyed HMAC-SHA256 of a normalised identifier. Without the key the
    hash cannot be reversed or brute-forced from a list of emails."""
    if value is None:
        return None
    return hmac.new(_key(), value.strip().lower().encode(), hashlib.sha256).hexdigest()
