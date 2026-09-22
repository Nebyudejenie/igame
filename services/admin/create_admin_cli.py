"""One-shot CLI for provisioning an admin account -- the out-of-band
"trusted operator" step services/admin/auth.py's own module docstring
already describes ("admin accounts are provisioned out-of-band by a
trusted operator... never through a public endpoint"), which never
actually had a script to run until now. Needed for the very first admin
account on a fresh production deploy (no self-registration path exists,
on purpose), and for onboarding any admin after that.

Run: `python -m services.admin.create_admin_cli --username <name> --role
<role>`. Prompts for the password interactively (never a CLI argument --
that would land in shell history and be visible in the process list to
any other user on the box). Prints the TOTP secret once on success --
auth.create_admin_user()'s own contract: it is never retrievable again
through this codebase, so scan it into an authenticator app immediately.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys

from packages.core.config import get_settings
from packages.core.db_pool import create_pool
from services.admin import rbac
from services.admin.auth import create_admin_user

# Derived from the real permission table rather than a second, hand-kept
# list of role names -- can never silently drift from what rbac.py
# actually recognizes.
VALID_ROLES: frozenset[str] = frozenset().union(*rbac.PERMISSIONS.values())

MIN_PASSWORD_LENGTH = 12


async def _create(username: str, password: str, role: str) -> tuple[int, str]:
    settings = get_settings()
    pool = await create_pool(dsn=settings.database_url, min_size=1, max_size=1)
    try:
        return await create_admin_user(pool, username=username, password=password, role=role)
    finally:
        await pool.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Provision a new Zemen Game admin account.")
    parser.add_argument("--username", required=True)
    parser.add_argument("--role", required=True, choices=sorted(VALID_ROLES))
    args = parser.parse_args()

    password = getpass.getpass("Password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        print("Passwords did not match.", file=sys.stderr)
        return 1
    # A basic sanity check, not a claimed policy -- this codebase has no
    # other password-strength requirement anywhere to match or contradict.
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"Password must be at least {MIN_PASSWORD_LENGTH} characters.", file=sys.stderr)
        return 1

    admin_id, totp_secret = asyncio.run(_create(args.username, password, args.role))
    print(f"Created admin '{args.username}' (id={admin_id}, role={args.role}).")
    print(f"TOTP secret -- scan into an authenticator app now, shown only this once: {totp_secret}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
