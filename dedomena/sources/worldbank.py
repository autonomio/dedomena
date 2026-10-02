"""World Bank Indicators v2: 60-indicator batches and large, counted pages."""
from __future__ import annotations

import json
import math
import re
from typing import Iterable, Iterator

import httpx

from .core import InvalidResponse, Page, Transport, positive_int


class WorldBank(Transport):
    # Documentation publishes no page ceiling. 32,767 worked in bounded probes;
    # 50,000 was rejected with HTTP 400. Keep the verified signed-short ceiling.
    MAX_PAGE_SIZE = 32_767
    MAX_INDICATORS = 60

    def __init__(self, **kwargs):
        # Configurable local pacing, not a claimed provider allowance.
        kwargs.setdefault("requests_per_second", 5)
        kwargs.setdefault("max_response_bytes", 32_000_000)
        super().__init__("worldbank", "https://api.worldbank.org/v2",
                         license="World Bank CC BY 4.0 with additional terms; metadata exceptions apply",
                         **kwargs)

    @staticmethod
    def _codes(values: str | Iterable[str], name: str, *, maximum: int | None = None) -> list[str]:
        codes = values.split(";") if isinstance(values, str) else list(values)
        pattern = r"[A-Za-z0-9_.-]+" if name == "indicators" else r"[A-Za-z0-9]+"
        if (not codes or any(not isinstance(code, str) or not re.fullmatch(pattern, code) for code in codes)
                or any(code in (".", "..") for code in codes)
                or len(set(codes)) != len(codes)):
            raise ValueError(f"{name} must contain distinct World Bank codes")
        if maximum is not None and len(codes) > maximum:
            raise ValueError(f"at most {maximum} {name} fit one World Bank query")
        if len(";".join(codes)) > 1500:
            raise ValueError(f"{name} exceeds World Bank's 1,500-character path segment limit")
        return codes

    @staticmethod
    def _dates(value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("date must be a World Bank year, month, quarter or range")
        parts = value.split(":")
        if len(parts) not in (1, 2):
            raise ValueError("date must be a World Bank period or range")
        parsed = []
        for part in parts:
            match = re.fullmatch(r"([0-9]{4})(?:(M)([0-9]{2})|(Q)([1-4]))?", part)
            if not match or match[1] == "0000":
                raise ValueError("date must contain valid years, YYYYQn or YYYYMnn")
            if match[2] and not 1 <= int(match[3]) <= 12:
                raise ValueError("date contains an invalid month")
            parsed.append((int(match[1]), match[2] or match[4] or "Y", int(match[3] or match[5] or 1)))
        if len(parsed) == 2 and (parsed[0][1] != parsed[1][1] or parsed[0] > parsed[1]):
            raise ValueError("date range must use one frequency with start no later than end")
        return value

    def _pages(self, path: str, params: dict, *, page_size: int, page: int,
               records_seen: int, refresh: bool, validate=None, warnings=()) -> Iterator[Page]:
        positive_int(page_size, self.MAX_PAGE_SIZE, "page_size")
        positive_int(page, 2_147_483_647, "page")
        if (type(records_seen) is not int or records_seen < 0
                or records_seen != (page - 1) * page_size):
            raise ValueError("page resume requires records_seen=(page-1)*page_size")
        params = {**params, "format": "json", "per_page": page_size, "page": page}
        if len(str(httpx.Request("GET", self.base_url + path, params=params).url)) > 4000:
            raise ValueError("World Bank query exceeds its 4,000-character URL limit")
        expected_total, update = None, None
        while True:
            response = self._request("GET", path, params=params, refresh=refresh)
            try:
                body = json.loads(response.body)
            except (ValueError, UnicodeDecodeError):
                raise InvalidResponse("worldbank: invalid JSON response") from None
            if (not isinstance(body, list) or len(body) != 2 or not isinstance(body[0], dict)
                    or not (isinstance(body[1], list) or body[1] is None)):
                raise InvalidResponse("worldbank: invalid result envelope")
            meta, rows = body[0], body[1] or []
            total, pages = meta.get("total"), meta.get("pages")
            # Observation endpoints use an integer; metadata endpoints use a
            # decimal string for this same documented pagination field.
            returned_size = meta.get("per_page")
            if isinstance(returned_size, str) and returned_size.isascii() and returned_size.isdigit():
                returned_size = int(returned_size)
            if (type(total) is not int or total < 0 or type(pages) is not int or pages < 0
                    or type(meta.get("page")) is not int or meta["page"] != params["page"]
                    or type(returned_size) is not int or returned_size != page_size
                    or len(rows) > page_size):
                raise InvalidResponse("worldbank: invalid count or page metadata")
            if pages != math.ceil(total / page_size):
                raise InvalidResponse("worldbank: page count disagrees with total")
            if expected_total is not None and (total != expected_total or meta.get("lastupdated") != update):
                raise InvalidResponse("worldbank: dataset changed during pagination; restart")
            expected_total, update = total, meta.get("lastupdated")
            if any(not isinstance(row, dict) for row in rows):
                raise InvalidResponse("worldbank: invalid record")
            if validate:
                for row in rows:
                    validate(row)
            observed = records_seen + len(rows)
            complete = observed == total
            if observed > total or (not complete and len(rows) != page_size):
                raise InvalidResponse("worldbank: pagination ended before reported count")
            following = params["page"] + 1
            if not complete and following > pages:
                raise InvalidResponse("worldbank: no next page before reported total")
            yield self.page(response, rows, total=total,
                            next_cursor=None if complete else str(following), complete=complete,
                            records_seen=observed, warnings=warnings)
            if complete:
                return
            records_seen = observed
            params["page"] = following

    def search(self, query: str | Iterable[str], *, countries: str | Iterable[str] = "all",
               source: int = 2, date: str | None = None, footnotes: bool = False,
               page_size: int = 32_767, page: int = 1, records_seen: int = 0,
               refresh: bool = False) -> Iterator[Page]:
        """Fetch observations for up to 60 exact indicator codes in one dataset.

        all includes countries and regional/income aggregates. Null observations,
        country/indicator IDs, dates, units, decimals and footnotes remain native.
        """
        indicators = self._codes(query, "indicators", maximum=self.MAX_INDICATORS)
        countries = self._codes(countries, "countries")
        all_countries = any(code.upper() == "ALL" for code in countries)
        if all_countries:
            if len(countries) != 1:
                raise ValueError("all cannot be combined with country codes")
            countries = ["all"]
        positive_int(source, 2_147_483_647, "source")
        if type(footnotes) is not bool:
            raise ValueError("footnotes must be boolean")
        params = {"source": source}
        if date is not None:
            params["date"] = self._dates(date)
        if footnotes:
            params["footnote"] = "y"
        wanted = {value.upper() for value in indicators}
        selected_countries = {value.upper() for value in countries}
        def validate(row):
            indicator, country = row.get("indicator"), row.get("country")
            if (not isinstance(indicator, dict) or not isinstance(indicator.get("id"), str)
                    or indicator["id"].upper() not in wanted
                    or not isinstance(country, dict) or not isinstance(country.get("id"), str)
                    or not country["id"] or not isinstance(row.get("date"), str)
                    or "value" not in row):
                raise InvalidResponse("worldbank: observation lacks requested indicator or identity")
            if not all_countries:
                actual = {country["id"].upper(), str(row.get("countryiso3code", "")).upper()}
                if not actual.intersection(selected_countries):
                    raise InvalidResponse("worldbank: response contains an unrequested country")
        path = "/country/" + ";".join(countries) + "/indicator/" + ";".join(indicators)
        yield from self._pages(path, params, page_size=page_size, page=page,
                               records_seen=records_seen, refresh=refresh, validate=validate,
                               warnings=("Latest observations can be revised; historical vintages are not supplied by this endpoint.",))

    def observations(self, indicators: str | Iterable[str], **kwargs) -> Iterator[Page]:
        return self.search(indicators, **kwargs)

    def indicators(self, *, source: int = 2, page_size: int = 32_767,
                   page: int = 1, records_seen: int = 0, refresh: bool = False) -> Iterator[Page]:
        """Discover indicator metadata including units, agency and source notes."""
        positive_int(source, 2_147_483_647, "source")
        def validate(row):
            if not isinstance(row.get("id"), str) or not row["id"]:
                raise InvalidResponse("worldbank: indicator metadata lacks identifier")
            if not isinstance(row.get("source"), dict) or str(row["source"].get("id")) != str(source):
                raise InvalidResponse("worldbank: indicator metadata belongs to a different dataset")
        yield from self._pages("/indicator", {"source": source}, page_size=page_size,
                               page=page, records_seen=records_seen, refresh=refresh, validate=validate)

    def fetch(self, indicator: str, *, source: int = 2, refresh: bool = False) -> Page:
        """Exact indicator metadata; source defaults to World Development Indicators."""
        ident = self._codes(indicator, "indicators", maximum=1)[0]
        positive_int(source, 2_147_483_647, "source")
        def validate(row):
            if not isinstance(row.get("id"), str) or row["id"].upper() != ident.upper():
                raise InvalidResponse("worldbank: exact lookup returned a different indicator")
            if not isinstance(row.get("source"), dict) or str(row["source"].get("id")) != str(source):
                raise InvalidResponse("worldbank: indicator metadata belongs to a different dataset")
        pages = self._pages("/indicator/" + ident, {"source": source}, page_size=self.MAX_PAGE_SIZE,
                            page=1, records_seen=0, refresh=refresh, validate=validate)
        result = next(pages)
        if not result.complete or len(result.records) != 1:
            raise InvalidResponse("worldbank: exact indicator metadata is missing or ambiguous")
        return result

    def quota(self) -> dict:
        return {"api_key_required": False, "daily_provider_quota": None,
                "local_requests_per_second": self.rate, "max_indicators_per_query": 60,
                "local_max_page_size": self.MAX_PAGE_SIZE,
                "note": "No published daily/rate/page ceiling. 32,767 per page was accepted and 50,000 rejected in bounded probes."}
