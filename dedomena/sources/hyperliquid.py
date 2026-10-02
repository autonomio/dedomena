"""Hyperliquid public market data with native precision and replayable receipts."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import json
import math
import re
from typing import Callable, Iterator

from .core import InvalidResponse, Page, SearchLimitExceeded, Transport, canonical, digest


REQUEST_TYPES = frozenset(("allMids", "metaAndAssetCtxs", "spotMetaAndAssetCtxs", "l2Book",
                           "candleSnapshot", "fundingHistory"))


def info_policy(content: bytes) -> tuple[int, Callable[[bytes], int] | None]:
    """Validate the fixed read-only info schema and derive its dispatch weight.

    Transport invokes this policy even for direct _request callers. Unsupported
    actions and unexpected fields fail before cache lookup or network dispatch.
    Response-sized weights reserve a conservative bound and settle by item count.
    """
    try:
        body = json.loads(content)
    except (ValueError, TypeError, UnicodeDecodeError):
        raise ValueError("Hyperliquid info body must be JSON") from None
    if (not isinstance(body, dict) or not isinstance(body.get("type"), str)
            or body["type"] not in REQUEST_TYPES):
        raise ValueError("Hyperliquid request type is outside the read-only adapter")
    kind = body["type"]
    required = {
        "allMids": {"type"}, "metaAndAssetCtxs": {"type"}, "spotMetaAndAssetCtxs": {"type"},
        "l2Book": {"type", "coin"},
        "candleSnapshot": {"type", "req"}, "fundingHistory": {"type", "coin", "startTime", "endTime"},
    }[kind]
    optional = {"allMids": {"dex"}, "metaAndAssetCtxs": {"dex"},
                "l2Book": {"nSigFigs", "mantissa"}}.get(kind, set())
    if not required <= set(body) <= required | optional:
        raise ValueError("Hyperliquid info body has unsupported or missing fields")
    if "dex" in body:
        Hyperliquid._dex(body["dex"])
    if "coin" in body:
        Hyperliquid._coin(body["coin"])
    if kind == "l2Book":
        Hyperliquid._aggregation(body.get("nSigFigs"), body.get("mantissa"))
    if kind == "fundingHistory":
        Hyperliquid._range(body["startTime"], body["endTime"])
    if kind == "candleSnapshot":
        request = body["req"]
        if not isinstance(request, dict) or set(request) != {"coin", "interval", "startTime", "endTime"}:
            raise ValueError("Hyperliquid candle request has invalid fields")
        Hyperliquid._coin(request["coin"])
        Hyperliquid._interval(request["interval"])
        Hyperliquid._range(request["startTime"], request["endTime"])
    if kind in ("allMids", "l2Book"):
        return 2, None
    if kind in ("metaAndAssetCtxs", "spotMetaAndAssetCtxs"):
        return 20, None
    maximum, per_unit = {"fundingHistory": (500, 20),
                         "candleSnapshot": (5000, 60)}[kind]
    def actual_weight(content: bytes) -> int:
        rows = json.loads(content)
        if (not isinstance(rows, list) or len(rows) > maximum
                or any(not isinstance(row, dict) for row in rows)):
            raise ValueError("invalid Hyperliquid item-count response")
        return 20 + math.ceil(len(rows) / per_unit)
    return 20 + math.ceil(maximum / per_unit), actual_weight


class Hyperliquid(Transport):
    """Restricted public /info queries; no account, signing, or exchange actions.

    Snapshot records preserve the native response envelope. Historical funding
    pages remove exact inclusive-boundary repeats; candles retain the provider's
    recent-history limitation rather than claiming complete historical coverage.
    """

    MAX_FUNDING_PAGE = 500
    MAX_CANDLES = 5000
    INTERVALS = frozenset(("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h",
                           "8h", "12h", "1d", "3d", "1w", "1M"))

    def __init__(self, **kwargs):
        kwargs.setdefault("requests_per_second", 1000)
        kwargs.setdefault("ip_limit", (1200, 60.0))
        limit = kwargs["ip_limit"]
        if (not isinstance(limit, tuple) or len(limit) != 2 or type(limit[0]) is not int
                or not 0 < limit[0] <= 1200 or isinstance(limit[1], bool)
                or not isinstance(limit[1], (int, float)) or not math.isfinite(limit[1]) or limit[1] < 60):
            raise ValueError("Hyperliquid IP policy cannot exceed 1,200 weight per minute")
        kwargs["ip_throttle_only"] = True
        kwargs.setdefault("cache_ttl", 0)
        super().__init__("hyperliquid", "https://api.hyperliquid.xyz",
                         license="Hyperliquid public API; provider terms apply", **kwargs)

    @staticmethod
    def _coin(coin: str) -> str:
        if (not isinstance(coin, str) or len(coin) > 128 or not re.fullmatch(
                r"(?:[A-Za-z0-9_]+:)?[A-Za-z0-9_@#]+(?:/[A-Za-z0-9_]+)?", coin)):
            raise ValueError("coin must be a native Hyperliquid asset name")
        return coin

    @staticmethod
    def _dex(dex: str) -> str:
        if not isinstance(dex, str) or not re.fullmatch(r"[A-Za-z0-9_-]{0,64}", dex):
            raise ValueError("dex must be a Hyperliquid perpetual dex name")
        return dex

    @staticmethod
    def _time(value: int, name: str) -> int:
        if type(value) is not int or not 0 <= value <= 9_007_199_254_740_991:
            raise ValueError(f"{name} must be a nonnegative integer timestamp in milliseconds")
        return value

    @classmethod
    def _range(cls, start_time: int, end_time: int) -> tuple[int, int]:
        start, end = cls._time(start_time, "start_time"), cls._time(end_time, "end_time")
        if start > end:
            raise ValueError("start_time must not follow end_time")
        return start, end

    @classmethod
    def _interval(cls, interval: str) -> str:
        if not isinstance(interval, str) or interval not in cls.INTERVALS:
            raise ValueError("interval must be a supported Hyperliquid candle interval")
        return interval

    @staticmethod
    def _aggregation(n_sig_figs, mantissa):
        if n_sig_figs is not None and (type(n_sig_figs) is not int or n_sig_figs not in (2, 3, 4, 5)):
            raise ValueError("n_sig_figs must be 2, 3, 4, 5 or None")
        if mantissa is not None and (type(mantissa) is not int or mantissa not in (1, 2, 5) or n_sig_figs != 5):
            raise ValueError("mantissa must be 1, 2 or 5 and requires n_sig_figs=5")

    @staticmethod
    def _decimal(value, *, nonnegative: bool = False) -> bool:
        if not isinstance(value, str) or not value or len(value) > 256:
            return False
        try:
            number = Decimal(value)
            return number.is_finite() and (not nonnegative or number >= 0)
        except InvalidOperation:
            return False

    def _info(self, body: dict, *, refresh: bool):
        content = canonical(body).encode()
        weight, actual = info_policy(content)
        response = self._request("POST", "/info", content=content,
                                 headers={"Content-Type": "application/json"},
                                 rate_weight=weight, actual_weight=actual, refresh=refresh)
        try:
            data = json.loads(response.body)
        except (ValueError, UnicodeDecodeError):
            raise InvalidResponse("hyperliquid: invalid JSON response") from None
        return response, data

    def all_mids(self, *, dex: str = "", refresh: bool = False) -> Page:
        """One native coin-to-price map; spot mids belong to the default dex."""
        dex = self._dex(dex)
        response, data = self._info({"type": "allMids", "dex": dex}, refresh=refresh)
        if (not isinstance(data, dict) or any(not isinstance(coin, str) or not coin
                or not self._decimal(price, nonnegative=True) for coin, price in data.items())):
            raise InvalidResponse("hyperliquid: invalid mids response")
        return self.page(response, [data], total=1, complete=True, records_seen=1,
                         warnings=("Point-in-time mids; an empty book can fall back to the last trade price.",))

    def markets(self, *, spot: bool = False, dex: str = "", refresh: bool = False) -> Page:
        """One native metadata/context envelope; spot contexts can outnumber pairs."""
        dex = self._dex(dex)
        if type(spot) is not bool or (spot and dex):
            raise ValueError("spot must be boolean; spot metadata does not accept a dex")
        body = {"type": "spotMetaAndAssetCtxs" if spot else "metaAndAssetCtxs"}
        if not spot:
            body["dex"] = dex
        response, data = self._info(body, refresh=refresh)
        if (not isinstance(data, list) or len(data) != 2 or not isinstance(data[0], dict)
                or not isinstance(data[1], list) or not isinstance(data[0].get("universe"), list)):
            raise InvalidResponse("hyperliquid: invalid market metadata envelope")
        meta, contexts = data
        universe = meta["universe"]
        if ((not spot and len(universe) != len(contexts)) or any(not isinstance(row, dict)
                or not isinstance(row.get("name"), str) or not row["name"] for row in universe)
                or len({row["name"] for row in universe}) != len(universe)
                or any(not isinstance(row, dict) for row in contexts)
                or (spot and (not isinstance(meta.get("tokens"), list)
                              or any(not isinstance(row, dict) for row in meta["tokens"])))):
            raise InvalidResponse("hyperliquid: inconsistent market metadata or asset contexts")
        return self.page(response, [{"meta": meta, "assetCtxs": contexts}], total=1,
                         complete=True, records_seen=1,
                         warnings=(("Spot contexts and pair metadata have independent lengths; native coin fields and token indices are retained."
                                    if spot else "Perpetual asset contexts match universe positions; native decimal strings are retained."),))

    def order_book(self, coin: str, *, n_sig_figs: int | None = None,
                   mantissa: int | None = None, refresh: bool = False) -> Page:
        """Native L2 snapshot, at most twenty levels on each side."""
        coin = self._coin(coin)
        self._aggregation(n_sig_figs, mantissa)
        body = {"type": "l2Book", "coin": coin}
        if n_sig_figs is not None:
            body["nSigFigs"] = n_sig_figs
        if mantissa is not None:
            body["mantissa"] = mantissa
        response, data = self._info(body, refresh=refresh)
        if (not isinstance(data, dict) or data.get("coin") != coin
                or type(data.get("time")) is not int or data["time"] < 0
                or not isinstance(data.get("levels"), list) or len(data["levels"]) != 2
                or any(not isinstance(side, list) or len(side) > 20 for side in data["levels"])):
            raise InvalidResponse("hyperliquid: invalid book identity or levels")
        for side in data["levels"]:
            if any(not isinstance(row, dict) or not self._decimal(row.get("px"), nonnegative=True)
                   or not self._decimal(row.get("sz"), nonnegative=True)
                   or type(row.get("n")) is not int or row["n"] < 0 for row in side):
                raise InvalidResponse("hyperliquid: invalid book level")
        return self.page(response, [data], total=1, complete=True, records_seen=1,
                         warnings=("Point-in-time L2 snapshot; the provider returns at most 20 levels per side.",))

    def candles(self, coin: str, interval: str, start_time: int, end_time: int,
                *, refresh: bool = False) -> Page:
        """Return available candles; only the provider's latest 5,000 exist here."""
        coin, interval = self._coin(coin), self._interval(interval)
        start, end = self._range(start_time, end_time)
        response, rows = self._info({"type": "candleSnapshot", "req": {
            "coin": coin, "interval": interval, "startTime": start, "endTime": end}}, refresh=refresh)
        if not isinstance(rows, list) or len(rows) > self.MAX_CANDLES:
            raise InvalidResponse("hyperliquid: invalid candle response")
        previous = -1
        for row in rows:
            if (not isinstance(row, dict) or row.get("s") != coin or row.get("i") != interval
                    or type(row.get("t")) is not int or type(row.get("T")) is not int
                    or row["t"] < 0 or row["T"] < row["t"] or row["t"] > end or row["T"] < start
                    or row["t"] <= previous or type(row.get("n")) is not int or row["n"] < 0
                    or any(not self._decimal(row.get(key), nonnegative=True) for key in ("o", "h", "l", "c", "v"))):
                raise InvalidResponse("hyperliquid: invalid candle identity, range or values")
            previous = row["t"]
        return self.page(response, rows, complete=False, records_seen=len(rows),
                         warnings=("Only the latest 5,000 candles are retained; a short or empty response does not prove historical coverage.",
                                   "The current interval can be unfinished; OHLCV values remain native decimal strings."))

    def funding_history(self, coin: str, start_time: int, end_time: int | None = None,
                        *, cursor: str | None = None, refresh: bool = False) -> Iterator[Page]:
        """Enumerate a pinned inclusive range with safe timestamp overlap.

        Resume with the same coin/start/end and the preceding Page.next_cursor.
        If end_time is omitted, the cursor retains the initial retrieval cutoff.
        The opaque cursor contains the count and hashes of already emitted rows
        at the inclusive boundary. A saturated timestamp cannot be enumerated
        safely and raises SearchLimitExceeded before that page is delivered.
        """
        coin = self._coin(coin)
        start = self._time(start_time, "start_time")
        if end_time is not None:
            self._time(end_time, "end_time")
        current, seen, boundary = start, 0, set()
        if cursor is not None:
            if not isinstance(cursor, str) or len(cursor) > 50_000:
                raise ValueError("cursor must be a funding checkpoint for this exact query")
            try:
                state = json.loads(cursor)
                if (not isinstance(state, dict) or set(state) != {"coin", "start", "end", "after", "seen", "boundary"}
                        or state["coin"] != coin or state["start"] != start
                        or (end_time is not None and state["end"] != end_time)
                        or type(state["seen"]) is not int or state["seen"] <= 0
                        or not isinstance(state["boundary"], list) or not state["boundary"]
                        or len(state["boundary"]) > self.MAX_FUNDING_PAGE
                        or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                               for value in state["boundary"])):
                    raise ValueError
                end = self._time(state["end"], "cursor end")
                current = self._time(state["after"], "cursor after")
                if not start < current <= end:
                    raise ValueError
                seen, boundary = state["seen"], set(state["boundary"])
            except (ValueError, TypeError, KeyError):
                raise ValueError("cursor must be a funding checkpoint for this exact query") from None
        else:
            end = self._time(end_time if end_time is not None else int(self.store.clock() * 1000), "end_time")
        self._range(start, end)
        while True:
            response, rows = self._info({"type": "fundingHistory", "coin": coin,
                                        "startTime": current, "endTime": end}, refresh=refresh)
            if not isinstance(rows, list) or len(rows) > self.MAX_FUNDING_PAGE:
                raise InvalidResponse("hyperliquid: invalid funding history response")
            previous, hashes = current, set()
            emitted = []
            for row in rows:
                if (not isinstance(row, dict) or row.get("coin") != coin
                        or type(row.get("time")) is not int or not current <= row["time"] <= end
                        or row["time"] < previous or not self._decimal(row.get("fundingRate"))
                        or not self._decimal(row.get("premium"))):
                    raise InvalidResponse("hyperliquid: invalid funding identity, order or range")
                ident = digest(canonical(row).encode())
                if ident in hashes:
                    raise InvalidResponse("hyperliquid: duplicate funding row in response")
                hashes.add(ident)
                previous = row["time"]
                if not (row["time"] == current and ident in boundary):
                    emitted.append(row)
            more = len(rows) == self.MAX_FUNDING_PAGE
            if more and previous == current:
                raise SearchLimitExceeded("hyperliquid: funding timestamp reaches the page cap; narrow the range")
            seen += len(emitted)
            following = None
            if more:
                boundary = {digest(canonical(row).encode()) for row in rows if row["time"] == previous}
                following = canonical({"coin": coin, "start": start, "end": end,
                                       "after": previous, "seen": seen, "boundary": sorted(boundary)})
            if len(rows) > len(emitted):
                self.store.record(self.scope, upstream_records=0 if response.cache_hit else len(rows) - len(emitted),
                                  boundary_repeats=len(rows) - len(emitted))
            yield self.page(response, emitted, complete=not more, next_cursor=following,
                            records_seen=seen,
                            warnings=("Inclusive funding timestamp overlap is deduplicated by exact row hash; this is provider-available history.",))
            if not more:
                return
            current = previous

    def fetch(self, identifier: str, *, refresh: bool = False) -> Page:
        return self.order_book(identifier, refresh=refresh)

    def search(self, query: str, *, refresh: bool = False) -> Iterator[Page]:
        yield self.fetch(query, refresh=refresh)

    def quota(self) -> dict:
        return {"api_key_required": False, "daily_provider_quota": None,
                "ip_weight_per_minute": 1200, "ip_weight_per_second_average": 20,
                "local_requests_per_second": self.rate,
                "reserved_request_weights": {"all_mids": 2, "order_book": 2, "markets": 20,
                                             "funding_history": 45, "candles": 104},
                "max_candles_retained": self.MAX_CANDLES,
                "funding_page_cap": self.MAX_FUNDING_PAGE,
                "note": "Response-sized requests reserve maximum weight, then use rounded-up returned counts for local accounting; provider billing is not reported. IP pools do not expand address limits."}
