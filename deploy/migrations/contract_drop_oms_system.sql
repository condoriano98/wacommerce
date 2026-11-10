-- CONTRACT step of the DB-role cutover (ADR-014). NOT wired into Alembic and NOT applied by default.
-- Apply only in a later release, after: (1) every deployed API/worker runs with OMS_DB_ROLE_MODE=v31,
-- (2) the v31 isolation suite passed in staging, (3) explicit approval. Rollback: re-create the role from 0002.
begin;
set local lock_timeout = '5s';
-- refuse if any session still uses the legacy role
do $$ begin
  if exists (select 1 from pg_stat_activity where usename = 'oms_system') then
    raise exception 'oms_system still has active sessions; abort contract step';
  end if;
end $$;
alter role oms_system nobypassrls nologin;
revoke all on all tables in schema public from oms_system;
revoke all on all sequences in schema public from oms_system;
revoke all on all functions in schema public from oms_system;
drop owned by oms_system;
drop role oms_system;
commit;
