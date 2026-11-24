"""Request handlers: the POST proxy and the /status page.

Proxy behaviour follows RFC 2616 (and its successors RFC 7230/7231):
  * hop-by-hop headers are stripped in both directions (§13.5.1),
  * a ``Via`` entry is appended in both directions (§14.45),
  * upstream connection failures map to 502 Bad Gateway, upstream timeouts to
    504 Gateway Timeout (§10.5.3, §10.5.5).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import aiohttp
from aiohttp import hdrs, web
from multidict import CIMultiDict, CIMultiDictProxy

from .config import Config
from .signing import create_token, extract_user

log = logging.getLogger("jwt_proxy")

JWT_HEADER = "x-my-jwt"
PROXY_NAME = "jwt-proxy"
READ_CHUNK = 64 * 1024

# RFC 2616 §13.5.1: hop-by-hop headers are meaningful for a single
# transport-level connection only and MUST NOT be forwarded by proxies.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)


@dataclass
class Metrics:
    """Per-process counters exposed on /status."""

    started_at: float = field(default_factory=time.time)
    _started_monotonic: float = field(default_factory=time.monotonic, repr=False)
    requests_processed: int = 0  # proxied POSTs that received a final answer
    requests_failed: int = 0  # subset answered with 502/504 by the proxy itself
    requests_in_flight: int = 0

    def uptime_seconds(self) -> float:
        return time.monotonic() - self._started_monotonic


CONFIG_KEY = web.AppKey("config", Config)
SESSION_KEY = web.AppKey("session", aiohttp.ClientSession)
METRICS_KEY = web.AppKey("metrics", Metrics)


# --------------------------------------------------------------------------- #
# Header helpers
# --------------------------------------------------------------------------- #
def end_to_end_headers(headers: CIMultiDictProxy[str] | CIMultiDict[str]) -> CIMultiDict[str]:
    """Copy ``headers`` without hop-by-hop fields (including any named in ``Connection``)."""
    drop = set(HOP_BY_HOP)
    for connection_value in headers.getall(hdrs.CONNECTION, []):
        drop.update(token.strip().lower() for token in connection_value.split(",") if token.strip())
    result: CIMultiDict[str] = CIMultiDict()
    for name, value in headers.items():
        if name.lower() not in drop:
            result.add(name, value)
    return result


def append_via(headers: CIMultiDict[str], version: aiohttp.HttpVersion) -> None:
    """Append this proxy to the ``Via`` chain (RFC 2616 §14.45)."""
    entry = f"{version.major}.{version.minor} {PROXY_NAME}"
    existing = headers.popall(hdrs.VIA, [])
    headers[hdrs.VIA] = ", ".join([*existing, entry])


def append_forwarded_for(headers: CIMultiDict[str], client_ip: str | None) -> None:
    if not client_ip:
        return
    existing = headers.popall("X-Forwarded-For", [])
    headers["X-Forwarded-For"] = ", ".join([*existing, client_ip])


# --------------------------------------------------------------------------- #
# Handlers
# --------------------------------------------------------------------------- #
async def proxy_post(request: web.Request) -> web.Response:
    """Forward a POST to the upstream with a freshly signed ``x-my-jwt`` header."""
    if request.method != hdrs.METH_POST:
        raise web.HTTPMethodNotAllowed(request.method, [hdrs.METH_POST])

    config = request.app[CONFIG_KEY]
    session = request.app[SESSION_KEY]
    metrics = request.app[METRICS_KEY]

    body = await request.read()  # bounded by client_max_size -> 413 if exceeded
    token = create_token(extract_user(body, config.default_user), config.jwt_secret)

    headers = end_to_end_headers(request.headers)
    headers.popall(hdrs.HOST, None)  # the client library sets the upstream Host
    headers.popall(hdrs.CONTENT_LENGTH, None)  # recomputed from the forwarded body
    headers.popall(JWT_HEADER, None)  # never trust a client-supplied token
    headers[JWT_HEADER] = token
    append_via(headers, request.version)
    append_forwarded_for(headers, request.remote)

    url = config.upstream_url + request.raw_path  # path + query, untouched
    metrics.requests_in_flight += 1
    started = time.monotonic()
    try:
        async with session.post(url, data=body, headers=headers, allow_redirects=False) as upstream:
            upstream_body = await _read_capped(upstream, config.max_response_bytes)
            response_headers = end_to_end_headers(upstream.headers)
            response_headers.popall(hdrs.CONTENT_LENGTH, None)
            append_via(response_headers, upstream.version)
            log.debug(
                "POST %s -> %d in %.1f ms", request.raw_path, upstream.status, (time.monotonic() - started) * 1000
            )
            return web.Response(
                status=upstream.status,
                reason=upstream.reason,
                headers=response_headers,
                body=upstream_body,
            )
    except asyncio.TimeoutError:
        metrics.requests_failed += 1
        log.warning("upstream %s timed out after %.1fs", url, config.upstream_timeout)
        raise web.HTTPGatewayTimeout(text="upstream request timed out")
    except UpstreamResponseTooLarge as exc:
        metrics.requests_failed += 1
        log.warning("upstream %s response exceeded %d bytes", url, exc.limit)
        raise web.HTTPBadGateway(text="upstream response too large")
    except aiohttp.ClientError as exc:
        metrics.requests_failed += 1
        log.warning("upstream %s failed: %s", url, exc)
        raise web.HTTPBadGateway(text="upstream request failed")
    finally:
        metrics.requests_in_flight -= 1
        metrics.requests_processed += 1


class UpstreamResponseTooLarge(Exception):
    def __init__(self, limit: int) -> None:
        super().__init__(f"upstream response exceeded {limit} bytes")
        self.limit = limit


async def _read_capped(upstream: aiohttp.ClientResponse, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    async for chunk in upstream.content.iter_chunked(READ_CHUNK):
        total += len(chunk)
        if total > limit:
            raise UpstreamResponseTooLarge(limit)
        chunks.append(chunk)
    return b"".join(chunks)


async def status(request: web.Request) -> web.Response:
    """Operational status: time since start and number of proxied requests."""
    if request.method not in (hdrs.METH_GET, hdrs.METH_HEAD):
        raise web.HTTPMethodNotAllowed(request.method, [hdrs.METH_GET, hdrs.METH_HEAD])
    metrics = request.app[METRICS_KEY]
    return web.json_response(
        {
            "status": "ok",
            "started_at": datetime.fromtimestamp(metrics.started_at, tz=timezone.utc).isoformat(timespec="seconds"),
            "uptime_seconds": round(metrics.uptime_seconds(), 3),
            "requests_processed": metrics.requests_processed,
            "requests_failed": metrics.requests_failed,
            "requests_in_flight": metrics.requests_in_flight,
        }
    )
