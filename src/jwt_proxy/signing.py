"""JWT creation (RFC 7519) signed with HS512 (RFC 7518 §3.2).

Claims:
  iat      request timestamp as a NumericDate (seconds since the Unix epoch)
  jti      cryptographically random nonce, unique per token
  payload  {"user": <username>, "date": <today's date, UTC, YYYY-MM-DD>}
"""

from __future__ import annotations

import json
import secrets
import time
from datetime import datetime, timezone
from typing import Any

import jwt

ALGORITHM = "HS512"
JTI_BYTES = 32  # 256 bits of randomness


def build_claims(user: str, now: float | None = None) -> dict[str, Any]:
    """Return the claim set for a request made at ``now`` (defaults to the current time)."""
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now, tz=timezone.utc).date().isoformat()
    return {
        "iat": int(now),
        "jti": secrets.token_hex(JTI_BYTES),
        "payload": {"user": user, "date": today},
    }


def sign(claims: dict[str, Any], secret: bytes) -> str:
    """Serialize ``claims`` as a compact JWS signed with HS512."""
    return jwt.encode(claims, secret, algorithm=ALGORITHM)


def create_token(user: str, secret: bytes, now: float | None = None) -> str:
    return sign(build_claims(user, now), secret)


def extract_user(body: bytes, default: str) -> str:
    """Pick the username from a JSON request body (``user`` or ``username`` key).

    Anything that is not a JSON object with a string in one of those keys falls
    back to ``default``; the proxy never rejects a request because of this.
    """
    if not body:
        return default
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return default
    if not isinstance(data, dict):
        return default
    for key in ("user", "username"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default
