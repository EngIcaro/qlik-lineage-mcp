"""Tests for the shared session cache.

Strategy: mock the Qlik API with respx and assert that the *number of HTTP
calls* drops to 1 after the first hit, even across separate QlikClient
instances (simulating back-to-back tool calls in the same Claude session).
"""

from __future__ import annotations

import time

import httpx
import pytest
import respx

from qlik_lineage_mcp.config import Settings
from qlik_lineage_mcp.qlik_client import QlikClient
from qlik_lineage_mcp.session_cache import _cache, SessionCache

BASE = "https://tenant.qlikcloud.com"
SETTINGS = Settings(tenant_url=BASE, api_key="test-key", request_timeout_s=10)

APPS_RESPONSE = {
    "data": [
        {
            "resourceType": "app",
            "resourceId": "app-1",
            "name": "Analytics App",
            "spaceId": "sp1",
            "resourceAttributes": {"id": "app-1"},
            "resourceSize": {},
        }
    ],
    "links": {},
}

LINEAGE_RESPONSE = [
    {"discriminator": "lib://CONN:DataFiles/Sales.qvd;", "statement": ""}
]


# ---------------------------------------------------------------------------
# list_apps_in_tenant: second call must not hit the network
# ---------------------------------------------------------------------------

@respx.mock
async def test_list_apps_in_tenant_cached_across_client_instances():
    route = respx.get(f"{BASE}/api/v1/items").mock(
        return_value=httpx.Response(200, json=APPS_RESPONSE)
    )

    # First client instance — simulates ghost_files tool call
    async with QlikClient(SETTINGS) as c1:
        apps1 = await c1.list_apps_in_tenant()

    # Second client instance — simulates unused_columns tool call
    async with QlikClient(SETTINGS) as c2:
        apps2 = await c2.list_apps_in_tenant()

    assert route.call_count == 1, (
        f"Expected 1 HTTP call (cache hit on 2nd client), got {route.call_count}"
    )
    assert len(apps1) == len(apps2) == 1
    assert apps1[0].id == apps2[0].id


# ---------------------------------------------------------------------------
# get_app_lineage: N apps × 2 tools must not double the requests
# ---------------------------------------------------------------------------

@respx.mock
async def test_get_app_lineage_cached_across_client_instances():
    route = respx.get(f"{BASE}/api/v1/apps/app-1/data/lineage").mock(
        return_value=httpx.Response(200, json=LINEAGE_RESPONSE)
    )

    async with QlikClient(SETTINGS) as c1:
        entries1 = await c1.get_app_lineage("app-1")

    async with QlikClient(SETTINGS) as c2:
        entries2 = await c2.get_app_lineage("app-1")

    assert route.call_count == 1
    assert entries1 == entries2


# ---------------------------------------------------------------------------
# Different tenants must NOT share cache entries
# ---------------------------------------------------------------------------

@respx.mock
async def test_different_tenants_do_not_share_cache():
    base_a = "https://tenant-a.qlikcloud.com"
    base_b = "https://tenant-b.qlikcloud.com"
    settings_a = Settings(tenant_url=base_a, api_key="key-a", request_timeout_s=10)
    settings_b = Settings(tenant_url=base_b, api_key="key-b", request_timeout_s=10)

    route_a = respx.get(f"{base_a}/api/v1/items").mock(
        return_value=httpx.Response(200, json=APPS_RESPONSE)
    )
    route_b = respx.get(f"{base_b}/api/v1/items").mock(
        return_value=httpx.Response(200, json=APPS_RESPONSE)
    )

    async with QlikClient(settings_a) as ca:
        await ca.list_apps_in_tenant()

    async with QlikClient(settings_b) as cb:
        await cb.list_apps_in_tenant()

    assert route_a.call_count == 1
    assert route_b.call_count == 1


# ---------------------------------------------------------------------------
# Cache expiry: after TTL a stale entry is evicted and network is called again
# ---------------------------------------------------------------------------

def test_cache_entry_expires_after_ttl(monkeypatch):
    cache = SessionCache(ttl_s=10)
    key = ("tenant", "apps")

    t0 = 1000.0
    monkeypatch.setattr(time, "monotonic", lambda: t0)
    cache.set(key, ["cached_value"])

    # Within TTL — hit
    monkeypatch.setattr(time, "monotonic", lambda: t0 + 9)
    hit, val = cache.get(key)
    assert hit is True
    assert val == ["cached_value"]

    # Past TTL — miss, entry evicted
    monkeypatch.setattr(time, "monotonic", lambda: t0 + 11)
    hit, val = cache.get(key)
    assert hit is False
    assert val is None
    assert key not in cache._store  # evicted


# ---------------------------------------------------------------------------
# Cache miss: unknown key returns (False, None)
# ---------------------------------------------------------------------------

def test_cache_miss_returns_false_none():
    cache = SessionCache()
    hit, val = cache.get(("no", "such", "key"))
    assert hit is False
    assert val is None


# ---------------------------------------------------------------------------
# find_space_by_name: second lookup must not paginate spaces again
# ---------------------------------------------------------------------------

@respx.mock
async def test_find_space_by_name_cached():
    route = respx.get(f"{BASE}/api/v1/spaces").mock(
        return_value=httpx.Response(200, json={
            "data": [{"id": "sp1", "name": "QVD_ASUS", "type": "managed",
                      "ownerId": "u1", "tenantId": "t1", "links": {}}],
            "links": {},
        })
    )

    async with QlikClient(SETTINGS) as c1:
        s1 = await c1.find_space_by_name("qvd_asus")

    async with QlikClient(SETTINGS) as c2:
        s2 = await c2.find_space_by_name("qvd_asus")

    assert route.call_count == 1
    assert s1 is not None and s2 is not None
    assert s1.id == s2.id == "sp1"
