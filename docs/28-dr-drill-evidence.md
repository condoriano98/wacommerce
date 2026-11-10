# 28 — DR drill evidence (local, PRELIMINARY)

> This is **preliminary evidence only**. Gate G-OPS1 (Sumopod managed PostgreSQL backup/PITR, measured restore against
> RPO ≤ 15 min / RTO ≤ 4 h) remains **open**. The M5 recovery gate cannot be signed off from this drill.

## What was run
`deploy/backup/dr_drill.sh` on 2026-11-10 against the local test database `oms_test` (PostgreSQL 16, data produced
by the full test suite: 167 tests). Steps: logical backup (`pg_dump -Fc` + `pg_dumpall --roles-only`) → a write after
the backup (marker row) → restore into a **new, isolated cluster** (separate `initdb`, port, data directory) →
verification (row counts of 11 critical tables, balanced journals, migration head, forced RLS) → timestamps.

## Measured result (verbatim script output)
```json
{"drill": "local-logical-backup", "source_db": "oms_test", "backup_start": "2026-11-10T06:55:46.157Z", "backup_end": "2026-11-10T06:55:46.429Z",
 "failure_simulated_at": "2026-11-10T06:55:46.848Z", "restore_start": "2026-11-10T06:55:46.849Z", "verified_at": "2026-11-10T06:55:50.149Z",
 "rto_seconds": 3.3, "rpo_seconds_measured": 0.691,
 "post_backup_write_lost": true, "row_counts_match": true,
 "unbalanced_journals": 0, "schema_head": "0017", "tables_with_forced_rls": 96, "mismatches": "",
 "dump_bytes": 1771836}
```

## Interpretation
- Restore correctness: row counts match for all critical tables, 0 unbalanced journals, schema head `0017`, 96 tables
  with FORCE RLS after restore, roles restored, credentials table restored (ciphertext only).
- **RTO 3.3 s is not representative**: the dataset is ~1.7 MB. Production RTO must be measured on a production-sized
  copy (restore throughput × size + verification + DNS/app cut-over).
- **RPO**: the write made after the backup was lost, as expected. With logical backups only, RPO equals the backup
  interval (daily → up to 24 h), which **does not meet the 15-minute target**. Meeting it requires continuous WAL
  archiving / PITR (Sumopod managed feature — unverified, G-OPS1) or self-managed `pg_basebackup` + WAL archiving
  (template in `deploy/backup/README.md`, not exercised here).

## Still required for sign-off
1. Confirm Sumopod PITR availability, retention and restore procedure (G-OPS1).
2. Restore a production-sized copy into an isolated environment; measure RTO end-to-end; verify with
   `/ops/invariants/check` and the readiness probe; record timestamps here.
3. Independent review of this document.

## Re-run after the remediation (2026-11-10, schema head 0024)

```json
{"drill": "local-logical-backup", "source_db": "oms_test", "backup_start": "2026-11-10T09:11:13.645Z",
 "backup_end": "2026-11-10T09:11:13.993Z", "failure_simulated_at": "2026-11-10T09:11:14.413Z",
 "restore_start": "2026-11-10T09:11:14.415Z", "verified_at": "2026-11-10T09:11:17.670Z",
 "rto_seconds": 3.255, "rpo_seconds_measured": 0.768, "post_backup_write_lost": true, "row_counts_match": true,
 "unbalanced_journals": 0, "schema_head": "0024", "tables_with_forced_rls": 100, "mismatches": "",
 "post_restore": {"outbox_pending": 1, "outbox_dead": 0, "external_operations_sent_unconfirmed": 1,
                  "external_operations_uncertain": 5, "payouts_awaiting_bank_proof": 9, "legacy_duplicate_postings": 0},
 "dump_bytes": 2110976}
```

Same interpretation as above: a 2.1 MB local logical backup restored in 3.3 s proves the procedure and the
post-restore checks work; it says nothing about Sumopod PITR or a production-sized RTO. The `post_restore` counts are
test data (deliberately uncertain operations from the reliability tests) and show what an operator must reconcile
before workers resume. **G-OPS1 remains open.**
