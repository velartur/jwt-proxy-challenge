"""Unit tests for JWT creation and username extraction."""

from __future__ import annotations

import secrets
from datetime import datetime, timezone

import jwt
import pytest

from jwt_proxy.signing import ALGORITHM, build_claims, create_token, extract_user

from .conftest import SECRET


def test_token_is_hs512_and_verifies_with_hex_decoded_secret():
    token = create_token("alice", SECRET)

    assert jwt.get_unverified_header(token) == {"alg": "HS512", "typ": "JWT"}
    assert ALGORITHM == "HS512"
    claims = jwt.decode(token, SECRET, algorithms=["HS512"])  # raises if the signature is wrong
    assert claims["payload"]["user"] == "alice"


def test_token_is_rejected_with_another_secret():
    token = create_token("alice", SECRET)
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, secrets.token_bytes(64), algorithms=["HS512"])


def test_token_is_rejected_when_algorithm_is_not_hs512():
    token = create_token("alice", SECRET)
    with pytest.raises(jwt.InvalidAlgorithmError):
        jwt.decode(token, SECRET, algorithms=["HS256"])


def test_claims_follow_the_specification():
    now = 1_700_000_000.75  # 2023-11-14T22:13:20.75Z
    claims = build_claims("bob", now=now)

    assert set(claims) == {"iat", "jti", "payload"}
    assert claims["iat"] == 1_700_000_000  # NumericDate, whole seconds (RFC 7519 §4.1.6)
    assert claims["payload"] == {"user": "bob", "date": "2023-11-14"}
    assert len(claims["jti"]) == 64 and int(claims["jti"], 16) >= 0  # 256-bit hex nonce


def test_date_is_todays_utc_date():
    claims = build_claims("bob")
    assert claims["payload"]["date"] == datetime.now(timezone.utc).date().isoformat()


def test_jti_is_unique_per_token():
    jtis = {build_claims("bob")["jti"] for _ in range(1000)}
    assert len(jtis) == 1000


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (b'{"user": "carol"}', "carol"),
        (b'{"username": "dave"}', "dave"),
        (b'{"user": "  erin  "}', "erin"),
        (b'{"user": "", "username": "frank"}', "frank"),
        (b'{"user": 42}', "anonymous"),
        (b'{"other": "field"}', "anonymous"),
        (b"[1, 2, 3]", "anonymous"),
        (b"not json at all", "anonymous"),
        (b"\xff\xfe", "anonymous"),
        (b"", "anonymous"),
    ],
)
def test_extract_user(body: bytes, expected: str):
    assert extract_user(body, "anonymous") == expected
