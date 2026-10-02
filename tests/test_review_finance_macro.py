"""Regression contracts for complete FRED checkpoints and World Bank selection."""
from datetime import datetime, timezone

import httpx
import pytest

from dedomena.sources.core import InvalidResponse, Store
from dedomena.sources.fred import FRED
from dedomena.sources.worldbank import WorldBank


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp()

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


@pytest.fixture
def resources():
    clock = Clock()
    store = Store(clock=clock)
    try:
        yield store, clock
    finally:
        store.close()


def fred(handler, resources):
    store, clock = resources
    return FRED("0123456789abcdef0123456789abcdef", store=store, sleep=clock.sleep,
                client=httpx.Client(transport=httpx.MockTransport(handler)))


def worldbank(handler, resources):
    store, clock = resources
    return WorldBank(store=store, sleep=clock.sleep,
                     client=httpx.Client(transport=httpx.MockTransport(handler)))


def release(date, *, more=False, cursor=None):
    body = {"release": {"release_id": 53}, "has_more": more,
            "series": [{"series_id": "GDP", "title": "Gross Domestic Product",
                        "frequency": "Quarterly", "units": "Billions of Dollars",
                        "last_updated": "2026-09-01T00:00:00Z", "copyright_id": "public domain",
                        "observations": [{"date": date, "value": "100.010000000001"}]}]}
    if cursor is not None:
        body["next_cursor"] = cursor
    return httpx.Response(200, json=body)


def observation(country="US", iso3="USA"):
    return {"indicator": {"id": "NY.GDP.MKTP.CD"}, "country": {"id": country},
            "countryiso3code": iso3, "date": "2024", "value": None}


def response(request, rows):
    return httpx.Response(200, json=[{"page": 1, "pages": 1, "total": len(rows),
                                    "per_page": int(request.url.params["per_page"])}, rows])


@pytest.mark.parametrize("cursor,records_seen", [
    (None, 1), (None, 10), ("GDP,2024-04-01", 0), ("", 1),
    ("GDP,2024-04-01", -1), (None, True), ("GDP,2024-04-01", True),
    (None, 1.0), ("GDP,2024-04-01", "1"),
])
def test_fred_release_requires_both_checkpoint_parts_before_network(resources, cursor, records_seen):
    source = fred(lambda request: pytest.fail("invalid checkpoint reached network"), resources)
    with pytest.raises(ValueError):
        list(source.release_observations(53, cursor=cursor, records_seen=records_seen))


def test_fred_release_two_page_resume_delivers_each_observation_once(resources):
    cursors = []
    def handler(request):
        cursor = request.url.params.get("next_cursor")
        cursors.append(cursor)
        if cursor is None:
            return release("2024-01-01", more=True, cursor="GDP,2024-04-01")
        assert cursor == "GDP,2024-04-01"
        return release("2024-04-01")
    source = fred(handler, resources)
    first_iterator = source.release_observations(53, page_size=1)
    first = next(first_iterator)
    first_iterator.close()
    remaining = list(source.release_observations(53, page_size=1,
                                                cursor=first.next_cursor,
                                                records_seen=first.records_seen))
    assert not first.complete and first.records_seen == 1
    assert len(remaining) == 1 and remaining[0].complete
    assert remaining[0].records_seen == 2 and remaining[0].next_cursor is None
    dates = [item["date"] for page in [first, *remaining] for record in page.records
             for item in record["observations"]]
    assert dates == ["2024-01-01", "2024-04-01"]
    assert cursors == [None, "GDP,2024-04-01"]
    assert source.usage()["observations_delivered"] == 2


@pytest.mark.parametrize("countries", ["all", "ALL", "All", ["aLl"]])
def test_worldbank_standalone_all_is_canonical_and_accepts_all_countries(resources, countries):
    def handler(request):
        assert request.url.path == "/v2/country/all/indicator/NY.GDP.MKTP.CD"
        return response(request, [observation(), observation("FR", "FRA")])
    page = next(worldbank(handler, resources).search("NY.GDP.MKTP.CD", countries=countries))
    assert page.complete and len(page.records) == 2
    assert "/country/all/" in page.provenance.url


@pytest.mark.parametrize("countries", [
    "all;FIN", "ALL;FIN", "Fin;All", ["All", "USA"],
    ["fin", "aLl"], ["ALL", "all"],
])
def test_worldbank_mixed_all_rejected_before_network(resources, countries):
    source = worldbank(lambda request: pytest.fail("mixed all selector reached network"), resources)
    with pytest.raises(ValueError, match="all cannot be combined"):
        list(source.search("NY.GDP.MKTP.CD", countries=countries))


@pytest.mark.parametrize("countries", ["USA", "usa;fin", ["uSa", "fiN"], "US;FI"])
def test_worldbank_explicit_countries_never_admit_foreign_country(resources, countries):
    source = worldbank(lambda request: response(request, [observation("FR", "FRA")]), resources)
    with pytest.raises(InvalidResponse, match="unrequested country"):
        next(source.search("NY.GDP.MKTP.CD", countries=countries))


def test_worldbank_explicit_country_selection_preserves_case_and_native_id_aliases(resources):
    def handler(request):
        assert request.url.path == "/v2/country/uSa;fiN/indicator/NY.GDP.MKTP.CD"
        return response(request, [observation(), observation("FI", "FIN")])
    page = next(worldbank(handler, resources).search("NY.GDP.MKTP.CD", countries=["uSa", "fiN"]))
    assert page.complete and len(page.records) == 2
