"""keno beta allowlist: gate both visibility and betting during a staged launch

A real gap found by a CTO review before go-live, 2026-09-23:
GET /api/keno/state (services/gateway/app.py::api_keno_state ->
packages.core.keno_queries.game_center_state) returns live round state
to *any* authenticated user the instant a single keno_rounds row has
ever existed -- it never checked keno_enabled at all. The Mini App's
own checkKenoAvailability() then trusted "did this request not error"
as its entire signal for showing the Keno button. Once the engine
worker started creating real rounds in production, the button (and the
live round/jackpot state behind it) was effectively visible to every
real user regardless of keno_enabled -- only place_ticket() was
actually gated. No money was ever at risk (place_ticket() does check
keno_enabled), but "fully gated off from real players" was not true.

This migration adds what a staged launch (spec-adjacent: internal
allowlist -> small real cohort -> general availability) needs on top of
the existing keno_enabled kill switch:

- keno_configs.beta_restricted: a new insert-only-versioned setting,
  same pinning-per-round behavior as every other keno_configs column
  (an admin flipping it mid-round never alters an in-flight round).
  Defaults true (safe-by-default: until an admin explicitly creates a
  config version with it set false, Keno stays allowlist-gated even if
  keno_enabled is somehow flipped true) -- same reasoning
  e9c3b7f2a5d8's reserve_withdrawal_floor default and
  f1a9d4c7e2b5's uncapped jackpot both already used.
- keno_beta_allowlist: current membership only (not itself versioned
  history -- every add/remove is audited in the existing
  admin_audit_log, same as every other keno:configure action; this
  table only needs to answer "is this user currently allowed," not
  "who was allowed on some past date").

Revision ID: a7c3e9f2d146
Revises: f1a9d4c7e2b5
Create Date: 2026-09-23

"""
from typing import Sequence, Union

from alembic import op

revision: str = "a7c3e9f2d146"
down_revision: Union[str, None] = "f1a9d4c7e2b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE keno_configs ADD COLUMN beta_restricted boolean NOT NULL DEFAULT true")
    op.execute(
        """
        CREATE TABLE keno_beta_allowlist (
          user_id            bigint PRIMARY KEY REFERENCES users(id),
          added_by_admin_id  bigint NOT NULL REFERENCES admin_users(id),
          reason             text NOT NULL,
          created_at         timestamptz NOT NULL DEFAULT now()
        );
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE keno_beta_allowlist")
    op.execute("ALTER TABLE keno_configs DROP COLUMN beta_restricted")
