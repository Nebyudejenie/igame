"""keno tier changes: a real, queryable audit trail for every tier change

Part 7.2's own "All tier changes audited, exposed as a metric, alerted"
has no home to write to for a system-driven change: admin_audit_log's
own admin_id is NOT NULL REFERENCES admin_users(id) -- structurally
admin-actor-only, correctly so (it answers "which admin did this"), and
an automated promotion/demotion/circuit-breaker trip has no admin actor
to attribute it to. This table is the durable record for every tier
change regardless of actor -- automated or a real admin override
(services/admin/keno_queries.py::set_current_tier_admin() now writes
here too, in addition to its existing admin_audit_log row, so this
becomes the single place to see full tier history either way).

Revision ID: d7a2f5c8e1b4
Revises: c4e8f1a9b6d3
Create Date: 2026-09-22

"""
from typing import Sequence, Union

from alembic import op

revision: str = "d7a2f5c8e1b4"
down_revision: Union[str, None] = "c4e8f1a9b6d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE keno_tier_changes (
          id                         bigserial PRIMARY KEY,
          from_tier_id               bigint REFERENCES keno_risk_tiers(id),
          to_tier_id                 bigint NOT NULL REFERENCES keno_risk_tiers(id),
          trigger                    text NOT NULL CHECK (
                                       trigger IN ('automated_promotion', 'automated_demotion',
                                                   'circuit_breaker', 'admin_override')
                                     ),
          reason                     text NOT NULL,
          reserve_balance_at_change  numeric(18,2),
          admin_id                   bigint REFERENCES admin_users(id),
          created_at                 timestamptz NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_keno_tier_changes_created ON keno_tier_changes (created_at DESC);
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE keno_tier_changes")
