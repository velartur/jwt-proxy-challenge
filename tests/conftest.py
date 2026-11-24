"""Shared fixtures: a fake upstream server and a proxy wired to it."""

from __future__ import annotations

import asyncio
import gzip

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from jwt_proxy.app import create_app
from jwt_proxy.config import Config

# The secret from the challenge statement (hex -> 64 bytes = 512 bits).
SECRET_HEX = (
    "a9ddbcaba8c0ac1a0a812dc0c2f08514b23f2db0a68343cb8199ebb38a6d91e4"
    "ebfb378e22ad39c2d01d0b4ec9c34aa91056862ddace3fbbd6852ee60c36acbf"
)
SECRET = bytes.fromhex(SECRET_HEX)

UPSTREAM_TIMEOUT = 0.5
MAX_BODY = 1024
MAX_RESPONSE = 4096


def make_config(upstream_url: str, **overrides) -> Config:
    values = dict(
        host="127.0.0.1",
        port=0,
        upstream_url=upstream_url,
        jwt_secret=SECRET,
        upstream_timeout=UPSTREAM_TIMEOUT,
        max_body_bytes=MAX_BODY,
        max_response_bytes=MAX_RESPONSE,
        default_user="anonymous",
        log_level="WARNING",
    )
    values.update(overrides)
    return Config(**values)


@pytest.fixture
def upstream_calls() -> list[dict]:
    """Every request the fake upstream received, in order."""
    return []


@pytest.fixture
async def upstream(aiohttp_server, upstream_calls) -> TestServer:
    """Fake upstream: records requests and offers a few misbehaving endpoints."""

    async def record(request: web.Request) -> bytes:
        body = await request.read()
        upstream_calls.append(
            {
                "method": request.method,
                "path_qs": request.path_qs,
                "headers": request.headers.copy(),
                "body": body,
            }
        )
        return body

    async def echo(request: web.Request) -> web.Response:
        body = await record(request)
        return web.Response(
            status=200,
            body=body,
            headers={
                "Content-Type": request.headers.get("Content-Type", "application/octet-stream"),
                "X-Upstream": "echo",
                # hop-by-hop headers that a compliant proxy must not forward
                "Keep-Alive": "timeout=5",
                "Proxy-Authenticate": "Basic realm=upstream",
            },
        )

    async def fail(request: web.Request) -> web.Response:
        await record(request)
        return web.Response(status=503, text="upstream says no", headers={"Retry-After": "7"})

    async def redirect(request: web.Request) -> web.Response:
        await record(request)
        raise web.HTTPFound("https://example.invalid/elsewhere")

    async def slow(request: web.Request) -> web.Response:
        await record(request)
        await asyncio.sleep(UPSTREAM_TIMEOUT * 4)
        return web.Response(text="too late")

    async def big(request: web.Request) -> web.Response:
        await record(request)
        return web.Response(body=b"x" * (MAX_RESPONSE * 2))

    async def gzipped(request: web.Request) -> web.Response:
        await record(request)
        return web.Response(body=gzip.compress(b"compressed payload"), headers={"Content-Encoding": "gzip"})

    app = web.Application()
    app.router.add_post("/fail", fail)
    app.router.add_post("/redirect", redirect)
    app.router.add_post("/slow", slow)
    app.router.add_post("/big", big)
    app.router.add_post("/gzipped", gzipped)
    app.router.add_route("*", "/{tail:.*}", echo)
    return await aiohttp_server(app)


def upstream_url(server: TestServer) -> str:
    return f"http://{server.host}:{server.port}"


@pytest.fixture
async def proxy(aiohttp_client, upstream) -> TestClient:
    """A proxy client whose upstream is the fake server above."""
    return await aiohttp_client(create_app(make_config(upstream_url(upstream))))
