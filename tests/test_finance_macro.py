"""Offline macro contracts: dated knowledge, decimals, identities and bulk limits."""
from datetime import datetime, timezone
import json

import httpx
import pytest

from dedomena.sources.core import InvalidResponse, Store
from dedomena.sources.fred import FRED
from dedomena.sources.worldbank import WorldBank


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp()
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.waits.append(seconds)
        self.now += seconds


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(clock):
    with_store = Store(clock=clock)
    yield with_store
    with_store.close()


def fred(handler, store, clock, **kwargs):
    return FRED("0123456789abcdef0123456789abcdef", store=store, sleep=clock.sleep,
                client=httpx.Client(transport=httpx.MockTransport(handler)), **kwargs)


def wb(handler, store, clock, **kwargs):
    return WorldBank(store=store, sleep=clock.sleep,
                     client=httpx.Client(transport=httpx.MockTransport(handler)), **kwargs)


def v1(request, rows, key="observations", total=None):
    return httpx.Response(200, json={"count": len(rows) if total is None else total,
                                   "offset": int(request.url.params.get("offset", 0)),
                                   "realtime_start": request.url.params.get("realtime_start"),
                                   "realtime_end": request.url.params.get("realtime_end"), key: rows})


def series(ident="GDP", value="100.010000000001", date="2024-01-01", updated="2026-09-01T00:00:00Z"):
    return {"series_id": ident, "title": "Gross Domestic Product", "units": "Billions of Dollars",
            "frequency": "Quarterly", "last_updated": updated,
            "copyright_id": "public domain: citation requested",
            "observations": [{"date": date, "value": value}]}


def release(rows, *, more=False, cursor=None, release_id=53):
    body = {"release": {"release_id": release_id, "name": "GDP", "sources": []},
            "series": rows, "has_more": more}
    if cursor is not None:
        body["next_cursor"] = cursor
    return httpx.Response(200, json=body)


def observation(indicator="NY.GDP.MKTP.CD", country="US", iso3="USA", value=1.5, date="2024"):
    return {"indicator": {"id": indicator, "value": "GDP"},
            "country": {"id": country, "value": "United States"},
            "countryiso3code": iso3, "date": date, "value": value,
            "unit": "", "obs_status": "", "decimal": 0}


def worldbank(request, rows, *, total=None, pages=None, updated="2026-07-13"):
    total = len(rows) if total is None else total
    size, page = int(request.url.params["per_page"]), int(request.url.params["page"])
    meta = {"page": page, "per_page": str(size) if request.url.path.startswith("/v2/indicator") else size, "total": total,
            "pages": (total + size - 1) // size if pages is None else pages,
            "lastupdated": updated}
    return httpx.Response(200, json=[meta, rows])


def test_fred_v1_asof_private_key_native_missing_and_replay(store, clock):
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.params["api_key"] == "0123456789abcdef0123456789abcdef"
        assert request.url.params["realtime_start"] == "2008-09-01"
        assert request.url.params["realtime_end"] == "2008-09-01"
        assert request.url.params["limit"] == "100000"
        return v1(request, [{"date": "2008-01-01", "value": "100.010000000001"},
                            {"date": "2008-02-01", "value": "."}])
    source = fred(handler, store, clock)
    page = next(source.observations("GDP", as_of="2008-09-01"))
    assert page.complete and page.records[0]["value"] == "100.010000000001"
    assert page.records[1]["value"] == "."
    assert "0123456789abcdef0123456789abcdef" not in json.dumps(page.to_dict())
    assert "api_key" not in page.provenance.parameters
    assert store.replay(page.provenance.snapshot_id).body
    assert next(source.observations("GDP", as_of="2008-09-01")).cache_hit
    assert len(requests) == 1


def test_fred_default_vintage_pinned_and_v1_pagination(store, clock):
    def handler(request):
        assert request.url.params["realtime_start"] == "2026-10-02"
        assert request.url.params["realtime_end"] == "2026-10-02"
        offset = int(request.url.params["offset"])
        return v1(request, [{"date": f"2024-01-0{offset+1}", "value": str(offset)}], total=2)
    pages = list(fred(handler, store, clock).observations("GDP", page_size=1))
    assert [page.complete for page in pages] == [False, True]
    assert pages[0].next_cursor == "1" and pages[-1].records_seen == 2
    assert sum(clock.waits) == 0.5


def test_fred_search_preserves_text_and_metadata_identity(store, clock):
    query = 'inflation "united states"'
    def handler(request):
        assert request.url.params["search_text"] == query
        assert request.url.params["order_by"] == "series_id"
        return v1(request, [{"id": "CPIAUCSL", "units": "Index 1982-1984=100"}], key="seriess")
    result = next(fred(handler, store, clock).search(query))
    assert result.records[0]["id"] == "CPIAUCSL"
    assert result.provenance.parameters["search_text"] == query


@pytest.mark.parametrize("change", ["wrong-asof", "changed-count", "empty-page", "wrong-offset"])
def test_fred_v1_refuses_incomplete_or_wrong_vintage(store, clock, change):
    def handler(request):
        offset = int(request.url.params["offset"])
        response = v1(request, [{"date": "2024-01-01", "value": "1"}], total=2)
        body = response.json()
        if change == "wrong-asof":
            body["realtime_end"] = "2026-10-03"
        elif change == "wrong-offset":
            body["offset"] = offset + 1
        elif offset:
            if change == "changed-count":
                body["count"] = 3
            if change == "empty-page":
                body["observations"] = []
        return httpx.Response(200, json=body)
    with pytest.raises(InvalidResponse):
        list(fred(handler, store, clock).observations("GDP", page_size=1))


def test_fred_metadata_and_vintage_identity(store, clock):
    def handler(request):
        if request.url.path.endswith("vintagedates"):
            return v1(request, ["2008-09-01", "2008-10-01"], key="vintage_dates")
        return v1(request, [{"id": "GDP", "units": "Billions of Dollars", "notes": "Source BEA"}], key="seriess")
    source = fred(handler, store, clock)
    assert source.fetch("GDP").records[0]["notes"] == "Source BEA"
    assert next(source.vintages("GDP")).records[0] == {"series_id": "GDP", "vintage_date": "2008-09-01"}
    with pytest.raises(InvalidResponse):
        source.fetch("UNRATE")


def test_fred_bulk_bearer_limit_grouping_and_metrics(store, clock):
    def handler(request):
        assert request.headers["Authorization"] == "Bearer 0123456789abcdef0123456789abcdef"
        assert "api_key" not in request.url.params
        assert request.url.params["limit"] == "500000"
        if "next_cursor" not in request.url.params:
            return release([series(value=".")], more=True, cursor="GDP,2024-04-01")
        return release([series(date="2024-04-01")])
    source = fred(handler, store, clock)
    pages = list(source.release_observations(53))
    assert pages[0].records[0]["observations"][0]["value"] == "."
    assert pages[-1].complete and pages[-1].records_seen == 2
    assert pages[-1].records[0]["units"] == "Billions of Dollars"
    assert source.usage()["observations_delivered"] == 2
    assert source.quota()["max_release_observations"] == 500000
    assert "0123456789abcdef0123456789abcdef" not in json.dumps(pages[0].to_dict())


@pytest.mark.parametrize("mode", ["wrong-release", "missing-cursor", "repeated-cursor", "revised", "oversize"])
def test_fred_bulk_rejects_bad_identity_or_incomplete_results(store, clock, mode):
    def handler(request):
        subsequent = "next_cursor" in request.url.params
        if mode == "wrong-release":
            return release([series()], release_id=99)
        if mode == "missing-cursor":
            return release([series()], more=True)
        if mode == "oversize":
            return release([series(), series("UNRATE")])
        if not subsequent:
            return release([series()], more=True, cursor="GDP,2024-04-01")
        if mode == "revised":
            return release([series(date="2024-04-01", updated="2026-10-02T00:00:00Z")])
        return release([series()], more=True, cursor="GDP,2024-04-01")
    with pytest.raises(InvalidResponse):
        list(fred(handler, store, clock).release_observations(53, page_size=1))


def test_fred_validation_fails_before_network(store, clock):
    source = fred(lambda r: pytest.fail("unexpected network"), store, clock)
    for operation in [lambda: list(source.observations("GDP", as_of="2008-09-01", realtime_end="2020-01-01")),
                      lambda: list(source.observations("GDP", observation_start="2025-01-01", observation_end="2024-01-01")),
                      lambda: list(source.observations("GDP", as_of="2024-02-30")),
                      lambda: list(source.observations("GDP", units="dollars")),
                      lambda: list(source.release_observations(53, page_size=500001)),
                      lambda: list(source.release_observations(53, cursor="GDP,2024-01-01")),
                      lambda: list(source.search("GDP", offset=1))]:
        with pytest.raises(ValueError):
            operation()
    for key in ("", "short", "A" * 32, "a" * 31, "a" * 33, "a" * 31 + "-"):
        with pytest.raises(ValueError):
            FRED(key)
    with pytest.raises(ValueError):
        FRED("a" * 32, requests_per_second=3)


def test_worldbank_batch_maximum_page_native_null_and_cache(store, clock):
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.params["per_page"] == "32767"
        assert request.url.params["source"] == "2"
        assert request.url.params["date"] == "2020:2024"
        assert request.url.path.endswith("/NY.GDP.MKTP.CD;FP.CPI.TOTL.ZG")
        return worldbank(request, [observation(), observation("FP.CPI.TOTL.ZG", value=None)])
    source = wb(handler, store, clock)
    result = next(source.search("NY.GDP.MKTP.CD;FP.CPI.TOTL.ZG", date="2020:2024"))
    assert result.complete and result.records[1]["value"] is None
    assert result.records[1]["decimal"] == 0
    assert next(source.search("NY.GDP.MKTP.CD;FP.CPI.TOTL.ZG", date="2020:2024")).cache_hit
    assert len(calls) == 1
    assert json.loads(store.replay(result.provenance.snapshot_id).body)[1][1]["value"] is None


def test_worldbank_pagination_resume_country_and_footnotes(store, clock):
    def handler(request):
        assert request.url.params["footnote"] == "y"
        return worldbank(request, [observation(date=str(2025-int(request.url.params["page"])))], total=2)
    source = wb(handler, store, clock)
    pages = list(source.search("NY.GDP.MKTP.CD", countries="USA", footnotes=True, page_size=1))
    assert pages[0].next_cursor == "2" and pages[-1].complete
    resumed = next(source.search("NY.GDP.MKTP.CD", countries="USA", footnotes=True,
                                  page_size=1, page=2, records_seen=1))
    assert resumed.cache_hit and resumed.records_seen == 2


@pytest.mark.parametrize("mode", ["error", "foreign-indicator", "foreign-country", "short-page", "wrong-pages", "revised", "count-change"])
def test_worldbank_rejects_wrong_or_incomplete_data(store, clock, mode):
    def handler(request):
        subsequent = request.url.params["page"] == "2"
        if mode == "error":
            return httpx.Response(200, json=[{"message": [{"id": "120", "value": "bad indicator"}]}])
        if mode == "foreign-indicator":
            return worldbank(request, [observation("SP.POP.TOTL")])
        if mode == "foreign-country":
            return worldbank(request, [observation(country="FR", iso3="FRA")])
        if mode == "short-page":
            return worldbank(request, [], total=2)
        if mode == "wrong-pages":
            return worldbank(request, [observation()], total=2, pages=1)
        return worldbank(request, [observation()], total=3 if subsequent and mode == "count-change" else 2,
                         updated="2026-10-02" if subsequent and mode == "revised" else "2026-07-13")
    with pytest.raises(InvalidResponse):
        list(wb(handler, store, clock).search("NY.GDP.MKTP.CD", countries="USA", page_size=1))


def test_worldbank_empty_result_complete(store, clock):
    source = wb(lambda request: worldbank(request, None, total=0), store, clock)
    result = next(source.search("NY.GDP.MKTP.CD"))
    assert result.records == () and result.complete and result.total == 0


def test_worldbank_indicator_metadata_identity(store, clock):
    def handler(request):
        return worldbank(request, [{"id": "NY.GDP.MKTP.CD", "unit": "", "source": {"id": "2"},
                                   "sourceOrganization": "World Bank national accounts data"}])
    source = wb(handler, store, clock)
    assert source.fetch("NY.GDP.MKTP.CD").records[0]["source"]["id"] == "2"
    assert next(source.indicators()).records[0]["sourceOrganization"]
    with pytest.raises(InvalidResponse):
        source.fetch("FP.CPI.TOTL.ZG")
    with pytest.raises(InvalidResponse):
        source.fetch("NY.GDP.MKTP.CD", source=16)
    with pytest.raises(InvalidResponse):
        next(source.indicators(source=16))


def test_worldbank_limits_before_network(store, clock):
    source = wb(lambda r: pytest.fail("unexpected network"), store, clock)
    for operation in [lambda: list(source.search([f"GDP{n}" for n in range(61)])),
                      lambda: list(source.search("../private")),
                      lambda: list(source.search("GDP", countries="all;US")),
                      lambda: list(source.search("GDP", countries="US", date="2025:2024")),
                      lambda: list(source.search("GDP", date="2024M13")),
                      lambda: list(source.search("GDP", date="2024:2025Q1")),
                      lambda: list(source.search("GDP", page_size=32768)),
                      lambda: list(source.search("GDP", page=2)),
                      lambda: list(source.search("GDP;GDP")),
                      lambda: list(source.search("A" * 1501))]:
        with pytest.raises(ValueError):
            operation()
