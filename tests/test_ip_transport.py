"""Quota, cache, dispatch and retry contracts across routed source clients."""
from datetime import datetime, timezone

import httpx
import pytest

from dedomena.sources import BudgetExceeded, IPPool, IPRoute, OpenAlex, Store, Throttled
from dedomena.sources.core import SourceError, Transport, digest, window


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def routed(*handlers):
    return IPPool([IPRoute(f"203.0.113.{i + 1}", client=httpx.Client(
        transport=httpx.MockTransport(handler))) for i, handler in enumerate(handlers)])


def source(store, clock, pool, **kwargs):
    kwargs.setdefault("requests_per_second", 1000)
    return Transport("test", "https://example.org", store=store, ip_pool=pool,
                     sleep=clock.sleep, **kwargs)


def test_shared_cache_identity_and_replay_preserve_acquisition_route():
    clock, sent = Clock(), []
    def handler(label):
        return lambda request: sent.append(label) or httpx.Response(200, json={"value": "1.00001"})
    with routed(handler("first"), handler("second")) as pool:
        store = Store(clock=clock)
        client = source(store, clock, pool, ip_limit=(10, 60))
        first = client._request("GET", "/same")
        cached = client._request("GET", "/same")
        refreshed = client._request("GET", "/same", refresh=True)
        assert sent == ["first", "second"]
        assert cached.cache_hit and cached.provenance == first.provenance
        assert first.provenance.request_sha256 == refreshed.provenance.request_sha256
        assert first.provenance.egress_ip_sha256 == digest(b"203.0.113.1")
        assert refreshed.provenance.egress_ip_sha256 == digest(b"203.0.113.2")
        assert store.replay(first.provenance.snapshot_id).provenance == first.provenance
        assert client.usage()["ip_weight_used"] == 2
        client.close()
        assert not pool._closed
        store.close()


def test_openalex_pool_cannot_multiply_key_budget_or_charge_unsent_requests():
    clock, sent = Clock(), []
    def handler(request):
        sent.append(request.url.path)
        if request.url.path == "/rate-limit":
            return httpx.Response(200, json={"rate_limit": {
                "credits_limit": 10000, "credits_remaining": 10000,
                "credit_costs": {"singleton": 0, "list": 1, "search": 10}}})
        return httpx.Response(200, json={"results": [{"id": "https://openalex.org/W1"}],
                                        "meta": {"count": 1, "next_cursor": None}},
                              headers={"X-RateLimit-Credits-Used": "1"})
    store = Store(clock=clock)
    with routed(handler, handler) as pool:
        client = OpenAlex("fixture-secret", ip_pool=pool, store=store,
                          daily_credit_limit=1, sleep=clock.sleep)
        assert next(client.search(filter="type:article")).complete
        with pytest.raises(BudgetExceeded):
            next(client.search(filter="type:article", refresh=True))
        assert sent == ["/rate-limit", "/works"]
        with store.transaction() as db:
            assert db.execute("SELECT sum(weight) FROM ip_events").fetchone()[0] == 2
        assert store.used(client.scope, "credits", "day") == 1
    store.close()


def test_verified_weight_releases_capacity_for_a_known_cheap_request():
    clock = Clock()
    store = Store(clock=clock)
    with routed(lambda _: httpx.Response(200, json={})) as pool:
        client = source(store, clock, pool, ip_limit=(4, 60), max_wait=0)
        client._request("GET", "/large-estimate", rate_weight=4, actual_weight=lambda _: 2)
        clock.sleep(0.01)
        client._request("GET", "/cheap", rate_weight=2)
        clock.sleep(0.01)
        with pytest.raises(Throttled):
            client._request("GET", "/exhausted", rate_weight=1)
        assert client.usage()["ip_weight_reserved"] == 6
        assert client.usage()["ip_weight_used"] == 4
    store.close()


def test_uncertain_send_retains_weight_and_error_does_not_expose_credentials():
    clock = Clock()
    store = Store(clock=clock)
    def handler(request):
        raise httpx.ProxyError("http://user:private-password@example.net", request=request)
    with routed(handler) as pool:
        client = source(store, clock, pool, ip_limit=(4, 60), max_wait=0, max_attempts=1)
        with pytest.raises(SourceError, match="transport failure") as error:
            client._request("GET", "/uncertain", rate_weight=4)
        assert "private-password" not in str(error.value)
        clock.sleep(0.01)
        with pytest.raises(Throttled):
            client._request("GET", "/next")
        assert client.usage()["ip_weight_used"] == 4
    store.close()


def test_budget_work_precedes_fresh_dispatch_lease():
    clock, sent = Clock(), []
    store = Store(clock=clock)
    original = store._reserve_in
    delayed = False
    def reserve(*args, **kwargs):
        nonlocal delayed
        if not delayed:
            delayed = True
            clock.sleep(60)
        return original(*args, **kwargs)
    store._reserve_in = reserve
    with routed(lambda _: sent.append(clock()) or httpx.Response(200, json={})) as pool:
        client = source(store, clock, pool, ip_limit=(1, 60))
        client._request("GET", "/first")
        client._request("GET", "/second")
    assert sent == [1060.0, 1120.0]
    store.close()


def test_slow_budget_admission_reanchors_both_calendar_windows():
    clock = Clock(datetime(2026, 10, 4, 23, 59, 59, tzinfo=timezone.utc).timestamp())
    store = Store(clock=clock)
    old_day, old_week = window("day", clock()), window("week", clock())
    original = store._reserve_in
    delayed = False
    def reserve(*args, **kwargs):
        nonlocal delayed
        result = original(*args, **kwargs)
        if not delayed:
            delayed = True
            clock.sleep(2)
        return result
    store._reserve_in = reserve
    with routed(lambda _: httpx.Response(200, content=b"{}")) as pool:
        client = source(store, clock, pool, ip_limit=(10, 60), max_response_bytes=10)
        response = client._request("GET", "/data", estimated_credits=1,
                                   credit_limit=100, byte_limit=100)
        assert response.provenance.retrieved_at_utc.startswith("2026-10-05")
        assert store.used(client.scope, "credits", "day") == 1
        assert store.used(client.scope, "bytes", "week") == 2
        with store.transaction() as db:
            rows = db.execute("SELECT window FROM budgets UNION SELECT window FROM budget_reservations").fetchall()
            assert all(key not in (old_day, old_week) for (key,) in rows)
    store.close()


@pytest.mark.parametrize("ip_only", [False, True])
def test_429_preserves_scope_while_an_independent_ip_can_continue(ip_only):
    clock, sent = Clock(), []
    store = Store(clock=clock)
    def first(request):
        sent.append(("first", clock()))
        return httpx.Response(429, headers={"Retry-After": "30"})
    def second(request):
        sent.append(("second", clock()))
        return httpx.Response(200, json={})
    with routed(first, second) as pool:
        client = source(store, clock, pool, ip_limit=(10, 60),
                        ip_throttle_only=ip_only, max_attempts=1)
        with pytest.raises(Throttled):
            client._request("GET", "/first")
        assert client._request("GET", "/second").provenance.http_status == 200
    assert [label for label, _ in sent] == ["first", "second"]
    assert (sent[1][1] - sent[0][1] < 30) == ip_only
    store.close()


@pytest.mark.parametrize("limit", [None, (101, 1), (100, 2), (True, 1)])
def test_openalex_provider_ip_policy_cannot_be_disabled_or_expanded(limit):
    with pytest.raises(ValueError, match="OpenAlex IP policy"):
        OpenAlex("fixture-secret", ip_limit=limit)


@pytest.mark.parametrize("use_pool", [False, True])
def test_positive_admission_deadline_includes_slow_budget_work(use_pool):
    clock, sent = Clock(), []
    store = Store(clock=clock)
    original = store._reserve_in
    delayed = False
    def reserve(*args, **kwargs):
        nonlocal delayed
        if not delayed:
            delayed = True
            clock.sleep(2)
        return original(*args, **kwargs)
    store._reserve_in = reserve
    handler = lambda _: sent.append(clock()) or httpx.Response(200, json={})
    with routed(handler) as pool:
        options = {"ip_pool": pool, "ip_limit": (10, 60)} if use_pool else {
            "client": httpx.Client(transport=httpx.MockTransport(handler))}
        client = Transport("test", "https://example.org", store=store,
                           sleep=clock.sleep, max_wait=1, **options)
        with pytest.raises(Throttled):
            client._request("GET", "/data", estimated_credits=1, credit_limit=100)
        assert not sent
        with store.transaction() as db:
            assert db.execute("SELECT count(*) FROM budget_reservations").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM pacing").fetchone()[0] == 0
        if not use_pool:
            options["client"].close()
    store.close()
