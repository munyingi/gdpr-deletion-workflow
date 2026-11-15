"""Day-by-day load into DuckDB, dbt snapshots, and nightly backups."""
from __future__ import annotations

import shutil
from datetime import date, timedelta
from pathlib import Path

import duckdb

from gdpr.config import BACKUP_DIR, RAW_DIR, WAREHOUSE, load_map
from gdpr.dbt_runner import run_dbt
from gdpr.suppression import apply_suppression

BACKUP_EVERY = {3, 5}  # take a backup after day 3 and day 5


def raw_days() -> list[str]:
    return sorted(p.stem.removeprefix("customers_") for p in (RAW_DIR / "crm").glob("customers_*.csv"))


def load_day(day: str, db_path: Path = WAREHOUSE) -> None:
    """Land one day's files in the raw schema, then apply the suppression
    list so a re-sent record of an erased customer never reaches the warehouse."""
    con = duckdb.connect(str(db_path))
    con.execute("create schema if not exists raw")
    con.execute(f"""
        create or replace table raw.customers as
        select * from read_csv('{RAW_DIR}/crm/customers_{day}.csv', header=true, all_varchar=true)""")
    con.execute(f"""
        create table if not exists raw.orders as
        select * from read_json('{RAW_DIR}/shop/orders_{day}.jsonl', format='newline_delimited') limit 0""")
    con.execute(f"""
        insert into raw.orders by name
        select * from read_json('{RAW_DIR}/shop/orders_{day}.jsonl', format='newline_delimited')""")
    con.execute(f"""
        create table if not exists raw.support_tickets as
        select * from read_csv('{RAW_DIR}/support/tickets_{day}.csv', header=true, all_varchar=true) limit 0""")
    con.execute(f"""
        insert into raw.support_tickets by name
        select * from read_csv('{RAW_DIR}/support/tickets_{day}.csv', header=true, all_varchar=true)""")
    con.execute(f"""
        create table if not exists raw.web_events as
        select * from read_json('{RAW_DIR}/web/events_{day}.jsonl', format='newline_delimited',
             columns={{event_id:'VARCHAR', customer_id:'VARCHAR', event_ts:'VARCHAR', page:'VARCHAR',
                      ip_address:'VARCHAR', user_agent:'VARCHAR'}}) limit 0""")
    con.execute(f"""
        insert into raw.web_events by name
        select * from read_json('{RAW_DIR}/web/events_{day}.jsonl', format='newline_delimited',
             columns={{event_id:'VARCHAR', customer_id:'VARCHAR', event_ts:'VARCHAR', page:'VARCHAR',
                      ip_address:'VARCHAR', user_agent:'VARCHAR'}})""")
    apply_suppression(con, tables=["raw.customers", "raw.orders", "raw.support_tickets", "raw.web_events"])
    con.close()


def backup(day: str) -> Path:
    target = BACKUP_DIR / f"warehouse_{day}"
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    con.execute(f"export database '{target}' (format parquet)")
    con.close()
    return target


def backup_expiry(backup_dir: Path) -> date:
    day = date.fromisoformat(backup_dir.name.removeprefix("warehouse_"))
    return day + timedelta(days=load_map()["backups"]["retention_days"])


def run_full() -> None:
    """Rebuild the warehouse from the raw files, one day at a time."""
    if WAREHOUSE.exists():
        WAREHOUSE.unlink()
    if BACKUP_DIR.exists():
        shutil.rmtree(BACKUP_DIR)
    WAREHOUSE.parent.mkdir(parents=True, exist_ok=True)
    for i, day in enumerate(raw_days(), start=1):
        load_day(day)
        run_dbt("snapshot")
        if i in BACKUP_EVERY:
            run_dbt("run")
            backup(day)
    run_dbt("run")
    run_dbt("test")


def restore_with_suppression(backup_dir: Path, target_db: Path) -> dict[str, int]:
    """Restore drill: import a backup into a fresh database, apply the
    suppression list, then rebuild derived models on top of it."""
    if target_db.exists():
        target_db.unlink()
    target_db.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(target_db))
    con.execute(f"import database '{backup_dir}'")
    touched = apply_suppression(con)
    con.close()
    run_dbt("run", db_path=target_db)
    return touched


def reset_demo() -> None:
    """Fresh raw files, warehouse and backups; clears the audit log and suppression list."""
    from gdpr.config import AUDIT_DIR, DATA_DIR
    from pipeline.generate import generate
    for d in (AUDIT_DIR, DATA_DIR / "restore_drill"):
        if d.exists():
            shutil.rmtree(d)
    generate()
    run_full()
