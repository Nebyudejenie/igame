-- DEV/TEST ONLY. Never run this against a production Postgres instance,
-- and never add an equivalent step to deploy/docker-compose.prod.yml.
--
-- Creates jobingo_dev_fixture -- a marker role with no login capability
-- and no privileges of its own -- and grants it to the jobingo login
-- role. migrations/versions/b8e4a1f0c3d7_ledger_append_only.py's
-- append-only trigger on ledger_entries/ledger_transactions only lets
-- SET LOCAL jobingo.allow_ledger_history_mutation = 'true' bypass it
-- when the connecting role is a member of jobingo_dev_fixture. A
-- production instance provisioned the normal way (that migration,
-- deploy/docker-compose.prod.yml, nothing else) never runs this file
-- and therefore never has this role -- the escape hatch is a structural
-- no-op there regardless of what GUC anyone sets. See that migration's
-- own module docstring for the full reasoning.
--
-- Mounted by deploy/docker-compose.yml (the dev compose file) into
-- /docker-entrypoint-initdb.d/, which Postgres's own image only runs
-- once, the first time a container starts against a genuinely empty
-- data directory. An already-initialized dev volume (the normal case --
-- this repo's own dev Postgres container has run for weeks across many
-- sessions) does not pick this up automatically; run it once by hand
-- against that volume instead (shown, and only after explicit approval,
-- in DECISIONS.md's 2026-09-17 entry) -- this file exists so a *future*
-- fresh dev volume (docker compose down -v && up) gets the role without
-- that manual step being tribal knowledge.

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'jobingo_dev_fixture') THEN
    CREATE ROLE jobingo_dev_fixture NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
  END IF;
END
$$;

GRANT jobingo_dev_fixture TO jobingo;
