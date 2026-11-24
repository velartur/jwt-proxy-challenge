"""End-to-end behaviour of the proxy against a fake upstream."""

from __future__ import annotations

import gzip
import json

import jwt
import pytest

from jwt_proxy.app import create_app
from jwt_proxy.proxy import JWT_HEADER

from .conftest import MAX_BODY, SECRET, make_config, upstream_url


def decode(token: str) -> dict:
    return jwt.decode(token, SECRET, algorithms=["HS512"])


async def test_post_is_forwarded_with_a_signed_jwt(proxy, upstream_calls):
    payload = {"user": "alice", "items": [1, 2, 3]}

    response = await proxy.post("/api/orders?dry_run=1", json=payload)

    assert response.status == 200
    assert await response.json() == payload  # upstream echoed what it received
    assert response.headers["X-Upstream"] == "echo"

    (call,) = upstream_calls
    assert call["method"] == "POST"
    assert call["path_qs"] == "/api/orders?dry_run=1"
    assert json.loads(call["body"]) == payload
    assert call["headers"]["Content-Type"] == "application/json"

    claims = decode(call["headers"][JWT_HEADER])
    assert claims["payload"]["user"] == "alice"
    assert set(claims) == {"iat", "jti", "payload"}


async def test_each_request_gets_a_fresh_token(proxy, upstream_calls):
    await proxy.post("/a", json={"user": "alice"})
    await proxy.post("/a", json={"user": "alice"})

    first, second = (decode(call["headers"][JWT_HEADER]) for call in upstream_calls)
    assert first["jti"] != second["jti"]


async def test_client_supplied_jwt_header_is_replaced(proxy, upstream_calls):
    await proxy.post("/a", data=b"{}", headers={JWT_HEADER: "forged"})

    tokens = upstream_calls[0]["headers"].getall(JWT_HEADER)
    assert len(tokens) == 1
    assert tokens[0] != "forged"
    decode(tokens[0])  # valid signature


async def test_username_falls_back_when_body_is_not_json(proxy, upstream_calls):
    await proxy.post("/a", data=b"plain text", headers={"Content-Type": "text/plain"})

    assert decode(upstream_calls[0]["headers"][JWT_HEADER])["payload"]["user"] == "anonymous"
    assert upstream_calls[0]["body"] == b"plain text"  # body forwarded untouched


async def test_hop_by_hop_headers_are_not_forwarded(proxy, upstream_calls):
    response = await proxy.post(
        "/a",
        data=b"{}",
        headers={
            "Connection": "keep-alive, X-Per-Hop",
            "X-Per-Hop": "1",
            "Proxy-Authorization": "Basic Zm9vOmJhcg==",
            "Keep-Alive": "timeout=5",
            "X-End-To-End": "kept",
        },
    )

    seen = upstream_calls[0]["headers"]
    for name in ("X-Per-Hop", "Proxy-Authorization", "Keep-Alive"):
        assert name not in seen, name
    assert seen["X-End-To-End"] == "kept"
    assert seen["Host"] != proxy.host  # upstream sees its own host, not the proxy's

    for name in ("Keep-Alive", "Proxy-Authenticate"):
        assert name not in response.headers, name


async def test_via_and_x_forwarded_for_are_added(proxy, upstream_calls):
    response = await proxy.post("/a", data=b"{}")

    seen = upstream_calls[0]["headers"]
    assert seen["Via"] == "1.1 jwt-proxy"
    assert seen["X-Forwarded-For"] == "127.0.0.1"
    assert response.headers["Via"] == "1.1 jwt-proxy"


async def test_existing_via_and_x_forwarded_for_are_extended(proxy, upstream_calls):
    await proxy.post("/a", data=b"{}", headers={"Via": "1.0 fred", "X-Forwarded-For": "10.1.2.3"})

    seen = upstream_calls[0]["headers"]
    assert seen["Via"] == "1.0 fred, 1.1 jwt-proxy"
    assert seen["X-Forwarded-For"] == "10.1.2.3, 127.0.0.1"


async def test_upstream_error_status_passes_through(proxy):
    response = await proxy.post("/fail", data=b"{}")

    assert response.status == 503
    assert await response.text() == "upstream says no"
    assert response.headers["Retry-After"] == "7"


async def test_upstream_redirect_is_returned_not_followed(proxy):
    response = await proxy.post("/redirect", data=b"{}", allow_redirects=False)

    assert response.status == 302
    assert response.headers["Location"] == "https://example.invalid/elsewhere"


async def test_compressed_upstream_body_passes_through_unchanged(aiohttp_client, upstream):
    client = await aiohttp_client(create_app(make_config(upstream_url(upstream))), auto_decompress=False)

    response = await client.post("/gzipped", data=b"{}")

    assert response.headers["Content-Encoding"] == "gzip"
    assert gzip.decompress(await response.read()) == b"compressed payload"


@pytest.mark.parametrize("method", ["GET", "PUT", "DELETE", "PATCH"])
async def test_only_post_is_proxied(proxy, upstream_calls, method):
    response = await proxy.request(method, "/a")

    assert response.status == 405
    assert response.headers["Allow"] == "POST"
    assert upstream_calls == []


async def test_oversized_request_body_is_rejected(proxy, upstream_calls):
    response = await proxy.post("/a", data=b"x" * (MAX_BODY + 1))

    assert response.status == 413
    assert upstream_calls == []


async def test_upstream_timeout_maps_to_504(proxy):
    response = await proxy.post("/slow", data=b"{}")

    assert response.status == 504


async def test_oversized_upstream_response_maps_to_502(proxy):
    response = await proxy.post("/big", data=b"{}")

    assert response.status == 502


async def test_unreachable_upstream_maps_to_502(aiohttp_client, unused_tcp_port):
    client = await aiohttp_client(create_app(make_config(f"http://127.0.0.1:{unused_tcp_port}")))

    response = await client.post("/a", data=b"{}")

    assert response.status == 502
