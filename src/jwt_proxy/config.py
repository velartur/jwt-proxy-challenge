"""Runtime configuration, read from environment variables (12-factor style).

Every setting has a safe default except the signing secret, which must be
provided explicitly so the proxy never starts with an unknown key.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from urllib.parse import urlsplit

# RFC 7518 §3.2: an HMAC key MUST be at least as long as the hash output
# (512 bits = 64 bytes for HS512).
MIN_HS512_KEY_BYTES = 64


class ConfigError(ValueError):
    """Raised when the environment does not describe a valid configuration."""


@dataclass(frozen=True, slots=True)
class Config:
    host: str
    port: int
    upstream_url: str
    jwt_secret: bytes
    upstream_timeout: float
    max_body_bytes: int
    max_response_bytes: int
    default_user: str
    log_level: str

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env

        secret_hex = env.get("JWT_SECRET_HEX", "").strip()
        if not secret_hex:
            raise ConfigError("JWT_SECRET_HEX is required (hex-encoded HS512 secret)")
        try:
            secret = bytes.fromhex(secret_hex)
        except ValueError as exc:
            raise ConfigError("JWT_SECRET_HEX must be a valid hex string") from exc
        if len(secret) < MIN_HS512_KEY_BYTES:
            raise ConfigError(
                f"JWT_SECRET_HEX must decode to at least {MIN_HS512_KEY_BYTES} bytes "
                f"for HS512 (RFC 7518 §3.2), got {len(secret)}"
            )

        upstream_url = env.get("UPSTREAM_URL", "https://postman-echo.com").strip().rstrip("/")
        parts = urlsplit(upstream_url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ConfigError("UPSTREAM_URL must be an absolute http(s) URL")

        return cls(
            host=env.get("HTTP_HOST", "127.0.0.1"),
            port=_positive_int(env, "HTTP_PORT", 8080),
            upstream_url=upstream_url,
            jwt_secret=secret,
            upstream_timeout=_positive_float(env, "UPSTREAM_TIMEOUT_SECONDS", 10.0),
            max_body_bytes=_positive_int(env, "MAX_BODY_BYTES", 1024 * 1024),
            max_response_bytes=_positive_int(env, "MAX_RESPONSE_BYTES", 10 * 1024 * 1024),
            default_user=env.get("DEFAULT_USER", "anonymous"),
            log_level=env.get("LOG_LEVEL", "INFO").upper(),
        )


def _positive_int(env: dict[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive")
    return value


def _positive_float(env: dict[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if value <= 0:
        raise ConfigError(f"{name} must be positive")
    return value
