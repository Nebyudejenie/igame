"""Integration tests for services/payments/telebirr_ingest.py -- real
Postgres, and the real MacroDroid HTTP route (services/payments/app.py)
exercised as genuine HTTP requests, the same discipline test_payments_app.py
already uses for the Chapa webhook.

payment_evidence.external_reference is UNIQUE at the database level and
this suite has no per-test transaction rollback (same as every other
integration test file here) -- _next_reference() mints a fresh reference
per test the same way conftest.py's next_telegram_id() does, so tests
never collide with each other's rows.
"""

import itertools
import random

import httpx

from services.payments.telebirr_ingest import (
    STATUS_CONFLICTING_DUPLICATE,
    STATUS_DUPLICATE,
    STATUS_INGESTED_AVAILABLE,
    STATUS_INGESTED_REJECTED,
    ingest_sms_evidence,
)

_ref_counter = itertools.count(random.randint(10**7, 2 * 10**7))


def _next_reference() -> str:
    return f"DI{next(_ref_counter):08d}"


def _unique_name_suffix() -> str:
    # Letters only -- a real Ethiopian name never contains digits, and
    # the "to X (phone)"/"from X (phone)" parser regex correctly doesn't
    # allow them either. A per-test-unique *reference* differs; this is
    # just enough entropy to keep test-local recipient names from
    # colliding with each other.
    return "".join(random.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(8))


def _build_sms(
    reference: str,
    *,
    amount: str = "10.00",
    payer: str = "DAWIT WERKALEMAHU",
    payer_phone: str = "2519****6294",
    recipient: str = "Nebyu",
) -> str:
    # The exact real Telebirr "money received" template (see services/
    # payments/telebirr_parser.py's own module docstring) -- only the
    # fields under test vary here.
    return (
        f"Dear {recipient} \n"
        f"You have received ETB {amount} from {payer}({payer_phone})  on 04/09/2026 10:27:23. "
        f"Your transaction number is {reference}. Your current E-Money Account balance is ETB 252.12.\n"
        "Thank you for using telebirr\n"
        "Ethio telecom"
    )


async def _add_recognized_recipient(conn, *, account_name: str = "Nebyu") -> None:
    # '0911000000' masks (telebirr_ingest._mask_ethiopian_phone) to
    # exactly '2519****0000' -- the fixed value the phone-matching tests
    # below build their "transferred" SMS samples against.
    await conn.execute(
        """
        INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, is_active)
        VALUES ('telebirr', '0911000000', $1, true)
        """,
        account_name,
    )


def _build_transferred_sms(
    reference: str,
    *,
    amount: str = "20.00",
    payer: str = "Nebyu",
    recipient: str = "SURAFEL DESALEGNE",
    recipient_phone: str = "2519****0000",
) -> str:
    # The real "transferred to" template (CTO directive 2026-09-04) --
    # only the fields under test vary here. recipient_phone defaults to
    # what '0911000000' (the fixed account_ref _add_recognized_recipient
    # inserts) masks to, so the happy-path phone-matching tests need no
    # extra setup.
    return (
        f"Dear {payer} You have transferred ETB {amount} to {recipient} ({recipient_phone}) "
        "on 02/09/2026 07:32:00. "
        f"Your transaction number is {reference}. "
        "The service fee is ETB 0.87 and 15% VAT on the service fee is ETB 0.13. "
        "Your current E-Money Account balance is ETB 385.12. "
        "Thank you for using telebirr Ethio telecom"
    )


async def test_ingestion_with_no_recognized_recipient_is_rejected(pool, conn):
    # The user's own product decision: recipient config ships empty, so
    # every real SMS fails closed until an admin configures one -- this is
    # the default state this whole feature ships in. Uses its own
    # dedicated recipient name so it can never accidentally match a row
    # another test in this file inserted.
    reference = _next_reference()
    sms = _build_sms(reference, recipient=f"NoOneConfigured{reference}")
    outcome = await ingest_sms_evidence(pool, raw_sms=sms, source="macrodroid", source_ref="test-device-1")
    assert outcome.status == STATUS_INGESTED_REJECTED
    assert outcome.reason == "recipient_not_recognized"
    assert outcome.external_reference == reference

    row = await conn.fetchrow(
        "SELECT status, amount FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert row is not None
    assert row["status"] == "rejected"


async def test_ingestion_with_a_recognized_recipient_becomes_available(pool, conn):
    recipient = f"Recognized{_next_reference()}"
    await _add_recognized_recipient(conn, account_name=recipient)
    reference = _next_reference()
    sms = _build_sms(reference, amount="100.00", payer="Kertina Gizachew", recipient=recipient)

    outcome = await ingest_sms_evidence(pool, raw_sms=sms, source="macrodroid", source_ref="test-device-1")
    assert outcome.status == STATUS_INGESTED_AVAILABLE
    assert outcome.reason is None
    assert outcome.external_reference == reference

    row = await conn.fetchrow(
        "SELECT status, amount, payer_name FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert row is not None
    assert row["status"] == "available"
    assert str(row["amount"]) == "100.00"
    assert row["payer_name"] == "Kertina Gizachew"


async def test_identical_resubmission_is_idempotent_not_a_second_row(pool, conn):
    recipient = f"Idempotent{_next_reference()}"
    await _add_recognized_recipient(conn, account_name=recipient)
    reference = _next_reference()
    sms = _build_sms(reference, recipient=recipient)

    first = await ingest_sms_evidence(pool, raw_sms=sms, source="macrodroid", source_ref="device-a")
    second = await ingest_sms_evidence(pool, raw_sms=sms, source="macrodroid", source_ref="device-a")
    assert first.status == STATUS_INGESTED_AVAILABLE
    assert second.status == STATUS_DUPLICATE
    assert second.evidence_id == first.evidence_id

    count = await conn.fetchval(
        "SELECT count(*) FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert count == 1


async def test_same_reference_different_text_is_flagged_disputed_not_overwritten(pool, conn):
    recipient = f"Disputed{_next_reference()}"
    await _add_recognized_recipient(conn, account_name=recipient)
    reference = _next_reference()
    original = _build_sms(reference, amount="10.00", recipient=recipient)

    first = await ingest_sms_evidence(pool, raw_sms=original, source="macrodroid", source_ref="device-a")
    assert first.status == STATUS_INGESTED_AVAILABLE

    # Same reference, but the amount is different -- a forged/tampered
    # resubmission attempt, not a legitimate retry.
    tampered = _build_sms(reference, amount="999.00", recipient=recipient)
    second = await ingest_sms_evidence(pool, raw_sms=tampered, source="macrodroid", source_ref="device-b")
    assert second.status == STATUS_CONFLICTING_DUPLICATE
    assert second.evidence_id == first.evidence_id

    row = await conn.fetchrow("SELECT status, amount FROM payment_evidence WHERE id = $1", first.evidence_id)
    assert row is not None
    assert row["status"] == "disputed"
    # The ORIGINAL amount is preserved -- the tampered resubmission never
    # overwrote it, it only flagged the row for human review.
    assert str(row["amount"]) == "10.00"


async def test_unparseable_message_persists_no_row(pool, conn):
    before = await conn.fetchval("SELECT count(*) FROM payment_evidence")
    outcome = await ingest_sms_evidence(
        pool, raw_sms="Your OTP is 483920.", source="macrodroid", source_ref="device-a"
    )
    assert outcome.status == "unparseable"
    assert outcome.evidence_id is None

    after = await conn.fetchval("SELECT count(*) FROM payment_evidence")
    assert after == before


# --- the "transferred" template: recipient + phone cross-validation -------
# (CTO directive sections 4/13/27 -- "SMS direction check" / "wrong
# recipient test": a valid reference and amount are never sufficient on
# their own, the money must also have gone TO the configured Zemen Game
# destination.)


async def test_transferred_to_the_recognized_recipient_with_matching_phone_becomes_available(pool, conn):
    recipient = f"Surafel {_unique_name_suffix()}"
    await _add_recognized_recipient(conn, account_name=recipient)
    reference = _next_reference()
    sms = _build_transferred_sms(reference, recipient=recipient, recipient_phone="2519****0000")

    outcome = await ingest_sms_evidence(pool, raw_sms=sms, source="macrodroid", source_ref="test-device")
    assert outcome.status == STATUS_INGESTED_AVAILABLE

    row = await conn.fetchrow(
        "SELECT status, direction, fee, vat, recipient_phone FROM payment_evidence WHERE external_reference = $1",
        reference,
    )
    assert row["status"] == "available"
    assert row["direction"] == "transferred"
    assert str(row["fee"]) == "0.87"
    assert str(row["vat"]) == "0.13"
    assert row["recipient_phone"] == "2519****0000"


async def test_transferred_to_a_wrong_recipient_name_is_rejected_despite_valid_reference_and_amount(pool, conn):
    # The exact CTO-directive test: reference exists, amount exists, but
    # the money went to someone else entirely -- must never become
    # AVAILABLE on reference+amount alone.
    configured_recipient = f"RealRecipient {_unique_name_suffix()}"
    await _add_recognized_recipient(conn, account_name=configured_recipient)
    reference = _next_reference()
    sms = _build_transferred_sms(
        reference, recipient="SOME COMPLETELY UNRELATED PERSON", recipient_phone="2519****1234"
    )

    outcome = await ingest_sms_evidence(pool, raw_sms=sms, source="macrodroid", source_ref="test-device")
    assert outcome.status == STATUS_INGESTED_REJECTED
    assert outcome.reason == "recipient_not_recognized"

    row = await conn.fetchrow(
        "SELECT status FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert row["status"] == "rejected"


async def test_transferred_with_matching_name_but_wrong_phone_is_rejected(pool, conn):
    # Name alone is not enough once the template also carries a phone --
    # proves the phone cross-check in _find_matching_recipient() actually
    # enforces, not just logs.
    recipient = f"NameOnly {_unique_name_suffix()}"
    await _add_recognized_recipient(conn, account_name=recipient)  # masks to 2519****0000
    reference = _next_reference()
    sms = _build_transferred_sms(reference, recipient=recipient, recipient_phone="2519****9999")

    outcome = await ingest_sms_evidence(pool, raw_sms=sms, source="macrodroid", source_ref="test-device")
    assert outcome.status == STATUS_INGESTED_REJECTED
    assert outcome.reason == "recipient_not_recognized"


async def test_received_template_recipient_check_is_unaffected_by_phone_matching(pool, conn):
    # The "received" template never carries a recipient_phone at all --
    # confirms the new phone cross-check doesn't regress the original,
    # name-only-signal template.
    recipient = f"ReceivedStillWorks{_next_reference()}"
    await _add_recognized_recipient(conn, account_name=recipient)
    reference = _next_reference()
    sms = _build_sms(reference, recipient=recipient)

    outcome = await ingest_sms_evidence(pool, raw_sms=sms, source="macrodroid", source_ref="test-device")
    assert outcome.status == STATUS_INGESTED_AVAILABLE


async def test_transferred_full_name_matches_a_shorter_configured_nickname_with_correct_phone(pool, conn):
    # The exact real SMS a real payer forwarded on 2026-09-14, reproduced
    # verbatim (only the reference is swapped for a test-unique one) --
    # was rejected in production purely because Telebirr's "transferred"
    # template states the payer's full registered name ("Nebyu Dejenie")
    # while the configured destination uses the shorter nickname Telebirr
    # itself uses in the "received" template's own "Dear {name}" greeting
    # ("Nebyu") -- despite the phone number matching exactly. This is the
    # real regression test for that fix.
    account_ref = "0911223344"  # masks to exactly 2519****3344, matching the real SMS below
    recipient_nickname = f"Nebyu{_unique_name_suffix()}"
    await conn.execute(
        "INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, is_active) "
        "VALUES ('telebirr', $1, $2, true)",
        account_ref,
        recipient_nickname,
    )
    reference = _next_reference()
    sms = (
        f"Dear DAWIT \n"
        f"You have transferred ETB 10.00 to {recipient_nickname} Dejenie (2519****3344) on "
        f"14/09/2026 14:19:30. Your transaction number is {reference}. The service fee is  ETB 0.87 and  "
        f"15% VAT on the service fee is ETB 0.13. Your current E-Money Account  balance is ETB 912.24. "
        f"To download your payment information please click this link: "
        f"https://transactioninfo.ethiotelecom.et/receipt/{reference}.\n\n"
        f"Thank you for using telebirr\n"
        f"Ethio telecom"
    )

    outcome = await ingest_sms_evidence(pool, raw_sms=sms, source="macrodroid", source_ref="test-device")
    assert outcome.status == STATUS_INGESTED_AVAILABLE
    assert outcome.reason is None

    row = await conn.fetchrow(
        "SELECT status, recipient_name FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert row["status"] == "available"
    assert row["recipient_name"] == f"{recipient_nickname} Dejenie"


async def test_transferred_nickname_must_match_a_whole_word_not_a_bare_substring(pool, conn):
    # Guards the word-boundary safety property the fix above depends on:
    # a configured nickname that happens to be a PREFIX of an unrelated
    # word in the SMS's full name must never match. "Neb" is a substring
    # of "Nebyu" but not a whole word within it.
    account_ref = "0911223355"  # masks to 2519****3355
    await conn.execute(
        "INSERT INTO manual_payment_destinations (method_kind, account_ref, account_name, is_active) "
        "VALUES ('telebirr', $1, 'Neb', true)",
        account_ref,
    )
    reference = _next_reference()
    sms = _build_transferred_sms(reference, recipient="Nebyu Dejenie", recipient_phone="2519****3355")

    outcome = await ingest_sms_evidence(pool, raw_sms=sms, source="macrodroid", source_ref="test-device")
    assert outcome.status == STATUS_INGESTED_REJECTED
    assert outcome.reason == "recipient_not_recognized"


# --- real HTTP: the MacroDroid ingestion route -----------------------------


async def test_macrodroid_route_rejects_missing_bearer_token(payments_server):
    sms = _build_sms(_next_reference())
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{payments_server}/internal/telebirr/ingest",
            json={"raw_sms": sms, "device_id": "phone-1"},
        )
    assert response.status_code == 401


async def test_macrodroid_route_rejects_wrong_bearer_token(payments_server):
    sms = _build_sms(_next_reference())
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{payments_server}/internal/telebirr/ingest",
            json={"raw_sms": sms, "device_id": "phone-1"},
            headers={"Authorization": "Bearer wrong-token"},
        )
    assert response.status_code == 401


async def test_macrodroid_route_ingests_a_real_sms_over_real_http(payments_server, conn):
    recipient = f"HttpRoute{_next_reference()}"
    await _add_recognized_recipient(conn, account_name=recipient)
    reference = _next_reference()
    sms = _build_sms(reference, amount="100.00", payer="Kertina Gizachew", recipient=recipient)

    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{payments_server}/internal/telebirr/ingest",
            json={"raw_sms": sms, "device_id": "phone-1"},
            headers={"Authorization": "Bearer test-macrodroid-token-for-suite"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == STATUS_INGESTED_AVAILABLE
    assert body["external_reference"] == reference

    row = await conn.fetchrow(
        "SELECT status FROM payment_evidence WHERE external_reference = $1", reference
    )
    assert row is not None
    assert row["status"] == "available"
