"""Application factory and command-line entry point."""

from __future__ import annotations

import logging
import sys
from collections.abc import AsyncIterator

import aiohttp
from aiohttp import web

from . import __version__
from .config import Config, ConfigError
from .proxy import CONFIG_KEY, METRICS_KEY, SESSION_KEY, Metrics, proxy_post, status

log = logging.getLogger("jwt_proxy")

# Headers aiohttp's client would add on its own. A proxy forwards what the
# client sent and nothing more, so these are left alone.
_NO_AUTO_HEADERS = ("Accept", "Accept-Encoding", "User-Agent", "Content-Type")


def create_app(config: Config) -> web.Application:
    app = web.Application(client_max_size=config.max_body_bytes)
    app[CONFIG_KEY] = config
    app[METRICS_KEY] = Metrics()
    app.cleanup_ctx.append(_upstream_session)

    # /status is reserved for the proxy itself; every other path is proxied.
    app.router.add_route("*", "/status", status)
    app.router.add_route("*", "/{tail:.*}", proxy_post)
    return app


async def _upstream_session(app: web.Application) -> AsyncIterator[None]:
    """One pooled client session for the whole process lifetime."""
    config = app[CONFIG_KEY]
    app[SESSION_KEY] = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=config.upstream_timeout),
        auto_decompress=False,  # pass upstream bodies through byte-for-byte
        skip_auto_headers=_NO_AUTO_HEADERS,
    )
    try:
        yield
    finally:
        await app[SESSION_KEY].close()


def main() -> None:
    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"jwt-proxy: configuration error: {exc}", file=sys.stderr)
        sys.exit(2)

    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info(
        "jwt-proxy %s listening on http://%s:%d, forwarding POSTs to %s",
        __version__,
        config.host,
        config.port,
        config.upstream_url,
    )
    web.run_app(create_app(config), host=config.host, port=config.port, print=None)


if __name__ == "__main__":  # pragma: no cover
    main()
