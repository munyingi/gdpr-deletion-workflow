"""Command line interface: python -m gdpr <command>."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date


def _table(rows: list[dict], cols: list[str]) -> str:
    if not rows:
        return "  (none)"
    widths = {c: max(len(c), *(len(str(r.get(c, ""))) for r in rows)) for c in cols}
    line = "  " + "  ".join(c.ljust(widths[c]) for c in cols)
    sep = "  " + "  ".join("-" * widths[c] for c in cols)
    body = ["  " + "  ".join(str(r.get(c, "")).ljust(widths[c]) for c in cols) for r in rows]
    return "\n".join([line, sep, *body])


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="gdpr", description="GDPR erasure workflow")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("generate", help="generate fake raw files")
    sub.add_parser("pipeline", help="load raw files, build SCD2 snapshots and marts, take backups")
    d = sub.add_parser("discover", help="show where a customer's data lives (no changes)")
    d.add_argument("customer_id")
    e = sub.add_parser("erase", help="erase a customer")
    e.add_argument("customer_id")
    e.add_argument("--dry-run", action="store_true")
    e.add_argument("--received-on", type=date.fromisoformat, help="date the request arrived (YYYY-MM-DD)")
    e.add_argument("--restore-drill", action="store_true", help="also restore the latest backup and prove suppression")
    e.add_argument("--json", action="store_true", help="print the full audit entry")
    a = sub.add_parser("audit", help="show the audit log")
    a.add_argument("--verify", action="store_true", help="verify the hash chain")
    args = p.parse_args(argv)

    if args.cmd == "generate":
        from pipeline.generate import generate
        generate()
        print("raw files generated")
    elif args.cmd == "pipeline":
        from pipeline.run import run_full
        run_full()
        print("pipeline complete: warehouse built, 2 backups taken")
    elif args.cmd == "discover":
        from gdpr.discover import classify, collect_subject, scan
        s = collect_subject(args.customer_id.upper())
        if s.is_empty():
            print(f"No data found for {args.customer_id}")
            return 0
        print(f"Identifiers found (values hidden): {s.summary()}")
        hits = classify(scan(s.identifiers()))
        live = [h for h in hits if h["type"] != "backup"]
        print(_table(live, ["type", "location", "column", "rows", "handled_by"]))
        backups: dict[str, int] = {}
        for h in hits:
            if h["type"] == "backup":
                name = h["location"].split("/")[1]
                backups[name] = backups.get(name, 0) + 1
        for name, n in backups.items():
            print(f"  backup  {name}: {n} column hits across files (immutable; suppression list + retention expiry)")
        unmapped = [h for h in hits if h["handled_by"] == "UNMAPPED"]
        if unmapped:
            print(f"\nWARNING: {len(unmapped)} location(s) hold this person's data but are not in the data map")
    elif args.cmd == "erase":
        from gdpr.erase import erase
        r = erase(args.customer_id, dry_run=args.dry_run, received_on=args.received_on,
                  restore_drill=args.restore_drill)
        if args.json:
            print(json.dumps(r, indent=2, ensure_ascii=False))
        else:
            print(f"request {r['request_id']}  status={r['status']}")
            print(_table(r["steps"], ["system", "location", "action", "rows_matched", "rows_redacted"]))
            v = r["verification"]
            print(f"\nPII hits outside backups after erasure: {v['pii_hits_outside_backups']}")
            for b in r["backups"]:
                print(f"backup {b['backup']}: {b['rows']} rows, {b['treatment']}, expires {b['expires_on']}")
            if v["restore_drill"]:
                print(f"restore drill: {v['restore_drill']}")
        return 0 if r["status"] in ("completed", "dry_run", "no_data_found") else 1
    elif args.cmd == "audit":
        from gdpr.audit import read_entries, verify_chain
        if args.verify:
            ok, msg = verify_chain()
            print(("OK: " if ok else "TAMPERED: ") + msg)
            return 0 if ok else 1
        rows = [{"completed_at": e["completed_at"], "request_id": e["request_id"][:8],
                 "subject_ref": e["subject_ref"][:12] + "…", "status": e["status"],
                 "steps": len(e["steps"]), "hits_after": e["verification"]["pii_hits_outside_backups"]}
                for e in read_entries()]
        print(_table(rows, ["completed_at", "request_id", "subject_ref", "status", "steps", "hits_after"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
