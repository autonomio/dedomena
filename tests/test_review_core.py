"""Deterministic interleavings for quota windows, reservations and shared dispatch."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import sqlite3
import threading

import httpx
import pytest

from dedomena.sources.core import BudgetExceeded, Store, Throttled, Transport, window


class Clock:
    def __init__(self, stamp="2026-10-04T23:59:59+00:00"):
        self.now = datetime.fromisoformat(stamp).timestamp()
        self.waits = []
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def sleep(self, seconds):
        with self.lock:
            self.waits.append(seconds)
            self.now += seconds


def transport(store, clock, handler, **options):
    return Transport("test", "https://example.org", store=store, sleep=clock.sleep,
                     client=httpx.Client(transport=httpx.MockTransport(handler)), **options)


@pytest.mark.parametrize("period,unit", [("day", "credits"), ("week", "bytes")])
def test_response_observation_is_pinned_to_dispatch_window(period, unit):
    clock = Clock()
    store = Store(clock=clock)
    old_window = window(period, clock())

    def handler(request):
        clock.sleep(2)
        return httpx.Response(200, content=b"{}", headers={"X-RateLimit-Credits-Used": "0"})

    source = transport(store, clock, handler, max_response_bytes=10)

    def observe(headers, *, request_windows):
        store.observe_used(source.scope, unit, 99, period,
                           window_key=request_windows[period])

    source._request("GET", "/data", estimated_credits=10, credit_limit=100,
                    byte_limit=100, observe=observe)
    assert window(period, clock()) != old_window
    assert store.used(source.scope, unit, period) == 0
    with store.transaction() as db:
        assert db.execute("SELECT window,used FROM budget_observations WHERE unit=?",
                          (unit,)).fetchone() == (old_window, 99)
    # A reset observation in the new period cannot inherit the previous high-water mark.
    store.observe_used(source.scope, unit, 0, period)
    token = store.reserve(source.scope, unit, 100, 100, period,
                          window_key=window(period, clock()))
    assert store.used(source.scope, unit, period) == 100
    store.settle(token, 0)
    store.close()


def test_provider_floor_survives_older_refund_and_keeps_other_reservations(tmp_path):
    clock = Clock()
    path = tmp_path / "shared.sqlite3"
    first, second = Store(path, clock=clock), Store(path, clock=clock)
    scope, key = "epo:key", window("week", clock())
    older = first.reserve(scope, "bytes", 100, 1000, "week", window_key=key)
    newer = second.reserve(scope, "bytes", 100, 1000, "week", window_key=key)
    second.settle(newer, 10)
    second.observe_used(scope, "bytes", 990, "week", window_key=key)
    assert first.used(scope, "bytes", "week") == 1090
    first.settle(older, 0)
    assert first.used(scope, "bytes", "week") == 990
    pending = first.reserve(scope, "bytes", 10, 1000, "week", window_key=key)
    with pytest.raises(BudgetExceeded):
        second.reserve(scope, "bytes", 1, 1000, "week", window_key=key)
    assert second.used(scope, "bytes", "week") == 1000
    first.settle(pending, 0)
    assert second.used(scope, "bytes", "week") == 990
    first.close()
    second.close()


def test_new_reservations_are_atomic_across_store_connections(tmp_path):
    clock = Clock()
    path = tmp_path / "shared.sqlite3"
    stores = [Store(path, clock=clock), Store(path, clock=clock)]
    stores[0].observe_used("test:key", "credits", 990, "day")

    def reserve(store):
        try:
            return store.reserve("test:key", "credits", 10, 1000, "day",
                                 window_key=window("day", clock()))
        except BudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, stores))
    assert sum(result is not None for result in results) == 1
    assert stores[0].used("test:key", "credits", "day") == 1000
    for store in stores:
        store.close()


@pytest.mark.parametrize("scopes", [("all",), ("all", "retrieval")])
def test_sleeping_dispatch_rechecks_new_shared_cooldown(tmp_path, scopes):
    clock = Clock("2026-10-01T00:00:00+00:00")
    initial = clock()
    path = tmp_path / "shared.sqlite3"
    waiting, peer = Store(path, clock=clock), Store(path, clock=clock)
    limits = tuple(("test:key:" + scope, 1) for scope in scopes)
    # Preoccupy only the service slot: all scopes still have to be rechecked together.
    waiting.pace(limits[-1][0], 1, clock.sleep, 60)
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 1:
            clock.sleep(0.5)
            peer.defer("test:key:all", 10)
            clock.sleep(seconds - 0.5)
        else:
            clock.sleep(seconds)

    waiting.pace_many(limits, sleep, 60)
    assert waits == [1, 9.5]
    assert clock() == initial + 10.5
    waiting.close()
    peer.close()


def test_authentication_finishes_before_dispatch_slots_are_acquired():
    clock = Clock()
    store = Store(clock=clock)
    events = []
    original = store.pace_many

    def pace_many(*args):
        events.append(("pace", clock()))
        return original(*args)

    store.pace_many = pace_many

    def authenticate():
        clock.sleep(5)
        events.append(("auth", clock()))
        return {"Authorization": "Bearer fixture-token"}

    def handler(request):
        events.append(("send", clock()))
        return httpx.Response(200, json={})

    source = transport(store, clock, handler)
    source._request("GET", "/data", headers_factory=authenticate, service_rate=1)
    assert [kind for kind, _ in events] == ["auth", "pace", "send"]
    assert len({stamp for _, stamp in events}) == 1
    store.close()


@pytest.mark.parametrize("attempts,max_wait,retry_after", [(1, 60, 3), (3, 60, 120)])
def test_terminal_429_cooldown_is_shared_with_other_clients(tmp_path, attempts, max_wait, retry_after):
    clock = Clock()
    path = tmp_path / "shared.sqlite3"
    first, second = Store(path, clock=clock), Store(path, clock=clock)
    source = transport(first, clock, lambda r: httpx.Response(429, headers={"Retry-After": str(retry_after)}),
                       max_attempts=attempts, max_wait=max_wait)
    with pytest.raises(Throttled) as error:
        source._request("GET", "/data")
    assert error.value.retry_after == retry_after
    sent = []
    peer = transport(second, clock, lambda r: sent.append(r) or httpx.Response(200, json={}), max_wait=0)
    with pytest.raises(Throttled) as peer_error:
        peer._request("GET", "/other")
    assert peer_error.value.retry_after == retry_after
    assert not sent
    first.close()
    second.close()


def test_allowance_rejection_preserves_retry_after_cooldown():
    clock = Clock()
    store = Store(clock=clock)
    sent = []

    def handler(request):
        sent.append(request)
        return httpx.Response(429, headers={"Retry-After": "120", "X-RateLimit-Remaining": "0"})

    source = transport(store, clock, handler)
    with pytest.raises(BudgetExceeded):
        source._request("GET", "/data")
    with pytest.raises(Throttled):
        source._request("GET", "/other")
    assert len(sent) == 1
    store.close()


def test_existing_budget_schema_and_saved_snapshots_survive_upgrade(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    clock = Clock()
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE budgets(scope TEXT, unit TEXT, window TEXT, used INTEGER, "
               "PRIMARY KEY(scope,unit,window))")
    db.execute("INSERT INTO budgets VALUES(?,?,?,?)", ("test:key", "credits", window("day", clock()), 70))
    db.commit()
    db.close()
    store = Store(path, clock=clock)
    assert store.used("test:key", "credits", "day") == 70
    source = transport(store, clock, lambda r: httpx.Response(200, json={"saved": True}))
    response = source._request("GET", "/data")
    store.close()
    reopened = Store(path, clock=clock)
    assert reopened.replay(response.provenance.snapshot_id).body == response.body
    assert reopened.used("test:key", "credits", "day") == 70
    reopened.close()


def test_token_lock_waiters_acquire_fresh_slots_after_slow_oauth_refresh():
    clock = Clock("2026-10-01T00:00:00+00:00")
    initial = clock()
    store = Store(clock=clock)
    entered, token_lock, counting, admission_lock = threading.Event(), threading.Lock(), threading.Lock(), threading.Lock()
    ready = 0
    refreshed_at = None
    admissions = []
    original = store.pace_many

    def authenticate():
        nonlocal ready, refreshed_at
        with counting:
            ready += 1
            if ready == 3:
                entered.set()
        with token_lock:
            if refreshed_at is None:
                assert entered.wait(5), "other token-lock waiters did not reach authentication"
                clock.sleep(10)
                refreshed_at = clock()
            return {"Authorization": "Bearer fixture-token"}

    def pace_many(*args):
        # Keep only this test's admission timestamps atomic with the fake clock.
        with admission_lock:
            original(*args)
            admissions.append(clock())

    store.pace_many = pace_many
    source = transport(store, clock, lambda r: httpx.Response(200, json={}), requests_per_second=100)

    def fetch(number):
        return source._request("GET", f"/data/{number}", headers_factory=authenticate, service_rate=1)

    with ThreadPoolExecutor(max_workers=3) as pool:
        responses = list(pool.map(fetch, range(3)))
    assert all(response.body == b"{}" for response in responses)
    assert refreshed_at == initial + 10
    assert admissions == [initial + 10, initial + 11, initial + 12]
    store.close()


def test_settled_calls_without_usage_headers_consume_remaining_provider_allowance():
    clock = Clock()
    store = Store(clock=clock)
    sent = []

    def handler(request):
        sent.append(request)
        # Actual request cost is known, but provider-wide limit/remaining headers are absent.
        return httpx.Response(200, json={}, headers={"X-RateLimit-Credits-Used": "3"})

    source = transport(store, clock, handler)
    store.observe_used(source.scope, "credits", 90, "day")
    for number in range(2):
        source._request("GET", f"/data/{number}", estimated_credits=5, credit_limit=100)
    assert store.used(source.scope, "credits", "day") == 96
    with pytest.raises(BudgetExceeded):
        source._request("GET", "/data/2", estimated_credits=5, credit_limit=100)
    assert len(sent) == 2
    assert source.usage()["credits_used"] == 6
    store.close()
