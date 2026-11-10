#!/usr/bin/env bash
# Local DR drill (M5, PRELIMINARY evidence only — not a substitute for the Sumopod PITR drill, gate G-OPS1).
# 1. logical backup of the source DB (pg_dump -Fc) + roles (pg_dumpall --roles-only)
# 2. a write AFTER the backup (simulates data at risk between backups)
# 3. restore into an ISOLATED new cluster (initdb, separate port and data dir)
# 4. verify: row counts per critical table, balanced journals, migration head, marker absent (measured data loss)
# 5. print timestamped measurements as JSON (RTO = restore start → verified; RPO = backup start → failure)
set -euo pipefail
SRC_DB="${SRC_DB:-oms_test}"; SRC_HOST="${SRC_HOST:-/tmp}"; SRC_PORT="${SRC_PORT:-5432}"
WORK="${WORK:-$(mktemp -d)}"; DRILL_PORT="${DRILL_PORT:-5544}"
PG="/usr/lib/postgresql/16/bin"
ts() { date -u +%Y-%m-%dT%H:%M:%S.%3NZ; }
q() { psql -h "$1" -p "$2" -U postgres -d "$3" -Atqc "$4"; }
chown postgres "$WORK"
CRIT="organizations ledger_entries orders order_items payments shipments reconciliation_items outbox_messages audit_logs account_credentials fpa_snapshots"

T_BACKUP_START=$(ts)
su postgres -c "$PG/pg_dumpall -h $SRC_HOST -p $SRC_PORT --roles-only > $WORK/roles.sql"
su postgres -c "$PG/pg_dump -h $SRC_HOST -p $SRC_PORT -Fc -f $WORK/$SRC_DB.dump $SRC_DB"
T_BACKUP_END=$(ts)
declare -A SRC_COUNTS
for t in $CRIT; do SRC_COUNTS[$t]=$(q $SRC_HOST $SRC_PORT $SRC_DB "select count(*) from $t"); done
SRC_HEAD=$(q $SRC_HOST $SRC_PORT $SRC_DB "select version_num from alembic_version")
# write after the backup: this is the data a logical-backup-only strategy loses
q $SRC_HOST $SRC_PORT $SRC_DB "insert into organizations (id, name, created_at, base_currency) values (gen_random_uuid(), 'dr-drill-marker-$T_BACKUP_END', now(), 'IDR')" >/dev/null 2>&1 || true
T_FAILURE=$(ts)

T_RESTORE_START=$(ts)
su postgres -c "$PG/initdb -D $WORK/data -A trust -U postgres >/dev/null"
su postgres -c "$PG/pg_ctl -D $WORK/data -o '-p $DRILL_PORT -k $WORK' -l $WORK/pg.log start -w >/dev/null"
trap 'su postgres -c "$PG/pg_ctl -D $WORK/data stop -m fast" >/dev/null 2>&1 || true' EXIT
su postgres -c "psql -h $WORK -p $DRILL_PORT -U postgres -d postgres -q -f $WORK/roles.sql" >/dev/null 2>&1 || true
su postgres -c "createdb -h $WORK -p $DRILL_PORT -U postgres $SRC_DB"
su postgres -c "$PG/pg_restore -h $WORK -p $DRILL_PORT -U postgres -d $SRC_DB --exit-on-error $WORK/$SRC_DB.dump"
OK=true; DETAILS=""
for t in $CRIT; do
  n=$(q $WORK $DRILL_PORT $SRC_DB "select count(*) from $t")
  [ "$n" = "${SRC_COUNTS[$t]}" ] || { OK=false; DETAILS="$DETAILS $t:$n!=${SRC_COUNTS[$t]}"; }
done
UNBAL=$(q $WORK $DRILL_PORT $SRC_DB "select count(*) from (select journal_id from ledger_entries group by journal_id having sum(debit) <> sum(credit)) x")
HEAD=$(q $WORK $DRILL_PORT $SRC_DB "select version_num from alembic_version")
MARKER=$(q $WORK $DRILL_PORT $SRC_DB "select count(*) from organizations where name like 'dr-drill-marker-%'")
RLS=$(q $WORK $DRILL_PORT $SRC_DB "select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace where n.nspname = 'public' and c.relkind = 'r' and c.relforcerowsecurity")
[ "$UNBAL" = "0" ] && [ "$HEAD" = "$SRC_HEAD" ] || OK=false
T_VERIFIED=$(ts)
secs() { python3 -c "from datetime import datetime as d;f=lambda s:d.fromisoformat(s.replace('Z','+00:00'));print(round((f('$2')-f('$1')).total_seconds(),3))"; }
cat <<JSON
{"drill": "local-logical-backup", "source_db": "$SRC_DB", "backup_start": "$T_BACKUP_START", "backup_end": "$T_BACKUP_END",
 "failure_simulated_at": "$T_FAILURE", "restore_start": "$T_RESTORE_START", "verified_at": "$T_VERIFIED",
 "rto_seconds": $(secs $T_RESTORE_START $T_VERIFIED), "rpo_seconds_measured": $(secs $T_BACKUP_START $T_FAILURE),
 "post_backup_write_lost": $([ "$MARKER" = "0" ] && echo true || echo false), "row_counts_match": $OK,
 "unbalanced_journals": $UNBAL, "schema_head": "$HEAD", "tables_with_forced_rls": $RLS, "mismatches": "${DETAILS# }",
 "dump_bytes": $(stat -c %s $WORK/$SRC_DB.dump)}
JSON
