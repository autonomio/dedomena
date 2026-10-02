"""SEC EDGAR: complete filing histories and native XBRL units and vintages."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date
import math
import os
import re
from typing import Iterable, Iterator

from .core import InvalidResponse, Page, Response, Transport, chunks, positive_int


class SEC(Transport):
    """Keyless EDGAR JSON with shared 10 requests/s pacing and declared contact."""

    MAX_REQUESTS_PER_SECOND = 10
    BULK_ARCHIVES = {
        "companyfacts": "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip",
        "submissions": "https://www.sec.gov/Archives/edgar/daily-index/bulkdata/submissions.zip",
    }

    def __init__(self, user_agent: str | None = None, **kwargs):
        user_agent = user_agent if user_agent is not None else os.environ.get("SEC_USER_AGENT", "")
        if (not isinstance(user_agent, str) or "\n" in user_agent or "\r" in user_agent
                or not re.search(r"\S+\s+[^\s@]+@[^\s@]+[.][^\s@]+", user_agent)):
            raise ValueError("SEC requires SEC_USER_AGENT with an organization and contact email")
        self.user_agent = user_agent
        if kwargs.pop("credential", ""):
            raise ValueError("SEC keyless clients must share the aggregate pacing scope")
        kwargs.setdefault("requests_per_second", 10)
        if kwargs["requests_per_second"] > 10:
            raise ValueError("SEC permits at most 10 requests per second across all clients")
        # No credential or User-Agent namespaces: every SEC client in this store shares pacing.
        kwargs.setdefault("max_response_bytes", 64_000_000)
        super().__init__("sec", "https://data.sec.gov",
                         credential="",
                         license="Public EDGAR data; SEC terms and underlying filing rights apply", **kwargs)

    @staticmethod
    def _cik(value: str | int) -> str:
        if isinstance(value, str):
            value = value.removeprefix("CIK")
            if not re.fullmatch(r"[0-9]{1,10}", value):
                raise ValueError("CIK must contain 1-10 digits")
            value = int(value)
        if type(value) is not int or not 1 <= value <= 9_999_999_999:
            raise ValueError("CIK must be a positive integer with at most 10 digits")
        return f"{value:010d}"

    @staticmethod
    def _name(value: str, kind: str) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]*", value):
            raise ValueError(f"invalid SEC {kind}")
        return value

    def _get(self, path: str, *, refresh: bool = False) -> Response:
        return self._request("GET", path, headers={"User-Agent": self.user_agent,
                             "Accept": "application/json"}, refresh=refresh)

    @staticmethod
    def _identity(body: dict, cik: str):
        value = body.get("cik")
        valid = type(value) is int or (isinstance(value, str) and re.fullmatch(r"[0-9]{1,10}", value))
        if not valid or int(value) != int(cik):
            raise InvalidResponse("sec: response does not match requested CIK")

    @staticmethod
    def _facts(units: object):
        if not isinstance(units, dict):
            raise InvalidResponse("sec: XBRL concept lacks units")
        for unit, rows in units.items():
            if not isinstance(unit, str) or not unit or not isinstance(rows, list):
                raise InvalidResponse("sec: invalid XBRL units")
            for row in rows:
                SEC._fact(row)

    @staticmethod
    def _fact(row: object):
        if (not isinstance(row, dict)
                or not isinstance(row.get("accn"), str)
                or not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", row["accn"])
                or type(row.get("val")) not in (int, float)
                or not math.isfinite(row["val"])):
            raise InvalidResponse("sec: fact lacks accession number or finite numeric value")
        for key in ("end", "start", "filed"):
            if key == "start" and key not in row:
                continue
            if key == "filed" and key not in row:
                continue  # Frames expose accession and period, but no filing date.
            value = row.get(key)
            try:
                if not isinstance(value, str):
                    raise ValueError
                date.fromisoformat(value)
            except ValueError:
                raise InvalidResponse("sec: invalid XBRL observation date") from None

    def company_facts(self, cik: str | int, *, refresh: bool = False) -> Page:
        """One call retains every taxonomy, concept, unit and filed observation."""
        ident = self._cik(cik)
        response = self._get(f"/api/xbrl/companyfacts/CIK{ident}.json", refresh=refresh)
        body = response.json()
        self._identity(body, ident)
        taxonomies = body.get("facts")
        if not isinstance(taxonomies, dict):
            raise InvalidResponse("sec: company facts lack taxonomy data")
        for taxonomy, concepts in taxonomies.items():
            if not isinstance(taxonomy, str) or not isinstance(concepts, dict):
                raise InvalidResponse("sec: invalid company taxonomy")
            for tag, concept in concepts.items():
                if not isinstance(tag, str) or not isinstance(concept, dict):
                    raise InvalidResponse("sec: invalid company concept")
                self._facts(concept.get("units"))
        return self.page(response, [body], total=1, complete=True, records_seen=1,
                         warnings=("Units and filing vintages remain native; no latest-value selection or currency conversion.",))

    def fetch(self, cik: str | int, *, refresh: bool = False) -> Page:
        return self.company_facts(cik, refresh=refresh)

    def fetch_many(self, ciks: Iterable[str | int], *, workers: int = 4,
                   refresh: bool = False) -> Iterator[Page]:
        """Bounded company reads preserve caller order and shared aggregate pacing."""
        positive_int(workers, 10, "workers")
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for batch in chunks(ciks, workers):
                pending = {}
                for cik in batch:
                    ident = self._cik(cik)
                    if ident not in pending:
                        pending[ident] = pool.submit(self.company_facts, ident, refresh=refresh)
                for cik in batch:
                    yield pending[self._cik(cik)].result()

    def company_concept(self, cik: str | int, taxonomy: str, tag: str,
                        *, refresh: bool = False) -> Page:
        """A lighter exact concept read, retaining distinct units and amended filings."""
        ident = self._cik(cik)
        taxonomy, tag = self._name(taxonomy, "taxonomy"), self._name(tag, "tag")
        response = self._get(f"/api/xbrl/companyconcept/CIK{ident}/{taxonomy}/{tag}.json",
                             refresh=refresh)
        body = response.json()
        self._identity(body, ident)
        if body.get("taxonomy") != taxonomy or body.get("tag") != tag:
            raise InvalidResponse("sec: company concept does not match requested taxonomy and tag")
        self._facts(body.get("units"))
        return self.page(response, [body], total=1, complete=True, records_seen=1)

    def frame(self, taxonomy: str, tag: str, unit: str, period: str,
              *, refresh: bool = False) -> Page:
        """Native cross-company calendar frame; this is SEC's latest matching filing view."""
        taxonomy, tag = self._name(taxonomy, "taxonomy"), self._name(tag, "tag")
        unit = self._name(unit, "unit")
        if not isinstance(period, str) or not re.fullmatch(r"CY[0-9]{4}(?:Q[1-4]I?)?", period):
            raise ValueError("period must be CYyyyy, CYyyyyQq, or CYyyyyQqI")
        response = self._get(f"/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json",
                             refresh=refresh)
        body = response.json()
        if (body.get("taxonomy"), body.get("tag"), body.get("uom"), body.get("ccp")) != (taxonomy, tag, unit.replace("-per-", "/"), period):
            raise InvalidResponse("sec: frame does not match requested concept, unit and period")
        rows, count = body.get("data"), body.get("pts")
        if not isinstance(rows, list) or type(count) is not int or count != len(rows):
            raise InvalidResponse("sec: frame lacks its reported observations")
        seen = set()
        for row in rows:
            self._fact(row)
            cik = row.get("cik")
            if type(cik) is not int or not 1 <= cik <= 9_999_999_999 or cik in seen:
                raise InvalidResponse("sec: frame contains an invalid or repeated CIK")
            seen.add(cik)
        return self.page(response, [body], total=1, complete=True, records_seen=1,
                         warnings=("Frame selects the latest filing matching a calendar period; company fiscal dates can differ.",))

    @staticmethod
    def _filing_rows(columns: object) -> list[dict]:
        if not isinstance(columns, dict) or not isinstance(columns.get("accessionNumber"), list):
            raise InvalidResponse("sec: filings lack accession numbers")
        count = len(columns["accessionNumber"])
        if any(not isinstance(value, list) or len(value) != count for value in columns.values()):
            raise InvalidResponse("sec: filing columns have unequal lengths")
        rows = [{name: values[index] for name, values in columns.items()} for index in range(count)]
        for row in rows:
            accession = row["accessionNumber"]
            if not isinstance(accession, str) or not re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}", accession):
                raise InvalidResponse("sec: invalid filing accession number")
        if len({row["accessionNumber"] for row in rows}) != len(rows):
            raise InvalidResponse("sec: repeated filing accession number")
        return rows

    def submissions(self, cik: str | int, *, include_history: bool = True,
                    cursor: str = "recent", records_seen: int = 0,
                    refresh: bool = False) -> Iterator[Page]:
        """Enumerate recent AND additional filing files; mark only the final page complete.

        Resume with a returned filename cursor and records_seen checkpoint. SEC's live
        manifest may change; a changed checkpoint fails instead of claiming completeness.
        """
        ident = self._cik(cik)
        if type(include_history) is not bool:
            raise ValueError("include_history must be boolean")
        if type(records_seen) is not int or records_seen < 0 or not isinstance(cursor, str):
            raise ValueError("invalid SEC filing checkpoint")
        response = self._get(f"/submissions/CIK{ident}.json", refresh=refresh)
        body = response.json()
        self._identity(body, ident)
        filings = body.get("filings")
        if not isinstance(filings, dict):
            raise InvalidResponse("sec: submissions lack filings")
        recent = self._filing_rows(filings.get("recent"))
        files = filings.get("files")
        if not isinstance(files, list):
            raise InvalidResponse("sec: submissions lack historical file manifest")
        names, counts = [], []
        for descriptor in files:
            if (not isinstance(descriptor, dict)
                    or not isinstance(descriptor.get("name"), str)
                    or not re.fullmatch(f"CIK{ident}-submissions-[0-9]+[.]json", descriptor["name"])
                    or type(descriptor.get("filingCount")) is not int
                    or descriptor["filingCount"] < 0):
                raise InvalidResponse("sec: invalid historical filing file descriptor")
            names.append(descriptor["name"])
            counts.append(descriptor["filingCount"])
        if len(set(names)) != len(names):
            raise InvalidResponse("sec: repeated historical filing file")
        total = len(recent) + sum(counts)
        metadata_warning = "Filing rows retain native columns; the full company metadata and columnar envelope are archived in the raw snapshot."
        if cursor == "recent":
            if records_seen:
                raise ValueError("initial SEC filings request cannot have a records_seen checkpoint")
            observed, begin = len(recent), 0
            next_cursor = names[0] if names else None
            warnings = (metadata_warning,)
            if names and not include_history:
                warnings += ("Historical filing files were not requested; enumeration is incomplete.",)
            yield self.page(response, recent, total=total, next_cursor=next_cursor,
                            complete=not names, records_seen=observed, warnings=warnings)
        else:
            if cursor not in names:
                raise ValueError("SEC cursor is absent from the current historical manifest")
            begin = names.index(cursor)
            expected = len(recent) + sum(counts[:begin])
            if records_seen != expected:
                raise ValueError("SEC checkpoint differs from the current historical manifest")
            observed = records_seen
        if not include_history:
            return
        all_seen = {row["accessionNumber"] for row in recent} if cursor == "recent" else set()
        for index in range(begin, len(names)):
            historical = self._get("/submissions/" + names[index], refresh=refresh)
            rows = self._filing_rows(historical.json())
            if len(rows) != counts[index]:
                raise InvalidResponse("sec: historical filing count differs from its manifest")
            if any(row["accessionNumber"] in all_seen for row in rows):
                raise InvalidResponse("sec: filing accession repeated across history pages")
            all_seen.update(row["accessionNumber"] for row in rows)
            observed += len(rows)
            complete = index + 1 == len(names)
            yield self.page(historical, rows, total=total,
                            next_cursor=None if complete else names[index + 1],
                            complete=complete, records_seen=observed,
                            warnings=("Historical filing rows retain native columns; company identity is the CIK in the source URL.",))

    def search(self, cik: str | int, *, refresh: bool = False) -> Iterator[Page]:
        """The consistent discovery operation is complete filing history for an exact CIK."""
        return self.submissions(cik, refresh=refresh)

    def quota(self) -> dict:
        return {"api_key_required": False, "identifying_user_agent_required": True,
                "provider_max_requests_per_second": 10,
                "local_requests_per_second": self.rate,
                "daily_provider_quota": None,
                "bulk_archives": dict(self.BULK_ARCHIVES),
                "note": "SEC's 10 requests/s limit aggregates all clients; one store shares pacing across User-Agents. Bulk ZIPs are nightly alternatives for corpus-scale ingestion."}
