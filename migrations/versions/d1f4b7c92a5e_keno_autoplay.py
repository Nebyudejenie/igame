"""keno autoplay / multi-race

Adds one server-driven mechanism covering both "autoplay with stop-on-win
/stop-on-loss conditions" and "multi-race" (buy N future rounds at once)
-- the same underlying need once built server-side (spec-adjacent UX
research, 2026-09-21): a persisted session (picks + stake + a cap on
rounds and/or win/loss thresholds) that auto-places a real ticket via the
*existing* packages/core/keno_tickets.py::place_ticket() path every time
a round opens for betting. "Multi-race" is this with a fixed round count
and no stop-on-win/loss; "autoplay" is the general case -- one table, not
two competing mechanisms.

Debited per-round as each ticket actually places (never the full
rounds_total * stake upfront) -- no escrow, and an insufficient-balance
rejection partway through a sequence just stops the session cleanly
rather than needing refund logic for rounds that never happened.

keno_tickets.autoplay_session_id is nullable and purely informational
(which session, if any, placed this ticket) -- every constraint
place_ticket() already enforces for a manually-placed ticket applies
identically here, since this reuses that exact function rather than a
parallel insert path.

Revision ID: d1f4b7c92a5e
Revises: b8e4a1f0c3d7
Create Date: 2026-09-21

"""
from typing import Sequence, Union

from alembic import op


revision: str = "d1f4b7c92a5e"
down_revision: Union[str, None] = "b8e4a1f0c3d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_SESSION_STATUSES = ("active", "stopped", "exhausted", "failed")


def upgrade() -> None:
    op.execute(
        f"""
        CREATE TABLE keno_autoplay_sessions (
          id                     bigserial PRIMARY KEY,
          user_id                bigint NOT NULL REFERENCES users(id),
          picks                  integer[] NOT NULL,
          stake                  numeric(18,2) NOT NULL CHECK (stake > 0),
          -- NULL means "no fixed count, run until stopped or a
          -- stop-on-win/loss threshold fires" -- plain autoplay. A real
          -- integer here is "multi-race": buy exactly this many rounds.
          rounds_total           int CHECK (rounds_total IS NULL OR rounds_total > 0),
          rounds_placed          int NOT NULL DEFAULT 0 CHECK (rounds_placed >= 0),
          -- Net position stops the session the moment it's reached or
          -- crossed (>= for win, <= -this for loss) -- checked once a
          -- placed ticket actually settles, not at placement time, since
          -- the outcome isn't known until then. Either or both may be
          -- NULL ("no cap on this side").
          stop_on_win_amount     numeric(18,2) CHECK (stop_on_win_amount IS NULL OR stop_on_win_amount > 0),
          stop_on_loss_amount    numeric(18,2) CHECK (stop_on_loss_amount IS NULL OR stop_on_loss_amount > 0),
          net_position           numeric(18,2) NOT NULL DEFAULT 0,
          status                 text NOT NULL DEFAULT 'active' CHECK (status IN ({", ".join(f"'{s}'" for s in _SESSION_STATUSES)})),
          -- 'manual' | 'rounds_exhausted' | 'stop_on_win' | 'stop_on_loss'
          -- | 'ticket_rejected:<TicketRejected subclass .code>' -- always
          -- set together with status leaving 'active', never independently.
          stop_reason            text,
          last_round_id          bigint REFERENCES keno_rounds(id),
          created_at             timestamptz NOT NULL DEFAULT now(),
          updated_at             timestamptz NOT NULL DEFAULT now()
        );

        -- At most one active session per user -- a second "start
        -- autoplay" call while one is already running is a configuration
        -- error the API should reject outright, not silently queue a
        -- second concurrent sequence racing the first for the same
        -- user's balance and per-round caps.
        CREATE UNIQUE INDEX ux_keno_autoplay_one_active_per_user
          ON keno_autoplay_sessions (user_id) WHERE status = 'active';

        CREATE INDEX ix_keno_autoplay_sessions_status ON keno_autoplay_sessions (status) WHERE status = 'active';

        ALTER TABLE keno_tickets
          ADD COLUMN autoplay_session_id bigint REFERENCES keno_autoplay_sessions(id);
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE keno_tickets DROP COLUMN autoplay_session_id;
        DROP TABLE keno_autoplay_sessions;
        """
    )
