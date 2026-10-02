"""EPO Open Patent Services: dynamic throttling, bulk XML and complete date partitions."""
from __future__ import annotations

from datetime import date, timedelta
from dataclasses import replace
import os
import re
import threading
from typing import Iterable, Iterator
from urllib.parse import quote

from defusedxml import ElementTree

from .core import (HTTPFailure, InvalidResponse, Page, SearchLimitExceeded, Transport,
                   chunks, positive_int)


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def nodes(root, name):
    return [node for node in root.iter() if local(node.tag) == name]


def xml_value(node):
    """Preserve repeated children, attributes and original text in a small JSON tree."""
    out = {"attributes": dict(node.attrib), "text": node.text or ""}
    if node.tail is not None:
        out["tail"] = node.tail
    for child in node:
        out.setdefault(local(child.tag), []).append(xml_value(child))
    return out


def records(root) -> list[dict]:
    out = []
    for node in nodes(root, "exchange-document"):
        country, number, kind = (node.get(key) for key in ("country", "doc-number", "kind"))
        if not country or not number or not kind:
            raise InvalidResponse("epo: patent lacks a stable DOCDB identifier")
        out.append({"id": ".".join((country, number, kind)),
                    "country": country, "doc_number": number, "kind": kind,
                    "family_id": node.get("family-id"),
                    "epodoc_ids": [child.text for ident in nodes(node, "document-id")
                                   if ident.get("document-id-type") == "epodoc"
                                   for child in ident if local(child.tag) == "doc-number"] or [country + number],
                    "data": xml_value(node)})
    if out:
        return out
    # Slim search returns publication-reference records instead of full biblio.
    for reference in nodes(root, "publication-reference"):
        identifiers = [node for node in nodes(reference, "document-id")
                       if node.get("document-id-type") == "docdb"]
        if not identifiers:
            raise InvalidResponse("epo: search reference lacks a DOCDB identifier")
        ident = identifiers[0]
        values = {local(child.tag): child.text for child in ident}
        if not all(values.get(key) for key in ("country", "doc-number", "kind")):
            raise InvalidResponse("epo: incomplete DOCDB identifier")
        out.append({"id": ".".join(values[key] for key in ("country", "doc-number", "kind")),
                    "data": xml_value(reference)})
    return out


class EPO(Transport):
    MAX_PAGE_SIZE = 100
    SEARCH_CEILING = 2000
    FREE_WEEKLY_BYTES = 4_000_000_000

    def __init__(self, consumer_key: str | None = None, consumer_secret: str | None = None,
                 *, weekly_byte_limit: int = FREE_WEEKLY_BYTES, **kwargs):
        self.consumer_key = consumer_key if consumer_key is not None else os.environ.get("EPO_OPS_KEY", "")
        self.consumer_secret = consumer_secret if consumer_secret is not None else os.environ.get("EPO_OPS_SECRET", "")
        if type(weekly_byte_limit) is not int or weekly_byte_limit < 1:
            raise ValueError("weekly_byte_limit must be a positive integer")
        self.weekly_byte_limit = weekly_byte_limit
        self._token, self._expires = "", 0.0
        self._token_lock = threading.Lock()
        self._last_headers = {}
        kwargs.setdefault("requests_per_second", 10)
        super().__init__("epo", "https://ops.epo.org",
                         credential=self.consumer_key + ":" + self.consumer_secret,
                         license="EPO OPS terms; raw data redistribution is restricted", **kwargs)

    def _auth(self, force=False) -> str:
        if not self.consumer_key or not self.consumer_secret:
            raise ValueError("EPO_OPS_KEY and EPO_OPS_SECRET are required")
        with self._token_lock:
            if force or self.store.clock() >= self._expires:
                response = self._request("POST", "/3.2/auth/accesstoken",
                                        form={"grant_type": "client_credentials"},
                                        auth=(self.consumer_key, self.consumer_secret),
                                        sensitive=True, refresh=True, service="auth")
                body = response.json()
                token, expires = body.get("access_token"), body.get("expires_in")
                try:
                    ttl = float(expires)
                except (TypeError, ValueError):
                    raise InvalidResponse("epo: invalid token lifetime") from None
                if not isinstance(token, str) or not token or not 30 < ttl < 86400:
                    raise InvalidResponse("epo: invalid authentication response")
                self._token, self._expires = token, self.store.clock() + ttl - 30
            return self._token

    def _observe(self, headers):
        self._last_headers = dict(headers)
        value = headers.get("x-registeredquotaperweek-used")
        if value is not None:
            try:
                used = int(value)
                if used < 0:
                    raise ValueError
            except ValueError:
                raise InvalidResponse("epo: invalid weekly quota header") from None
            self.store.observe_used(self.scope, "bytes", used, "week")
        # EPO requires the most restrictive instance signal during a 60-second window.
        for service, color, limit in re.findall(r"([a-z]+)=(green|yellow|red|black):([0-9]+)",
                                                headers.get("x-throttling-control", "")):
            scope = self.scope + ":" + service
            if color == "black" or int(limit) == 0:
                self.store.defer(scope, max(60, self._retry_after(headers.get("retry-after"))))
            else:
                self.store.throttle(scope, int(limit) / 60)
        try:
            hourly = int(headers.get("x-individualquotaperhour-used", "0"))
        except ValueError:
            raise InvalidResponse("epo: invalid hourly quota header") from None
        if hourly >= 450_000_000:
            self.store.defer(self.scope + ":all", 3600)

    def _call(self, path: str, *, params=None, content=None, service="retrieval",
              refresh=False, accept="application/exchange+xml"):
        options = dict(params=params, content=content,
                       headers={"Accept": accept,
                                **({"Content-Type": "text/plain"} if content is not None else {})},
                       service=service, service_rate=(30 if service == "search" else 200) / 60,
                       byte_limit=self.weekly_byte_limit, refresh=refresh, observe=self._observe,
                       headers_factory=lambda: {"Authorization": "Bearer " + self._auth()})
        try:
            response = self._request("POST" if content is not None else "GET",
                                    "/3.2/rest-services" + path, **options)
        except HTTPFailure as exc:
            if exc.status_code != 401:
                raise
            self._auth(force=True)
            response = self._request("POST" if content is not None else "GET",
                                    "/3.2/rest-services" + path, **options)
        if not response.cache_hit:
            # Approximately 1 Mbit/s for a steady profile, in addition to service RPM.
            self.store.defer(self.scope + ":all", response.wire_bytes * 8 / 1_000_000)
        return response

    @staticmethod
    def _root(response):
        try:
            root = ElementTree.fromstring(response.body)
        except Exception:
            raise InvalidResponse("epo: invalid patent XML") from None
        if local(root.tag) != "world-patent-data":
            raise InvalidResponse("epo: missing patent response envelope")
        return root

    def search(self, query: str, *, page_size: int = 100, start: int = 1,
               biblio: bool = False, allow_partial: bool = False,
               refresh: bool = False) -> Iterator[Page]:
        """Native CQL, maximum-sized pages; refuse undisclosed 2,000-hit truncation."""
        positive_int(page_size, 100, "page_size")
        positive_int(start, 2000, "start")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a nonempty EPO CQL query")
        path = "/published-data/search" + ("/biblio" if biblio else "")
        while True:
            end = min(start + page_size - 1, self.SEARCH_CEILING)
            response = self._call(path, params={"q": query, "Range": f"{start}-{end}"},
                                  service="search", refresh=refresh)
            root = self._root(response)
            searches = nodes(root, "biblio-search")
            if len(searches) != 1:
                raise InvalidResponse("epo: missing search metadata")
            try:
                total = int(searches[0].get("total-result-count"))
                if total < 0:
                    raise ValueError
            except (ValueError, TypeError):
                raise InvalidResponse("epo: invalid result count") from None
            if total > self.SEARCH_CEILING and not allow_partial:
                raise SearchLimitExceeded(f"epo: {total} hits exceed the 2000-result ceiling; use search_partitioned")
            rows = records(root)
            ranges = nodes(searches[0], "range")
            if len(ranges) != 1:
                raise InvalidResponse("epo: missing result range")
            try:
                begin, actual_end = (int(ranges[0].get(key)) for key in ("begin", "end"))
            except (ValueError, TypeError):
                raise InvalidResponse("epo: invalid result range") from None
            if begin != start or (actual_end < start and total != 0) or actual_end > end:
                raise InvalidResponse("epo: response range differs from request")
            capped = total > self.SEARCH_CEILING
            finished = actual_end >= min(total, self.SEARCH_CEILING) or total == 0
            if not rows and total > 0:
                raise InvalidResponse("epo: result range contains no patent records")
            expected = max(0, min(actual_end, total) - begin + 1)
            if len(rows) < expected and not allow_partial:
                raise InvalidResponse("epo: source returned fewer patent records than the result range")
            following = None if finished else str(actual_end + 1)
            warnings = (f"Only the first 2000 of {total} hits are accessible for this query.",) if capped else ()
            if len(rows) < expected:
                warnings += (f"Source returned {len(rows)} records for a {expected}-hit range.",)
            yield self.page(response, rows, total=total, next_cursor=following,
                            complete=finished and not capped and len(rows) >= expected, warnings=warnings, records_seen=min(actual_end, total))
            if finished:
                return
            start = actual_end + 1

    def search_partitioned(self, query: str, start_date: date, end_date: date, **kwargs) -> Iterator[Page]:
        """Bisect explicit publication-date ranges until every partition is enumerable.

        Bounds are inclusive, disjoint and recorded in each native CQL query.
        A single day exceeding 2,000 hits raises; no evidence branch is discarded.
        """
        if "start" in kwargs:
            raise ValueError("partitioned retrieval must start each partition at its first result")
        if not isinstance(start_date, date) or not isinstance(end_date, date) or start_date > end_date:
            raise ValueError("provide an ordered pair of publication dates")
        if kwargs.get("allow_partial"):
            raise ValueError("partitioned retrieval requires complete partitions")
        def partitions(begin, end):
            dated = f'({query}) AND pd within "{begin:%Y%m%d} {end:%Y%m%d}"'
            try:
                for page in self.search(dated, **kwargs):
                    yield page, end
            except SearchLimitExceeded:
                if begin == end:
                    raise SearchLimitExceeded("epo: one publication day exceeds 2000 hits; refine the CQL query") from None
                midpoint = begin + timedelta(days=(end - begin).days // 2)
                yield from partitions(begin, midpoint)
                yield from partitions(midpoint + timedelta(days=1), end)
        for page, partition_end in partitions(start_date, end_date):
            if page.complete and partition_end < end_date:
                page = replace(page, complete=False, next_cursor="date:" +
                               (partition_end + timedelta(days=1)).isoformat())
            yield page

    @staticmethod
    def _id(identifier: str, format: str):
        pattern = r"[A-Z]{2}\.[A-Za-z0-9]+\.[A-Z][0-9]?" if format == "docdb" else r"[A-Z]{2}[A-Za-z0-9]+(?:\.[A-Z][0-9]?)?"
        if format not in ("docdb", "epodoc") or not isinstance(identifier, str) or not re.fullmatch(pattern, identifier):
            raise ValueError("identifier must match the requested docdb or epodoc format")
        return identifier

    @staticmethod
    def _matches(row, ident, format):
        if format == "docdb":
            return row["id"] == ident
        base, _, kind = ident.partition(".")
        return base in row.get("epodoc_ids", ()) and (not kind or row.get("kind") == kind)

    def fetch(self, identifier: str, *, format: str = "docdb", refresh: bool = False) -> Page:
        ident = self._id(identifier, format)
        response = self._call("/published-data/publication/" + format + "/" + quote(ident) + "/biblio",
                              refresh=refresh)
        rows = records(self._root(response))
        if not rows:
            raise InvalidResponse("epo: bibliographic lookup returned no patents")
        if any(not self._matches(row, ident, format) for row in rows):
            raise InvalidResponse("epo: exact lookup returned a different patent")
        return self.page(response, rows, total=len(rows), complete=True)

    def fetch_many(self, identifiers: Iterable[str], *, format: str = "docdb",
                   refresh: bool = False) -> Iterator[Page]:
        for batch in chunks(identifiers, 100):
            ids = [self._id(value, format) for value in batch]
            response = self._call("/published-data/publication/" + format + "/biblio",
                                  content="\n".join(ids).encode(), refresh=refresh)
            rows = records(self._root(response))
            if any(not any(self._matches(row, ident, format) for ident in ids) for row in rows):
                raise InvalidResponse("epo: bulk lookup returned an unrequested patent")
            unresolved = tuple(ident for ident in ids
                               if not any(self._matches(row, ident, format) for row in rows))
            yield self.page(response, rows, total=len(rows), complete=True, unresolved=unresolved)

    def full_text(self, identifier: str, *, format: str = "docdb",
                  section: str = "claims", refresh: bool = False):
        """Fetch original claims or description XML on demand, without paying for images."""
        ident = self._id(identifier, format)
        if section not in ("claims", "description"):
            raise ValueError("section must be claims or description")
        response = self._call("/published-data/publication/" + format + "/" +
                              quote(ident) + "/" + section, refresh=refresh,
                              accept="application/fulltext+xml")
        root = self._root(response)
        documents = nodes(root, "fulltext-document")
        if not documents or not any(nodes(node, section) for node in documents):
            raise InvalidResponse("epo: requested full-text section is missing")
        # DOCDB fulltext envelopes use the same three identity attributes.
        if format == "docdb" and any(
                ".".join(node.get(key, "") for key in ("country", "doc-number", "kind")) != ident
                for node in documents):
            raise InvalidResponse("epo: full text returned a different patent")
        return response

    def quota(self) -> dict:
        return {"weekly_byte_limit": self.weekly_byte_limit,
                "weekly_bytes_accounted": self.store.used(self.scope, "bytes", "week"),
                "weekly_bytes_remaining": max(0, self.weekly_byte_limit -
                                             self.store.used(self.scope, "bytes", "week")),
                "max_page_size": 100, "search_result_ceiling": 2000,
                "latest_provider_headers": dict(self._last_headers),
                "note": "Calendar week resets Monday 00:00 UTC; headers reflect account usage."}
