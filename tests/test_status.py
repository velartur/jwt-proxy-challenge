"""The /status page: uptime and request counters."""

from __future__ import annotations

from datetime import datetime


async def test_status_reports_uptime_and_counters(proxy):
    before = await (await proxy.get("/status")).json()
    assert before["status"] == "ok"
    assert before["uptime_seconds"] >= 0
    assert before["requests_processed"] == 0
    datetime.fromisoformat(before["started_at"])  # ISO 8601 with timezone

    for _ in range(3):
        await proxy.post("/a", data=b"{}")
    await proxy.post("/slow", data=b"{}")  # answered 504 by the proxy

    after = await (await proxy.get("/status")).json()
    assert after["uptime_seconds"] >= before["uptime_seconds"]
    assert after["requests_processed"] == 4
    assert after["requests_failed"] == 1
    assert after["requests_in_flight"] == 0


async def test_status_requests_are_not_counted_or_proxied(proxy, upstream_calls):
    await proxy.get("/status")
    await proxy.head("/status")
    response = await proxy.post("/status", data=b"{}")

    assert response.status == 405
    assert response.headers["Allow"] == "GET,HEAD"
    assert upstream_calls == []
    assert (await (await proxy.get("/status")).json())["requests_processed"] == 0
