"""OpenAlex works: compact cursor search, free exact lookup and bounded batches."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import os
import math
import re
from dataclasses import replace
from typing import Iterable, Iterator
from urllib.parse import quote

import httpx

from .core import (InvalidResponse, Page, Transport, chunks,
                   positive_int, window)

FIELDS = {
    "discovery": "id,doi,title,publication_year,ids,type",
    "evidence": ("id,doi,title,publication_year,publication_date,ids,type,"
                 "abstract_inverted_index,authorships,primary_location,cited_by_count,"
                 "open_access,best_oa_location,locations,has_content,content_urls"),
}
QUOTA_FIELDS = {
    "credits_limit", "credits_used", "credits_remaining", "daily_budget_usd",
    "daily_used_usd", "daily_remaining_usd", "resets_at", "resets_in_seconds",
}
OPERATIONS = {"singleton", "list", "search", "semantic", "content", "text"}


class OpenAlex(Transport):
    MAX_PAGE_SIZE = 100
    DAILY_FREE_CREDITS = 10_000

    def __init__(self, api_key: str | None = None, *, daily_credit_limit: int = 10_000,
                 profile: str = "discovery", **kwargs):
        self.api_key = api_key if api_key is not None else os.environ.get("OPENALEX_API_KEY", "")
        if type(daily_credit_limit) is not int or daily_credit_limit < 0:
            raise ValueError("daily_credit_limit must be a nonnegative integer")
        if profile not in FIELDS:
            raise ValueError("profile must be discovery or evidence")
        self.daily_credit_limit, self.profile = daily_credit_limit, profile
        self._quota_date = None
        import threading
        self._quota_lock = threading.Lock()
        kwargs.setdefault("requests_per_second", 100)
        if kwargs["requests_per_second"] > 100:
            raise ValueError("OpenAlex permits at most 100 requests per second")
        super().__init__("openalex", "https://api.openalex.org", credential=self.api_key,
                         license="CC0 metadata; article text has separate rights", **kwargs)

    def _headers(self):
        return {"Authorization": "Bearer " + self.api_key} if self.api_key else {}

    def _selected_fields(self, fields, required=("id",)):
        selected = FIELDS[self.profile] if fields is None else fields
        if not isinstance(selected, str) or not selected.strip():
            raise ValueError("fields must be a nonempty string or None")
        if not set(required).issubset(selected.split(",")):
            raise ValueError("selected fields must retain exact identity")
        return selected

    def _observe(self, headers, *, request_windows=None):
        try:
            remaining = int(headers["x-ratelimit-remaining"])
            limit = int(headers["x-ratelimit-limit"])
        except (KeyError, ValueError):
            return
        if remaining >= 0 and limit >= remaining:
            # For larger paid allowances, maintain this client's separate spending cap.
            observed = max(0, min(limit, self.daily_credit_limit) - remaining)
            self.store.observe_used(self.scope, "credits", observed, "day",
                                    window_key=(request_windows or {}).get("day"))

    def quota(self) -> dict:
        """Read authoritative key allowance without persisting the echoed API key."""
        response = self._request("GET", "/rate-limit", headers=self._headers(),
                                refresh=True, sensitive=True, observe=self._observe, service="quota")
        raw = response.json().get("rate_limit")
        if not isinstance(raw, dict):
            raise InvalidResponse("openalex: invalid quota response")
        result = {}
        for key in QUOTA_FIELDS:
            value = raw.get(key)
            if key == "resets_at" and isinstance(value, str):
                result[key] = value
            elif type(value) in (int, float) and math.isfinite(value) and value >= 0:
                result[key] = value
        for key in ("credit_costs", "endpoint_costs_usd"):
            costs = raw.get(key)
            if isinstance(costs, dict):
                result[key] = {name: value for name, value in costs.items()
                               if name in OPERATIONS and type(value) in (int, float) and math.isfinite(value) and value >= 0}
        remaining = result.get("credits_remaining")
        limit = result.get("credits_limit")
        if type(remaining) is not int or type(limit) is not int:
            raise InvalidResponse("openalex: quota lacks integer credit allowance")
        self._observe({"x-ratelimit-remaining": str(remaining), "x-ratelimit-limit": str(limit)},
                      request_windows={"day": response.provenance.retrieved_at_utc[:10]})
        return result

    def _ensure_quota(self):
        with self._quota_lock:
            today = window("day", self.store.clock())
            if self._quota_date != today:
                quota = self.quota()
                costs = quota.get("credit_costs", {})
                if any(name in costs and costs[name] != cost
                       for name, cost in (("search", 10), ("list", 1), ("singleton", 0))):
                    raise InvalidResponse("openalex: provider pricing changed; update the adapter")
                self._quota_date = today

    def _paid(self, params, *, refresh=False):
        if not self.api_key:
            raise ValueError("OpenAlex search requires OPENALEX_API_KEY; exact lookups work without it")
        cost = 10 if "search" in params else 1
        return self._request("GET", "/works", params=params, headers=self._headers(),
                            estimated_credits=cost, credit_limit=self.daily_credit_limit,
                            observe=self._observe, refresh=refresh, before_request=self._ensure_quota,
                            service="search" if "search" in params else "list")

    def search(self, query: str | None = None, *, filter: str | None = None,
               fields: str | None = None, corpus: str = "default", cursor: str = "*",
               page_size: int = 100, records_seen: int = 0, refresh: bool = False) -> Iterator[Page]:
        """Stream every page. Boolean syntax is forwarded verbatim, never rewritten."""
        positive_int(page_size, 100, "page_size")
        if query is not None and (not isinstance(query, str) or not query.strip() or len(query) > 1500):
            raise ValueError("query must contain 1-1500 characters")
        if filter is not None and (not isinstance(filter, str) or not filter.strip()):
            raise ValueError("filter must be a nonempty string")
        if query is None and filter is None:
            raise ValueError("provide query or filter; use the snapshot for entire-corpus ingestion")
        if corpus not in ("default", "all"):
            raise ValueError("corpus must be default or all")
        params = {"per_page": str(page_size), "select": self._selected_fields(fields), "cursor": cursor}
        if query is not None:
            params["search"] = query
        if filter is not None:
            params["filter"] = filter
        if corpus == "all":
            params["corpus"] = "all"
        if type(records_seen) is not int or records_seen < 0 or (cursor != "*" and not records_seen):
            raise ValueError("cursor resume requires a positive records_seen checkpoint")
        seen, observed = set(), records_seen
        while True:
            current = params["cursor"]
            if current in seen:
                raise InvalidResponse("openalex: repeated cursor")
            seen.add(current)
            request = httpx.Request("GET", self.base_url + "/works", params=params)
            if len(str(request.url).encode()) > 4096:
                raise ValueError("OpenAlex encoded request exceeds 4 KB; split the explicit query")
            response = self._paid(params, refresh=refresh)
            body = response.json()
            rows, meta = body.get("results"), body.get("meta")
            if not isinstance(rows, list) or not isinstance(meta, dict):
                raise InvalidResponse("openalex: missing results or metadata")
            if any(not isinstance(row, dict) or not row.get("id") for row in rows):
                raise InvalidResponse("openalex: result lacks a stable identifier")
            total = meta.get("count")
            if type(total) is not int or total < 0:
                raise InvalidResponse("openalex: invalid result count")
            observed += len(rows)
            following = meta.get("next_cursor")
            if following is not None and not isinstance(following, str):
                raise InvalidResponse("openalex: invalid next cursor")
            complete = observed >= total
            if not complete and (not rows or not following):
                raise InvalidResponse("openalex: pagination ended before reported count")
            if not complete and following in seen:
                raise InvalidResponse("openalex: repeated cursor")
            yield self.page(response, rows, total=total, next_cursor=None if complete else following,
                            complete=complete, records_seen=observed)
            if complete:
                return
            params["cursor"] = following

    @staticmethod
    def _identifier(identifier: str) -> str:
        if not isinstance(identifier, str):
            raise ValueError("identifier must be an OpenAlex work ID or DOI")
        value = identifier.removeprefix("https://openalex.org/").removeprefix("https://doi.org/").removeprefix("doi:")
        if re.fullmatch(r"W[1-9][0-9]*", value):
            return value
        if re.fullmatch(r"10\.[0-9]{4,9}/[^\s?#]+", value) and not any(part in (".", "..") for part in value.split("/")):
            return "doi:" + value
        raise ValueError("identifier must be an OpenAlex work ID or DOI")

    def fetch(self, identifier: str, *, fields: str | None = None, refresh: bool = False) -> Page:
        """Exact ID/DOI lookup costs zero credits; rate and cache limits still apply."""
        ident = self._identifier(identifier)
        required = {"id", "doi"} if ident.startswith("doi:") else {"id"}
        selected = self._selected_fields(fields, required)
        response = self._request("GET", "/works/" + quote(ident, safe=":/"),
                                params={"select": selected},
                                headers=self._headers(), refresh=refresh, observe=self._observe, service="singleton")
        # Cache hits cost nothing now, but retain the original provider billing header.
        # Refuse a response billed on acquisition on every read, including cached reads.
        try:
            acquired_credits = int(response.headers.get("x-ratelimit-credits-used", response.credits_used))
            if acquired_credits < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise InvalidResponse("openalex: invalid exact lookup billing header") from None
        if response.credits_used or acquired_credits:
            raise InvalidResponse("openalex: exact lookup unexpectedly charged credits")
        row = response.json()
        if not isinstance(row.get("id"), str) or not re.fullmatch(r"https://openalex.org/W[1-9][0-9]*", row["id"]):
            raise InvalidResponse("openalex: invalid work identifier")
        if ident.startswith("W") and row["id"].rsplit("/", 1)[-1] != ident:
            raise InvalidResponse("openalex: exact lookup returned a different work")
        if ident.startswith("doi:") and (row.get("doi") or "").lower() != ("https://doi.org/" + ident[4:]).lower():
            raise InvalidResponse("openalex: exact lookup returned a different DOI")
        return self.page(response, [row], total=1, complete=True)

    def fetch_many(self, identifiers: Iterable[str], *, workers: int = 16,
                   fields: str | None = None, refresh: bool = False) -> Iterator[Page]:
        """Bounded parallel zero-credit lookup, preserving caller order and O(workers) memory."""
        positive_int(workers, 100, "workers")
        self._selected_fields(fields)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for batch in chunks(identifiers, workers):
                by_id = {}
                for ident in batch:
                    canonical_id = self._identifier(ident)
                    if canonical_id not in by_id:
                        by_id[canonical_id] = pool.submit(self.fetch, ident, fields=fields, refresh=refresh)
                futures = [by_id[self._identifier(ident)] for ident in batch]
                for future in futures:
                    yield future.result()

    def fetch_batch(self, identifiers: Iterable[str], *, fields: str | None = None,
                    refresh: bool = False) -> Iterator[Page]:
        """Up to 100 work IDs per one-credit call when HTTP request count matters most."""
        self._selected_fields(fields)
        for batch in chunks(identifiers, 100):
            ids = [self._identifier(value) for value in batch]
            if any(not value.startswith("W") for value in ids):
                raise ValueError("fetch_batch accepts OpenAlex work IDs; use fetch_many for DOIs")
            wanted, found = set(ids), set()
            for page in self.search(filter="openalex_id:" + "|".join(ids),
                                    fields=fields, refresh=refresh):
                for row in page.records:
                    ident = row["id"].rsplit("/", 1)[-1]
                    if ident not in wanted:
                        raise InvalidResponse("openalex: batch returned an unrequested identifier")
                    found.add(ident)
                if page.complete:
                    page = replace(page, unresolved=tuple(ident for ident in ids if ident not in found))
                yield page
