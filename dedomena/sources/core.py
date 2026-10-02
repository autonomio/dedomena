"""Small, shared source transport: pooled HTTP, durable snapshots and key budgets."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
import logging
import math
import os
import re
from pathlib import Path
import sqlite3
import threading
import time
import uuid
from typing import Callable, Iterator, Mapping
from urllib.parse import urlencode

import httpx

SCHEMA_VERSION = "0.6.0"
PUBLIC_HEADERS = {
    "content-type", "content-encoding", "x-ratelimit-limit", "x-ratelimit-remaining",
    "x-ratelimit-credits-used", "x-ratelimit-reset", "retry-after",
    "x-individualquotaperhour-used", "x-registeredquotaperweek-used",
    "x-registeredpayingquotaperweek-used", "x-throttling-control", "x-rejection-reason",
    "etag", "last-modified", "date",
}


class SourceError(RuntimeError):
    """A source failure with no credential, request body or upstream error body."""


class HTTPFailure(SourceError):
    def __init__(self, source: str, status_code: int):
        self.status_code = status_code
        super().__init__(f"{source}: HTTP {status_code}")


class BudgetExceeded(SourceError):
    """The configured or provider allowance cannot cover another request."""


class Throttled(SourceError):
    def __init__(self, source: str, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"{source}: retry after {retry_after:.1f} seconds")


class InvalidResponse(SourceError):
    """The source response violated its documented result contract."""


class SearchLimitExceeded(SourceError):
    """A provider cannot enumerate the complete query; refine or partition it."""


def canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def utc_stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def window(period: str, seconds: float) -> str:
    now = datetime.fromtimestamp(seconds, timezone.utc)
    if period == "week":
        year, week, _ = now.isocalendar()
        return f"{year}-W{week:02}"
    return now.date().isoformat()


@dataclass(frozen=True)
class Provenance:
    source: str
    method: str
    url: str
    parameters: Mapping[str, str]
    request_sha256: str
    response_sha256: str
    retrieved_at_utc: str
    snapshot_id: str
    adapter_version: str = SCHEMA_VERSION
    license: str = ""
    public_request_headers: Mapping[str, str] | None = None
    request_body: str | None = None
    completed_at_utc: str | None = None
    http_status: int | None = None
    egress_ip_sha256: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Page:
    """Provider-native records with a durable, independently verifiable page receipt.

    complete means this page finishes enumeration; earlier pages are not complete.
    A resume cursor and the exact source parameters travel with every page.
    """
    records: tuple[dict, ...]
    provenance: Provenance
    total: int | None = None
    next_cursor: str | None = None
    complete: bool = False
    warnings: tuple[str, ...] = ()
    cache_hit: bool = False
    credits_used: int = 0
    wire_bytes: int = 0
    decoded_bytes: int = 0
    records_seen: int = 0
    unresolved: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Response:
    body: bytes
    headers: Mapping[str, str]
    provenance: Provenance
    cache_hit: bool
    credits_used: int
    wire_bytes: int

    def json(self) -> dict:
        try:
            body = json.loads(self.body)
            if not isinstance(body, dict):
                raise ValueError
            return body
        except (ValueError, UnicodeDecodeError):
            raise InvalidResponse(f"{self.provenance.source}: invalid JSON response") from None


class _PrivateQueryLogFilter(logging.Filter):
    # A stable filter avoids races caused by per-request filter removal. Redact
    # the query field itself, so no credential registry or lifetime is needed.
    def filter(self, record):
        message = record.getMessage()
        redacted = re.sub(r'([?&]api_key=)[^&\s"<>]+', r'\1[REDACTED]', message)
        if redacted != message:
            record.msg, record.args = redacted, ()
        return True


logging.getLogger("httpx").addFilter(_PrivateQueryLogFilter())


class Store:
    """SQLite WAL storage shared by threads/processes using the same source key.

    Credentials are never stored. Cache namespaces use their SHA-256 fingerprints.
    Old response snapshots remain available even after refresh replaces a cache entry.
    """
    def __init__(self, path: str | Path | None = None, *, clock: Callable[[], float] = time.time):
        self.clock = clock
        self.path = Path(path).expanduser().absolute() if path is not None else None
        self._lock = threading.RLock()
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise ValueError("source store must not be a symlink")
        self._db = sqlite3.connect(str(self.path) if self.path else ":memory:",
                                   timeout=30, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS snapshots(
                id TEXT PRIMARY KEY, body BLOB NOT NULL, headers TEXT NOT NULL,
                provenance TEXT NOT NULL, wire_bytes INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS cache(
                key TEXT PRIMARY KEY, snapshot TEXT NOT NULL, acquired REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS budgets(
                scope TEXT NOT NULL, unit TEXT NOT NULL, window TEXT NOT NULL,
                used INTEGER NOT NULL, PRIMARY KEY(scope, unit, window));
            CREATE TABLE IF NOT EXISTS budget_observations(
                scope TEXT NOT NULL, unit TEXT NOT NULL, window TEXT NOT NULL,
                used INTEGER NOT NULL, PRIMARY KEY(scope, unit, window));
            CREATE TABLE IF NOT EXISTS budget_reservations(
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, unit TEXT NOT NULL,
                window TEXT NOT NULL, amount INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS reservations_by_window
                ON budget_reservations(scope, unit, window);
            CREATE TABLE IF NOT EXISTS pacing(scope TEXT PRIMARY KEY, next_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS cooldowns(scope TEXT PRIMARY KEY, until REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS throttles(scope TEXT NOT NULL, rate REAL NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS stats(
                scope TEXT NOT NULL, day TEXT NOT NULL, name TEXT NOT NULL, value INTEGER NOT NULL,
                PRIMARY KEY(scope, day, name));
        """)
        self._db.commit()
        if self.path:
            self.path.chmod(0o600)

    @contextmanager
    def transaction(self):
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    def close(self):
        with self._lock:
            self._db.close()

    def get(self, key: str, ttl: float) -> Response | None:
        with self._lock:
            row = self._db.execute("""
                SELECT s.body,s.headers,s.provenance,s.wire_bytes FROM cache c
                JOIN snapshots s ON c.snapshot=s.id WHERE c.key=? AND c.acquired>=?
            """, (key, self.clock() - ttl)).fetchone()
        return self._response(row, cache_hit=True) if row else None

    @staticmethod
    def _response(row, *, cache_hit: bool) -> Response:
        body, headers, provenance, wire = row
        p = Provenance(**json.loads(provenance))
        if digest(body) != p.response_sha256:
            raise InvalidResponse(f"{p.source}: stored response hash mismatch")
        return Response(body, json.loads(headers), p, cache_hit, 0, 0 if cache_hit else wire)

    def put(self, key: str, response: Response):
        p = response.provenance
        with self.transaction() as db:
            db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?)",
                       (p.snapshot_id, response.body, canonical(dict(response.headers)),
                        canonical(p.to_dict()), response.wire_bytes))
            db.execute("INSERT OR REPLACE INTO cache VALUES(?,?,?)",
                       (key, p.snapshot_id, self.clock()))

    def replay(self, snapshot_id: str) -> Response:
        with self._lock:
            row = self._db.execute("SELECT body,headers,provenance,wire_bytes FROM snapshots WHERE id=?",
                                   (snapshot_id,)).fetchone()
        if not row:
            raise KeyError(snapshot_id)
        return self._response(row, cache_hit=True)

    @staticmethod
    def _budget(db, scope: str, unit: str, key: str) -> tuple[int, int]:
        args = (scope, unit, key)
        local = db.execute("SELECT used FROM budgets WHERE scope=? AND unit=? AND window=?", args).fetchone()
        observed = db.execute("SELECT used FROM budget_observations WHERE scope=? AND unit=? AND window=?",
                              args).fetchone()
        pending = db.execute("SELECT coalesce(sum(amount),0) FROM budget_reservations "
                             "WHERE scope=? AND unit=? AND window=?", args).fetchone()[0]
        return max(local[0] if local else 0, observed[0] if observed else 0), pending

    @staticmethod
    def _set_budget(db, scope: str, unit: str, key: str, used: int):
        db.execute("""INSERT INTO budgets VALUES(?,?,?,?)
            ON CONFLICT(scope,unit,window) DO UPDATE SET used=excluded.used""",
                   (scope, unit, key, used))

    def charge(self, scope: str, unit: str, amount: int, limit: int | None, period: str,
               *, window_key: str | None = None):
        key = window_key or window(period, self.clock())
        with self.transaction() as db:
            baseline, pending = self._budget(db, scope, unit, key)
            if limit is not None and amount > 0 and baseline + pending + amount > limit:
                raise BudgetExceeded(f"{scope.split(':')[0]}: {period} {unit} allowance exhausted")
            # A local adjustment never modifies the separate provider high-water mark.
            self._set_budget(db, scope, unit, key, max(0, baseline + amount))

    def reserve(self, scope: str, unit: str, amount: int, limit: int | None, period: str,
                *, window_key: str) -> str:
        """Reserve atomically without mixing refundable estimates with provider usage."""
        with self.transaction() as db:
            return self._reserve_in(db, scope, unit, amount, limit, period, window_key=window_key)

    def _reserve_in(self, db, scope: str, unit: str, amount: int, limit: int | None,
                    period: str, *, window_key: str) -> str:
        """Reserve inside the caller's final dispatch transaction."""
        reservation = uuid.uuid4().hex
        baseline, pending = self._budget(db, scope, unit, window_key)
        if limit is not None and baseline + pending + amount > limit:
            raise BudgetExceeded(f"{scope.split(':')[0]}: {period} {unit} allowance exhausted")
        db.execute("INSERT INTO budget_reservations VALUES(?,?,?,?,?)",
                   (reservation, scope, unit, window_key, amount))
        return reservation

    def settle(self, reservation: str, actual: int):
        """Replace one reservation with cost, retaining every other worker's estimate.

        An out-of-order provider total may already include this response. Without a
        provider coverage watermark, count its cost conservatively above that floor.
        Actual local costs remain separately available in request statistics.
        """
        with self.transaction() as db:
            row = db.execute("SELECT scope,unit,window FROM budget_reservations WHERE id=?",
                             (reservation,)).fetchone()
            if row is None:
                raise ValueError("unknown budget reservation")
            scope, unit, key = row
            baseline, _ = self._budget(db, scope, unit, key)
            self._set_budget(db, scope, unit, key, baseline + actual)
            db.execute("DELETE FROM budget_reservations WHERE id=?", (reservation,))

    def observe_used(self, scope: str, unit: str, used: int, period: str,
                     *, window_key: str | None = None):
        key = window_key or window(period, self.clock())
        with self.transaction() as db:
            db.execute("""INSERT INTO budget_observations VALUES(?,?,?,?)
                ON CONFLICT(scope,unit,window) DO UPDATE SET used=max(used,excluded.used)""",
                       (scope, unit, key, used))

    def used(self, scope: str, unit: str, period: str) -> int:
        with self._lock:
            baseline, pending = self._budget(self._db, scope, unit, window(period, self.clock()))
        return baseline + pending

    def record(self, scope: str, **values: int):
        with self.transaction() as db:
            for name, value in values.items():
                db.execute("""INSERT INTO stats VALUES(?,?,?,?)
                    ON CONFLICT(scope,day,name) DO UPDATE SET value=value+excluded.value""",
                           (scope, window("day", self.clock()), name, value))

    def usage(self, scope: str) -> dict:
        with self._lock:
            rows = self._db.execute("SELECT name,value FROM stats WHERE scope=? AND day=?",
                                   (scope, window("day", self.clock()))).fetchall()
        return {"date_utc": window("day", self.clock()), **dict(rows),
                "scope": "Requests through this store; other clients are excluded."}

    def throttle(self, scope: str, rate: float):
        if rate <= 0:
            return
        with self.transaction() as db:
            db.execute("INSERT INTO throttles VALUES(?,?,?)", (scope, rate, self.clock() + 60))

    def pace(self, scope: str, rate: float, sleep: Callable[[float], None], max_wait: float):
        self.pace_many(((scope, rate),), sleep, max_wait)

    def pace_many(self, limits: tuple[tuple[str, float], ...],
                  sleep: Callable[[float], None], max_wait: float, before_admit=None):
        """Admit all rate scopes together; waiters acquire no stale future dispatch slots."""
        began = self.clock()
        while True:
            with self.transaction() as db:
                now = self.clock()
                db.execute("DELETE FROM throttles WHERE expires<=?", (now,))
                due, rates = now, []
                for scope, rate in limits:
                    row = db.execute("SELECT min(rate) FROM throttles WHERE scope=?", (scope,)).fetchone()
                    effective = min(rate, row[0]) if row[0] else rate
                    row = db.execute("SELECT next_at FROM pacing WHERE scope=?", (scope,)).fetchone()
                    cooldown = db.execute("SELECT until FROM cooldowns WHERE scope=?", (scope,)).fetchone()
                    due = max(due, row[0] if row else now, cooldown[0] if cooldown else now)
                    rates.append((scope, effective))
                delay = due - now
                if (delay > max(0, max_wait - (now - began))
                        or (max_wait > 0 and now - began > max_wait)):
                    raise Throttled(limits[0][0].split(":")[0], delay)
                if not delay:
                    if before_admit is not None:
                        before_admit(db, now)
                    # No separately locking work may intervene between admission and send.
                    now = self.clock()
                    if max_wait > 0 and now - began > max_wait:
                        raise Throttled(limits[0][0].split(":")[0], 0)
                    for scope, rate in rates:
                        db.execute("INSERT OR REPLACE INTO pacing VALUES(?,?)", (scope, now + 1 / rate))
                    return
            # Another worker can update rates/cooldowns while this worker sleeps.
            # Recheck every scope atomically after waking, immediately before admission.
            sleep(delay)

    def defer(self, scope: str, seconds: float):
        with self.transaction() as db:
            db.execute("""INSERT INTO cooldowns VALUES(?,?)
                ON CONFLICT(scope) DO UPDATE SET until=max(until,excluded.until)""",
                       (scope, self.clock() + seconds))


class Transport:
    """Restricted HTTPS transport; no redirects, arbitrary URLs or credential parameters."""
    def __init__(self, source: str, base_url: str, *, credential: str = "",
                 client: httpx.Client | None = None, store: Store | None = None,
                 requests_per_second: float = 10, cache_ttl: float = 86400,
                 max_response_bytes: int = 16_000_000, max_attempts: int = 3,
                 max_wait: float = 60, sleep: Callable[[float], None] = time.sleep,
                 license: str = "", ip_pool=None, ip_limit: tuple[int, float] | None = None,
                 ip_throttle_only: bool = False):
        if not base_url.startswith("https://"):
            raise ValueError("source URL must use HTTPS")
        if requests_per_second <= 0 or not math.isfinite(requests_per_second):
            raise ValueError("request rate must be positive and finite")
        if (not math.isfinite(cache_ttl) or cache_ttl < 0
                or type(max_response_bytes) is not int or max_response_bytes < 1
                or type(max_attempts) is not int or max_attempts < 1
                or not math.isfinite(max_wait) or max_wait < 0):
            raise ValueError("invalid transport limits")
        from .egress import IPPool, IPRoute
        if ip_pool is not None and (not isinstance(ip_pool, IPPool) or client is not None):
            raise ValueError("ip_pool must be an IPPool and cannot be combined with client")
        if ip_limit is not None and (
                not isinstance(ip_limit, tuple) or len(ip_limit) != 2
                or type(ip_limit[0]) is not int or ip_limit[0] < 1
                or type(ip_limit[1]) not in (int, float)
                or not math.isfinite(ip_limit[1]) or ip_limit[1] <= 0):
            raise ValueError("ip_limit must contain positive integer capacity and window seconds")
        if type(ip_throttle_only) is not bool:
            raise ValueError("ip_throttle_only must be boolean")
        self.source, self.base_url = source, base_url.rstrip("/")
        self.scope = source + ":" + digest(credential.encode())
        self._secrets = tuple(value for value in {credential, *credential.split(":")} if len(value) >= 8)
        self.client = None if ip_pool is not None else client or httpx.Client(
            timeout=30, follow_redirects=False,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=100))
        self._owns_client = client is None and ip_pool is None
        self.ip_pool = ip_pool
        self._owns_pool = ip_pool is None and ip_limit is not None
        if self._owns_pool:
            self.ip_pool = IPPool([IPRoute("0.0.0.0", client=self.client)])
        self.ip_limit, self.ip_throttle_only = ip_limit, ip_throttle_only
        self.store = store or Store(os.environ.get("DEDOMENA_SOURCE_STORE",
                                str(Path.home() / ".cache" / "dedomena" / "sources.sqlite3")))
        self._owns_store = store is None
        self.rate, self.cache_ttl = requests_per_second, cache_ttl
        self.max_response_bytes, self.max_attempts = max_response_bytes, max_attempts
        self.max_wait, self.sleep, self.license = max_wait, sleep, license

    def close(self):
        if self._owns_pool:
            self.ip_pool.close()
        if self._owns_client:
            self.client.close()
        if self._owns_store:
            self.store.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def usage(self) -> dict:
        return self.store.usage(self.scope)

    def page(self, response: Response, records: list[dict], *, total=None, next_cursor=None,
             complete=False, warnings=(), records_seen=0, unresolved=()) -> Page:
        self.store.record(self.scope, records_delivered=len(records),
                          upstream_records=0 if response.cache_hit else len(records))
        return Page(tuple(records), response.provenance, total, next_cursor, complete,
                    tuple(warnings), response.cache_hit, response.credits_used,
                    response.wire_bytes, len(response.body), records_seen, tuple(unresolved))

    def _retry_after(self, value: str | None) -> float:
        if value is None:
            return 0
        try:
            seconds = float(value)
            if math.isfinite(seconds):
                return max(0, seconds)
        except ValueError:
            try:
                return max(0, parsedate_to_datetime(value).timestamp() - self.store.clock())
            except (ValueError, TypeError, OverflowError):
                pass
        return 0

    def _request(self, method: str, path: str, *, params: Mapping | None = None,
                headers: Mapping | None = None, private_params: Mapping | None = None,
                content: bytes | None = None,
                form: Mapping | None = None, auth=None, estimated_credits: int = 0,
                credit_limit: int | None = None, byte_limit: int | None = None,
                service: str = "requests", service_rate: float | None = None,
                refresh: bool = False, sensitive: bool = False,
                observe: Callable[..., None] | None = None,
                before_request: Callable[[], None] | None = None,
                headers_factory: Callable[[], Mapping] | None = None,
                rate_weight: int = 1, actual_weight: Callable[[bytes], int] | None = None) -> Response:
        if method not in ("GET", "POST") or not path.startswith("/") or path.startswith("//") or "?" in path:
            raise ValueError("invalid source request")
        routes = {
            "hyperliquid": (("POST", r"/info"),),
            "fred": (("GET", r"/(?:series(?:/search|/observations|/vintagedates)?|v2/release/observations)"),),
            "worldbank": (("GET", r"/country/[A-Za-z0-9;]+/indicator/[A-Za-z0-9._;\-]+"),
                          ("GET", r"/indicator(?:/[A-Za-z0-9._;\-]+)?")),
            "sec": (("GET", r"/submissions/CIK[0-9]{10}(?:-submissions-[0-9]+)?[.]json"),
                    ("GET", r"/api/xbrl/companyfacts/CIK[0-9]{10}[.]json"),
                    ("GET", r"/api/xbrl/companyconcept/CIK[0-9]{10}/[A-Za-z][A-Za-z0-9_.-]*/[A-Za-z][A-Za-z0-9_.-]*[.]json"),
                    ("GET", r"/api/xbrl/frames/[A-Za-z][A-Za-z0-9_.-]*/[A-Za-z][A-Za-z0-9_.-]*/[A-Za-z0-9_-]+/CY[0-9]{4}(?:Q[1-4])?I?[.]json")),
            "ecb": (("GET", r"/data/[A-Za-z][A-Za-z0-9_,.-]*(?:/[A-Za-z0-9_.+\-]*)?"),
                    ("GET", r"/dataflow/ECB/all/latest")),
            "openalex": (("GET", r"/works(?:/.+)?"), ("GET", r"/rate-limit")),
            "europepmc": (("GET", r"/search"), ("POST", r"/searchPOST"),
                          ("GET", r"/PMC[0-9]+/fullTextXML")),
            "epo": (("POST", r"/3[.]2/auth/accesstoken"),
                    ("GET", r"/3[.]2/rest-services/published-data/search(?:/biblio)?"),
                    ("POST", r"/3[.]2/rest-services/published-data/publication/(?:docdb|epodoc)/biblio"),
                    ("GET", r"/3[.]2/rest-services/published-data/publication/(?:docdb|epodoc)/[^/]+/(?:biblio|claims|description)")),
        }
        if self.source in routes and not any(
                method == verb and re.fullmatch(pattern, path)
                for verb, pattern in routes[self.source]):
            raise ValueError("source route is outside the read-only adapter")
        if self.source == "hyperliquid":
            from .hyperliquid import info_policy
            if params or private_params or form is not None or auth is not None:
                raise ValueError("Hyperliquid info requests use a public JSON body only")
            rate_weight, actual_weight = info_policy(content)
        if type(rate_weight) is not int or rate_weight < 1:
            raise ValueError("rate_weight must be a positive integer")
        params = {str(k): str(v) for k, v in (params or {}).items()}
        if any(k.lower() in ("api_key", "access_token", "authorization") for k in params):
            raise ValueError("credentials must not appear in source parameters")
        private_params = {str(k): str(v) for k, v in (private_params or {}).items()}
        # FRED v1 only accepts a query key. Keep it out of public request identity
        # and snapshots; the hashed credential namespace still isolates caches.
        if private_params and (self.source != "fred" or set(private_params) != {"api_key"}
                               or self.scope != "fred:" + digest(private_params["api_key"].encode())):
            raise ValueError("private query credentials are restricted to FRED authentication")
        form = {str(k): str(v) for k, v in (form or {}).items()} if form is not None else None
        if form and any(k.lower() in ("api_key", "access_token", "authorization") for k in form):
            raise ValueError("credentials must not appear in source forms")
        if form is not None and content is not None:
            raise ValueError("provide a form or a body, not both")
        request_body = urlencode(sorted(form.items())).encode() if form is not None else content
        public_input = canonical({"parameters": params, "form": form,
                                  "headers": {k: v for k, v in (headers or {}).items()
                                              if k.lower() != "authorization"}}).encode() + (content or b"")
        if not sensitive and any(value.encode() in public_input for value in self._secrets):
            raise ValueError("source credentials cannot appear in public query data")
        url = self.base_url + path
        destination, origin = httpx.URL(url), httpx.URL(self.base_url)
        if (destination.scheme, destination.host, destination.port) != (origin.scheme, origin.host, origin.port):
            raise ValueError("source request must retain its configured origin")
        base_path = origin.path.rstrip("/")
        actual_path = destination.path[len(base_path):] if destination.path.startswith(base_path) else ""
        if self.source in routes and not any(
                method == verb and re.fullmatch(pattern, actual_path)
                for verb, pattern in routes[self.source]):
            raise ValueError("normalized source route is outside the read-only adapter")
        # Public headers affecting the response (e.g. OPS ranges) are part of identity.
        identity = {"source": self.source, "method": method, "url": url, "params": params,
                    "headers": {k.lower(): v for k, v in (headers or {}).items()
                                if k.lower() not in ("authorization",)},
                    "body_sha256": digest(request_body or b""), "adapter": SCHEMA_VERSION}
        request_hash = digest(canonical(identity).encode())
        cache_key = digest((self.scope + request_hash).encode())
        if not refresh and not sensitive and method in ("GET", "POST"):
            cached = self.store.get(cache_key, self.cache_ttl)
            if cached:
                self.store.record(self.scope, cache_hits=1)
                return cached
        if before_request:
            before_request()
        public_headers = {"Accept-Encoding": "gzip, deflate", "Authorization": "", **(headers or {})}
        if form is not None:
            public_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        pace_scope = self.scope + ":" + service
        for attempt in range(self.max_attempts):
            # Resolve potentially blocking OAuth/token locks before acquiring dispatch slots.
            attempt_headers = {**public_headers, **(headers_factory() if headers_factory else {})}
            limits = ((self.scope + ":all", self.rate),)
            if service_rate is not None:
                limits += ((pace_scope, service_rate),)
            admitted = {}

            def reserve_budgets(db, now):
                # IP capacity, pacing and these reservations commit together. No
                # additional database lock can leave a queued dispatch lease stale.
                while True:
                    day, week = window("day", now), window("week", now)
                    credit = self.store._reserve_in(db, self.scope, "credits", estimated_credits,
                                                    credit_limit, "day", window_key=day)
                    byte = (self.store._reserve_in(db, self.scope, "bytes", self.max_response_bytes,
                                                  byte_limit, "week", window_key=week)
                            if byte_limit is not None else None)
                    fresh = self.store.clock()
                    if day == window("day", fresh) and week == window("week", fresh):
                        admitted.update(credit=credit, byte=byte, day=day, week=week)
                        return
                    # Reanchor if even the local transaction spans a quota reset.
                    db.execute("DELETE FROM budget_reservations WHERE id=?", (credit,))
                    if byte:
                        db.execute("DELETE FROM budget_reservations WHERE id=?", (byte,))
                    now = fresh

            lease = None
            if self.ip_pool is not None:
                capacity, period = self.ip_limit or (None, 60.0)
                lease = self.ip_pool.acquire(self.store, self.source, rate_weight, capacity,
                                             period, self.sleep, self.max_wait, pacing=limits,
                                             before_admit=reserve_budgets)
            else:
                self.store.pace_many(limits, self.sleep, self.max_wait, reserve_budgets)
            active_client = lease.client if lease is not None else self.client
            weight_stats = ({"ip_weight_reserved": rate_weight, "ip_weight_used": rate_weight}
                            if lease is not None else {})
            credit_window, byte_window = admitted["day"], admitted["week"]
            credit_reservation, byte_reservation = admitted["credit"], admitted["byte"]
            started = self.store.clock()
            try:
                with active_client.stream(
                                        method, url, params={**params, **private_params}, headers=attempt_headers,
                                        content=request_body, auth=auth,
                                        timeout=30, follow_redirects=False) as upstream:
                    received = bytearray()
                    for chunk in upstream.iter_bytes():
                        if len(received) + len(chunk) > self.max_response_bytes:
                            raise InvalidResponse(f"{self.source}: response size limit exceeded")
                        received.extend(chunk)
                    body = bytes(received)
                    wire = upstream.num_bytes_downloaded
                    # MockTransport preloaded bodies do not have a wire counter.
                    wire = wire or len(body)
                    status = upstream.status_code
                    safe_headers = {k.lower(): v for k, v in upstream.headers.items()
                                    if k.lower() in PUBLIC_HEADERS}
            except httpx.HTTPError:
                self.store.record(self.scope, requests=1, failures=1, credits_used=estimated_credits,
                                  **{service + "_requests": 1}, **weight_stats)
                # An uncertain request may have been charged. Keep its reservations.
                if attempt + 1 < self.max_attempts:
                    self.sleep(min(2 ** attempt, self.max_wait))
                    continue
                raise SourceError(f"{self.source}: transport failure") from None
            except InvalidResponse:
                self.store.record(self.scope, requests=1, failures=1, credits_used=estimated_credits,
                                  **{service + "_requests": 1}, **weight_stats)
                raise
            if lease is not None and 200 <= status < 300 and actual_weight is not None:
                try:
                    used_weight = actual_weight(body)
                    if type(used_weight) is not int or not 1 <= used_weight <= rate_weight:
                        raise ValueError
                except (ValueError, TypeError, UnicodeDecodeError):
                    self.store.record(self.scope, requests=1, failures=1,
                                      credits_used=estimated_credits,
                                      **{service + "_requests": 1}, **weight_stats)
                    raise InvalidResponse(f"{self.source}: invalid response weight") from None
                self.ip_pool.settle(self.store, lease, used_weight)
                weight_stats["ip_weight_used"] = used_weight
            try:
                charged = int(safe_headers.get("x-ratelimit-credits-used", estimated_credits))
                if charged < 0:
                    raise ValueError
            except ValueError:
                charged = estimated_credits
            self.store.settle(credit_reservation, charged)
            if byte_reservation is not None:
                self.store.settle(byte_reservation, wire)
            self.store.record(self.scope, requests=1, wire_bytes=wire, decoded_bytes=len(body),
                              credits_used=charged, failures=int(status >= 400),
                              **{service + "_requests": 1}, **weight_stats)
            # Persist provider cooldowns even when the caller cannot wait or retry.
            # Budget rejection takes precedence in the error contract, never in pacing.
            retry_delay = max(2 ** attempt, self._retry_after(safe_headers.get("retry-after")))
            if status == 429 and lease is not None:
                self.ip_pool.defer(self.store, self.source, lease.route, retry_delay)
            if (500 <= status <= 599 or (status == 429 and (
                    lease is None or not self.ip_throttle_only
                    or safe_headers.get("x-ratelimit-remaining") == "0"))):
                self.store.defer(self.scope + ":all", retry_delay)
            if observe:
                observe(safe_headers, request_windows={"day": credit_window, "week": byte_window})
            if status in (403, 429) and (safe_headers.get("x-ratelimit-remaining") == "0"
                                        or safe_headers.get("x-rejection-reason")):
                raise BudgetExceeded(f"{self.source}: provider allowance exhausted")
            if status == 429 or status == 503 or 500 <= status <= 599:
                delay = retry_delay
                if delay > self.max_wait or attempt + 1 == self.max_attempts:
                    if status == 429:
                        raise Throttled(self.source, delay)
                    raise HTTPFailure(self.source, status)
                continue
            if status < 200 or status >= 300:
                raise HTTPFailure(self.source, status)
            private = list(self._secrets)
            if auth and isinstance(auth, tuple):
                private.extend(value for value in auth if isinstance(value, str) and len(value) >= 8)
            authorization = attempt_headers.get("Authorization", "").partition(" ")[2]
            if len(authorization) >= 8:
                private.append(authorization)
            if not sensitive and any(
                    value.encode() in body or any(value in header for header in safe_headers.values())
                    for value in private):
                raise InvalidResponse(f"{self.source}: provider echoed a source credential")
            acquired = utc_stamp(started)
            response_hash = digest(body)
            snapshot_id = digest((request_hash + response_hash + acquired).encode())
            p = Provenance(self.source, method, url, {**params, **(form or {})}, request_hash, response_hash,
                           acquired, snapshot_id, license=self.license,
                           public_request_headers=identity["headers"],
                           request_body=request_body.decode("utf-8") if request_body is not None else None,
                           completed_at_utc=utc_stamp(self.store.clock()), http_status=status,
                           egress_ip_sha256=(digest(lease.public_ip.encode())
                                             if lease and lease.public_ip != "0.0.0.0" else None))
            response = Response(body, safe_headers, p, False, charged, wire)
            if not sensitive:
                self.store.put(cache_key, response)
            return response
        raise SourceError(f"{self.source}: retry limit reached")


def positive_int(value: int, maximum: int, name: str) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 1 and {maximum}")
    return value


def chunks(values, size: int) -> Iterator[list]:
    batch = []
    for value in values:
        batch.append(value)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch
