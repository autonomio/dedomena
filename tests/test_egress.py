from concurrent.futures import ThreadPoolExecutor
import math

import httpx
import pytest

from dedomena.sources.core import BudgetExceeded, Store, Throttled
from dedomena.sources.egress import IPLease, IPPool, IPRoute


class Clock:
    def __init__(self):
        self.now = 1000.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


def pool(*ips):
    return IPPool(IPRoute(ip, client=httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200)))) for ip in ips)


def acquire(routes, store, **kwargs):
    return routes.acquire(store, "provider", 1, 2, 60, max_wait=0, **kwargs)


def test_canonical_nat_aliases_do_not_multiply_quota():
    with pool("2001:db8::1") as routes:
        assert routes.routes[0].public_ip == "2001:db8::1"
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    assert IPRoute("::ffff:192.0.2.1", client=client).public_ip == "192.0.2.1"
    with pytest.raises(ValueError, match="distinct"):
        IPPool([IPRoute("192.0.2.1", client=client), IPRoute("::ffff:c000:201", client=client)])
    with pytest.raises(ValueError, match="distinct"):
        IPPool([IPRoute("2001:db8::1", client=client), IPRoute("2001:0DB8:0:0:0:0:0:1", client=client)])
    client.close()


@pytest.mark.parametrize("ip", ["host.example", "", "192.0.2.999", "fe80::1%en0", None])
def test_invalid_ip_is_not_disclosed_in_error(ip):
    with pytest.raises(ValueError, match="IPv4 or IPv6"):
        IPRoute(ip, local_address="127.0.0.1")


def test_routing_is_real_and_cannot_be_overridden_by_injected_client(monkeypatch):
    clients, transports = [], []
    original_client, original_transport = httpx.Client, httpx.HTTPTransport

    def transport(**kwargs):
        transports.append(kwargs)
        return original_transport(**kwargs)

    # IPRoute validates injected clients against the real class.
    route = IPRoute("192.0.2.1", local_address="127.0.0.1")
    monkeypatch.setattr(httpx, "HTTPTransport", transport)
    # The constructor creates a bound client, then validates its immutable route.
    # Restore the class while constructing the post-bind route.
    def make_client(**kwargs):
        clients.append(kwargs)
        result = original_client(**kwargs)
        monkeypatch.setattr(httpx, "Client", original_client)
        return result
    monkeypatch.setattr(httpx, "Client", make_client)
    routes = IPPool([route])
    owned = routes.routes[0].client
    assert transports[0]["local_address"] == "127.0.0.1"
    assert transports[0]["trust_env"] is False
    assert transports[0]["limits"].max_keepalive_connections == 100
    assert clients[0]["trust_env"] is False
    assert clients[0]["follow_redirects"] is False
    routes.close()
    assert owned.is_closed
    with pytest.raises(ValueError, match="exactly one"):
        IPRoute("192.0.2.1", local_address="127.0.0.1", client=original_client())
    with pytest.raises(ValueError, match="exactly one"):
        IPRoute("192.0.2.1")


def test_proxy_construction_and_secret_free_representations(monkeypatch):
    calls = []
    original_client = httpx.Client
    secret = "proxy-password-token"
    route = IPRoute("192.0.2.1", proxy=f"http://user:{secret}@proxy.example:8080")

    def client(**kwargs):
        calls.append(kwargs)
        result = original_client(**kwargs)
        monkeypatch.setattr(httpx, "Client", original_client)
        return result

    monkeypatch.setattr(httpx, "Client", client)
    with IPPool([route]) as routes:
        assert calls[0]["proxy"] == route.proxy
        assert calls[0]["trust_env"] is False
        store = Store()
        lease = acquire(routes, store)
        assert secret not in repr(route) + repr(routes.routes[0]) + repr(lease)
        with store.transaction() as db:
            assert secret not in "\n".join(db.iterdump())
    with pytest.raises(ValueError) as error:
        IPRoute("192.0.2.1", proxy=f"file://user:{secret}@host")
    assert secret not in str(error.value)


def test_pool_does_not_close_injected_clients():
    routes = pool("192.0.2.1")
    client = routes.routes[0].client
    routes.close()
    routes.close()
    assert not client.is_closed
    with pytest.raises(ValueError, match="closed"):
        acquire(routes, Store())
    client.close()


def test_round_robin_is_deterministic_and_shared_across_pool_instances():
    store = Store()
    with pool("192.0.2.1", "192.0.2.2") as first, pool("192.0.2.1", "192.0.2.2") as second:
        assert [acquire(routes, store).public_ip for routes in [first, second, first, second]] == [
            "192.0.2.1", "192.0.2.2", "192.0.2.1", "192.0.2.2"]
        with pytest.raises(Throttled):
            acquire(first, store)


def test_ip_consumption_shared_across_restarts_and_subsets(tmp_path):
    path = tmp_path / "shared.sqlite3"
    clock = Clock()
    with pool("192.0.2.1", "192.0.2.2") as first:
        store = Store(path, clock=clock)
        first.acquire(store, "provider", 2, 2, 60, max_wait=0)
        store.close()
    with pool("192.0.2.1") as restarted:
        store = Store(path, clock=clock)
        with pytest.raises(Throttled):
            acquire(restarted, store)
        # The same IP's unrelated provider remains independent.
        assert restarted.acquire(store, "other", 2, 2, 60, max_wait=0).public_ip == "192.0.2.1"
        store.close()


def test_weighted_rolling_window_waits_for_exact_capacity():
    clock = Clock()
    store = Store(clock=clock)
    with pool("192.0.2.1") as routes:
        routes.acquire(store, "provider", 2, 3, 10, clock.sleep, 0)
        clock.now += 3
        routes.acquire(store, "provider", 1, 3, 10, clock.sleep, 0)
        lease = routes.acquire(store, "provider", 2, 3, 10, clock.sleep, 7)
        assert lease.public_ip == "192.0.2.1"
        assert clock.waits == [7]
        with pytest.raises(Throttled) as error:
            routes.acquire(store, "provider", 2, 3, 10, clock.sleep, 0)
        assert error.value.retry_after == 10


def test_waiters_charge_no_future_slot_and_recheck_cooldown():
    clock = Clock()
    store = Store(clock=clock)
    with pool("192.0.2.1", "192.0.2.2") as routes:
        first = routes.acquire(store, "provider", 1, 1, 10, max_wait=0)
        second = routes.acquire(store, "provider", 1, 1, 10, max_wait=0)

        def sleep(seconds):
            clock.sleep(seconds)
            if len(clock.waits) == 1:
                routes.defer(store, "provider", first, 15)
                # Another worker acquires the newly available route before this waiter.
                assert routes.acquire(store, "provider", 1, 1, 10, max_wait=0).public_ip == second.public_ip

        admitted = routes.acquire(store, "provider", 1, 1, 10, sleep, 20)
        assert admitted.public_ip == second.public_ip
        assert clock.waits == [10, 10]


def test_one_ip_cooldown_does_not_poison_other_routes_or_providers(tmp_path):
    clock = Clock()
    first = Store(tmp_path / "shared.sqlite3", clock=clock)
    second = Store(tmp_path / "shared.sqlite3", clock=clock)
    with pool("192.0.2.1", "192.0.2.2") as routes, pool("192.0.2.1") as subset:
        lease = acquire(routes, first)
        routes.defer(first, "provider", lease, 25)
        routes.defer(first, "provider", lease, 10)  # cannot shorten an existing cooldown
        assert acquire(routes, second).public_ip == "192.0.2.2"
        with pytest.raises(Throttled) as error:
            acquire(subset, second)
        assert error.value.retry_after == 25
        assert subset.acquire(second, "other", 1, 2, 60, max_wait=0).public_ip == "192.0.2.1"
    first.close()
    second.close()


def test_shared_provider_limit_cannot_be_multiplied_by_more_ips():
    clock = Clock()
    store = Store(clock=clock)
    limits = (("provider:key:hashed", 1, 2, 60),)
    with pool("192.0.2.1", "192.0.2.2", "192.0.2.3") as routes:
        acquire(routes, store, limits=limits)
        acquire(routes, store, limits=limits)
        with pytest.raises(Throttled):
            acquire(routes, store, limits=limits)
        with store.transaction() as db:
            assert db.execute("SELECT sum(weight) FROM ip_events WHERE scope=?", (limits[0][0],)).fetchone()[0] == 2
            assert db.execute("SELECT sum(weight) FROM ip_events WHERE scope LIKE 'provider:ip:%'").fetchone()[0] == 2


def test_existing_core_pacing_is_atomic_with_ip_admission():
    clock = Clock()
    store = Store(clock=clock)
    pacing = (("provider:key", 2),)
    with pool("192.0.2.1", "192.0.2.2") as routes:
        store.pace("provider:key", 2, clock.sleep, 0)
        with pytest.raises(Throttled) as error:
            acquire(routes, store, pacing=pacing)
        assert error.value.retry_after == 0.5
        with store.transaction() as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='ip_events'").fetchone()
            assert not exists or db.execute("SELECT count(*) FROM ip_events").fetchone()[0] == 0
        lease = routes.acquire(store, "provider", 1, 2, 60, clock.sleep, 1, pacing=pacing)
        assert clock.waits == [0.5]
        assert lease.public_ip == "192.0.2.1"
        with pytest.raises(Throttled):
            store.pace("provider:key", 2, clock.sleep, 0)


def test_core_throttle_and_global_cooldown_survive_route_selection():
    clock = Clock()
    store = Store(clock=clock)
    store.throttle("provider:key", 1)
    store.defer("provider:key", 3)
    with pool("192.0.2.1", "192.0.2.2") as routes:
        routes.acquire(store, "provider", 1, 10, 60, clock.sleep, 3, pacing=(("provider:key", 10),))
        assert clock.waits == [3]
        with pytest.raises(Throttled) as error:
            routes.acquire(store, "provider", 1, 10, 60, clock.sleep, 0, pacing=(("provider:key", 10),))
        assert error.value.retry_after == 1


def test_route_only_mode_still_observes_cooldown_and_pacing():
    clock = Clock()
    store = Store(clock=clock)
    with pool("192.0.2.1", "192.0.2.2") as routes:
        lease = routes.acquire(store, "provider", 1, None, 60, max_wait=0)
        assert lease.reservation_id is None
        routes.defer(store, "provider", lease, 10)
        assert routes.acquire(store, "provider", 1, None, 60, max_wait=0).public_ip == "192.0.2.2"
        with store.transaction() as db:
            assert db.execute("SELECT count(*) FROM ip_events").fetchone()[0] == 0


def test_settlement_releases_only_this_lease_and_never_reinserts_expired_events():
    clock = Clock()
    store = Store(clock=clock)
    with pool("192.0.2.1") as routes:
        first = routes.acquire(store, "provider", 4, 8, 10, max_wait=0)
        second = routes.acquire(store, "provider", 4, 8, 10, max_wait=0)
        routes.settle(store, second, 1)
        assert routes.acquire(store, "provider", 3, 8, 10, max_wait=0).public_ip == first.public_ip
        with pytest.raises(Throttled):
            routes.acquire(store, "provider", 1, 8, 10, max_wait=0)
        routes.settle(store, second, 1)  # same-cost settlement is idempotent
        with pytest.raises(ValueError):
            routes.settle(store, second, 0)
        with pytest.raises(ValueError):
            routes.settle(store, second, 4)
        with pytest.raises(ValueError):
            routes.settle(store, first, 5)
        clock.now += 10
        routes.acquire(store, "provider", 1, 8, 10, max_wait=0)
        routes.settle(store, first, 0)
        with store.transaction() as db:
            assert db.execute("SELECT sum(weight) FROM ip_events").fetchone()[0] == 1


def test_oversleep_cannot_dispatch_after_deadline():
    clock = Clock()
    store = Store(clock=clock)
    with pool("192.0.2.1") as routes:
        routes.acquire(store, "provider", 1, 1, 10, max_wait=0)
        with pytest.raises(Throttled):
            routes.acquire(store, "provider", 1, 1, 10, lambda seconds: clock.sleep(seconds + 1), 10)
        with store.transaction() as db:
            assert db.execute("SELECT count(*) FROM ip_events").fetchone()[0] == 1


def test_atomic_admission_across_concurrent_stores(tmp_path):
    path = tmp_path / "shared.sqlite3"
    clock = Clock()
    stores = [Store(path, clock=clock) for _ in range(8)]
    with pool("192.0.2.1", "192.0.2.2") as routes:
        def request(index):
            try:
                return routes.acquire(stores[index % len(stores)], "provider", 1, 3, 60, max_wait=0)
            except Throttled:
                return None
        with ThreadPoolExecutor(max_workers=8) as threads:
            admitted = list(threads.map(request, range(40)))
        assert sum(isinstance(lease, IPLease) for lease in admitted) == 6
        assert sum(lease.public_ip == "192.0.2.1" for lease in admitted if lease) == 3
        assert sum(lease.public_ip == "192.0.2.2" for lease in admitted if lease) == 3
    for store in stores:
        store.close()


@pytest.mark.parametrize("kwargs", [
    {"weight": True}, {"weight": 0}, {"weight": -1}, {"capacity": 0},
    {"period": 0}, {"period": math.inf}, {"max_wait": -1}, {"max_wait": math.nan},
])
def test_invalid_admission_configuration_has_no_side_effects(kwargs):
    defaults = dict(weight=1, capacity=2, period=60, max_wait=0)
    defaults.update(kwargs)
    store = Store()
    with pool("192.0.2.1") as routes:
        with pytest.raises(ValueError):
            routes.acquire(store, "provider", **defaults)
        with store.transaction() as db:
            assert db.execute("SELECT count(*) FROM pacing").fetchone()[0] == 0


def test_excessive_weight_fails_without_wait_or_charge():
    with pool("192.0.2.1") as routes:
        with pytest.raises(BudgetExceeded):
            routes.acquire(Store(), "provider", 3, 2, 60, lambda _: pytest.fail("must not sleep"), 60)


def test_window_cannot_be_shortened_after_history_has_been_pruned():
    store = Store()
    with pool("192.0.2.1") as routes:
        acquire(routes, store)
        with pytest.raises(ValueError, match="period"):
            routes.acquire(store, "provider", 1, 2, 10, max_wait=0)


def test_budget_callback_runs_only_on_final_admission_and_rolls_back_failures():
    clock = Clock()
    store = Store(clock=clock)
    callbacks = []
    with pool("192.0.2.1") as routes:
        routes.acquire(store, "provider", 1, 1, 10, max_wait=0)

        def deny(db, now):
            callbacks.append(now)
            db.execute("INSERT INTO budgets VALUES('provider:key','credits','day',1)")
            raise BudgetExceeded("provider: day allowance exhausted")

        with pytest.raises(BudgetExceeded):
            routes.acquire(store, "provider", 1, 1, 10, clock.sleep, 10,
                           pacing=(("provider:key", 2),), before_admit=deny)
        assert callbacks == [1010]
        assert clock.waits == [10]
        with store.transaction() as db:
            assert db.execute("SELECT count(*) FROM budgets").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM pacing").fetchone()[0] == 0
        lease = routes.acquire(store, "provider", 1, 1, 10, max_wait=0,
                               before_admit=lambda db, now: callbacks.append(now))
        assert lease.public_ip == "192.0.2.1"
        assert callbacks == [1010, 1010]
