import os
import sys
import tempfile
from pathlib import Path

# Isolated data dir and key per test session, set before any project import.
_TMP = Path(tempfile.mkdtemp(prefix="gdpr_test_"))
os.environ["GDPR_DATA_DIR"] = str(_TMP / "data")
os.environ["GDPR_KEY_FILE"] = str(_TMP / "key")
os.environ["DUCKDB_PATH"] = str(_TMP / "data" / "warehouse" / "warehouse.duckdb")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import duckdb  # noqa: E402
import pytest  # noqa: E402

from gdpr.config import RAW_DIR, WAREHOUSE  # noqa: E402

DEMO = "C00042"


@pytest.fixture(scope="session")
def world():
    from gdpr.discover import classify, collect_subject, scan
    from gdpr.erase import erase
    from pipeline.generate import generate
    from pipeline.run import run_full

    generate()
    run_full()
    subject = collect_subject(DEMO)
    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    before = {
        "revenue": con.execute("select sum(amount_eur) from marts.fct_orders").fetchone()[0],
        "orders": con.execute("select count(*) from marts.fct_orders").fetchone()[0],
        "snapshot_rows": con.execute("select count(*) from snapshots.customers_snapshot").fetchone()[0],
        "versions": con.execute("select count(*) from snapshots.customers_snapshot where customer_id=?",
                                [DEMO]).fetchone()[0],
        "other_customer": con.execute("select * from snapshots.customers_snapshot where customer_id='C00100' "
                                      "order by dbt_valid_from").fetchall(),
    }
    con.close()
    hits_before = classify(scan(subject.identifiers()))
    crm_day5 = (RAW_DIR / "crm" / "customers_2026-11-05.csv").read_text()
    result = erase(DEMO, restore_drill=True)
    return {"subject": subject, "before": before, "hits_before": hits_before,
            "result": result, "crm_day5_original": crm_day5}
