### The seven steps of one request

1. **Collect identifiers.** Starting from the customer ID, pull every email, phone, name and address the person has *ever* had: from every daily CRM extract, every order row and every SCD2 version. The current CRM record is not enough, because old values only survive in history.
2. **Scan everything.** Search raw files, every warehouse table and view, and every backup file for those values. The scan does not trust the data map, so undocumented copies (a family member's support ticket, a materialised mart) still show up. Each hit is classified against `config/pii_map.yml`, and anything outside the map is flagged `UNMAPPED`.
3. **Erase sources.** Raw files are rewritten atomically. Warehouse rows are deleted or anonymised in one transaction. Free text is redacted.
4. **Anonymise SCD2 history.** Every version is kept, but personal columns are nulled, the natural key becomes a random `ERASED-…` token (the mapping is never stored) and `dbt_scd_id`, an md5 of the old key, is recomputed.
5. **Rebuild derived models** with `dbt run`, so staging views and marts are regenerated from cleaned sources.
6. **Suppress.** Keyed HMACs of the identifiers go on a suppression list. It is applied on every backup restore and every nightly load, so neither an old backup nor a CRM re-send brings the person back.
7. **Verify and log.** Re-scan. Anything left outside immutable backups makes the request `incomplete`. The audit entry records locations, actions, row counts, legal basis for anything retained, backup expiry dates and the verification result, all without personal data, and is chained to the previous entry by SHA-256.

### Delete or anonymise?

| Data | Treatment | Why |
|---|---|---|
| CRM customer records | delete | no reason to keep |
| Web events | delete | no reason to keep |
| Support tickets from the person | delete | no reason to keep |
| Mentions in other people's tickets | redact in place | the ticket belongs to someone else |
| Orders | anonymise | bookkeeping records have a statutory retention period |
| SCD2 history | anonymise every version | keeps historical segment counts correct |
| Backups | suppress on restore, expire after 35 days | backups are immutable |
