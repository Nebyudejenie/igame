"""ledger_entries / ledger_transactions append-only

A real incident during Keno development (this session, 2026-09-17) found
the cached-balance-vs-ledger-truth invariant can be broken by direct
DELETE against ledger_entries -- even done with good intentions
(cleaning up test data), deleting an entry silently desyncs
account_balances (the cache) from ledger_entries (the source of truth)
for the *other* leg of that same transaction, since nothing recomputes
the cache when history is deleted out from under it. The mutation that
caused it was a bare, ad-hoc shell command with no application code or
test fixture around it at all.

This closes that hole the way admin_audit_log already closes an
analogous one (migrations/versions/1c85c3d09653_admin_console.py): a
`BEFORE UPDATE OR DELETE` trigger, so the invariant doesn't depend on
every future call site -- including an engineer's own shell command --
getting it right by convention alone.

## The escape hatch, and why it turned out to be unused

Grepping the whole repo for existing UPDATE/DELETE against these two
tables initially found five call sites presented as needing an
exception. On inspection, all five were fixable and were fixed instead
of exempted:

- Two timezone-boundary tests (tests/integration/test_admin_queries.py,
  tests/integration/test_responsible_gaming.py) were inserting an entry
  at now() and then UPDATE-ing its created_at to a specific historical
  instant, to test that daily/loss-cap aggregation queries bucket
  correctly by Ethiopia calendar day. Fixed by giving
  packages/core/ledger.py's post() an optional `created_at` parameter --
  every other caller is unaffected (defaults to the column's own
  DEFAULT now() via COALESCE at the SQL level), and these two tests now
  pass the desired timestamp at INSERT time, which was never blocked by
  this trigger in the first place.
- Three simulated-players test-fixture teardown helpers
  (tests/integration/test_simulated_players.py,
  test_simulated_players_console_e2e.py,
  test_seed_simulated_players_cli.py) were fully unwinding a test bot's
  ledger_entries/account_balances/accounts/users rows on teardown. On
  inspection, services/admin/simulated_players_queries.py's 10-bot
  roster cap is enforced by a bare `SELECT count(*) FROM
  simulated_players` with no join to any of those other tables, no test
  in any of the three files counts users/accounts globally, users.
  display_name has no uniqueness constraint, and new bots get fresh
  negative telegram_ids via MIN(telegram_id)-1 regardless of what's left
  over. None of the ledger-adjacent deletes were ever load-bearing --
  fixed by deleting only the simulated_players row (the one real
  constraint) and leaving the rest as the same harmless orphaned test
  data every other test file in this suite already accumulates.

Zero application code and zero test code needs an exception as of this
migration. The escape hatch below is kept anyway, for a genuine future
need this session can't rule out -- but per the operator's own review of
this incident, "not used in production" was judged insufficient: a
hatch that merely goes unused is still a hatch someone could reach for.

## The escape hatch is structurally inert outside a deliberately
## provisioned dev/test instance, not just conventionally unused

`SET LOCAL jobingo.allow_ledger_history_mutation = 'true'` alone is NOT
sufficient to bypass the trigger. The trigger also requires the
connecting role to be a member of a role named `jobingo_dev_fixture`,
which:

  - is never created by this migration, or by any migration, or by any
    code path that runs in every environment (which is the whole
    problem a GUC-only gate has -- a GUC can be set by anyone with a
    connection, in any environment, including production, since
    `SET LOCAL` needs no elevated privilege);
  - is only ever created by deploy/postgres-dev-init/01-dev-fixture-role.sql,
    a Postgres init script mounted ONLY by deploy/docker-compose.yml
    (the dev compose file) -- never by deploy/docker-compose.prod.yml,
    and never by this or any other migration in this directory.

A production Postgres instance provisioned the normal way (this
migration chain, docker-compose.prod.yml, nothing else) therefore never
has a `jobingo_dev_fixture` role at all -- `SET LOCAL
jobingo.allow_ledger_history_mutation = 'true'` there is a no-op, full
stop, regardless of who sets it or what role they connect as (including
a role that happens to also be named `jobingo`, since dev and prod may
share that login-role name -- the gate is the existence of the
*separate* marker role, not the login role's name). The only way the
hatch could ever work in production is for someone to run
deploy/postgres-dev-init/01-dev-fixture-role.sql directly against it --
a deliberate, reviewable action of exactly the kind this whole incident
response exists to require, never an accidental side effect of ordinary
deployment.

account_balances is deliberately NOT covered by this trigger: it is a
cache, updated in place by ledger.post() on every transaction by design
(`balance = balance + delta`) -- that mutability is not the hole this
closes. What must never be mutated after the fact without the (in
production, inert) escape hatch is the *history* (ledger_entries, and
the ledger_transactions rows that key it).

Revision ID: b8e4a1f0c3d7
Revises: a3f7c2e91b04
Create Date: 2026-09-17

"""
from typing import Sequence, Union

from alembic import op


revision: str = "b8e4a1f0c3d7"
down_revision: Union[str, None] = "a3f7c2e91b04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_ledger_history_mutation() RETURNS trigger AS $$
        BEGIN
          -- Both conditions required: the GUC alone proves nothing (any
          -- connection, anywhere, can SET LOCAL a session variable) --
          -- the jobingo_dev_fixture role's mere existence is the real
          -- gate, and it exists only where deploy/postgres-dev-init/
          -- 01-dev-fixture-role.sql was deliberately run (dev/test only,
          -- see this migration's own module docstring).
          IF current_setting('jobingo.allow_ledger_history_mutation', true) = 'true'
             AND EXISTS (
               SELECT 1 FROM pg_roles r
               WHERE r.rolname = 'jobingo_dev_fixture'
                 AND pg_has_role(current_user, r.oid, 'MEMBER')
             )
          THEN
            RETURN COALESCE(NEW, OLD);
          END IF;
          RAISE EXCEPTION
            '% is append-only; % on % is not allowed -- post a reversing ledger.post() entry instead. '
            'No production instance can bypass this (see this migration''s own docstring); '
            'in a dev/test instance with deploy/postgres-dev-init/01-dev-fixture-role.sql applied, '
            'SET LOCAL jobingo.allow_ledger_history_mutation = ''true'' as a jobingo_dev_fixture member.',
            TG_TABLE_NAME, TG_OP, TG_TABLE_NAME
            USING ERRCODE = '23514';
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_ledger_entries_immutable
          BEFORE UPDATE OR DELETE ON ledger_entries
          FOR EACH ROW
          EXECUTE FUNCTION prevent_ledger_history_mutation();

        CREATE TRIGGER trg_ledger_transactions_immutable
          BEFORE UPDATE OR DELETE ON ledger_transactions
          FOR EACH ROW
          EXECUTE FUNCTION prevent_ledger_history_mutation();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_ledger_transactions_immutable ON ledger_transactions")
    op.execute("DROP TRIGGER trg_ledger_entries_immutable ON ledger_entries")
    op.execute("DROP FUNCTION prevent_ledger_history_mutation")
