"""Execute an Article 17 erasure request end to end.

  1. collect every identifier the subject ever had (incl. SCD2 history)
  2. scan the whole estate and classify each hit against the data map
  3. rewrite raw files, delete/anonymise warehouse rows, anonymise SCD2
     history, redact free text
  4. rebuild derived dbt models
  5. add the subject's hashes to the suppression list (backups, re-ingest)
  6. re-scan to verify nothing is left outside immutable backups
  7. write a hash-chained audit entry containing no personal data
"""
from __future__ import annotations

import csv
import json
import os
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import duckdb

from . import audit, suppression
from .config import BACKUP_DIR, DATA_DIR, WAREHOUSE, load_map, subject_hash
from .dbt_runner import run_dbt
from .discover import Subject, classify, collect_subject, scan
from .redact import redact_known, sql_regex


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _values_for(subject: Subject, kind: str) -> set[str]:
    return {"customer_id": {subject.customer_id}, "email": {e.lower() for e in subject.emails}}[kind]


def _row_matches(row: dict, match: dict, subject: Subject) -> bool:
    return any((row.get(col) or "").lower() in {v.lower() for v in _values_for(subject, kind)}
               for col, kind in match.items())


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp_")
    with os.fdopen(fd, "w", newline="") as f:
        f.write(text)
    os.replace(tmp, path)


# ---- raw files --------------------------------------------------------------
def _process_file(path: Path, spec: dict, subject: Subject, token: str, dry_run: bool) -> dict:
    idents = subject.identifiers()
    if spec["format"] == "csv":
        with open(path, newline="") as f:
            reader = csv.DictReader(f)
            fields, rows = reader.fieldnames, list(reader)
    else:
        fields, rows = None, [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    kept, matched, redacted = [], 0, 0
    for row in rows:
        if _row_matches(row, spec["match"], subject):
            matched += 1
            if spec["action"] == "delete":
                continue
            row[spec["key_column"]] = token
            for col in spec["pii_columns"]:
                row[col] = None
        for col in spec.get("free_text", []):
            row[col], changed = redact_known(row.get(col), idents)
            redacted += changed
        kept.append(row)

    if not dry_run and (matched or redacted):
        if spec["format"] == "csv":
            import io
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=fields, lineterminator="\r\n")
            w.writeheader()
            w.writerows(kept)
            _atomic_write(path, buf.getvalue())
        else:
            _atomic_write(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept))
    return {"rows_matched": matched, "rows_redacted": redacted}


# ---- warehouse --------------------------------------------------------------
def _process_table(con: duckdb.DuckDBPyConnection, spec: dict, subject: Subject, token: str,
                   dry_run: bool) -> dict:
    t = spec["table"]
    conds, params = [], []
    for col, kind in spec["match"].items():
        conds.append(f"lower({col}) in (select unnest(?))")
        params.append(sorted(v.lower() for v in _values_for(subject, kind)))
    where = " or ".join(conds)
    matched = con.execute(f"select count(*) from {t} where {where}", params).fetchone()[0]
    if matched and not dry_run:
        if spec["action"] == "delete":
            con.execute(f"delete from {t} where {where}", params)
        else:
            key = spec["key_column"]
            sets = ", ".join(f"{c} = null" for c in spec["pii_columns"])
            con.execute(f"update {t} set {key} = ?, {sets} where {where}", [token, *params])
            if spec["action"] == "anonymise_history":
                # dbt_scd_id = md5(natural key || valid_from) is derived from the
                # old customer id, so it is recomputed from the token
                con.execute(f"update {t} set dbt_scd_id = md5({key} || cast(dbt_valid_from as varchar)) "
                            f"where {key} = ?", [token])
    redacted = 0
    idents = subject.identifiers()
    pattern = sql_regex(idents)
    for col in spec.get("free_text", []):
        # rows already deleted above (or about to be, in a dry run) are not counted again
        skip = f" and not ({where})" if spec["action"] == "delete" else ""
        rows = con.execute(f"select rowid, {col} from {t} where regexp_matches({col}, ?){skip}",
                           [pattern, *(params if skip else [])]).fetchall()
        redacted += len(rows)
        if not dry_run:
            for rowid, txt in rows:
                con.execute(f"update {t} set {col} = ? where rowid = ?", [redact_known(txt, idents)[0], rowid])
    return {"rows_matched": matched, "rows_redacted": redacted}


# ---- main entry point -------------------------------------------------------
def erase(customer_id: str, dry_run: bool = False, received_on: date | None = None,
          operator: str = "privacy@shop.example", channel: str = "email",
          restore_drill: bool = False) -> dict:
    customer_id = customer_id.strip().upper()
    request_id = str(uuid.uuid4())
    started = _now()
    received_on = received_on or datetime.now(timezone.utc).date()
    m = load_map()

    subject = collect_subject(customer_id)
    idents = subject.identifiers()
    before = classify(scan(idents)) if not subject.is_empty() else []
    token = f"ERASED-{uuid.uuid4().hex[:12]}"   # random, mapping never stored
    steps: list[dict] = []

    if not subject.is_empty():
        for spec in m["raw_files"]:
            for path in sorted(DATA_DIR.glob(spec["glob"])):
                r = _process_file(path, spec, subject, token, dry_run)
                if r["rows_matched"] or r["rows_redacted"]:
                    steps.append({"system": "raw_files", "location": str(path.relative_to(DATA_DIR)),
                                  "action": spec["action"], **r})
        if WAREHOUSE.exists():
            con = duckdb.connect(str(WAREHOUSE))
            con.execute("begin transaction")
            for spec in m["warehouse"]:
                r = _process_table(con, spec, subject, token, dry_run)
                if r["rows_matched"] or r["rows_redacted"]:
                    steps.append({"system": "warehouse", "location": spec["table"], "action": spec["action"], **r})
            con.execute("rollback" if dry_run else "commit")
            con.close()
        if not dry_run:
            run_dbt("run")
            steps.append({"system": "warehouse", "location": "staging.*, marts.*",
                          "action": "rebuilt (dbt run)", "rows_matched": None, "rows_redacted": None})

    new_hashes = 0
    if not dry_run and not subject.is_empty():
        new_hashes = suppression.add_entry(request_id, subject.hashes())

    after = classify(scan(idents)) if (not subject.is_empty() and not dry_run) else []
    residual = [h for h in after if h["type"] != "backup"]
    backups: dict[str, dict] = {}
    for h in after:
        if h["type"] == "backup":
            name = Path(h["location"]).parts[1]
            day = date.fromisoformat(name.removeprefix("warehouse_"))
            b = backups.setdefault(name, {"backup": name, "rows": 0,
                                          "expires_on": str(day + timedelta(days=m["backups"]["retention_days"])),
                                          "treatment": "immutable; suppressed on restore; deleted at expiry"})
            b["rows"] += h["rows"]

    drill = None
    if restore_drill and not dry_run and backups:
        from pipeline.run import restore_with_suppression
        latest = sorted(BACKUP_DIR.glob("warehouse_*"))[-1]
        drill_db = DATA_DIR / "restore_drill" / "restored.duckdb"
        touched = restore_with_suppression(latest, drill_db)
        drill_hits = [h for h in scan(idents, include_backups=False, db_path=drill_db) if h["type"] != "raw_file"]
        drill = {"backup": latest.name, "rows_suppressed": sum(touched.values()),
                 "pii_hits_after_restore": sum(h["rows"] for h in drill_hits)}

    retained = [{"location": s.get("table") or s.get("name"), "treatment": "anonymised",
                 "reason": " ".join(s["retention_reason"].split())}
                for s in m["raw_files"] + m["warehouse"] if s.get("retention_reason")]

    if subject.is_empty():
        status = "no_data_found"
    elif dry_run:
        status = "dry_run"
    else:
        status = "completed" if not residual else "incomplete"

    entry = {
        "request_id": request_id,
        "subject_ref": subject_hash(customer_id),
        "request": {"article": "GDPR Art. 17", "channel": channel, "received_on": str(received_on),
                    "due_by": str(received_on + timedelta(days=30))},
        "operator": operator,
        "started_at": started,
        "completed_at": _now(),
        "status": status,
        "identifiers_found": subject.summary(),
        "locations_found": [{k: h[k] for k in ("type", "location", "column", "rows", "handled_by")}
                            for h in before],
        "steps": steps,
        "retained_with_legal_basis": retained if not subject.is_empty() else [],
        "backups": list(backups.values()),
        "suppression_hashes_added": new_hashes,
        "verification": {"pii_hits_outside_backups": sum(h["rows"] for h in residual),
                         "residual_locations": [h["location"] for h in residual],
                         "restore_drill": drill},
    }
    if dry_run:
        return entry  # dry runs are previews, not part of the legal record
    return audit.append(entry, forbidden=idents)
