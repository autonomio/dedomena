"""ECB SDMX: batched dimensions, full attributes, revision deltas, and EUR FX."""
from __future__ import annotations

import csv
from datetime import date, datetime
import io
import re
from typing import Iterable, Iterator

from defusedxml import ElementTree

from .core import InvalidResponse, Page, Transport, positive_int


class ECB(Transport):
    """Preserve source CSV strings, units, multipliers, status, and series keys."""

    def __init__(self, **kwargs):
        # No published request quota: a local policy, not provider entitlement.
        kwargs.setdefault("requests_per_second", 2)
        super().__init__("ecb", "https://data-api.ecb.europa.eu/service",
                         license="ECB reuse policy; third-party data may require permission", **kwargs)

    @staticmethod
    def _flow(flow: str) -> str:
        if not isinstance(flow, str) or not re.fullmatch(
                r"[A-Za-z][A-Za-z0-9_-]*(?:,[A-Za-z0-9_-]+(?:,[A-Za-z0-9_.-]+)?)?", flow):
            raise ValueError("flow must be an SDMX dataflow ID or agency,ID,version")
        return flow

    @staticmethod
    def _key(key: str) -> str:
        if not isinstance(key, str) or key in (".", "..") or not re.fullmatch(r"[A-Za-z0-9_.+\-]*", key):
            raise ValueError("key must contain SDMX dimension codes, dots, and plus signs")
        return key

    @staticmethod
    def _period(value: str | None) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not re.fullmatch(
                r"[0-9]{4}(?:-S[12]|-Q[1-4]|-W(?:0[1-9]|[1-4][0-9]|5[0-3])|-(?:0[1-9]|1[0-2])(?:-(?:0[1-9]|[12][0-9]|3[01]))?)?", value):
            raise ValueError("period must be an ISO date or SDMX reporting period")
        if len(value) == 10:
            date.fromisoformat(value)
        return value

    def _data(self, flow: str, key: str, *, detail: str = "full",
              start_period: str | None = None, end_period: str | None = None,
              updated_after: str | None = None, include_history: bool = False,
              last_n: int | None = None, refresh: bool = False) -> Page:
        flow, key = self._flow(flow), self._key(key)
        if type(include_history) is not bool:
            raise ValueError("include_history must be a boolean")
        params = {"format": "csvdata", "detail": detail,
                  "includeHistory": str(include_history).lower()}
        start, end = self._period(start_period), self._period(end_period)
        if start and end and len(start) == len(end) and start > end:
            raise ValueError("start_period must not follow end_period")
        if start:
            params["startPeriod"] = start
        if end:
            params["endPeriod"] = end
        if updated_after:
            try:
                parsed = datetime.fromisoformat(updated_after.replace("Z", "+00:00"))
                if parsed.utcoffset() is None:
                    raise ValueError
            except (ValueError, TypeError, AttributeError):
                raise ValueError("updated_after must be an ISO timestamp with timezone") from None
            params["updatedAfter"] = updated_after
        if last_n is not None:
            params["lastNObservations"] = str(positive_int(last_n, 1_000_000, "last_n"))
        response = self._request("GET", f"/data/{flow}" + (f"/{key}" if key else ""),
                                 params=params, headers={"Accept": "text/csv"}, refresh=refresh)
        if not response.body.strip():
            return self.page(response, [], total=0, complete=True, records_seen=0,
                             warnings=("ECB returned no observations for the selected query.",))
        try:
            reader = csv.DictReader(io.StringIO(response.body.decode("utf-8-sig")), strict=True)
            fields = reader.fieldnames
            if not fields or len(fields) != len(set(fields)) or "KEY" not in fields:
                raise ValueError
            if detail == "full" and not {"TIME_PERIOD", "OBS_VALUE"}.issubset(fields):
                raise ValueError
            rows = list(reader)
            dataset = flow.split(",")[1] if "," in flow else flow
            dimensions = key.split(".") if key else []
            def matches(series_key):
                actual = series_key.removeprefix(dataset + ".").split(".")
                return len(actual) >= len(dimensions) and all(
                    not wanted or value in wanted.split("+")
                    for wanted, value in zip(dimensions, actual))
            if any(None in row or any(value is None for value in row.values())
                   or not row["KEY"].startswith(dataset + ".") or not matches(row["KEY"])
                   or (detail == "full" and not row["TIME_PERIOD"])
                   for row in rows):
                raise ValueError
        except (ValueError, UnicodeDecodeError, csv.Error):
            raise InvalidResponse("ecb: invalid CSV or mismatched dataflow") from None
        warnings = ["Values retain native units, UNIT_MULT, and observation status; no numeric coercion."]
        if last_n is not None:
            warnings.append("last_n selects the latest observations per series; earlier history is excluded.")
        if updated_after:
            warnings.append("Revision delta: additions, corrections, and deletions must be reconciled with prior data.")
        if include_history:
            warnings.append("Historical versions are included; do not collapse rows by series and period.")
        return self.page(response, rows, total=len(rows), complete=True,
                         records_seen=len(rows), warnings=warnings)

    def observations(self, flow: str, key: str = "", **kwargs) -> Page:
        """One complete selected query; narrow dates if the response size cap is hit."""
        return self._data(flow, key, **kwargs)

    def series(self, flow: str, key: str = "", *, refresh: bool = False) -> Page:
        """Discover matching keys and all series attributes without observations."""
        return self._data(flow, key, detail="nodata", refresh=refresh)

    @staticmethod
    def _query(query: str) -> tuple[str, str]:
        if not isinstance(query, str) or query.count("/") > 1:
            raise ValueError("query must be FLOW/SDMX.KEY or FLOW")
        flow, separator, key = query.partition("/")
        return flow, key if separator else ""

    def fetch(self, identifier: str, *, refresh: bool = False) -> Page:
        return self.observations(*self._query(identifier), refresh=refresh)

    def search(self, query: str, *, refresh: bool = False) -> Iterator[Page]:
        yield self.fetch(query, refresh=refresh)

    def fx(self, currencies: Iterable[str] | str | None = None, *, frequency: str = "D",
           start_period: str | None = None, end_period: str | None = None,
           last_n: int | None = None, refresh: bool = False) -> Page:
        """Batch reference rates: OBS_VALUE is currency units per one EUR."""
        if frequency not in ("D", "M", "Q", "A"):
            raise ValueError("frequency must be D, M, Q, or A")
        values = [currencies] if isinstance(currencies, str) else list(currencies or ())
        if currencies is not None and (not values or any(
                not isinstance(value, str) or not re.fullmatch(r"[A-Z]{3}", value) for value in values)):
            raise ValueError("currencies must be uppercase ISO currency codes")
        codes = "+".join(dict.fromkeys(values))
        if len(codes) > 1500:
            raise ValueError("currency batch exceeds the local URL length limit")
        return self.observations("EXR", f"{frequency}.{codes}.EUR.SP00.A",
                                 start_period=start_period, end_period=end_period,
                                 last_n=last_n, refresh=refresh)

    def dataflows(self, *, refresh: bool = False) -> Page:
        """Discover dataset IDs, versions, and names; original XML is archived."""
        response = self._request("GET", "/dataflow/ECB/all/latest", params={"detail": "allstubs"},
                                 headers={"Accept": "application/vnd.sdmx.structure+xml;version=2.1"},
                                 refresh=refresh)
        try:
            root = ElementTree.fromstring(response.body)
            rows = []
            for node in root.iter():
                if node.tag.rsplit("}", 1)[-1] == "Dataflow":
                    if not node.get("id") or node.get("agencyID") != "ECB":
                        raise ValueError
                    rows.append({**node.attrib, "names": [
                        {**child.attrib, "text": child.text or ""}
                        for child in node if child.tag.rsplit("}", 1)[-1] == "Name"]})
            if not rows:
                raise ValueError
        except Exception:
            raise InvalidResponse("ecb: invalid dataflow metadata XML") from None
        return self.page(response, rows, total=len(rows), complete=True, records_seen=len(rows))

    def quota(self) -> dict:
        return {"api_key_required": False, "daily_provider_quota": None,
                "local_requests_per_second": self.rate, "max_response_bytes": self.max_response_bytes,
                "note": "No published daily allowance. Batch dimensions and use updated_after for deltas; narrow large queries by dates."}
