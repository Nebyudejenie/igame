"""widen simulated players cap to 200

Revision ID: b4d8f2e91a3c
Revises: a1c9e7f4d2b6
Create Date: 2026-09-18 07:20:00.000000

Raises the simulated-player roster cap from the original spec's "up to 10"
to 200, per explicit operator request. This is a real product-scale
decision, not a bug fix: 200 bots each funded at the existing
INITIAL_SIMULATED_BALANCE (services/admin/simulated_players_queries.py)
is a real, if fully reversible, house_float-backed ledger movement 20x
larger than before, and 200 concurrent bots can fill multiple rooms
(max_players=100 each) well past what any single room needs to "not feel
empty" -- both are intentional, operator-accepted consequences of this
change, not something this migration itself needs to guard against.

Only the DB-level CHECK constraint changes here; the application-level
cap (services/admin/simulated_players_queries.py::MAX_SIMULATED_PLAYERS)
is a separate, matching Python-side edit -- both must move together or
the two enforcement layers would disagree.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'b4d8f2e91a3c'
down_revision: Union[str, None] = 'a1c9e7f4d2b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OLD_MAX = 10
NEW_MAX = 200


def upgrade() -> None:
    op.execute(
        "ALTER TABLE simulated_players_settings "
        "DROP CONSTRAINT simulated_players_settings_max_concurrent_bots_check"
    )
    op.execute(
        f"ALTER TABLE simulated_players_settings "
        f"ADD CONSTRAINT simulated_players_settings_max_concurrent_bots_check "
        f"CHECK (max_concurrent_bots BETWEEN 0 AND {NEW_MAX})"
    )


def downgrade() -> None:
    # A real value already set above OLD_MAX would violate the narrowed
    # constraint on downgrade -- clamp defensively rather than let this
    # fail loudly on a downgrade nobody's actively doing right now.
    op.execute(
        f"UPDATE simulated_players_settings SET max_concurrent_bots = {OLD_MAX} "
        f"WHERE max_concurrent_bots > {OLD_MAX}"
    )
    op.execute(
        "ALTER TABLE simulated_players_settings "
        "DROP CONSTRAINT simulated_players_settings_max_concurrent_bots_check"
    )
    op.execute(
        f"ALTER TABLE simulated_players_settings "
        f"ADD CONSTRAINT simulated_players_settings_max_concurrent_bots_check "
        f"CHECK (max_concurrent_bots BETWEEN 0 AND {OLD_MAX})"
    )
