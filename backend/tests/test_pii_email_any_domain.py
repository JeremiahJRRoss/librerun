"""An email address is PII at any top-level domain (blueprint S10 finding).

The release candidate's G0 walk planted an address in a support log and
watched the intake's redaction preview return it untouched. The chassis
left email addresses to stage 3, whose recognizer validates the domain
against the public-suffix list — so ``jsmith@corp.local``, the Active
Directory UPN that SSO and Kerberos logs carry on every line, reached the
database raw, while ``@….com`` was caught. The identifier check had its
own any-domain pattern all along, so a key was refused for what a value
kept.

Each case below fails on the tree before the fix: take
``("EMAIL_ADDRESS", _EMAIL_PATTERN, …)`` out of ``STAGE1_PATTERNS`` and
the non-public domains come back raw.
"""
from __future__ import annotations

import pytest

from app.services.pii_service import check_identifier, redact, walk

PLACEHOLDER = "[REDACTED_EMAIL_ADDRESS_1]"

# Domains the public-suffix list does not know. Every one of them is a
# real shape: AD's default forest name, the RFC 8375-era internal zones
# companies actually use, and RFC 2606's reserved example TLD.
NON_PUBLIC = ["corp.local", "acme.internal", "acme.corp", "helpdesk.lan", "zenith-customer.example"]


@pytest.mark.parametrize("domain", NON_PUBLIC)
def test_an_address_at_a_non_public_domain_is_redacted(domain):
    out, applied = redact(f"UPN user=jsmith@{domain} failed pre-auth", quiet=True)
    assert f"jsmith@{domain}" not in out
    # Only the address goes: `user=` is not part of it.
    assert f"user={PLACEHOLDER}" in out
    assert [r.pii_type for r in applied if r.placeholder == PLACEHOLDER] == ["EMAIL_ADDRESS"]


def test_a_public_domain_is_redacted_under_the_same_name():
    # The placeholder is the one stage 3 always produced, so every walker,
    # relay and report downstream sees exactly what it saw before.
    out, applied = redact("reply to ops@example.com", quiet=True)
    assert out == f"reply to {PLACEHOLDER}"
    assert applied[0].pii_type == "EMAIL_ADDRESS"


@pytest.mark.parametrize("domain", NON_PUBLIC + ["example.com"])
def test_content_and_identifiers_agree(domain):
    address = f"ops@{domain}"
    refused = check_identifier(address)
    assert refused is not None and refused.pii_type == "EMAIL_ADDRESS"
    out, _ = redact(f"contact {address}", quiet=True)
    assert address not in out


def test_digits_in_a_local_part_do_not_leave_the_domain_behind():
    # Stage 1 runs the address first. Run after PHONE, the digit run would
    # become a phone placeholder and `@corp.local` would survive beside it.
    out, _ = redact("badge 4155550139@corp.local swiped", quiet=True)
    assert "corp.local" not in out
    assert PLACEHOLDER in out


def test_a_package_version_is_not_an_address():
    # The top-level domain must be letters, so a pinned version is left
    # alone rather than masked out of every dependency log line.
    out, applied = redact("installed @ai-sdk/openai@4.0.71 and llama@3.2", quiet=True)
    assert "openai@4.0.71" in out and "llama@3.2" in out
    assert not [r for r in applied if r.pii_type == "EMAIL_ADDRESS"]


def test_the_walker_redacts_it_in_content_positions():
    result = walk({"note": "escalated to alice@acme.corp"})
    assert result.value == {"note": f"escalated to {PLACEHOLDER}"}
    assert [(f.kind, f.pii_type, f.action) for f in result.findings] == [
        ("content", "EMAIL_ADDRESS", "redacted")
    ]


def test_the_identifier_check_names_its_stage1_patterns():
    # The identifier list took stage 1's SSN and API-key patterns by INDEX;
    # a pattern added at the front of stage 1 would have made its "SSN"
    # the email rule and its "API_KEY" the phone rule, with no line of the
    # check changed. It looks them up by name now, and these say so.
    ssn = check_identifier("123-45-6789")
    assert ssn is not None and ssn.pii_type == "SSN"
    key = check_identifier("sk-" + "a" * 24)
    assert key is not None and key.pii_type == "API_KEY"
