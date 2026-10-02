"""FRED/ALFRED series, dated vintages, and 500,000-observation release pages."""
from __future__ import annotations

from datetime import date, datetime, timezone
import os
import re
from typing import Iterator

from .core import InvalidResponse, Page, Transport, positive_int


class FRED(Transport):
    """A user's free FRED key; preserve native decimal strings and missing dots."""
    MAX_PAGE_SIZE = 100_000
    MAX_RELEASE_OBSERVATIONS = 500_000

    def __init__(self, api_key: str | None = None, **kwargs):
        self.api_key = api_key if api_key is not None else os.environ.get("FRED_API_KEY", "")
        if not isinstance(self.api_key, str) or not re.fullmatch(r"[a-z0-9]{32}", self.api_key):
            raise ValueError("FRED requires a 32-character lowercase alphanumeric FRED_API_KEY or api_key")
        kwargs.setdefault("requests_per_second", 2)
        if kwargs["requests_per_second"] > 2:
            raise ValueError("FRED permits at most 2 requests per second / 120 per minute")
        # A maximum release page can exceed the generic 16 MB response ceiling.
        kwargs.setdefault("max_response_bytes", 128_000_000)
        super().__init__("fred", "https://api.stlouisfed.org/fred", credential=self.api_key,
                         license="FRED terms; per-series copyright and attribution apply", **kwargs)

    @staticmethod
    def _id(value: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", value):
            raise ValueError("series_id must be a FRED series identifier")
        return value

    @staticmethod
    def _date(value: str, name: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            raise ValueError(f"{name} must be YYYY-MM-DD")
        try:
            date.fromisoformat(value)
        except ValueError:
            raise ValueError(f"{name} must be a valid calendar date") from None
        return value

    def _today(self) -> str:
        return datetime.fromtimestamp(self.store.clock(), timezone.utc).date().isoformat()

    def _v1(self, path: str, params: dict, *, refresh: bool):
        return self._request("GET", path, params={"file_type": "json", **params},
                             private_params={"api_key": self.api_key}, refresh=refresh)

    def _pages(self, path: str, params: dict, rows_key: str, *, page_size: int,
               offset: int, records_seen: int, refresh: bool, convert=None) -> Iterator[Page]:
        if (type(offset) is not int or offset < 0 or type(records_seen) is not int
                or records_seen < 0 or offset != records_seen):
            raise ValueError("offset must match the nonnegative records_seen checkpoint")
        params = {**params, "limit": page_size, "offset": offset, "sort_order": "asc"}
        expected_total = None
        while True:
            response = self._v1(path, params, refresh=refresh)
            body = response.json()
            rows, total = body.get(rows_key), body.get("count")
            if (not isinstance(rows, list) or type(total) is not int or total < 0
                    or type(body.get("offset")) is not int or body["offset"] != params["offset"]
                    or len(rows) > page_size):
                raise InvalidResponse("fred: invalid results, count or offset")
            if expected_total is not None and total != expected_total:
                raise InvalidResponse("fred: result count changed during pagination")
            expected_total = total
            for bound in ("realtime_start", "realtime_end"):
                if bound in params and body.get(bound) != params[bound]:
                    raise InvalidResponse("fred: response does not match requested real-time bounds")
            rows = [convert(row) for row in rows] if convert else rows
            if any(not isinstance(row, dict) for row in rows):
                raise InvalidResponse("fred: invalid record")
            observed = records_seen + len(rows)
            if observed > total or (observed < total and not rows):
                raise InvalidResponse("fred: pagination disagrees with reported count")
            complete = observed == total
            yield self.page(response, rows, total=total,
                            next_cursor=None if complete else str(observed),
                            complete=complete, records_seen=observed)
            if complete:
                return
            records_seen = observed
            params["offset"] = observed

    def search(self, query: str, *, page_size: int = 1000, offset: int = 0,
               records_seen: int = 0, as_of: str | None = None,
               refresh: bool = False) -> Iterator[Page]:
        """Search native FRED text; pin the metadata real-time period to one date."""
        positive_int(page_size, 1000, "page_size")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a nonempty FRED search")
        vintage = self._date(as_of or self._today(), "as_of")
        def record(row):
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                raise InvalidResponse("fred: search result lacks series identifier")
            return row
        yield from self._pages("/series/search", {"search_text": query,
                               "search_type": "full_text", "order_by": "series_id",
                               "realtime_start": vintage, "realtime_end": vintage}, "seriess",
                               page_size=page_size, offset=offset, records_seen=records_seen,
                               refresh=refresh, convert=record)

    def fetch(self, series_id: str, *, as_of: str | None = None, refresh: bool = False) -> Page:
        """Metadata keeps units, frequency, seasonality, notes, and source links."""
        series_id = self._id(series_id)
        vintage = self._date(as_of or self._today(), "as_of")
        response = self._v1("/series", {"series_id": series_id,
                            "realtime_start": vintage, "realtime_end": vintage}, refresh=refresh)
        body = response.json()
        if body.get("realtime_start") != vintage or body.get("realtime_end") != vintage:
            raise InvalidResponse("fred: metadata does not match requested real-time bounds")
        rows = body.get("seriess")
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise InvalidResponse("fred: invalid exact series metadata")
        if rows[0].get("id") != series_id:
            raise InvalidResponse("fred: exact lookup returned a different series")
        return self.page(response, rows, total=1, complete=True, records_seen=1)

    def observations(self, series_id: str, *, as_of: str | None = None,
                     realtime_start: str | None = None, realtime_end: str | None = None,
                     observation_start: str | None = None, observation_end: str | None = None,
                     output_type: int = 1, units: str = "lin", frequency: str | None = None,
                     aggregation_method: str = "avg", page_size: int = 100_000,
                     offset: int = 0, records_seen: int = 0,
                     refresh: bool = False) -> Iterator[Page]:
        """ALFRED as-of data or an explicit revision interval, with no float conversion.

        as_of sets both real-time bounds. Without explicit bounds, today's UTC date
        is pinned in the request receipt. Fetch series metadata for native units.
        output_type=4 requests initial releases; 1 preserves real-time periods.
        """
        positive_int(page_size, self.MAX_PAGE_SIZE, "page_size")
        positive_int(output_type, 4, "output_type")
        series_id = self._id(series_id)
        if as_of is not None and (realtime_start is not None or realtime_end is not None):
            raise ValueError("provide as_of or explicit real-time bounds")
        today = self._today()
        start = self._date(as_of or realtime_start or today, "realtime_start")
        end = self._date(as_of or realtime_end or today, "realtime_end")
        if start > end:
            raise ValueError("realtime_start must not exceed realtime_end")
        if units not in ("lin", "chg", "ch1", "pch", "pc1", "pca", "cch", "cca", "log"):
            raise ValueError("unsupported FRED units transformation")
        if aggregation_method not in ("avg", "sum", "eop"):
            raise ValueError("unsupported FRED aggregation method")
        if frequency is not None and frequency not in (
                "d", "w", "bw", "m", "q", "sa", "a", "wef", "weth", "wew",
                "wetu", "wem", "wesu", "wesa", "bwew", "bwem"):
            raise ValueError("unsupported FRED frequency")
        params = {"series_id": series_id, "realtime_start": start, "realtime_end": end,
                  "units": units, "output_type": output_type}
        if observation_start is not None:
            params["observation_start"] = self._date(observation_start, "observation_start")
        if observation_end is not None:
            params["observation_end"] = self._date(observation_end, "observation_end")
        if (observation_start is not None and observation_end is not None
                and observation_start > observation_end):
            raise ValueError("observation_start must not exceed observation_end")
        if frequency is not None:
            params.update(frequency=frequency, aggregation_method=aggregation_method)
        def record(row):
            if not isinstance(row, dict) or not isinstance(row.get("date"), str):
                raise InvalidResponse("fred: observation lacks date")
            try:
                self._date(row["date"], "observation date")
            except ValueError:
                raise InvalidResponse("fred: invalid observation date") from None
            # Output types 2/3 name value columns by vintage; retain all native keys.
            values = [value for key, value in row.items()
                      if key not in ("date", "realtime_start", "realtime_end")]
            if not values or any(not isinstance(value, str) for value in values):
                raise InvalidResponse("fred: observation values must be native strings")
            return row
        yield from self._pages("/series/observations", params, "observations",
                               page_size=page_size, offset=offset, records_seen=records_seen,
                               refresh=refresh, convert=record)

    def vintages(self, series_id: str, *, page_size: int = 10_000, offset: int = 0,
                 records_seen: int = 0, refresh: bool = False) -> Iterator[Page]:
        """Enumerate dates on which ALFRED published new or revised observations."""
        positive_int(page_size, 10_000, "page_size")
        series_id = self._id(series_id)
        def record(value):
            try:
                self._date(value, "vintage_date")
            except ValueError:
                raise InvalidResponse("fred: invalid vintage date") from None
            return {"series_id": series_id, "vintage_date": value}
        yield from self._pages("/series/vintagedates", {"series_id": series_id}, "vintage_dates",
                               page_size=page_size, offset=offset, records_seen=records_seen,
                               refresh=refresh, convert=record)

    def release_observations(self, release_id: int, *, page_size: int = 500_000,
                             cursor: str | None = None, records_seen: int = 0,
                             refresh: bool = False) -> Iterator[Page]:
        """Bulk latest data for every series in a release; preserve grouped series.

        page_size counts observations; Page.records counts native series fragments.
        A series can span pages. ALFRED observations(as_of=...) is the dated route.
        """
        positive_int(release_id, 2_147_483_647, "release_id")
        positive_int(page_size, self.MAX_RELEASE_OBSERVATIONS, "page_size")
        if (cursor is not None and (not isinstance(cursor, str) or not cursor)
                or type(records_seen) is not int or records_seen < 0
                or (cursor is not None and not records_seen)):
            raise ValueError("cursor resume requires positive records_seen")
        params = {"release_id": release_id, "format": "json", "limit": page_size}
        if cursor is not None:
            params["next_cursor"] = cursor
        cursors, versions = set(), {}
        while True:
            current = params.get("next_cursor")
            if current in cursors:
                raise InvalidResponse("fred: repeated release cursor")
            cursors.add(current)
            response = self._request("GET", "/v2/release/observations", params=params,
                                     headers={"Authorization": "Bearer " + self.api_key},
                                     refresh=refresh)
            body = response.json()
            release, rows, more = body.get("release"), body.get("series"), body.get("has_more")
            if (not isinstance(release, dict) or release.get("release_id") != release_id
                    or not isinstance(rows, list) or type(more) is not bool):
                raise InvalidResponse("fred: invalid release identity or result envelope")
            observations = 0
            ids = set()
            for row in rows:
                if (not isinstance(row, dict) or not isinstance(row.get("series_id"), str)
                        or not row["series_id"] or row["series_id"] in ids
                        or not isinstance(row.get("observations"), list)
                        or any(not isinstance(row.get(key), str)
                               for key in ("title", "frequency", "units", "last_updated", "copyright_id"))):
                    raise InvalidResponse("fred: release lacks series identity or metadata")
                ident, version = row["series_id"], row["last_updated"]
                ids.add(ident)
                if ident in versions and versions[ident] != version:
                    raise InvalidResponse("fred: series revised during release pagination; restart")
                versions[ident] = version
                dates = set()
                for observation in row["observations"]:
                    if (not isinstance(observation, dict)
                            or not isinstance(observation.get("date"), str)
                            or not isinstance(observation.get("value"), str)
                            or observation["date"] in dates):
                        raise InvalidResponse("fred: invalid or repeated release observation")
                    try:
                        self._date(observation["date"], "observation date")
                    except ValueError:
                        raise InvalidResponse("fred: invalid release observation date") from None
                    dates.add(observation["date"])
                observations += len(row["observations"])
            following = body.get("next_cursor")
            if (observations > page_size or (more and (not observations
                    or not isinstance(following, str) or not following or following in cursors))):
                raise InvalidResponse("fred: incomplete release pagination")
            records_seen += len(rows)
            self.store.record(self.scope, observations_delivered=observations,
                              upstream_observations=0 if response.cache_hit else observations)
            yield self.page(response, rows, next_cursor=following if more else None,
                            complete=not more, records_seen=records_seen,
                            warnings=("Latest release data can contain mixed update times; use ALFRED as_of for historical knowledge.",
                                      "Records are series fragments; series can span pages. Page size counts observations."))
            if not more:
                return
            params["next_cursor"] = following

    def quota(self) -> dict:
        return {"api_key_required": True, "daily_provider_quota": None,
                "requests_per_minute": 120, "max_requests_per_second": 2,
                "local_requests_per_second": self.rate,
                "max_series_observations": self.MAX_PAGE_SIZE,
                "max_release_observations": self.MAX_RELEASE_OBSERVATIONS,
                "note": "v1 uses private query authentication; v2 uses Bearer. Each user needs their own free key."}
