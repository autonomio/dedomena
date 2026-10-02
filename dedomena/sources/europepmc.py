"""Europe PMC (including PubMed): 1,000-record cursor pages, abstracts and OA XML."""
from __future__ import annotations

import re
import httpx
from dataclasses import replace
from typing import Iterable, Iterator

from defusedxml import ElementTree

from .core import InvalidResponse, Page, Response, Transport, chunks, positive_int


class EuropePMC(Transport):
    MAX_PAGE_SIZE = 1000

    def __init__(self, *, result_type: str = "lite", **kwargs):
        if result_type not in ("lite", "core"):
            raise ValueError("result_type must be lite or core")
        self.result_type = result_type
        # Local pacing policy, not a claimed provider quota. Tune to observed 429s.
        kwargs.setdefault("requests_per_second", 10)
        super().__init__("europepmc", "https://www.ebi.ac.uk/europepmc/webservices/rest",
                         license="Per-record rights; PubMed abstracts may be copyrighted", **kwargs)

    def search(self, query: str, *, result_type: str | None = None, page_size: int = 1000,
               cursor: str = "*", records_seen: int = 0, refresh: bool = False) -> Iterator[Page]:
        positive_int(page_size, 1000, "page_size")
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a nonempty Europe PMC query")
        kind = self.result_type if result_type is None else result_type
        if kind not in ("lite", "core"):
            raise ValueError("result_type must be lite or core")
        params = {"query": query, "format": "json", "resultType": kind,
                  "pageSize": str(page_size), "cursorMark": cursor}
        if type(records_seen) is not int or records_seen < 0 or (cursor != "*" and not records_seen):
            raise ValueError("cursor resume requires a positive records_seen checkpoint")
        seen, observed = set(), records_seen
        while True:
            current = params["cursorMark"]
            if current in seen:
                raise InvalidResponse("europepmc: repeated cursor")
            seen.add(current)
            request = httpx.Request("GET", self.base_url + "/search", params=params)
            if len(str(request.url).encode()) > 4096:
                response = self._request("POST", "/searchPOST", form=params, refresh=refresh,
                                        headers={"Content-Type": "application/x-www-form-urlencoded"})
            else:
                response = self._request("GET", "/search", params=params, refresh=refresh)
            body = response.json()
            result_list = body.get("resultList")
            rows = result_list.get("result") if isinstance(result_list, dict) else None
            total = body.get("hitCount")
            if not isinstance(rows, list) or type(total) is not int or total < 0:
                raise InvalidResponse("europepmc: invalid results or hit count")
            if any(not isinstance(row, dict) or not row.get("id") or not row.get("source") for row in rows):
                raise InvalidResponse("europepmc: result lacks source and identifier")
            observed += len(rows)
            following = body.get("nextCursorMark")
            if following is not None and not isinstance(following, str):
                raise InvalidResponse("europepmc: invalid next cursor")
            complete = observed >= total
            if not complete and (not rows or not following or following in seen):
                raise InvalidResponse("europepmc: pagination ended before reported count")
            yield self.page(response, rows, total=total, next_cursor=None if complete else following,
                            complete=complete, records_seen=observed)
            if complete:
                return
            params["cursorMark"] = following

    @staticmethod
    def _id(identifier: str) -> tuple[str, str]:
        if not isinstance(identifier, str):
            raise ValueError("identifier must be a PMID, PMCID or SOURCE:ID")
        if re.fullmatch(r"PMC[0-9]+", identifier):
            return "PMC", identifier
        if re.fullmatch(r"[0-9]+", identifier):
            return "MED", identifier
        match = re.fullmatch(r"([A-Z]{2,8}):([A-Za-z0-9._/-]+)", identifier)
        if match:
            source, value = match[1], match[2]
            if source == "PMC":
                if not re.fullmatch(r"(?:PMC)?[0-9]+", value):
                    raise ValueError("PMC identifier must contain a numeric PMCID")
                value = "PMC" + value.removeprefix("PMC")
            return source, value
        raise ValueError("identifier must be a PMID, PMCID or SOURCE:ID")

    def fetch(self, identifier: str, *, refresh: bool = False) -> Page:
        source, value = self._id(identifier)
        query = f'PMCID:{value}' if source == "PMC" else f'(SRC:{source} AND EXT_ID:"{value}")'
        pages = self.search(query, result_type="core", page_size=1000, refresh=refresh)
        first = next(pages)
        if not first.complete:
            raise InvalidResponse("europepmc: exact identifier returned multiple pages")
        if not first.records:
            from .core import SourceError
            raise SourceError("europepmc: identifier not found")
        for row in first.records:
            if source == "PMC":
                matches = row.get("pmcid") == value
            else:
                matches = row.get("source") == source and row.get("id") == value
            if not matches:
                raise InvalidResponse("europepmc: exact lookup returned a different identifier")
        return first

    def fetch_many(self, identifiers: Iterable[str], *, result_type: str = "core",
                   refresh: bool = False) -> Iterator[Page]:
        """Batch up to 100 exact IDs per query; never silently remove unresolved IDs."""
        if result_type not in ("lite", "core"):
            raise ValueError("result_type must be lite or core")
        for batch in chunks(identifiers, 100):
            clauses = []
            for identifier in batch:
                source, value = self._id(identifier)
                clauses.append(f"PMCID:{value}" if source == "PMC"
                               else f'(SRC:{source} AND EXT_ID:"{value}")')
            found = set()
            wanted = dict(zip(batch, (self._id(value) for value in batch)))
            for page in self.search(" OR ".join(clauses), result_type=result_type, refresh=refresh):
                for row in page.records:
                    matches = [ident for ident, (source, value) in wanted.items()
                               if (row.get("pmcid") == value if source == "PMC"
                                   else row.get("source") == source and row.get("id") == value)]
                    if not matches:
                        raise InvalidResponse("europepmc: batch returned an unrequested identifier")
                    found.update(matches)
                if page.complete:
                    page = replace(page, unresolved=tuple(value for value in batch if value not in found))
                yield page


    def full_text(self, pmcid: str, *, refresh: bool = False) -> Response:
        """Return original open-access JATS XML, preserved as a replayable snapshot."""
        if not isinstance(pmcid, str) or not re.fullmatch(r"PMC[0-9]+", pmcid):
            raise ValueError("full_text requires a PMCID")
        response = self._request("GET", "/" + pmcid + "/fullTextXML",
                                headers={"Accept": "application/xml"}, refresh=refresh)
        try:
            root = ElementTree.fromstring(response.body)
        except Exception:
            raise InvalidResponse("europepmc: invalid article XML") from None
        ids = {node.text for node in root.iter() if node.tag.rsplit("}", 1)[-1] == "article-id"
               and node.get("pub-id-type") in ("pmc", "pmcid")}
        if pmcid not in ids and pmcid.removeprefix("PMC") not in ids:
            raise InvalidResponse("europepmc: full text does not match requested PMCID")
        return response

    def quota(self) -> dict:
        return {"api_key_required": False, "daily_provider_quota": None,
                "local_requests_per_second": self.rate, "max_page_size": 1000,
                "note": "No published daily allowance; local pacing is configurable, Retry-After is respected."}
