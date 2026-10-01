"""The walker (blueprint S4): one function over every agent-supplied value
the chassis persists, forwards or exports.

Every case in the batch's Accept list is here as a unit case, so the
chassis-side and telemetry-side callers only need to prove they CALL the
walker; what it decides is decided once, here:

* the clean number case — Luhn-valid epochs under time-named keys, a
  nanosecond timestamp, ordinary ids — passes unchanged;
* a Luhn-valid leading-1 value under a key with no context is a card;
* a phone number as a JSON number is flagged under a phone key, under a
  key with no context, and as a foreign number valid only with ``+``;
* a Luhn-valid card as a number under any key; the fixture's address as
  an object key; nine digits only under a social-security context;
* integral doubles count as their integer, fractional ones never;
* string leaves are redacted in place, identifiers are never rewritten.
"""
from __future__ import annotations

import pytest

from app.services import pii_service
from app.services.pii_service import (
    Finding,
    PiiRefused,
    UnwalkableValue,
    check_identifier,
    check_number_text,
    luhn_valid,
    walk,
    walk_or_refuse,
)

FIXTURE_EMAIL = "pii-fixture@example.com"


def _refused(result):
    return [(f.path, f.kind, f.pii_type) for f in result.refusals]


# ---- the number rule --------------------------------------------------------

def test_luhn():
    assert luhn_valid("4111111111111111")
    assert luhn_valid("1234567890123452")
    assert not luhn_valid("4111111111111112")
    assert not luhn_valid("")
    assert not luhn_valid("12a4")


def test_the_clean_number_case_passes_unchanged():
    clean = {
        "created_at": 1788998400005,  # Luhn-valid epoch millis under a time key
        "ts_ns": 1757534400000000000,
        "id": 9876543,
        "message_id": 1788998400005123456,
    }
    result = walk(clean)
    assert result.findings == []
    assert result.value == clean


def test_a_luhn_valid_leading_one_value_without_context_is_a_card():
    result = walk({"v": 1234567890123452})
    assert _refused(result) == [("$.v", "number", "CREDIT_CARD")]


def test_the_timestamp_exemption_needs_the_context_word():
    # Same shape, same checksum: a time-named key makes it an epoch.
    assert walk({"created_at": 1234567890123452}).findings == []
    assert walk({"event": {"time": 1234567890123452}}).findings == []
    # A leading 4 has no timestamp shape whatever the key says.
    assert _refused(walk({"created_at": 4111111111111111})) == [
        ("$.created_at", "number", "CREDIT_CARD")
    ]


@pytest.mark.parametrize(
    ("value", "expected_path"),
    [
        ({"phone": 2125551234}, "$.phone"),
        ({"contacts": [2125551234]}, "$.contacts[0]"),
        ({"contacts": [442071838750]}, "$.contacts[0]"),  # valid only as +44…
        ({"k": 2125551234.0}, "$.k"),  # an integral double is its integer
    ],
)
def test_phone_numbers_are_flagged_wherever_they_sit(value, expected_path):
    assert _refused(walk(value)) == [(expected_path, "number", "PHONE")]


def test_a_phone_context_word_only_raises_the_confidence():
    with_context = walk({"phone": 2125551234}).refusals[0]
    without = walk({"contacts": [2125551234]}).refusals[0]
    assert with_context.confidence > without.confidence


def test_a_fractional_double_is_never_a_number_finding():
    # 4222222222222 is a Luhn-valid 13-digit card number; with a fraction
    # a double still holds, it is a measurement, not a number to check.
    assert walk({"k": 2125551234.5}).findings == []
    assert walk({"k": 4222222222222.25}).findings == []
    assert walk({"card": 4222222222222}).refused


def test_a_double_that_cannot_hold_its_fraction_is_integral():
    # 4111111111111111.25 rounds to 4111111111111111.0 (the mantissa is
    # exhausted at sixteen digits), so it IS the card number.
    assert walk({"k": 4111111111111111.25}).refused


def test_ten_digit_leading_one_is_epoch_seconds():
    assert walk({"n": 1757534400}).findings == []
    assert walk({"phone": 1757534400}).findings == []


def test_nine_digits_are_an_ssn_only_under_a_social_security_context():
    assert _refused(walk({"ssn": 123456789})) == [("$.ssn", "number", "SSN")]
    assert _refused(walk({"social_security": 123456789})) == [
        ("$.social_security", "number", "SSN")
    ]
    assert walk({"n": 123456789}).findings == []


def test_negative_numbers_and_booleans_are_not_numbers_the_rule_knows():
    assert walk({"n": -2125551234, "b": True, "z": None}).findings == []


def test_check_number_text_only_takes_bare_digits():
    assert check_number_text("212-555-1234") is None
    assert check_number_text("+12125551234") is None
    assert check_number_text("2125551234") is not None


# ---- identifiers ------------------------------------------------------------

def test_the_address_as_an_object_key_refuses():
    result = walk({"ok": 1, FIXTURE_EMAIL: {"inner": f"note {FIXTURE_EMAIL}"}})
    assert _refused(result) == [("$.<key 1>", "identifier", "EMAIL_ADDRESS")]
    # The path is what a refusal names, and a key IS the value it must
    # not name: a flagged key is addressed by its ordinal, never its text —
    # and so are the positions beneath it.
    assert all(FIXTURE_EMAIL not in f.path for f in result.findings)
    assert [f.path for f in result.findings if f.kind == "content"] == ["$.<key 1>.inner"]


def test_a_declared_identifier_is_checked_and_never_rewritten():
    result = walk(f"vendor={FIXTURE_EMAIL}", identifier=True)
    assert result.value == f"vendor={FIXTURE_EMAIL}"
    assert _refused(result) == [("$", "identifier", "EMAIL_ADDRESS")]
    assert walk("vendor=abc123", identifier=True).findings == []


def test_identifier_digit_runs_follow_the_number_rule_not_the_free_text_regexes():
    # A 13-digit run in a key named for a time is a timestamp, not a card.
    assert check_identifier("created_1788998400005") is None
    assert check_identifier("evt-1757534400000000000") is None
    # A bare card number as a name is a card; a phone number as a name a phone.
    assert check_identifier("1234567890123452").pii_type == "CREDIT_CARD"
    assert check_identifier("user-2125551234").pii_type == "PHONE"
    # Short digit runs (an ordinary id) are not numbers the rule examines.
    assert check_identifier("order-9876543") is None


def test_identifier_checks_ignore_name_entity_noise():
    # A lone token a name-entity model might call a location or a person.
    for key in ("ts_ns", "paris", "john", "washington_st"):
        assert check_identifier(key) is None, key


def test_structural_recognizers_apply_to_identifiers():
    assert check_identifier("10.0.0.1").pii_type == "IP"
    assert check_identifier("https://x.example/t").pii_type == "URL"
    assert check_identifier("123-45-6789").pii_type == "SSN"


# ---- content ----------------------------------------------------------------

def test_string_leaves_are_redacted_in_place_and_recorded():
    result = walk({"note": f"mail {FIXTURE_EMAIL}", "nested": {"list": ["call 212-555-1234"]}})
    assert FIXTURE_EMAIL not in str(result.value)
    assert "212-555-1234" not in str(result.value)
    assert result.value["note"].startswith("mail [REDACTED_")
    kinds = {(f.path, f.kind, f.action) for f in result.findings}
    assert ("$.note", "content", "redacted") in kinds
    assert ("$.nested.list[0]", "content", "redacted") in kinds
    assert not result.refused


def test_overlapping_recognizer_results_do_not_corrupt_the_text():
    # An email the NER may also read as a location: one placeholder, intact.
    redacted, _ = pii_service.redact(f"mail me at {FIXTURE_EMAIL} please")
    assert redacted.startswith("mail me at [REDACTED_")
    assert redacted.endswith("] please")
    assert "TED_" not in redacted.replace("[REDACTED_", "")


# ---- walk_or_refuse and guards ------------------------------------------------

def test_walk_or_refuse_names_the_reason_argument_and_path_never_the_value():
    with pytest.raises(PiiRefused) as info:
        walk_or_refuse({"contacts": [2125551234]}, argument="output", reason="pii_in_output")
    err = info.value
    assert err.reason == "pii_in_output" and err.argument == "output"
    assert err.finding.path == "$.contacts[0]"
    assert "2125551234" not in str(err)
    assert walk_or_refuse({"ok": "fine"}, argument="output", reason="pii_in_output") == {"ok": "fine"}


def test_the_walker_refuses_values_beyond_its_guards():
    deep = current = {}
    for _ in range(pii_service.WALK_MAX_DEPTH + 2):
        current["d"] = {}
        current = current["d"]
    with pytest.raises(UnwalkableValue):
        walk(deep)


def test_findings_are_dataclasses_with_a_path():
    f = Finding("$.x", "number", "PHONE", 0.85, "refused")
    assert f.path == "$.x"
