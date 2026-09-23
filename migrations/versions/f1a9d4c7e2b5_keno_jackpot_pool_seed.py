"""keno jackpot pool seed row

Real bug found while writing docs/keno/07-economics-and-bankroll.md and
checking production state directly: keno_jackpot_pool (the singleton
metadata row -- cap_amount, trigger_description, seed_amount; the real
money balance lives separately, in the ledger's own keno_jackpot_pool
account) was never seeded by any prior migration. services/engine/
keno_round_engine.py::_pay_jackpot() handles a missing row gracefully
(`if pool_row is None: return Decimal(0)`) -- so nothing crashes -- but
that also means the jackpot can structurally never be paid out, even
after real contributions have grown the ledger account's own balance,
until this row exists. Contribution isn't blocked (it posts through
ledger.get_or_create_account, independent of this table), only payout
is -- so this would have been a silent trap: the pool visibly grows,
someone eventually hits 5-for-5, and the payout quietly no-ops.

Seeds only account_id (required, no default) and leaves every other
column at its own table default: cap_amount stays NULL (uncapped --
the safe, conservative choice, same reasoning as
e9c3b7f2a5d8's reserve_withdrawal_floor default of 0), seed_amount
stays 0 (no extra operator seeding beyond ordinary stake diversions --
also a real money decision, not invented here), trigger_description
keeps its own already-correct default ('All picks matched on a 5-spot
ticket', matching keno.md Part 8.3). No money moves in this migration --
purely metadata, the same non-financial-default pattern already used
for the reserve withdrawal floor.

Revision ID: f1a9d4c7e2b5
Revises: e9c3b7f2a5d8
Create Date: 2026-09-23

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1a9d4c7e2b5"
down_revision: Union[str, None] = "e9c3b7f2a5d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Mirrors packages/core/ledger.py::get_or_create_account()'s own
    # exact pattern for a system (user_id IS NULL) account -- same
    # conflict target (ux_accounts_system_kind_currency), same
    # insert-then-select-on-conflict shape -- rather than inventing a
    # slightly different one here.
    conn = op.get_bind()
    account_id = conn.execute(
        sa.text(
            "INSERT INTO accounts (user_id, kind, currency) VALUES (NULL, 'keno_jackpot_pool', 'ETB') "
            "ON CONFLICT (kind, currency) WHERE user_id IS NULL DO NOTHING "
            "RETURNING id"
        )
    ).scalar()
    if account_id is None:
        account_id = conn.execute(
            sa.text(
                "SELECT id FROM accounts WHERE user_id IS NULL AND kind = 'keno_jackpot_pool' AND currency = 'ETB'"
            )
        ).scalar_one()
    conn.execute(
        sa.text("INSERT INTO keno_jackpot_pool (id, account_id) VALUES (1, :account_id) ON CONFLICT (id) DO NOTHING"),
        {"account_id": account_id},
    )


def downgrade() -> None:
    op.execute("DELETE FROM keno_jackpot_pool WHERE id = 1")
