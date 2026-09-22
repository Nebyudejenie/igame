"""rebrand sms default tenant to zemen game

The 'default' sms_tenants row's own display name (INSERT INTO sms_tenants
(slug, name) VALUES ('default', 'Arada Bingo'), migrations/versions/
a2ac063da449_sms_control_plane_core.py) is a real historical migration
step, already applied wherever that migration has run -- not edited in
place, the same "migrations are one-off, frozen-in-time scripts" precedent
this codebase already follows everywhere else (see e.g. keno's own
migration chain this same session). This is a genuinely new, separate
step: a plain UPDATE by slug, not a schema change, matching how the
2026-09-07 Jo Bingo -> Arada Bingo rebrand handled the identical situation
(name is a read-only display field per services/sms/app.py, never a
lookup key -- 'slug' is, and stays 'default').

Revision ID: f3a8c6e2b9d4
Revises: d1f4b7c92a5e
Create Date: 2026-09-22

"""
from typing import Sequence, Union

from alembic import op


revision: str = "f3a8c6e2b9d4"
down_revision: Union[str, None] = "d1f4b7c92a5e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("UPDATE sms_tenants SET name = 'Zemen Game' WHERE slug = 'default'")


def downgrade() -> None:
    op.execute("UPDATE sms_tenants SET name = 'Arada Bingo' WHERE slug = 'default'")
