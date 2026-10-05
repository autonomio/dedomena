"""Owned egress routes and durable, weighted per-IP admission.

The declared public IP identifies a quota; it does not configure routing. Each
route therefore needs a local interface, a proxy, or an already routed HTTP
client. No address is discovered automatically. Different local addresses
behind the same NAT must declare the same public IP and cannot form a pool.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import ipaddress
import math
import time
import uuid
from typing import Callable, Iterable, TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .core import Store


def _address(value: str) -> str:
    if not isinstance(value, str) or "%" in value:
        raise ValueError("route IP must be an IPv4 or IPv6 address")
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError("route IP must be an IPv4 or IPv6 address") from None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return str(address)


@dataclass(frozen=True)
class IPRoute:
    """An explicitly routed connection and its operator-declared public IP.

    Injected clients must already route through the declared address. They
    cannot be combined with local_address or proxy, which configure a client
    owned by the pool. Proxy credentials are deliberately absent from repr.
    """
    public_ip: str
    local_address: str | None = None
    proxy: str | None = field(default=None, repr=False)
    client: httpx.Client | None = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        object.__setattr__(self, "public_ip", _address(self.public_ip))
        if sum(value is not None for value in (self.local_address, self.proxy, self.client)) != 1:
            raise ValueError("route requires exactly one local address, proxy or routed client")
        if self.local_address is not None:
            object.__setattr__(self, "local_address", _address(self.local_address))
        if self.proxy is not None:
            if not isinstance(self.proxy, str):
                raise ValueError("route proxy must be an HTTP or HTTPS URL")
            try:
                url = httpx.URL(self.proxy)
                valid = (url.scheme in ("http", "https") and bool(url.host)
                         and not url.query and not url.fragment and url.path in ("", "/"))
            except (httpx.InvalidURL, ValueError):
                valid = False
            if not valid:
                raise ValueError("route proxy must be an HTTP or HTTPS origin") from None
        if self.client is not None and not isinstance(self.client, httpx.Client):
            raise ValueError("route client must be an httpx.Client")


@dataclass(frozen=True)
class IPLease:
    """One admitted request, with a uniquely owned refundable IP estimate."""
    route: IPRoute
    reservation_id: str | None
    weight: int
    scope: str

    @property
    def client(self) -> httpx.Client:
        return self.route.client

    @property
    def public_ip(self) -> str:
        return self.route.public_ip


class IPPool:
    """Share a route pool across sources while keeping each provider's IP quota.

    Admission uses rolling weighted windows in Store, shared by threads,
    processes and restarts using that database. Provider/key limits supplied
    in limits are admitted in the same transaction; adding routes cannot
    multiply those allowances. Quota charges represent dispatched attempts,
    including failed requests. Only a verified response may reduce an estimate.
    """
    def __init__(self, routes: Iterable[IPRoute]):
        routes = tuple(routes)
        if not routes or any(not isinstance(route, IPRoute) for route in routes):
            raise ValueError("pool requires at least one IPRoute")
        if len({route.public_ip for route in routes}) != len(routes):
            raise ValueError("pool public IP addresses must be distinct, including NAT aliases")
        self._owned = []
        self._closed = False
        try:
            configured = []
            for route in routes:
                if route.client is None:
                    options = dict(timeout=30, follow_redirects=False, trust_env=False,
                                   limits=httpx.Limits(max_connections=100, max_keepalive_connections=100))
                    if route.local_address is not None:
                        options["transport"] = httpx.HTTPTransport(local_address=route.local_address,
                                                                   trust_env=False, limits=options["limits"])
                    else:
                        options["proxy"] = route.proxy
                    client = httpx.Client(**options)
                    self._owned.append(client)
                    configured.append(IPRoute(route.public_ip, client=client))
                else:
                    configured.append(route)
            self.routes = tuple(configured)
        except BaseException:
            self.close()
            raise
        # The scheduling cursor is shared by pools with the same ordered routes.
        self._identity = hashlib.sha256("\0".join(r.public_ip for r in self.routes).encode()).hexdigest()

    def close(self):
        if not self._closed:
            self._closed = True
            for client in self._owned:
                client.close()

    def __enter__(self):
        if self._closed:
            raise ValueError("IP pool is closed")
        return self

    def __exit__(self, *args):
        self.close()

    @staticmethod
    def scope(source: str, route: IPRoute) -> str:
        """Stable public-IP quota identity; excludes proxy and client credentials."""
        if not isinstance(source, str) or not source:
            raise ValueError("source must be a nonempty string")
        return source + ":ip:" + route.public_ip

    @staticmethod
    def _schema(db):
        db.execute("CREATE TABLE IF NOT EXISTS ip_events("
                   "id TEXT PRIMARY KEY, scope TEXT NOT NULL, charged REAL NOT NULL, weight INTEGER NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS ip_events_by_scope ON ip_events(scope,charged)")
        db.execute("CREATE TABLE IF NOT EXISTS ip_windows(scope TEXT PRIMARY KEY, period REAL NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS ip_cursors(scope TEXT PRIMARY KEY, position INTEGER NOT NULL)")
        # Store owns cooldowns so admission and existing pacing see the same deferral.
        db.execute("CREATE TABLE IF NOT EXISTS cooldowns(scope TEXT PRIMARY KEY, until REAL NOT NULL)")

    @staticmethod
    def _limit(scope, weight, capacity, period):
        if (not isinstance(scope, str) or not scope
                or type(weight) is not int or weight < 1
                or type(capacity) is not int or capacity < 1
                or isinstance(period, bool) or not isinstance(period, (int, float))
                or not math.isfinite(period) or period <= 0):
            raise ValueError("weighted admission requires a scope, positive integer cost and capacity, and finite period")
        if weight > capacity:
            from .core import BudgetExceeded
            raise BudgetExceeded("request weight exceeds the configured window allowance")
        return scope, weight, capacity, float(period)

    @staticmethod
    def _ready(db, limit, now):
        scope, weight, capacity, period = limit
        row = db.execute("SELECT period FROM ip_windows WHERE scope=?", (scope,)).fetchone()
        if row and row[0] != period:
            # Changing a scope's period after pruning history could hide consumption.
            raise ValueError("quota scope cannot change its rolling window period")
        db.execute("INSERT OR IGNORE INTO ip_windows VALUES(?,?)", (scope, period))
        db.execute("DELETE FROM ip_events WHERE scope=? AND charged<=?", (scope, now - period))
        rows = db.execute("SELECT charged,weight FROM ip_events WHERE scope=? ORDER BY charged",
                          (scope,)).fetchall()
        used = sum(amount for _, amount in rows)
        due = now
        if used + weight > capacity:
            for charged, amount in rows:
                used -= amount
                due = charged + period
                if used + weight <= capacity:
                    break
        cooldown = db.execute("SELECT until FROM cooldowns WHERE scope=?", (scope,)).fetchone()
        return max(due, cooldown[0] if cooldown else now)

    def acquire(self, store: Store, source: str, weight: int, capacity: int | None, period: float,
                sleep: Callable[[float], None] = time.sleep, max_wait: float = 60,
                *, limits: Iterable[tuple[str, int, int, float]] = (),
                pacing: Iterable[tuple[str, float]] = (), before_admit=None) -> IPLease:
        """Admit the next ready IP without reserving future dispatch slots.

        limits contains (scope, weight, capacity, period) for provider/key
        ceilings. Scopes must be distinct from the IP scopes and each other.
        Reuse a stable scope and period across every worker and pool. Existing
        Store pacing scopes are admitted atomically too. capacity=None skips
        weighted IP admission while retaining routing and cooldowns. Optional
        before_admit(db, now) reserves provider budgets in the same transaction
        immediately before admission; an exception rolls back every reservation.
        """
        if self._closed:
            raise ValueError("IP pool is closed")
        if (isinstance(max_wait, bool) or not isinstance(max_wait, (int, float))
                or not math.isfinite(max_wait) or max_wait < 0):
            raise ValueError("max_wait must be finite and nonnegative")
        common = tuple(self._limit(*limit) for limit in limits)
        rate_limits = tuple(pacing)
        if any(not isinstance(scope, str) or not scope
               or isinstance(rate, bool) or not isinstance(rate, (int, float))
               or not math.isfinite(rate) or rate <= 0 for scope, rate in rate_limits):
            raise ValueError("pacing requires a scope and positive finite rate")
        if len({scope for scope, _ in rate_limits}) != len(rate_limits):
            raise ValueError("pacing scopes must be distinct")
        # Validate weight and period even when this provider has no IP quota.
        self._limit("validation", weight, capacity if capacity is not None else weight, period)
        route_limits = tuple(self._limit(self.scope(source, route), weight,
                                         capacity if capacity is not None else weight, period)
                             for route in self.routes)
        common_scopes = [limit[0] for limit in common]
        if (len(set(common_scopes)) != len(common_scopes)
                or set(common_scopes).intersection(limit[0] for limit in route_limits)):
            raise ValueError("admission scopes must be distinct")
        began = store.clock()
        scheduling = source + ":pool:" + self._identity
        waited = False
        while True:
            if self._closed:
                raise ValueError("IP pool is closed")
            with store.transaction() as db:
                self._schema(db)
                now = store.clock()
                db.execute("DELETE FROM throttles WHERE expires<=?", (now,))
                common_due = max([now] + [self._ready(db, limit, now) for limit in common])
                effective_rates = []
                for scope, rate in rate_limits:
                    throttled = db.execute("SELECT min(rate) FROM throttles WHERE scope=?", (scope,)).fetchone()[0]
                    effective_rates.append((scope, min(rate, throttled) if throttled else rate))
                    next_at = db.execute("SELECT next_at FROM pacing WHERE scope=?", (scope,)).fetchone()
                    cooldown = db.execute("SELECT until FROM cooldowns WHERE scope=?", (scope,)).fetchone()
                    common_due = max(common_due, next_at[0] if next_at else now,
                                     cooldown[0] if cooldown else now)
                due = []
                for limit in route_limits:
                    if capacity is not None:
                        route_due = self._ready(db, limit, now)
                    else:
                        cooldown = db.execute("SELECT until FROM cooldowns WHERE scope=?", (limit[0],)).fetchone()
                        route_due = cooldown[0] if cooldown else now
                    due.append(max(common_due, route_due))
                row = db.execute("SELECT position FROM ip_cursors WHERE scope=?", (scheduling,)).fetchone()
                first = row[0] % len(self.routes) if row else 0
                ordering = [(first + offset) % len(self.routes) for offset in range(len(self.routes))]
                chosen = min(ordering, key=lambda index: due[index])
                delay = max(0, due[chosen] - now)
                remaining = max_wait - max(0, now - began)
                if delay > max(0, remaining) or (remaining < 0 and (waited or max_wait > 0)):
                    from .core import Throttled
                    raise Throttled(source, delay)
                if delay == 0:
                    if before_admit is not None:
                        before_admit(db, now)
                    now = store.clock()
                    if max_wait > 0 and now - began > max_wait:
                        from .core import Throttled
                        raise Throttled(source, 0)
                    reservation = uuid.uuid4().hex if capacity is not None else None
                    for scope, amount, _, _ in common:
                        db.execute("INSERT INTO ip_events VALUES(?,?,?,?)", (uuid.uuid4().hex, scope, now, amount))
                    if reservation:
                        db.execute("INSERT INTO ip_events VALUES(?,?,?,?)",
                                   (reservation, route_limits[chosen][0], now, weight))
                    for scope, rate in effective_rates:
                        db.execute("INSERT OR REPLACE INTO pacing VALUES(?,?)", (scope, now + 1 / rate))
                    db.execute("INSERT OR REPLACE INTO ip_cursors VALUES(?,?)",
                               (scheduling, (chosen + 1) % len(self.routes)))
                    return IPLease(self.routes[chosen], reservation, weight, route_limits[chosen][0])
            waited = True
            sleep(delay)

    @staticmethod
    def settle(store: Store, lease: IPLease, actual_weight: int):
        """Reduce only this request's known estimate; expired charges stay expired.

        Settle after a trustworthy response has established actual cost. An
        uncertain send or malformed response must retain its full reservation.
        """
        if not isinstance(lease, IPLease) or type(actual_weight) is not int or not 0 <= actual_weight <= lease.weight:
            raise ValueError("actual weight must be a nonnegative integer within the reserved estimate")
        if lease.reservation_id is None:
            return
        with store.transaction() as db:
            IPPool._schema(db)
            row = db.execute("SELECT weight FROM ip_events WHERE id=? AND scope=?",
                             (lease.reservation_id, lease.scope)).fetchone()
            if row and row[0] != lease.weight and actual_weight != row[0]:
                raise ValueError("settlement cannot revise an already reduced reservation")
            db.execute("UPDATE ip_events SET weight=? WHERE id=? AND scope=?",
                       (actual_weight, lease.reservation_id, lease.scope))

    def defer(self, store: Store, source: str, route: IPRoute | IPLease, seconds: float):
        """Persist a 429 cooldown only for this provider and public IP."""
        if (isinstance(seconds, bool) or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds) or seconds < 0):
            raise ValueError("cooldown must be finite and nonnegative")
        if isinstance(route, IPLease):
            route = route.route
        if route not in self.routes:
            raise ValueError("cooldown route must belong to this pool")
        store.defer(self.scope(source, route), seconds)
