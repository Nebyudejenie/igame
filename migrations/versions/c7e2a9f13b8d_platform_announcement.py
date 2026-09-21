"""platform announcement

Revision ID: c7e2a9f13b8d
Revises: b4d8f2e91a3c
Create Date: 2026-09-18 10:00:00.000000

A single, admin-configurable announcement/ad banner shown as a scrolling
marquee in the Mini App -- initially in place of the spectate screen's
"Reserve a card" button, which turned out to accomplish nothing real
(join() can't assign a card mid-round; the player is already moved into
the next round automatically) and was replaced with this: something an
admin can actually put content into.

Same true-singleton shape as payment_provider_availability
(60dc29201d1c_manual_payments.py) and simulated_players_settings
(a1c9e7f4d2b6_simulated_players.py) -- id is always 1, enforced by a
CHECK constraint, not a separate feature-flag table for a single row.
enabled defaults to false: no announcement appears until an admin
explicitly writes one and turns it on, the same "never activate on
deploy alone" guarantee this codebase already applies to every other
admin-toggled feature.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c7e2a9f13b8d'
down_revision: Union[str, None] = 'b4d8f2e91a3c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE platform_announcement (
          id                   smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
          text                 text NOT NULL DEFAULT '',
          enabled              boolean NOT NULL DEFAULT false,
          updated_by_admin_id  bigint REFERENCES admin_users(id),
          updated_at           timestamptz NOT NULL DEFAULT now()
        );
        """
    )
    op.execute("INSERT INTO platform_announcement (id) VALUES (1)")


def downgrade() -> None:
    op.execute("DROP TABLE platform_announcement")
