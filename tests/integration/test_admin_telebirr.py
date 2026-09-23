"""Tests for the Telebirr SMS-evidence admin review surface: RBAC enforced
through real HTTP (not has_permission() called directly -- same discipline
test_admin_app.py's own docstring establishes), the explicit state-
transition policy (section 99), and the audited raw-SMS view (section 97).
"""

import itertools
import random

import httpx
import pytest

from services.admin import queries as admin_queries
from services.payments.telebirr_ingest import ingest_sms_evidence
from tests.integration.test_admin_app import _auth_headers
from tests.integration.test_admin_auth import create_test_admin

_ref_counter = itertools.count(random.randint(6 * 10**7, 7 * 10**7))


def _next_reference() -> str:
    return f"DI{next(_ref_counter):08d}"


def _build_sms(reference: str, *, recipient: str) -> str:
    return (
        f"Dear {recipient} \n"
        "You have received ETB 10.00 from DAWIT WERKALEMAHU(2519****6294)  on 04/09/2026 10:27:23. "
        f"Your transaction number is {reference}. Your current E-Money Account balance is ETB 252.12.\n"
        "Thank you for using telebirr\n"
        "Ethio telecom"
    )


async def _make_evidence(pool) -> tuple[int, str]:
    # No recognized recipient configured for this test's made-up name --
    # ingestion always lands on 'rejected', exactly the common real-world
    # starting state this whole review surface exists to work from.
    reference = _next_reference()
    recipient = f"AdminTest{reference}"
    outcome = await ingest_sms_evidence(
        pool, raw_sms=_build_sms(reference, recipient=recipient), source="macrodroid", source_ref="admin-test"
    )
    assert outcome.status == "ingested_rejected"
    assert outcome.evidence_id is not None
    return outcome.evidence_id, reference


# --- query-level: state transitions ----------------------------------------


async def test_rejected_evidence_can_be_resolved_to_available(pool):
    admin_id, *_ = await create_test_admin(pool)
    evidence_id, _ = await _make_evidence(pool)

    resolved = await admin_queries.resolve_payment_evidence_admin(
        pool, admin_id=admin_id, evidence_id=evidence_id, to_status="available",
        reason="verified manually, recipient config now added", ip_address=None,
    )
    assert resolved is True

    row = await pool.fetchrow("SELECT status, reject_reason FROM payment_evidence WHERE id = $1", evidence_id)
    assert row["status"] == "available"
    assert row["reject_reason"] is None


async def test_available_cannot_transition_directly_to_redeemed_via_admin_resolution():
    # "redeemed" isn't reachable through resolve_payment_evidence_admin at
    # all -- only real redemption (services/payments/telebirr_redemption.py)
    # ever sets it. Covered structurally below by asserting the transition
    # table itself never lists it as a target from any source.
    for allowed_targets in admin_queries._EVIDENCE_TRANSITIONS.values():  # noqa: SLF001
        assert "redeemed" not in allowed_targets


async def test_invalid_transition_is_rejected(pool):
    admin_id, *_ = await create_test_admin(pool)
    evidence_id, _ = await _make_evidence(pool)  # starts 'rejected'

    with pytest.raises(admin_queries.InvalidEvidenceTransition):
        # rejected -> blocked is not an allowed edge (only rejected ->
        # available is).
        await admin_queries.resolve_payment_evidence_admin(
            pool, admin_id=admin_id, evidence_id=evidence_id, to_status="blocked",
            reason="test", ip_address=None,
        )

    row = await pool.fetchrow("SELECT status FROM payment_evidence WHERE id = $1", evidence_id)
    assert row["status"] == "rejected"  # untouched


async def test_resolving_unknown_evidence_id_returns_false(pool):
    admin_id, *_ = await create_test_admin(pool)
    resolved = await admin_queries.resolve_payment_evidence_admin(
        pool, admin_id=admin_id, evidence_id=999999999, to_status="available",
        reason="test", ip_address=None,
    )
    assert resolved is False


# --- query-level: audited raw-SMS view --------------------------------------


async def test_viewing_raw_sms_writes_an_audit_row_every_time(pool):
    admin_id, *_ = await create_test_admin(pool)
    evidence_id, reference = await _make_evidence(pool)

    raw = await admin_queries.get_payment_evidence_raw_sms(
        pool, admin_id=admin_id, evidence_id=evidence_id, ip_address="10.0.0.1"
    )
    assert raw is not None
    assert reference in raw

    audit_row = await pool.fetchrow(
        "SELECT admin_id, action, target_id, ip_address FROM admin_audit_log "
        "WHERE action = 'payment_evidence.view_raw_sms' AND target_id = $1 ORDER BY id DESC LIMIT 1",
        str(evidence_id),
    )
    assert audit_row is not None
    assert audit_row["admin_id"] == admin_id
    assert audit_row["ip_address"] == "10.0.0.1"


async def test_viewing_raw_sms_for_a_nonexistent_id_is_still_audited(pool):
    # A fresh random id, not a fixed literal -- this suite has no per-test
    # DB rollback, so a hardcoded "nonexistent" id's audit rows would
    # accumulate across repeated runs and break an exact-count assertion.
    nonexistent_id = random.randint(9 * 10**8, 10**9)
    admin_id, *_ = await create_test_admin(pool)
    raw = await admin_queries.get_payment_evidence_raw_sms(
        pool, admin_id=admin_id, evidence_id=nonexistent_id, ip_address=None
    )
    assert raw is None

    audit_row = await pool.fetchval(
        "SELECT count(*) FROM admin_audit_log WHERE action = 'payment_evidence.view_raw_sms' "
        "AND target_id = $1",
        str(nonexistent_id),
    )
    assert audit_row == 1


# --- HTTP-level: RBAC enforced through the real dependency chain -----------


async def test_support_can_list_evidence_but_not_view_raw_sms_or_resolve(admin_server, pool):
    evidence_id, _ = await _make_evidence(pool)
    headers = await _auth_headers(admin_server, pool, role="support")

    async with httpx.AsyncClient() as client:
        list_response = await client.get(f"{admin_server}/telebirr-evidence", headers=headers)
        assert list_response.status_code == 200

        raw_response = await client.get(f"{admin_server}/telebirr-evidence/{evidence_id}/raw-sms", headers=headers)
        assert raw_response.status_code == 403

        resolve_response = await client.post(
            f"{admin_server}/telebirr-evidence/{evidence_id}/resolve",
            headers=headers,
            json={"to_status": "available", "reason": "test"},
        )
        assert resolve_response.status_code == 403


async def test_finance_can_view_raw_sms_and_resolve_over_http(admin_server, pool):
    evidence_id, reference = await _make_evidence(pool)
    headers = await _auth_headers(admin_server, pool, role="finance")

    async with httpx.AsyncClient() as client:
        raw_response = await client.get(f"{admin_server}/telebirr-evidence/{evidence_id}/raw-sms", headers=headers)
        assert raw_response.status_code == 200
        assert reference in raw_response.json()["raw_sms"]

        resolve_response = await client.post(
            f"{admin_server}/telebirr-evidence/{evidence_id}/resolve",
            headers=headers,
            json={"to_status": "available", "reason": "verified against real SMS"},
        )
        assert resolve_response.status_code == 200
        assert resolve_response.json()["resolved"] is True


async def test_resolve_over_http_rejects_an_invalid_transition_with_422(admin_server, pool):
    evidence_id, _ = await _make_evidence(pool)  # 'rejected'
    headers = await _auth_headers(admin_server, pool, role="finance")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/telebirr-evidence/{evidence_id}/resolve",
            headers=headers,
            # A real, well-formed reason -- this test is about the
            # invalid-transition rejection specifically, not about
            # reason validation (see test_resolve_over_http_requires_a_
            # real_reason below for that), so the reason itself must be
            # one that would otherwise pass cleanly.
            json={"to_status": "blocked", "reason": "attempting an invalid transition"},
        )
    assert response.status_code == 422


async def test_resolve_over_http_requires_a_real_reason(admin_server, pool):
    evidence_id, _ = await _make_evidence(pool)
    headers = await _auth_headers(admin_server, pool, role="finance")

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{admin_server}/telebirr-evidence/{evidence_id}/resolve",
            headers=headers,
            json={"to_status": "available", "reason": "   "},
        )
    assert response.status_code == 422


# --- HTTP-level: payment agent allowlist RBAC -------------------------------


async def test_finance_can_view_but_not_configure_payment_agents(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="finance")
    async with httpx.AsyncClient() as client:
        list_response = await client.get(f"{admin_server}/payment-agents", headers=headers)
        assert list_response.status_code == 200

        create_response = await client.post(
            f"{admin_server}/payment-agents", headers=headers,
            json={"telegram_user_id": 555000111, "display_name": "Test Agent"},
        )
        assert create_response.status_code == 403


async def test_superadmin_can_create_and_deactivate_a_payment_agent_over_http(admin_server, pool):
    headers = await _auth_headers(admin_server, pool, role="superadmin")
    telegram_user_id = random.randint(6 * 10**8, 7 * 10**8)

    async with httpx.AsyncClient() as client:
        create_response = await client.post(
            f"{admin_server}/payment-agents", headers=headers,
            json={"telegram_user_id": telegram_user_id, "display_name": "Field Agent"},
        )
        assert create_response.status_code == 200
        agent_id = create_response.json()["id"]

        deactivate_response = await client.patch(
            f"{admin_server}/payment-agents/{agent_id}", headers=headers, json={"is_active": False}
        )
        assert deactivate_response.status_code == 200

    row = await pool.fetchrow("SELECT is_active FROM payment_agents WHERE id = $1", agent_id)
    assert row["is_active"] is False
