from __future__ import annotations

import hashlib
import hmac

import pytest

from ringhub.models import Gesture
from ringhub.webhook import WebhookError, authenticate, parse_button_body, sign, signature_input

from .conftest import SECRET, signed_request


def test_signature_input_matches_app_vector() -> None:
    # Mirrors IndexWebhookSigningTest.signatureInputContainsVersionedMetadataThenExactBodyBytes
    body = bytes([0, 1, 2, 255])
    assert signature_input("1", 1_700_000_000, "delivery-123", "single-click-hold", False, body) == (
        b"v1\n1700000000\ndelivery-123\nsingle-click-hold\n0\n" + body
    )


def test_sign_is_lowercase_hex_hmac_sha256() -> None:
    sig = sign("0123456789abcdef0123456789abcdef", "1", 1_700_000_000, "delivery-123", "single-click-hold",
               False, b"\x01\x02\x03")
    expected = hmac.new(b"0123456789abcdef0123456789abcdef",
                        b"v1\n1700000000\ndelivery-123\nsingle-click-hold\n0\n\x01\x02\x03",
                        hashlib.sha256).hexdigest()
    assert sig == expected and len(sig) == 64 and sig == sig.lower()


def test_valid_request_authenticates() -> None:
    body, headers = signed_request("click")
    env = authenticate(headers, body, [SECRET], max_skew_seconds=300)
    assert env.gesture is Gesture.CLICK and not env.is_test


def test_header_names_are_case_insensitive() -> None:
    body, headers = signed_request("click")
    env = authenticate({k.lower(): v for k, v in headers.items()}, body, [SECRET], max_skew_seconds=300)
    assert env.trigger == "click"


@pytest.mark.parametrize("mutate,status", [
    (lambda h, b: (h | {"X-Index-Signature": "0" * 64}, b), 401),
    (lambda h, b: (h, b + b" "), 401),
    (lambda h, b: (h | {"X-Index-Trigger": "double-click"}, b), 401),
    (lambda h, b: (h | {"X-Index-Webhook-Version": "2"}, b), 400),
    (lambda h, b: ({k: v for k, v in h.items() if k != "X-Index-Timestamp"}, b), 401),
    (lambda h, b: ({k: v for k, v in h.items() if k != "X-Index-Delivery"}, b), 400),
])
def test_tampering_is_rejected(mutate, status) -> None:  # type: ignore[no-untyped-def]
    body, headers = signed_request("click")
    headers, body = mutate(headers, body)
    with pytest.raises(WebhookError) as exc:
        authenticate(headers, body, [SECRET], max_skew_seconds=300)
    assert exc.value.status == status


def test_stale_timestamp_rejected() -> None:
    body, headers = signed_request("click", ts=1_000_000_000)
    with pytest.raises(WebhookError, match="window"):
        authenticate(headers, body, [SECRET], max_skew_seconds=300)


def test_any_candidate_secret_accepted_for_test_events() -> None:
    body, headers = signed_request("test-event", test=True, secret="per-gesture-secret-xxxxxxxxxxxxxxxxxx")
    env = authenticate(headers, body, [SECRET, "per-gesture-secret-xxxxxxxxxxxxxxxxxx"], max_skew_seconds=300)
    assert env.is_test


def test_unsigned_allowed_only_without_secrets() -> None:
    body, headers = signed_request("click", secret=None)
    assert authenticate(headers, body, [], max_skew_seconds=300).trigger == "click"
    with pytest.raises(WebhookError):
        authenticate(headers, body, [SECRET], max_skew_seconds=300)


def test_parse_button_body_embeds_payload() -> None:
    body, _ = signed_request("triple-click", payload={"entity_id": "light.desk"})
    parsed = parse_button_body(body)
    assert parsed.gesture is Gesture.TRIPLE_CLICK and parsed.payload == {"entity_id": "light.desk"}


@pytest.mark.parametrize("body", [b"nope", b"[]", b'{"event":"other"}', b'{"event":"button","gesture":"x"}'])
def test_parse_button_body_rejects_garbage(body: bytes) -> None:
    with pytest.raises(WebhookError):
        parse_button_body(body)
