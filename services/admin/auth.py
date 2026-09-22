"""Admin authentication: separate from player auth entirely (spec section
33 / 9.2 -- "Use separate administrator authentication"). Password + TOTP
second factor, session tokens held in Redis rather than a client-trusted
JWT so a compromised or offboarded admin's session can be revoked
server-side instantly.

There is no self-registration path here on purpose: admin accounts are
provisioned out-of-band by a trusted operator (see create_admin_user),
never through a public endpoint.
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass

import asyncpg
import bcrypt
import pyotp
from redis.asyncio import Redis

from packages.core import rate_limit
from packages.core.ledger import AsyncpgConnection

SESSION_TTL_SECONDS = 8 * 60 * 60  # one working shift
SESSION_KEY_PREFIX = "admin_session:"


class LoginFailed(Exception):
    """Deliberately generic -- never reveals whether the username, the
    password, or the TOTP code was the wrong part.
    """


class LoginRateLimited(Exception):
    """Too many attempts against this username recently. Kept distinct
    from LoginFailed (a 429, not a 401, at the HTTP layer) -- this
    reveals nothing about whether the username is real, only that this
    exact key has been tried too many times, which an attacker already
    knows going in.
    """


@dataclass(frozen=True)
class AdminSession:
    admin_id: int
    username: str
    role: str


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


# A fixed, real bcrypt hash of a value no real password will ever equal --
# checked against an unknown username's login attempt so that path pays
# the same ~100ms bcrypt cost a real user's password check would, rather
# than returning instantly. A real code review pass caught the timing
# side-channel this closes: this module's own docstring/LoginFailed
# comment already promised "a caller can't use error text ... to
# enumerate valid usernames," but an unknown username returned
# immediately while a known one always paid the bcrypt cost first --
# exactly the kind of signal error *text* alone doesn't reveal but
# response *timing* does.
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def generate_totp_secret() -> str:
    return pyotp.random_base32()


def totp_provisioning_uri(secret: str, username: str, issuer: str = "Zemen Game Admin") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


async def create_admin_user(
    pool: asyncpg.Pool | AsyncpgConnection, *, username: str, password: str, role: str
) -> tuple[int, str]:
    """Provisions a new admin account. Returns (admin_id, totp_secret) --
    the secret must be handed to the operator out-of-band (shown once,
    scanned into an authenticator app) and is never retrievable again
    through this module. Accepts either a pool or an already-acquired
    connection -- services/admin/queries.py's console-facing wrapper
    calls this from inside its own transaction, so the audit log entry
    for a new account commits atomically with the account itself.
    """
    totp_secret = generate_totp_secret()
    row = await pool.fetchrow(
        """
        INSERT INTO admin_users (username, password_hash, totp_secret, role)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        username,
        hash_password(password),
        totp_secret,
        role,
    )
    assert row is not None
    return row["id"], totp_secret


async def login(
    pool: asyncpg.Pool,
    redis: Redis,
    *,
    username: str,
    password: str,
    totp_code: str,
) -> str:
    """Returns a bearer session token on success. Raises LoginFailed
    otherwise -- every failure mode (unknown user, wrong password, wrong
    TOTP, deactivated account) raises the exact same exception with the
    same message, so a caller can't use error text to enumerate valid
    usernames or probe which factor was wrong. Raises LoginRateLimited
    (checked first, before any credential is even looked at) if this
    username has been attempted too many times recently -- a real gap a
    code review pass caught: nothing anywhere in the admin console
    throttled login attempts, so a known username's password could be
    brute-forced online with no lockout.
    """
    if not await rate_limit.allow(redis, "admin_login", username, **rate_limit.ADMIN_LOGIN):
        raise LoginRateLimited("too many login attempts for this username")

    row = await pool.fetchrow(
        "SELECT id, password_hash, totp_secret, role, is_active FROM admin_users "
        "WHERE username = $1",
        username,
    )
    if row is None or not row["is_active"]:
        _verify_password(password, _DUMMY_PASSWORD_HASH)  # pay the same bcrypt cost either way
        raise LoginFailed("invalid credentials")
    if not _verify_password(password, row["password_hash"]):
        raise LoginFailed("invalid credentials")
    if not pyotp.TOTP(row["totp_secret"]).verify(totp_code, valid_window=1):
        raise LoginFailed("invalid credentials")

    await pool.execute("UPDATE admin_users SET last_login_at = now() WHERE id = $1", row["id"])

    token = secrets.token_urlsafe(32)
    session = AdminSession(admin_id=row["id"], username=username, role=row["role"])
    await redis.set(
        SESSION_KEY_PREFIX + token,
        json.dumps({"admin_id": session.admin_id, "username": session.username, "role": session.role}),
        ex=SESSION_TTL_SECONDS,
    )
    return token


async def resolve_session(pool: asyncpg.Pool, redis: Redis, token: str) -> AdminSession | None:
    """Returns None for a missing/expired Redis session, same as before --
    but also now for a *live* one belonging to an admin who's since been
    deactivated (admin_users.is_active). An architecture audit caught that
    this module's own docstring already promised "a compromised or
    offboarded admin's session can be revoked server-side instantly," but
    nothing here ever actually re-checked account state after login --
    flipping is_active to false left every session already issued valid
    for up to the full SESSION_TTL_SECONDS regardless. Checked on every
    call rather than only at login, the same as every other privileged
    admin route already re-reads real DB state per request rather than
    trusting a cached value.
    """
    raw = await redis.get(SESSION_KEY_PREFIX + token)
    if raw is None:
        return None
    data = json.loads(raw)
    is_active = await pool.fetchval("SELECT is_active FROM admin_users WHERE id = $1", data["admin_id"])
    if not is_active:
        return None
    return AdminSession(admin_id=data["admin_id"], username=data["username"], role=data["role"])


async def logout(redis: Redis, token: str) -> None:
    await redis.delete(SESSION_KEY_PREFIX + token)
