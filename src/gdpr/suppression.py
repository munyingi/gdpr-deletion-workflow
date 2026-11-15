"""Suppression list: keyed hashes of erased subjects' identifiers.

Backups cannot be edited, and upstream systems sometimes re-send data that
was already erased. Both are handled the same way: every restore and every
load runs `apply_suppression`, which removes or anonymises any row whose
identifiers hash to an entry on this list. The list holds no plaintext.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import duckdb

from .config import SUPPRESSION_FILE, load_map, subject_hash
from .redact import redact_by_hash


def load_hashes() -> set[str]:
    if not SUPPRESSION_FILE.exists():
        return set()
    data = json.loads(SUPPRESSION_FILE.read_text())
    return {h for e in data["entries"] for h in e["hashes"]}


def add_entry(request_id: str, hashes: list[str]) -> int:
    SUPPRESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(SUPPRESSION_FILE.read_text()) if SUPPRESSION_FILE.exists() else {"entries": []}
    existing = {h for e in data["entries"] for h in e["hashes"]}
    new = sorted(set(hashes) - existing)
    data["entries"].append({"request_id": request_id,
                            "added_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "hashes": new})
    SUPPRESSION_FILE.write_text(json.dumps(data, indent=2))
    return len(new)


def _table_exists(con: duckdb.DuckDBPyConnection, table: str) -> bool:
    schema, name = table.split(".")
    return con.execute("select count(*) from information_schema.tables where table_schema=? and table_name=?",
                       [schema, name]).fetchone()[0] > 0


def apply_suppression(con: duckdb.DuckDBPyConnection, tables: list[str] | None = None) -> dict[str, int]:
    """Apply the suppression list to warehouse tables in `con`. Returns rows touched per table."""
    hashes = load_hashes()
    touched: dict[str, int] = {}
    if not hashes:
        return touched
    try:
        con.create_function("gdpr_hmac", subject_hash, ["VARCHAR"], "VARCHAR")
    except duckdb.Error:
        pass  # already registered on this connection
    hlist = sorted(hashes)
    for spec in load_map()["warehouse"]:
        t = spec["table"]
        if (tables and t not in tables) or not _table_exists(con, t):
            continue
        cond = " or ".join(f"gdpr_hmac({col}) in (select unnest($h))" for col in spec["match"])
        n = con.execute(f"select count(*) from {t} where {cond}", {"h": hlist}).fetchone()[0]
        if n:
            if spec["action"] == "delete":
                con.execute(f"delete from {t} where {cond}", {"h": hlist})
            else:
                key = spec["key_column"]
                sets = ", ".join(f"{c} = null" for c in spec["pii_columns"])
                token = "'ERASED-' || substr(replace(cast(uuid() as varchar), '-', ''), 1, 12)"
                con.execute(f"update {t} set {key} = {token}, {sets} where {cond}", {"h": hlist})
                if spec["action"] == "anonymise_history":
                    # dbt_scd_id is md5(natural key || timestamp): recompute from the token
                    con.execute(f"update {t} set dbt_scd_id = md5({key} || cast(dbt_valid_from as varchar)) "
                                f"where {key} like 'ERASED-%'")
        redacted = 0
        for col in spec.get("free_text", []):
            for rowid, txt in con.execute(f"select rowid, {col} from {t} where {col} is not null").fetchall():
                new, changed = redact_by_hash(txt, hashes)
                if changed:
                    con.execute(f"update {t} set {col} = ? where rowid = ?", [new, rowid])
                    redacted += 1
        if n or redacted:
            touched[t] = n + redacted
    return touched
