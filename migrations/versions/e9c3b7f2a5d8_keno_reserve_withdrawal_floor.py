"""keno_configs.reserve_withdrawal_floor -- Part 7.1's own "the admin
cannot withdraw below a configurable reserve floor. Attempts are blocked
and audited." No column existed anywhere for this. Default 0: the only
value defensible without a real operator decision (an admin simply
cannot withdraw the reserve into negative territory) -- the spec calls
this "configurable," not a specific number, so a higher floor is a real
business-risk-tolerance decision for later, raised via an ordinary new
keno_configs row (this column, like every other keno_configs field, is
insert-only versioned) once decided, not invented here.

Revision ID: e9c3b7f2a5d8
Revises: d7a2f5c8e1b4
Create Date: 2026-09-22

"""
from typing import Sequence, Union

from alembic import op

revision: str = "e9c3b7f2a5d8"
down_revision: Union[str, None] = "d7a2f5c8e1b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE keno_configs ADD COLUMN reserve_withdrawal_floor numeric(18,2) "
        "NOT NULL DEFAULT 0 CHECK (reserve_withdrawal_floor >= 0)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE keno_configs DROP COLUMN reserve_withdrawal_floor")
