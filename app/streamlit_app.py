"""Streamlit front end for the GDPR erasure workflow."""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import duckdb  # noqa: E402
import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from gdpr import audit  # noqa: E402
from gdpr.config import AUDIT_LOG, BACKUP_DIR, WAREHOUSE  # noqa: E402
from gdpr.discover import classify, collect_subject, scan  # noqa: E402
from gdpr.erase import erase  # noqa: E402
from pipeline.run import reset_demo  # noqa: E402

st.set_page_config(page_title="GDPR erasure workflow", page_icon="🧹", layout="wide")


def mask(value: object) -> object:
    if not isinstance(value, str) or not value:
        return value
    if "@" in value:
        user, dom = value.split("@", 1)
        return f"{user[0]}***@{dom}"
    if value.startswith("+358"):
        return value[:7] + " *** ****"
    if value.startswith("ERASED-"):
        return value
    return value[0] + "*" * min(len(value) - 1, 8)


def query(sql: str, params: list | None = None) -> pd.DataFrame:
    con = duckdb.connect(str(WAREHOUSE), read_only=True)
    try:
        return con.execute(sql, params or []).df()
    finally:
        con.close()


# ---- sidebar -----------------------------------------------------------------
with st.sidebar:
    st.header("Demo environment")
    if WAREHOUSE.exists():
        stats = query("select (select count(distinct customer_id) from raw.customers) as customers, "
                      "(select count(*) from snapshots.customers_snapshot) as scd2_rows, "
                      "(select count(*) from raw.orders) as orders")
        c1, c2 = st.columns(2)
        c1.metric("Customers", int(stats.customers[0]))
        c2.metric("SCD2 rows", int(stats.scd2_rows[0]))
        st.caption(f"{int(stats.orders[0])} orders · "
                   f"{len(list(BACKUP_DIR.glob('warehouse_*')))} nightly backups · DuckDB + dbt")
    else:
        st.warning("No warehouse yet. Build the demo data first.")
    if st.button("Reset demo data", help="Regenerate fake data, rebuild the warehouse and backups, clear the audit log"):
        with st.spinner("Generating 5 days of data, running dbt snapshots, taking backups..."):
            reset_demo()
        st.session_state.clear()
        st.rerun()
    st.divider()
    st.caption("All data is fake (Faker, fi_FI). Try customer **C00042**: changed email and "
               "address mid-week, has orders, tickets, and is mentioned in a family member's ticket.")

st.title("GDPR deletion requests")
st.caption("A customer writes *“delete all my data”*. Find every copy, erase or anonymise it, "
           "and prove it, without the proof itself holding personal data.")

if not WAREHOUSE.exists():
    st.stop()

tab_erase, tab_history, tab_audit, tab_how = st.tabs(
    ["1 · Discover & erase", "2 · SCD2 history", "3 · Audit log", "How it works"])

# ---- discover & erase --------------------------------------------------------
with tab_erase:
    cid = st.text_input("Customer ID from the request", value="C00042").strip().upper()
    if st.button("Discover", type="primary"):
        with st.spinner("Collecting identifiers and scanning raw files, warehouse and backups..."):
            subject = collect_subject(cid)
            st.session_state["discovery"] = (cid, subject.summary(), classify(scan(subject.identifiers()))
                                             if not subject.is_empty() else [])

    if "discovery" in st.session_state and st.session_state["discovery"][0] == cid:
        _, summary, hits = st.session_state["discovery"]
        if not hits:
            st.info(f"No personal data found for {cid}.")
        else:
            st.subheader("Where this person's data lives")
            m = st.columns(4)
            for col, (k, v) in zip(m, summary.items()):
                col.metric(f"Distinct {k}", v)
            st.caption("Values are never displayed. Counts above include identifiers that only "
                       "survive in SCD2 history and old extracts.")
            df = pd.DataFrame(hits)
            live = df[df.type != "backup"]
            grouped = (live.groupby(["type", "location", "handled_by"], as_index=False)
                       .agg(columns=("column", lambda c: ", ".join(c)), rows=("rows", "max")))
            st.dataframe(grouped, width="stretch", hide_index=True)
            unmapped = grouped[grouped.handled_by == "UNMAPPED"]
            if len(unmapped):
                st.error(f"{len(unmapped)} location(s) are not in the data map")
            b = df[df.type == "backup"].copy()
            if len(b):
                b["backup"] = b.location.str.split("/").str[1]
                st.caption("Immutable backups: " + ", ".join(
                    f"**{n}** ({g.location.nunique()} files)" for n, g in b.groupby("backup")))

            st.subheader("Execute erasure")
            c1, c2 = st.columns(2)
            received = c1.date_input("Request received on", value=date.today())
            drill = c2.checkbox("Run a backup restore drill afterwards", value=True)
            verified = st.checkbox("I have verified the requester's identity")
            go = st.button("Erase / anonymise", type="primary", disabled=not verified)
            if go:
                with st.spinner("Rewriting raw files, anonymising SCD2 history, rebuilding dbt models..."):
                    st.session_state["result"] = erase(cid, received_on=received, restore_drill=drill)
                st.session_state.pop("discovery")
                st.rerun()

    if "result" in st.session_state:
        r = st.session_state["result"]
        v = r["verification"]
        st.success(f"Request {r['request_id'][:8]} · status **{r['status']}**")
        m = st.columns(4)
        m[0].metric("Locations changed", len(r["steps"]))
        m[1].metric("PII hits left (live)", v["pii_hits_outside_backups"])
        m[2].metric("Suppression hashes added", r["suppression_hashes_added"])
        if v["restore_drill"]:
            m[3].metric("PII after restore drill", v["restore_drill"]["pii_hits_after_restore"])
        st.dataframe(pd.DataFrame(r["steps"]), width="stretch", hide_index=True)
        if r["backups"]:
            st.dataframe(pd.DataFrame(r["backups"]), width="stretch", hide_index=True)
        with st.expander("Audit entry written (contains no personal data)"):
            st.json(r)

# ---- SCD2 history ------------------------------------------------------------
with tab_history:
    st.markdown("SCD Type 2 keeps every past version of a customer. That is the point of it, and "
                "also why erasure is hard: old emails and addresses live on here after the CRM forgets them. "
                "Erasure anonymises every version in place, so segment history and point-in-time joins "
                "still work but nothing points back to the person.")
    who = st.text_input("Customer ID or ERASED- token", value="C00042", key="hist").strip()
    df = query("select customer_id, first_name, last_name, email, phone, street_address, city, segment, "
               "marketing_opt_in, dbt_valid_from, dbt_valid_to, dbt_scd_id "
               "from snapshots.customers_snapshot where customer_id = ? order by dbt_valid_from", [who])
    if df.empty:
        tokens = query("select distinct customer_id from snapshots.customers_snapshot "
                       "where customer_id like 'ERASED-%'")
        st.info("No versions for that key." + (" Erased subjects now appear as: " +
                ", ".join(tokens.customer_id) if len(tokens) else ""))
    else:
        for c in ["first_name", "last_name", "email", "phone", "street_address", "city"]:
            df[c] = df[c].map(mask)
        st.dataframe(df, width="stretch", hide_index=True)
        st.caption("Personal values are masked in this view.")

# ---- audit log ---------------------------------------------------------------
with tab_audit:
    entries = audit.read_entries()
    ok, msg = audit.verify_chain()
    (st.success if ok else st.error)(f"Hash chain: {msg}")
    if entries:
        st.dataframe(pd.DataFrame([{
            "completed_at": e["completed_at"], "request_id": e["request_id"],
            "subject_ref (HMAC)": e["subject_ref"][:16] + "…", "status": e["status"],
            "received_on": e["request"]["received_on"], "due_by": e["request"]["due_by"],
            "locations_changed": len(e["steps"]),
            "pii_left": e["verification"]["pii_hits_outside_backups"],
            "entry_hash": e["entry_hash"][:16] + "…"} for e in entries]),
            width="stretch", hide_index=True)
        st.download_button("Download audit log (JSONL)", AUDIT_LOG.read_text(), "erasure_log.jsonl")
    else:
        st.info("No requests processed yet.")

# ---- how it works ------------------------------------------------------------
with tab_how:
    st.markdown((ROOT / "docs" / "how_it_works.md").read_text())
