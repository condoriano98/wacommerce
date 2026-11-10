# Backups (templates — nothing here is deployed)

- `dr_drill.sh`: local logical-backup restore drill (preliminary evidence, docs/28).
- Daily logical backup (off-site copy): `pg_dump -Fc "$DATABASE_URL" > oms-$(date -u +%F).dump` + `pg_dumpall --roles-only`.
- 15-minute RPO requires continuous WAL archiving / PITR. Preferred: Sumopod managed PITR (G-OPS1, unverified).
  Self-managed alternative (PostgreSQL 16): `archive_mode = on`, `archive_command = 'test ! -f /wal/%f && cp %p /wal/%f'`,
  weekly `pg_basebackup -D /base -Ft -z -X stream`, restore with `restore_command = 'cp /wal/%f %p'` and
  `recovery_target_time`. Not exercised in this repository.
- Credentials: backups contain encrypted credentials only; the master key lives in the secret store and must be backed
  up separately (losing it makes stored credentials unrecoverable — re-authorize providers).
