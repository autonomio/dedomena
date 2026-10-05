"""SEC source contracts: identity, native units and complete filing history."""
from datetime import datetime, timezone
import json

import httpx
import pytest

from dedomena.sources.core import HTTPFailure, InvalidResponse, Store
from dedomena.sources.sec import SEC


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp()
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, amount):
        self.waits.append(amount)
        self.now += amount


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(clock):
    with_store = Store(clock=clock)
    yield with_store
    with_store.close()


def sec(handler, store, clock, **kwargs):
    return SEC("Research institution researcher@example.org",
               client=httpx.Client(transport=httpx.MockTransport(handler)),
               store=store, sleep=clock.sleep, **kwargs)


def fact(accn="0000320193-26-000001", **kwargs):
    return {"accn": accn, "end": "2026-06-30", "filed": "2026-07-15",
            "form": "10-Q", "val": 1234, **kwargs}


def company_facts(cik=320193):
    return {"cik": cik, "entityName": "Company", "facts": {"us-gaap": {"Assets": {
        "label": "Assets", "description": "Total assets",
        "units": {"USD": [fact(), fact("0000320193-26-000002", val=2345)],
                  "CAD": [fact(val=3456)]}}}}}


def columns(numbers=(1,)):
    return {"accessionNumber": [f"0000320193-26-{n:06d}" for n in numbers],
            "filingDate": ["2026-07-15"] * len(numbers),
            "form": ["10-Q"] * len(numbers),
            "primaryDocument": ["report.htm"] * len(numbers)}


def submissions(files=None):
    return {"cik": "0000320193", "name": "Company", "tickers": ["CMP"],
            "filings": {"recent": columns(), "files": files or []}}


def descriptor(number=1, count=1):
    return {"name": f"CIK0000320193-submissions-{number:03d}.json", "filingCount": count,
            "filingFrom": "2025-01-01", "filingTo": "2025-12-31"}


def test_companyfacts_one_call_keeps_units_amendments_identity_and_replay(store, clock):
    calls = []
    body = company_facts()
    def handler(request):
        calls.append(request)
        assert request.url.path == "/api/xbrl/companyfacts/CIK0000320193.json"
        assert request.headers["User-Agent"] == "Research institution researcher@example.org"
        assert request.headers.get("Authorization", "") == ""
        return httpx.Response(200, json=body)
    source = sec(handler, store, clock)
    page = source.fetch("CIK320193")
    assert page.records == (body,)
    assert page.complete and page.total == 1
    units = page.records[0]["facts"]["us-gaap"]["Assets"]["units"]
    assert list(units) == ["USD", "CAD"]
    assert [row["val"] for row in units["USD"]] == [1234, 2345]
    assert json.loads(store.replay(page.provenance.snapshot_id).body) == body
    assert source.fetch(320193).cache_hit
    assert len(calls) == 1


@pytest.mark.parametrize("cik", [True, 0, -1, 10_000_000_000, "apple", "320193/..", "00000000000", "CIK", None])
def test_invalid_cik_rejected_before_network(cik, store, clock):
    source = sec(lambda request: pytest.fail("invalid CIK sent upstream"), store, clock)
    with pytest.raises(ValueError):
        source.fetch(cik)


@pytest.mark.parametrize("user_agent", ["", "python-httpx", "user@example.org", "Company no-email", "Company a@b.c\nInjected: x"])
def test_user_agent_requires_organization_and_contact(user_agent):
    with pytest.raises(ValueError):
        SEC(user_agent)


def test_rate_limit_aggregate_across_user_agents(store, clock):
    handler = lambda request: httpx.Response(200, json=company_facts())
    first = sec(handler, store, clock)
    second = SEC("Another institution contact@example.org", store=store, sleep=clock.sleep,
                 client=httpx.Client(transport=httpx.MockTransport(handler)))
    first.fetch(320193)
    second.fetch(320193)
    assert first.scope == second.scope
    assert clock.waits == pytest.approx([0.1])
    with pytest.raises(ValueError):
        SEC("Company contact@example.org", requests_per_second=11)


@pytest.mark.parametrize("body", [
    company_facts(cik=1),
    {"cik": 320193, "facts": []},
    {"cik": 320193, "facts": {"us-gaap": {"Assets": {"units": {"USD": [fact(val=None)]}}}}},
    {"cik": 320193, "facts": {"us-gaap": {"Assets": {"units": {"USD": [fact(accn="missing")]}}}}},
    {"cik": 320193, "facts": {"us-gaap": {"Assets": {"units": {"USD": [fact(end="bad-date")]}}}}},
])
def test_companyfacts_rejects_foreign_identity_or_broken_units(body, store, clock):
    source = sec(lambda request: httpx.Response(200, json=body), store, clock)
    with pytest.raises(InvalidResponse):
        source.fetch(320193)


def test_single_concept_and_frame_keep_semantic_envelopes(store, clock):
    concept = {"cik": 320193, "taxonomy": "us-gaap", "tag": "Assets",
               "units": {"USD": [fact()]}, "entityName": "Company"}
    observation = fact(cik=320193, entityName="Company")
    del observation["filed"]
    frame = {"taxonomy": "us-gaap", "tag": "Assets", "uom": "USD", "ccp": "CY2026Q2I",
             "pts": 1, "data": [observation]}
    def handler(request):
        if "companyconcept" in request.url.path:
            assert request.url.path.endswith("/us-gaap/Assets.json")
            return httpx.Response(200, json=concept)
        assert request.url.path.endswith("/us-gaap/Assets/USD/CY2026Q2I.json")
        return httpx.Response(200, json=frame)
    source = sec(handler, store, clock)
    assert source.company_concept(320193, "us-gaap", "Assets").records == (concept,)
    page = source.frame("us-gaap", "Assets", "USD", "CY2026Q2I")
    assert page.records == (frame,) and page.complete
    assert "latest filing" in page.warnings[0]


@pytest.mark.parametrize("field,value", [("ccp", "CY2025Q2I"), ("uom", "CAD"), ("pts", 2)])
def test_frame_identity_and_counts_must_match(field, value, store, clock):
    body = {"taxonomy": "us-gaap", "tag": "Assets", "uom": "USD", "ccp": "CY2026Q2I",
            "pts": 1, "data": [fact(cik=320193)], field: value}
    source = sec(lambda request: httpx.Response(200, json=body), store, clock)
    with pytest.raises(InvalidResponse):
        source.frame("us-gaap", "Assets", "USD", "CY2026Q2I")


def test_submissions_enumerates_recent_and_every_historical_file(store, clock):
    calls = []
    bodies = {"/submissions/CIK0000320193.json": submissions([descriptor(1), descriptor(2)]),
              "/submissions/CIK0000320193-submissions-001.json": columns((2,)),
              "/submissions/CIK0000320193-submissions-002.json": columns((3,))}
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=bodies[request.url.path])
    source = sec(handler, store, clock)
    pages = list(source.submissions(320193))
    assert len(calls) == 3
    assert [page.complete for page in pages] == [False, False, True]
    assert [page.total for page in pages] == [3, 3, 3]
    assert [page.records_seen for page in pages] == [1, 2, 3]
    assert [page.records[0]["accessionNumber"] for page in pages] == [
        "0000320193-26-000001", "0000320193-26-000002", "0000320193-26-000003"]
    assert "company metadata" in pages[0].warnings[0]
    assert "tickers" in json.loads(store.replay(pages[0].provenance.snapshot_id).body)
    resumed = list(source.submissions(320193, cursor=pages[0].next_cursor,
                                     records_seen=pages[0].records_seen))
    assert [page.records for page in resumed] == [page.records for page in pages[1:]]
    assert all(page.cache_hit for page in resumed)


def test_recent_only_is_explicitly_incomplete(store, clock):
    body = submissions([descriptor()])
    source = sec(lambda request: httpx.Response(200, json=body), store, clock)
    page = next(source.submissions(320193, include_history=False))
    assert not page.complete and page.total == 2
    assert page.next_cursor == "CIK0000320193-submissions-001.json"
    assert "incomplete" in page.warnings[-1]


@pytest.mark.parametrize("historical", [columns(()), columns((1,)), {"accessionNumber": ["wrong"]},
                                         {"accessionNumber": ["0000320193-26-000002"], "form": []}])
def test_history_cannot_silently_truncate_or_duplicate(historical, store, clock):
    def handler(request):
        body = submissions([descriptor()]) if request.url.path.endswith("CIK0000320193.json") else historical
        return httpx.Response(200, json=body)
    with pytest.raises(InvalidResponse):
        list(sec(handler, store, clock).submissions(320193))


@pytest.mark.parametrize("filename", ["../other.json", "CIK0000000001-submissions-001.json", "https://evil.test/file.json"])
def test_manifest_cannot_redirect_company_identity_or_origin(filename, store, clock):
    body = submissions([{**descriptor(), "name": filename}])
    source = sec(lambda request: httpx.Response(200, json=body), store, clock)
    with pytest.raises(InvalidResponse):
        list(source.submissions(320193))


def test_changed_manifest_refuses_resume_and_size_limit_is_configurable(store, clock):
    body = submissions([descriptor()])
    source = sec(lambda request: httpx.Response(200, json=body), store, clock)
    with pytest.raises(ValueError, match="checkpoint"):
        list(source.submissions(320193, cursor=descriptor()["name"], records_seen=2))
    limited = sec(lambda request: httpx.Response(200, json=company_facts()), store, clock,
                  max_response_bytes=30)
    with pytest.raises(InvalidResponse, match="size limit"):
        limited.fetch(320193)


def test_provider_no_facts_404_is_failure_not_empty_dataset(store, clock):
    source = sec(lambda request: httpx.Response(404, json={"error": "No company facts"}), store, clock)
    with pytest.raises(HTTPFailure, match="HTTP 404"):
        source.fetch(320193)


def test_ratio_frame_uses_native_slash_unit_and_url_per_encoding(store, clock):
    body = {"taxonomy": "us-gaap", "tag": "EarningsPerShareDiluted", "uom": "USD/shares",
            "ccp": "CY2025", "pts": 1, "data": [fact(cik=320193)]}
    def handler(request):
        assert "/USD-per-shares/" in request.url.path
        return httpx.Response(200, json=body)
    page = sec(handler, store, clock).frame("us-gaap", "EarningsPerShareDiluted", "USD-per-shares", "CY2025")
    assert page.records[0]["uom"] == "USD/shares"


def test_keyless_pacing_scope_cannot_be_overridden():
    with pytest.raises(ValueError, match="aggregate pacing"):
        SEC("Company researcher@example.org", credential="separate-quota")


def test_parallel_company_reads_preserve_order_and_window_duplicates(store, clock):
    called = []
    def handler(request):
        cik = int(request.url.path.rsplit("/", 1)[-1].removeprefix("CIK").removesuffix(".json"))
        called.append(cik)
        return httpx.Response(200, json=company_facts(cik))
    source = sec(handler, store, clock)
    pages = list(source.fetch_many([2, 1, "CIK2"], workers=3, refresh=True))
    assert [page.records[0]["cik"] for page in pages] == [2, 1, 2]
    assert sorted(called) == [1, 2]
