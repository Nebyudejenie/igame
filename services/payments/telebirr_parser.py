"""Telebirr SMS parser (CTO directive sections 94/95/121) -- pure, no I/O.

Built and tested against THREE real Telebirr SMS samples now, covering two
genuinely different templates:

1. "money received" (2026-09-04, two samples) -- lands on the RECIPIENT's
   own phone:

    Dear Nebyu
    You have received ETB 10.00 from DAWIT WERKALEMAHU(2519****6294)  on 04/09/2026 10:27:23. Your transaction number is DI41FHSD4J. Your current E-Money Account balance is ETB 252.12.
    Thank you for using telebirr
    Ethio telecom

2. "money transferred/sent" (2026-09-04, one sample, CTO directive) --
   lands on the PAYER's own phone:

    Dear Nebyu You have transferred ETB 20.00 to SURAFEL DESALEGNE (2519****0917) on 02/09/2026 07:32:00. Your transaction number is DI26D9N4AW. The service fee is ETB 0.87 and 15% VAT on the service fee is ETB 0.13. Your current E-Money Account balance is ETB 385.12. To download your payment information please click this link: https://transactioninfo.ethiotelecom.et/receipt/DI26D9N4AW. Thank you for using telebirr Ethio telecom

The greeting ("Dear {name}") always names whoever's phone the SMS landed
on -- for template 1 that's the RECIPIENT (no phone ever shown for them);
for template 2 that's the PAYER (also no phone ever shown for them, since
Telebirr never restates your own number back to you). This is the load-
bearing distinction CTO directive section 13 calls "SMS direction check":
parse_telebirr_sms() always resolves recipient_name/recipient_phone to
whoever the money actually went TO, regardless of which phone the SMS
came from -- the caller (telebirr_ingest.py) then matches THAT against
the configured Arada Bingo destination, so a player forwarding their own
"transferred to some unrelated person" SMS is naturally rejected: the
extracted recipient is that unrelated person, not Arada Bingo, and no
special-casing is needed beyond extracting the right field.

Fails closed (ParseFailure, never a guess) on anything that matches
neither confirmed template. An Amharic-language template has still not
been seen in a real sample and remains unhandled.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo

# Bumped whenever extraction logic changes -- stored per payment_evidence
# row (see migrations/versions/9c1f4d7a2b3e and 2f6b1a9c4d8e) so a later
# re-parse of historical evidence is never ambiguous about which logic
# produced it.
PARSER_VERSION = "telebirr-v2-received-and-transferred"

# Same tz value/library services/admin/queries.py's own ETHIOPIA_TZ
# already uses -- not imported from there to avoid a payments-service ->
# admin-service dependency; this codebase already repeats the
# 'Africa/Addis_Ababa' literal per-file (responsible_gaming.py, admin/
# queries.py, payments/deposits.py all do), so a local constant here
# matches the established convention rather than fighting it.
_ADDIS_TZ = ZoneInfo("Africa/Addis_Ababa")

# A masked Ethiopian mobile number as Telebirr itself renders it: country
# code + first subscriber digit (4 chars), 4 literal asterisks, last 4
# digits -- e.g. "2519****6294". Shared by both templates' name(phone)
# capture.
_MASKED_PHONE = r"\d{4}\*{4}\d{4}"
# A person's name as free text: real samples show both ALL CAPS and mixed
# case, so casing is never assumed. A hyphen/apostrophe/period is allowed
# since a real Ethiopian name could plausibly include one (e.g. an
# initial), even though no real sample does yet.
_NAME = r"[A-Za-z][A-Za-z.\-' ]*?"

# "Dear Nebyu" -- stops at the fixed "You have" boundary rather than
# end-of-line, since the two confirmed templates disagree on whether a
# newline separates the greeting from the body (template 1's real samples
# do; template 2's given text doesn't).
_GREETING_RE = re.compile(r"Dear\s+(.+?)\s+You have\b", re.IGNORECASE)

# Anchored on "received"/"transferred" specifically, never a bare
# "ETB <number>" search -- both templates restate a SECOND, unrelated ETB
# amount later (the post-transaction account balance; template 2 also has
# a fee and a VAT-on-fee amount), and a position-only first-match search
# would be one template reordering away from silently reading the wrong
# number.
_RECEIVED_AMOUNT_RE = re.compile(r"received\s+ETB\s*([0-9]+(?:\.[0-9]{1,2})?)", re.IGNORECASE)
_TRANSFERRED_AMOUNT_RE = re.compile(r"transferred\s+ETB\s*([0-9]+(?:\.[0-9]{1,2})?)", re.IGNORECASE)

_RECEIVED_SENDER_RE = re.compile(
    rf"from\s+({_NAME})\s*\(({_MASKED_PHONE})\)", re.IGNORECASE
)
_TRANSFERRED_RECIPIENT_RE = re.compile(
    rf"to\s+({_NAME})\s*\(({_MASKED_PHONE})\)", re.IGNORECASE
)

# "on 04/09/2026 10:27:23" -- DD/MM/YYYY (confirmed against both real
# samples' own send dates), 24h clock. Shared by both templates.
_DATETIME_RE = re.compile(r"\bon\s+([0-9]{2})/([0-9]{2})/([0-9]{4})\s+([0-9]{2}):([0-9]{2}):([0-9]{2})")

# "Your transaction number is DI41FHSD4J." -- Telebirr's own reference,
# the canonical identity (sections 93/121). Length is bounded (6-20), not
# pinned to exactly 10, in case it varies by transaction type -- both
# confirmed templates use 10 uppercase alphanumeric characters. Shared.
_REFERENCE_RE = re.compile(r"transaction number is\s+([A-Za-z0-9]{6,20})", re.IGNORECASE)

# Template 2 only. "The service fee is ETB 0.87 and 15% VAT on the
# service fee is ETB 0.13." -- the percentage figure itself is never
# captured (not needed for anything; VAT-on-fee is already stated as its
# own ETB amount).
_FEE_RE = re.compile(r"service fee is\s+ETB\s*([0-9]+(?:\.[0-9]{1,2})?)", re.IGNORECASE)
_VAT_RE = re.compile(r"VAT on the service fee is\s+ETB\s*([0-9]+(?:\.[0-9]{1,2})?)", re.IGNORECASE)

# Template 2 only, optional. Real sample points at Ethio Telecom's own
# transactioninfo domain -- extracted if present, but its *absence* is
# never a failure (template 1 never has one at all). Its presence with a
# WRONG domain is treated as a tamper signal, not silently ignored.
_RECEIPT_URL_RE = re.compile(r"(https?://\S+)")
_TRUSTED_RECEIPT_DOMAIN = "ethiotelecom.et"


@dataclass(frozen=True)
class ParsedEvidence:
    external_reference: str
    raw_reference: str
    amount: Decimal
    fee: Decimal | None
    vat: Decimal | None
    payer_name: str
    payer_phone: str | None
    recipient_name: str
    recipient_phone: str | None
    receipt_url: str | None
    transaction_at: datetime
    direction: Literal["received", "transferred"]


@dataclass(frozen=True)
class ParseFailure:
    reason: str


def _normalize_whitespace(raw: str) -> str:
    """Collapses runs of any Unicode whitespace (regular spaces, non-
    breaking spaces, tabs, the double-space real samples already show in
    a few places) to a single space -- one line at a time, so a genuine
    newline between fields (template 1's greeting vs. body) still
    terminates a line-anchored match instead of being swallowed. NFKC
    first so a lookalike Unicode character (e.g. a fullwidth digit) can't
    slip a required field past the regexes below unnoticed.
    """
    lines = unicodedata.normalize("NFKC", raw).splitlines()
    return "\n".join(re.sub(r"\s+", " ", line, flags=re.UNICODE).strip() for line in lines)


def normalize_reference(raw: str) -> str:
    """Uppercase + strip only (section 121) -- safe, reversible formatting
    normalization, never a transformation that could turn one legitimate
    reference into another. Shared by the parser (raw_reference ->
    external_reference) and telebirr_redemption.py (a player-typed
    reference must be normalized identically or a real match would be
    missed on casing/whitespace alone).
    """
    return raw.strip().upper()


def extract_reference(raw: str) -> str | None:
    """Pulls a bare reference out of free text that might be an entire
    pasted SMS body rather than just the code -- same anchor phrase,
    character class, and length bounds as _REFERENCE_RE above, exposed
    for callers that only need the bare code (never a full
    parse_telebirr_sms() result), namely services/bot/handlers.py's
    plain-chat-message redemption path. Deliberately strict (None on no
    match, never a raw-text fallback): unlike a dedicated reference input
    field, an ordinary chat message with no match is far more likely to
    be small talk or a menu button press than a mistyped reference, so a
    fallback here would misfire on almost everything a player types.
    web/miniapp/js/app.v6.js's own extractTelebirrReference() mirrors
    this same regex for its input-field case, where a raw-text fallback
    *is* safe (everything typed there is already reference-shaped input).
    """
    match = _REFERENCE_RE.search(raw)
    return normalize_reference(match.group(1)) if match else None


def _parse_datetime(text: str) -> datetime | ParseFailure:
    match = _DATETIME_RE.search(text)
    if match is None:
        return ParseFailure("transaction_datetime_not_found")
    day, month, year, hour, minute, second = (int(g) for g in match.groups())
    try:
        return datetime(year, month, day, hour, minute, second, tzinfo=_ADDIS_TZ)
    except ValueError:
        return ParseFailure("transaction_datetime_invalid")


def _parse_reference(text: str) -> tuple[str, str] | ParseFailure:
    match = _REFERENCE_RE.search(text)
    if match is None:
        return ParseFailure("reference_not_found")
    raw_reference = match.group(1)
    return raw_reference, normalize_reference(raw_reference)


def _parse_receipt_url(text: str) -> str | None | ParseFailure:
    match = _RECEIPT_URL_RE.search(text)
    if match is None:
        return None
    url = match.group(1).rstrip(".")  # a trailing sentence period is not part of the URL
    if _TRUSTED_RECEIPT_DOMAIN not in url.lower():
        return ParseFailure("receipt_url_untrusted_domain")
    return url


def _parse_decimal(text: str, pattern: re.Pattern[str], *, failure_reason: str) -> Decimal | ParseFailure:
    match = pattern.search(text)
    if match is None:
        return ParseFailure(failure_reason)
    try:
        return Decimal(match.group(1))
    except InvalidOperation:
        return ParseFailure(failure_reason)


def _parse_received(text: str) -> ParsedEvidence | ParseFailure:
    amount = _parse_decimal(text, _RECEIVED_AMOUNT_RE, failure_reason="amount_not_found")
    if isinstance(amount, ParseFailure):
        return amount
    if amount <= 0:
        return ParseFailure("amount_not_positive")

    sender_match = _RECEIVED_SENDER_RE.search(text)
    if sender_match is None:
        return ParseFailure("sender_not_found")
    payer_name = sender_match.group(1).strip()
    if not payer_name:
        return ParseFailure("sender_name_empty")
    payer_phone = sender_match.group(2)

    reference = _parse_reference(text)
    if isinstance(reference, ParseFailure):
        return reference
    raw_reference, external_reference = reference

    greeting_match = _GREETING_RE.search(text)
    if greeting_match is None:
        return ParseFailure("recipient_greeting_not_found")
    recipient_name = greeting_match.group(1).strip()
    if not recipient_name:
        return ParseFailure("recipient_greeting_empty")

    transaction_at = _parse_datetime(text)
    if isinstance(transaction_at, ParseFailure):
        return transaction_at

    return ParsedEvidence(
        external_reference=external_reference,
        raw_reference=raw_reference,
        amount=amount,
        fee=None,
        vat=None,
        payer_name=payer_name,
        payer_phone=payer_phone,
        recipient_name=recipient_name,
        recipient_phone=None,
        receipt_url=None,
        transaction_at=transaction_at,
        direction="received",
    )


def _parse_transferred(text: str) -> ParsedEvidence | ParseFailure:
    amount = _parse_decimal(text, _TRANSFERRED_AMOUNT_RE, failure_reason="amount_not_found")
    if isinstance(amount, ParseFailure):
        return amount
    if amount <= 0:
        return ParseFailure("amount_not_positive")

    recipient_match = _TRANSFERRED_RECIPIENT_RE.search(text)
    if recipient_match is None:
        return ParseFailure("recipient_not_found")
    recipient_name = recipient_match.group(1).strip()
    if not recipient_name:
        return ParseFailure("recipient_name_empty")
    recipient_phone = recipient_match.group(2)

    reference = _parse_reference(text)
    if isinstance(reference, ParseFailure):
        return reference
    raw_reference, external_reference = reference

    greeting_match = _GREETING_RE.search(text)
    if greeting_match is None:
        return ParseFailure("payer_greeting_not_found")
    payer_name = greeting_match.group(1).strip()
    if not payer_name:
        return ParseFailure("payer_greeting_empty")

    transaction_at = _parse_datetime(text)
    if isinstance(transaction_at, ParseFailure):
        return transaction_at

    # Fee/VAT/receipt URL are all optional in principle (a zero-fee
    # transfer is plausible), but if the fee anchor phrase IS present and
    # doesn't parse, that's a malformed message, not an absent field --
    # only a message with NO fee mention at all gets fee=None.
    fee: Decimal | None = None
    if _FEE_RE.search(text) is not None:
        fee_result = _parse_decimal(text, _FEE_RE, failure_reason="fee_not_parseable")
        if isinstance(fee_result, ParseFailure):
            return fee_result
        fee = fee_result

    vat: Decimal | None = None
    if _VAT_RE.search(text) is not None:
        vat_result = _parse_decimal(text, _VAT_RE, failure_reason="vat_not_parseable")
        if isinstance(vat_result, ParseFailure):
            return vat_result
        vat = vat_result

    receipt_url = _parse_receipt_url(text)
    if isinstance(receipt_url, ParseFailure):
        return receipt_url

    return ParsedEvidence(
        external_reference=external_reference,
        raw_reference=raw_reference,
        amount=amount,
        fee=fee,
        vat=vat,
        payer_name=payer_name,
        payer_phone=None,
        recipient_name=recipient_name,
        recipient_phone=recipient_phone,
        receipt_url=receipt_url,
        transaction_at=transaction_at,
        direction="transferred",
    )


def parse_telebirr_sms(raw: str) -> ParsedEvidence | ParseFailure:
    text = _normalize_whitespace(raw)

    # Template detection: an unambiguous fixed anchor phrase decides which
    # of the two confirmed templates this is, before any field extraction
    # runs -- never both attempted blindly, since e.g. _GREETING_RE alone
    # can't tell a payer from a recipient.
    has_received = re.search(r"you have received", text, re.IGNORECASE) is not None
    has_transferred = re.search(r"you have transferred", text, re.IGNORECASE) is not None

    if has_received and not has_transferred:
        return _parse_received(text)
    if has_transferred and not has_received:
        return _parse_transferred(text)
    return ParseFailure("unrecognized_template")
