import json

import duckdb

from conftest import DEMO
from gdpr import audit
from gdpr.config import AUDIT_LOG, DATA_DIR, RAW_DIR, WAREHOUSE


def _ro():
    return duckdb.connect(str(WAREHOUSE), read_only=True)


# --- discovery ---------------------------------------------------------------
def test_history_holds_identifiers_the_crm_no_longer_has(world):
    s = world["subject"]
    assert len(s.emails) == 2, "old email should be recovered from SCD2 history and old extracts"
    assert len(s.addresses) >= 2
    assert world["before"]["versions"] == 4


def test_discovery_finds_mention_in_someone_elses_ticket(world):
    ticket_hits = [h for h in world["hits_before"] if h["location"] == "raw/support/tickets_2026-11-03.csv"]
    assert ticket_hits, "free-text mention by a family member must be discovered"


def test_every_live_location_is_covered_by_the_map(world):
    assert not [h for h in world["hits_before"] if h["handled_by"] == "UNMAPPED"]


# --- erasure -----------------------------------------------------------------
def test_erasure_completes_with_zero_residual_pii(world):
    r = world["result"]
    assert r["status"] == "completed"
    assert r["verification"]["pii_hits_outside_backups"] == 0


def test_no_identifier_left_in_any_raw_file(world):
    text = "\n".join(p.read_text().lower() for p in RAW_DIR.rglob("*") if p.is_file())
    for ident in world["subject"].identifiers():
        assert ident.lower() not in text


def test_scd2_history_is_anonymised_not_deleted(world):
    con = _ro()
    rows = con.execute("select customer_id, first_name, email, phone, street_address, date_of_birth, "
                       "segment, dbt_scd_id from snapshots.customers_snapshot "
                       "where customer_id like 'ERASED-%'").fetchall()
    old_scd_ids = con.execute("select md5(? || cast(dbt_valid_from as varchar)) from snapshots.customers_snapshot "
                              "where customer_id like 'ERASED-%'", [DEMO]).fetchall()
    total = con.execute("select count(*) from snapshots.customers_snapshot").fetchone()[0]
    con.close()
    assert len(rows) == world["before"]["versions"]
    assert len({r[0] for r in rows}) == 1, "all versions share one random token"
    assert all(r[1] is None and r[2] is None and r[3] is None and r[4] is None and r[5] is None for r in rows)
    assert {r[6] for r in rows} == {"regular", "loyal"}, "non-personal history is kept"
    assert not {r[7] for r in rows} & {o[0] for o in old_scd_ids}, "dbt_scd_id must not be derivable from old id"
    assert total == world["before"]["snapshot_rows"]


def test_other_customers_are_untouched(world):
    con = _ro()
    after = con.execute("select * from snapshots.customers_snapshot where customer_id='C00100' "
                        "order by dbt_valid_from").fetchall()
    con.close()
    assert after == world["before"]["other_customer"]


def test_order_financials_are_retained(world):
    con = _ro()
    revenue, orders = con.execute("select sum(amount_eur), count(*) from marts.fct_orders").fetchone()
    con.close()
    assert revenue == world["before"]["revenue"]
    assert orders == world["before"]["orders"]


def test_backups_reported_with_expiry(world):
    backups = world["result"]["backups"]
    assert {b["backup"] for b in backups} == {"warehouse_2026-11-03", "warehouse_2026-11-05"}
    assert all(b["expires_on"] for b in backups)


def test_restore_drill_suppresses_erased_subject(world):
    drill = world["result"]["verification"]["restore_drill"]
    assert drill["rows_suppressed"] > 0
    assert drill["pii_hits_after_restore"] == 0


# --- audit log ---------------------------------------------------------------
def test_audit_log_contains_no_personal_data(world):
    raw = AUDIT_LOG.read_text().lower()
    for ident in world["subject"].identifiers():
        assert ident.lower() not in raw
    supp = (DATA_DIR / "audit" / "suppression_list.json").read_text().lower()
    for ident in world["subject"].identifiers():
        assert ident.lower() not in supp


def test_audit_refuses_to_write_pii(world):
    import pytest
    with pytest.raises(audit.PIILeak):
        audit.append({"note": f"erased {DEMO}"}, forbidden=[DEMO])


def test_audit_chain_detects_tampering(world, tmp_path, monkeypatch):
    ok, _ = audit.verify_chain()
    assert ok
    entries = [json.loads(line) for line in AUDIT_LOG.read_text().splitlines()]
    entries[0]["steps"] = entries[0]["steps"][1:]          # someone hides a step
    forged = tmp_path / "log.jsonl"
    forged.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    monkeypatch.setattr(audit, "AUDIT_LOG", forged)
    ok, msg = audit.verify_chain()
    assert not ok and "modified" in msg


def test_repeat_request_is_idempotent_and_logged(world):
    from gdpr.erase import erase
    r = erase(DEMO)
    assert r["status"] == "no_data_found"
    assert r["subject_ref"] == world["result"]["subject_ref"]
    assert audit.verify_chain()[0]


# --- must run last: simulates the CRM re-sending the erased customer ----------
def test_zz_reingest_is_blocked_by_suppression_list(world):
    from gdpr.dbt_runner import run_dbt
    from gdpr.discover import scan
    from pipeline.run import load_day

    (RAW_DIR / "crm" / "customers_2026-11-05.csv").write_text(world["crm_day5_original"])
    load_day("2026-11-05")
    run_dbt("snapshot")
    run_dbt("run")
    hits = [h for h in scan(world["subject"].identifiers(), include_backups=False) if h["type"] != "raw_file"]
    assert hits == []
