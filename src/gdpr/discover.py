"""Find every identifier a customer has ever had, then every place it lives.

Step 1 (collect_subject) builds the full identifier set from the CRM
extracts, the SCD2 history and order rows. The current CRM row alone is not
enough: old emails and addresses only exist in history.

Step 2 (scan) is a brute-force search for those identifiers across raw
files, every warehouse table and view, and every backup file. It does not
trust the data map, so it also catches copies nobody documented.
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from .config import BACKUP_DIR, DATA_DIR, RAW_DIR, WAREHOUSE, load_map, subject_hash
from .redact import sql_regex


@dataclass
class Subject:
    customer_id: str
    emails: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    names: set[str] = field(default_factory=set)
    addresses: set[str] = field(default_factory=set)

    def identifiers(self) -> list[str]:
        vals = {self.customer_id, *self.emails, *self.phones, *self.names, *self.addresses}
        return sorted(v for v in vals if v)

    def hashes(self) -> list[str]:
        """What goes on the suppression list (addresses excluded: they are
        shared by households and only ever appear next to a hashed key)."""
        vals = {self.customer_id, *self.emails, *self.phones, *self.names}
        return sorted(subject_hash(v) for v in vals if v)

    def summary(self) -> dict[str, int]:
        """Counts only. Safe to show or log."""
        return {"emails": len(self.emails), "phones": len(self.phones),
                "names": len(self.names), "addresses": len(self.addresses)}

    def is_empty(self) -> bool:
        return not (self.emails or self.phones or self.names)


def _add_customer_row(s: Subject, row: dict) -> None:
    if row.get("email"):
        s.emails.add(row["email"])
    if row.get("phone"):
        s.phones.add(row["phone"])
    if row.get("first_name") and row.get("last_name"):
        s.names.add(f"{row['first_name']} {row['last_name']}")
    if row.get("street_address"):
        s.addresses.add(row["street_address"])


def collect_subject(customer_id: str) -> Subject:
    s = Subject(customer_id)
    for path in sorted((RAW_DIR / "crm").glob("customers_*.csv")):
        with open(path, newline="") as f:
            for row in csv.DictReader(f):
                if row["customer_id"] == customer_id:
                    _add_customer_row(s, row)
    for path in sorted((RAW_DIR / "shop").glob("orders_*.jsonl")):
        for line in path.read_text().splitlines():
            o = json.loads(line)
            if o["customer_id"] == customer_id:
                if o.get("customer_email"):
                    s.emails.add(o["customer_email"])
                if o.get("shipping_name"):
                    s.names.add(o["shipping_name"])
                if o.get("shipping_address"):
                    s.addresses.add(o["shipping_address"].split(",")[0])
    if WAREHOUSE.exists():
        con = duckdb.connect(str(WAREHOUSE), read_only=True)
        try:
            cur = con.execute("select * from snapshots.customers_snapshot where customer_id = ?", [customer_id])
            cols = [d[0] for d in cur.description]
            for r in cur.fetchall():
                _add_customer_row(s, dict(zip(cols, r)))
        except duckdb.CatalogException:
            pass
        con.close()
    return s


# ---------------------------------------------------------------------------
def _scan_raw(identifiers: list[str]) -> list[dict]:
    hits = []
    low = [i.lower() for i in identifiers]
    for path in sorted(p for p in RAW_DIR.rglob("*") if p.is_file()):
        lines = path.read_text().splitlines()
        if path.suffix == ".csv":
            lines = lines[1:]
        n = sum(1 for line in lines if any(i in line.lower() for i in low))
        if n:
            hits.append({"type": "raw_file", "location": str(path.relative_to(DATA_DIR)), "column": "*", "rows": n})
    return hits


def _scan_duckdb(con: duckdb.DuckDBPyConnection, pattern: str, relations: list[tuple[str, str, list[str]]],
                 kind: str) -> list[dict]:
    hits = []
    for location, sql_from, columns in relations:
        for col in columns:
            n = con.execute(f"select count(*) from {sql_from} where regexp_matches(cast({col} as varchar), ?)",
                            [pattern]).fetchone()[0]
            if n:
                hits.append({"type": kind, "location": location, "column": col, "rows": n})
    return hits


def _warehouse_relations(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str, str, list[str]]]:
    rows = con.execute("""
        select c.table_schema, c.table_name, t.table_type, list(c.column_name order by c.ordinal_position)
        from information_schema.columns c
        join information_schema.tables t using (table_catalog, table_schema, table_name)
        where c.data_type = 'VARCHAR' and c.table_schema not in ('information_schema', 'pg_catalog')
        group by all order by 1, 2""").fetchall()
    return [(f"{s}.{t}", tt, cols) for s, t, tt, cols in rows]


def scan(identifiers: list[str], include_backups: bool = True, db_path: Path = WAREHOUSE) -> list[dict]:
    if not identifiers:
        return []
    pattern = sql_regex(identifiers)
    hits = _scan_raw(identifiers)
    if db_path.exists():
        con = duckdb.connect(str(db_path), read_only=True)
        for name, ttype, cols in _warehouse_relations(con):
            kind = "warehouse_view" if ttype == "VIEW" else "warehouse_table"
            hits += _scan_duckdb(con, pattern, [(name, name, cols)], kind)
        con.close()
    if include_backups and BACKUP_DIR.exists():
        con = duckdb.connect()
        for pq in sorted(BACKUP_DIR.rglob("*.parquet")):
            cols = [r[0] for r in con.execute(f"describe select * from read_parquet('{pq}')").fetchall()
                    if r[1] == "VARCHAR"]
            hits += _scan_duckdb(con, pattern, [(str(pq.relative_to(DATA_DIR)), f"read_parquet('{pq}')", cols)],
                                 "backup")
        con.close()
    return hits


def classify(hits: list[dict]) -> list[dict]:
    """Tag each hit with how the data map treats it, flagging UNMAPPED ones."""
    m = load_map()
    file_globs = {spec["glob"]: spec["name"] for spec in m["raw_files"]}
    tables = {spec["table"]: spec["action"] for spec in m["warehouse"]}
    derived = set(m["derived"]["schemas"])
    out = []
    for h in hits:
        h = dict(h)
        if h["type"] == "raw_file":
            match = next((name for g, name in file_globs.items() if Path(h["location"]).match(g)), None)
            h["handled_by"] = f"map: {match}" if match else "UNMAPPED"
        elif h["type"] == "backup":
            h["handled_by"] = "suppression list + retention expiry"
        else:
            schema = h["location"].split(".")[0]
            if h["location"] in tables:
                h["handled_by"] = f"map: {tables[h['location']]}"
            elif schema in derived:
                h["handled_by"] = "derived: dbt rebuild"
            else:
                h["handled_by"] = "UNMAPPED"
        out.append(h)
    return out
