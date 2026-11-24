"""Configuration parsing and validation."""

from __future__ import annotations

import pytest

from jwt_proxy.config import Config, ConfigError

from .conftest import SECRET, SECRET_HEX


def test_defaults_with_only_the_secret():
    config = Config.from_env({"JWT_SECRET_HEX": SECRET_HEX})

    assert config.host == "127.0.0.1"  # private by default
    assert config.port == 8080
    assert config.upstream_url == "https://postman-echo.com"
    assert config.jwt_secret == SECRET
    assert config.upstream_timeout == 10.0
    assert config.default_user == "anonymous"


def test_environment_overrides():
    config = Config.from_env(
        {
            "JWT_SECRET_HEX": SECRET_HEX,
            "HTTP_HOST": "0.0.0.0",
            "HTTP_PORT": "9090",
            "UPSTREAM_URL": "http://upstream.internal:8000/",
            "UPSTREAM_TIMEOUT_SECONDS": "2.5",
            "DEFAULT_USER": "service-account",
            "LOG_LEVEL": "debug",
        }
    )

    assert (config.host, config.port) == ("0.0.0.0", 9090)
    assert config.upstream_url == "http://upstream.internal:8000"  # trailing slash removed
    assert config.upstream_timeout == 2.5
    assert config.default_user == "service-account"
    assert config.log_level == "DEBUG"


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "JWT_SECRET_HEX is required"),
        ({"JWT_SECRET_HEX": "not-hex"}, "valid hex"),
        ({"JWT_SECRET_HEX": "ab" * 32}, "at least 64 bytes"),
        ({"JWT_SECRET_HEX": SECRET_HEX, "UPSTREAM_URL": "postman-echo.com"}, "absolute http"),
        ({"JWT_SECRET_HEX": SECRET_HEX, "HTTP_PORT": "eighty"}, "HTTP_PORT must be an integer"),
        ({"JWT_SECRET_HEX": SECRET_HEX, "HTTP_PORT": "0"}, "HTTP_PORT must be positive"),
        ({"JWT_SECRET_HEX": SECRET_HEX, "UPSTREAM_TIMEOUT_SECONDS": "-1"}, "must be positive"),
    ],
)
def test_invalid_configuration_is_rejected(env: dict[str, str], message: str):
    with pytest.raises(ConfigError, match=message):
        Config.from_env(env)
