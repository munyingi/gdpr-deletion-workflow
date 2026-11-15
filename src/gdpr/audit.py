"""Append-only, hash-chained erasure audit log that never stores PII.

Each entry records what was removed, where, how many rows and when. The
subject is referenced only by a keyed hash (so the controller can answer
"did we already process a request for this person?" while holding the key),
and every entry carries the SHA-256 of the previous one so any edit or
deletion of history breaks the chain.
"""
from __future__ import annotations

import hashlib
import json

from .config import AUDIT_LOG

GENESIS = "0" * 64


class PIILeak(RuntimeError):
    pass


def _canonical(entry: dict) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def read_entries() -> list[dict]:
    if not AUDIT_LOG.exists():
        return []
    return [json.loads(line) for line in AUDIT_LOG.read_text().splitlines() if line.strip()]


def append(entry: dict, forbidden: list[str]) -> dict:
    """Write an entry. Refuses to write if any plaintext identifier of the
    subject appears anywhere in it."""
    body = _canonical(entry).lower()
    leaked = [f for f in forbidden if f and f.lower() in body]
    if leaked:
        raise PIILeak(f"audit entry would contain {len(leaked)} personal identifier(s); refusing to write")
    entries = read_entries()
    entry = {**entry, "prev_hash": entries[-1]["entry_hash"] if entries else GENESIS}
    entry["entry_hash"] = hashlib.sha256(_canonical(entry).encode()).hexdigest()
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_LOG, "a") as f:
        f.write(_canonical(entry) + "\n")
    return entry


def verify_chain() -> tuple[bool, str]:
    prev = GENESIS
    for i, e in enumerate(read_entries(), start=1):
        claimed = e.get("entry_hash")
        body = {k: v for k, v in e.items() if k != "entry_hash"}
        if e.get("prev_hash") != prev:
            return False, f"entry {i}: prev_hash does not match entry {i - 1}"
        if hashlib.sha256(_canonical(body).encode()).hexdigest() != claimed:
            return False, f"entry {i}: content was modified after it was written"
        prev = claimed
    return True, f"{len(read_entries())} entries, chain intact"
