"""Offline provider contracts: completeness, key budgets, payload identity and replay."""
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
import gzip
import json
import threading

import httpx
import pytest

from dedomena.sources import (BudgetExceeded, EPO, EuropePMC, InvalidResponse, OpenAlex,
                              SearchLimitExceeded, SourceError, Store, Throttled)
from dedomena.sources.core import Transport, digest


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
        self.waits = []
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def sleep(self, amount):
        with self.lock:
            self.waits.append(amount)
            self.now += amount


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(clock):
    value = Store(clock=clock)
    yield value
    value.close()


def oa_row(n=1):
    return {"id": f"https://openalex.org/W{n}", "doi": f"https://doi.org/10.1234/{n}",
            "title": f"Work {n}", "publication_year": 2026, "ids": {}, "type": "article"}


def oa_response(rows, total=None, cursor=None, **headers):
    return httpx.Response(200, json={"results": rows,
                                    "meta": {"count": len(rows) if total is None else total,
                                             "next_cursor": cursor}},
                          headers={"X-RateLimit-Credits-Used": "10", **headers})


def quota(remaining=10000, limit=10000):
    return httpx.Response(200, json={"api_key": "fixture-secret",
                                    "rate_limit": {"credits_remaining": remaining,
                                                   "credits_limit": limit,
                                                   "credit_costs": {"singleton": 0, "search": 10, "list": 1}}})


def oa(handler, store, clock, **kwargs):
    def wrapped(request):
        if request.url.path == "/rate-limit":
            return quota()
        return handler(request)
    return OpenAlex("fixture-secret", client=httpx.Client(transport=httpx.MockTransport(wrapped)),
                    store=store, sleep=clock.sleep, **kwargs)


def epmc_row(n=1):
    return {"id": str(n), "source": "MED", "pmcid": f"PMC{n}", "title": f"Paper {n}"}


def epmc_response(rows, total=None, cursor=None):
    return httpx.Response(200, json={"hitCount": len(rows) if total is None else total,
                                    "nextCursorMark": cursor, "resultList": {"result": rows}})


def epmc(handler, store, clock, **kwargs):
    return EuropePMC(client=httpx.Client(transport=httpx.MockTransport(handler)),
                     store=store, sleep=clock.sleep, **kwargs)


def patent(ident="EP.1000000.A1"):
    country, number, kind = ident.split(".")
    return f'<exchange-document country="{country}" doc-number="{number}" kind="{kind}" family-id="1"><bibliographic-data><invention-title lang="en">Biology</invention-title></bibliographic-data></exchange-document>'


def patent_response(ids=("EP.1000000.A1",), *, total=None, begin=1, end=None, headers=None):
    body = '<ops:world-patent-data xmlns:ops="http://ops.epo.org" xmlns="http://www.epo.org/exchange">'
    if total is not None:
        body += f'<ops:biblio-search total-result-count="{total}"><ops:range begin="{begin}" end="{end or max(begin, len(ids))}"/><ops:search-result>'
    body += "<exchange-documents>" + "".join(patent(ident) for ident in ids) + "</exchange-documents>"
    if total is not None:
        body += "</ops:search-result></ops:biblio-search>"
    body += "</ops:world-patent-data>"
    return httpx.Response(200, content=body.encode(), headers=headers)


def epo(handler, store, clock, **kwargs):
    def wrapped(request):
        if request.url.path == "/3.2/auth/accesstoken":
            return httpx.Response(200, json={"access_token": "fixture-token", "expires_in": "1199"})
        return handler(request)
    return EPO("fixture-key", "fixture-password",
               client=httpx.Client(transport=httpx.MockTransport(wrapped)),
               store=store, sleep=clock.sleep, **kwargs)


def test_openalex_preserves_query_maximizes_pages_and_retains_id(store, clock):
    seen = []
    query = '("gene editing" OR CRISPR) AND biology'
    def handler(request):
        seen.append(request)
        assert request.headers["Authorization"] == "Bearer fixture-secret"
        assert "fixture-secret" not in str(request.url)
        assert request.url.params["search"] == query
        assert request.url.params["per_page"] == "100"
        assert request.url.params["select"] == "id,doi,title,publication_year,ids,type"
        return oa_response([oa_row(1)], 2, "second") if len(seen) == 1 else oa_response([oa_row(2)], 2)
    pages = list(oa(handler, store, clock).search(query))
    assert [p.records[0]["id"] for p in pages] == ["https://openalex.org/W1", "https://openalex.org/W2"]
    assert [p.complete for p in pages] == [False, True]
    assert pages[0].next_cursor == "second"
    assert pages[1].records_seen == 2
    assert sum(p.credits_used for p in pages) == 20


def test_cursor_checkpoint_resumes_without_restarting(store, clock):
    def handler(request):
        if request.url.params["cursor"] == "*":
            return oa_response([oa_row(1)], 2, "next")
        return oa_response([oa_row(2)], 2)
    source = oa(handler, store, clock)
    first = next(source.search("biology"))
    final = list(source.search("biology", cursor=first.next_cursor, records_seen=first.records_seen))
    assert final[0].complete and final[0].records_seen == 2
    assert len(final) == 1


@pytest.mark.parametrize("body", [
    {"results": [], "meta": {"count": 1, "next_cursor": None}},
    {"results": [oa_row()], "meta": {"count": 2, "next_cursor": "*"}},
    {"results": [{"title": "No identity"}], "meta": {"count": 1}},
    {"results": [], "meta": {"count": -1}},
    {"results": {}, "meta": {"count": 0}},
])
def test_openalex_never_labels_bad_enumeration_complete(body, store, clock):
    source = oa(lambda r: httpx.Response(200, json=body), store, clock)
    with pytest.raises(InvalidResponse):
        list(source.search("biology"))


def test_paid_search_refuses_unaffordable_request(store, clock):
    requested = []
    def handler(request):
        requested.append(request.url.path)
        return quota(9) if request.url.path == "/rate-limit" else oa_response([oa_row()])
    source = OpenAlex("fixture-secret", store=store, sleep=clock.sleep,
                     client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(BudgetExceeded):
        list(source.search("biology"))
    assert requested == ["/rate-limit"]


def test_budget_reservation_is_atomic_across_connections(tmp_path, clock):
    path = tmp_path / "shared.sqlite3"
    a, b = Store(path, clock=clock), Store(path, clock=clock)
    def reserve(value):
        try:
            value.charge("openalex:key", "credits", 10, 10, "day")
            return True
        except BudgetExceeded:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, (a, b)))
    assert sorted(results) == [False, True]
    assert a.used("openalex:key", "credits", "day") == 10
    a.close(); b.close()


def test_exact_openalex_lookups_use_no_quota_endpoint_or_credits(store, clock):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, json=oa_row(int(request.url.path.rsplit("W", 1)[-1])),
                              headers={"X-RateLimit-Credits-Used": "0"})
    source = OpenAlex("fixture-secret", store=store, sleep=clock.sleep,
                     client=httpx.Client(transport=httpx.MockTransport(handler)))
    pages = list(source.fetch_many(["W1", "W2", "W3"], workers=2))
    assert [p.records[0]["id"] for p in pages] == [f"https://openalex.org/W{i}" for i in (1, 2, 3)]
    assert len(seen) == 3 and all("/works/W" in r.url.path for r in seen)
    assert source.usage()["credits_used"] == 0


def test_exact_lookups_validate_the_requested_identity(store, clock):
    source = oa(lambda r: httpx.Response(200, json=oa_row(2)), store, clock)
    with pytest.raises(InvalidResponse):
        source.fetch("W1")


def test_openalex_100_work_batch_uses_one_credit(store, clock):
    def handler(request):
        ids = request.url.params["filter"].split(":", 1)[-1].split("|")
        assert len(ids) == 100
        return oa_response([oa_row(int(value[1:])) for value in ids],
                           **{"X-RateLimit-Credits-Used": "1"})
    source = oa(handler, store, clock)
    page = next(source.fetch_batch(f"W{n}" for n in range(1, 101)))
    assert len(page.records) == 100 and page.credits_used == 1


def test_durable_cache_and_offline_replay_exclude_credentials(tmp_path, clock):
    path = tmp_path / "source.sqlite3"
    first_store = Store(path, clock=clock)
    source = oa(lambda r: oa_response([oa_row()]), first_store, clock)
    page = next(source.search("biology"))
    raw = first_store.replay(page.provenance.snapshot_id)
    assert raw.provenance.request_sha256 == page.provenance.request_sha256
    assert digest(raw.body) == page.provenance.response_sha256
    first_store.close()
    second_store = Store(path, clock=clock)
    def fail(request):
        raise AssertionError("cache reuse must not make even a quota HTTP call")
    source = OpenAlex("fixture-secret", store=second_store, sleep=clock.sleep,
                     client=httpx.Client(transport=httpx.MockTransport(fail)))
    cached = next(source.search("biology"))
    assert cached.cache_hit and cached.credits_used == cached.wire_bytes == 0
    assert cached.provenance == page.provenance
    second_store.close()
    assert b"fixture-secret" not in path.read_bytes()


def test_refresh_preserves_old_snapshot_and_changes_new_one(store, clock):
    counter = [0]
    def handler(r):
        counter[0] += 1
        return oa_response([oa_row(counter[0])])
    source = oa(handler, store, clock)
    old = next(source.search("biology"))
    new = next(source.search("biology", refresh=True))
    assert old.provenance.snapshot_id != new.provenance.snapshot_id
    assert store.replay(old.provenance.snapshot_id).json()["results"][0]["id"].endswith("W1")
    assert store.replay(new.provenance.snapshot_id).json()["results"][0]["id"].endswith("W2")


def test_corrupted_snapshot_is_rejected(store, clock):
    page = next(oa(lambda r: oa_response([oa_row()]), store, clock).search("biology"))
    with store.transaction() as db:
        db.execute("UPDATE snapshots SET body=?", (b"tampered",))
    with pytest.raises(InvalidResponse, match="hash mismatch"):
        store.replay(page.provenance.snapshot_id)


def test_europepmc_1000_records_native_query_and_cursor(store, clock):
    seen = []
    def handler(request):
        seen.append(request)
        assert request.url.params["query"] == "TITLE:CRISPR"
        assert request.url.params["pageSize"] == "1000"
        assert request.url.params["resultType"] == "lite"
        return epmc_response([epmc_row(n) for n in range(1, 1001)], 1001, "next") if len(seen) == 1 else epmc_response([epmc_row(1001)], 1001)
    pages = list(epmc(handler, store, clock).search("TITLE:CRISPR"))
    assert sum(len(p.records) for p in pages) == 1001
    assert pages[-1].complete and pages[-1].records_seen == 1001
    assert pages[0].provenance.parameters["cursorMark"] == "*"


def test_europepmc_missing_or_repeated_cursor_is_a_failure(store, clock):
    source = epmc(lambda r: epmc_response([epmc_row()], 2, "*"), store, clock)
    with pytest.raises(InvalidResponse):
        list(source.search("biology"))


@pytest.mark.parametrize("body", [{"hitCount": 0, "resultList": []}, {"hitCount": 1, "resultList": {"result": [{"id": "1"}]}}])
def test_europepmc_malformed_envelope_has_typed_failure(body, store, clock):
    source = epmc(lambda r: httpx.Response(200, json=body), store, clock)
    with pytest.raises(InvalidResponse):
        list(source.search("biology"))


def test_europepmc_batches_disclose_unresolved_ids(store, clock):
    seen = []
    def handler(request):
        seen.append(request.url.params["query"])
        return epmc_response([epmc_row(1)])
    pages = list(epmc(handler, store, clock).fetch_many(["1", "2"]))
    assert len(seen) == 1 and " OR " in seen[0]
    assert pages[0].unresolved == ("2",)


def test_europepmc_batch_rejects_foreign_results(store, clock):
    source = epmc(lambda r: epmc_response([epmc_row(7)]), store, clock)
    with pytest.raises(InvalidResponse):
        list(source.fetch_many(["1", "2"]))


def test_europepmc_fulltext_preserves_original_jats_and_identity(store, clock):
    xml = b'<article><front><article-id pub-id-type="pmc">123</article-id></front><body>Evidence</body></article>'
    source = epmc(lambda r: httpx.Response(200, content=xml), store, clock)
    response = source.full_text("PMC123")
    assert response.body == store.replay(response.provenance.snapshot_id).body == xml
    with pytest.raises(InvalidResponse):
        source.full_text("PMC456")


def test_xml_entity_expansion_is_rejected(store, clock):
    xml = b'<!DOCTYPE article [<!ENTITY secret SYSTEM "file:///etc/passwd">]><article>&secret;</article>'
    source = epmc(lambda r: httpx.Response(200, content=xml), store, clock)
    with pytest.raises(InvalidResponse):
        source.full_text("PMC123")


def test_epo_search_100_patents_and_exact_cql(store, clock):
    seen = []
    def handler(request):
        seen.append(request)
        assert request.url.params["q"] == 'ta="CRISPR"'
        assert request.url.params["Range"] == "1-100"
        assert request.headers["Authorization"] == "Bearer fixture-token"
        return patent_response([f"EP.{n}.A1" for n in range(1, 101)], total=100, end=100)
    page = next(epo(handler, store, clock).search('ta="CRISPR"'))
    assert len(page.records) == 100 and page.complete
    assert page.records[0]["id"] == "EP.1.A1"
    assert page.provenance.parameters["Range"] == "1-100"


def test_epo_search_ceiling_is_never_silent(store, clock):
    source = epo(lambda r: patent_response(total=2001, end=100), store, clock)
    with pytest.raises(SearchLimitExceeded):
        list(source.search('ta="biology"'))
    page = next(source.search('ta="biology"', allow_partial=True))
    assert not page.complete and page.warnings


def test_epo_partitions_publication_dates_without_gaps(store, clock):
    queries = []
    def handler(request):
        query = request.url.params["q"]
        queries.append(query)
        if "20260101 20260104" in query:
            return patent_response(total=2001, end=100)
        ident = "EP.1.A1" if "20260101 20260102" in query else "EP.2.A1"
        return patent_response([ident], total=1, end=1)
    source = epo(handler, store, clock)
    pages = list(source.search_partitioned('ta="CRISPR"', date(2026, 1, 1), date(2026, 1, 4)))
    assert len(pages) == 2 and not pages[0].complete and pages[1].complete
    assert pages[0].next_cursor == "date:2026-01-03"
    assert [q.split("pd within ")[-1] for q in queries] == [
        '"20260101 20260104"', '"20260101 20260102"', '"20260103 20260104"']


def test_epo_single_day_overflow_fails_instead_of_dropping_hits(store, clock):
    source = epo(lambda r: patent_response(total=2001, end=100), store, clock)
    with pytest.raises(SearchLimitExceeded, match="one publication day"):
        list(source.search_partitioned("ta=biology", date(2026, 1, 1), date(2026, 1, 1)))


def test_epo_100_patent_bulk_post_and_unresolved_ids(store, clock):
    requested = []
    def handler(request):
        assert request.method == "POST"
        ids = request.content.decode().splitlines()
        requested.append(ids)
        return patent_response(ids[:-1])
    source = epo(handler, store, clock)
    page = next(source.fetch_many(f"EP.{n}.A1" for n in range(1, 101)))
    assert len(requested[0]) == 100 and len(page.records) == 99
    assert page.unresolved == ("EP.100.A1",)
    assert page.provenance.request_body == "\n".join(requested[0])
    assert "fixture-token" not in json.dumps(page.provenance.to_dict())


def test_epo_cache_needs_no_new_token_or_upstream_request(store, clock):
    source = epo(lambda r: patent_response(), store, clock)
    original = source.fetch("EP.1000000.A1")
    def fail(request):
        raise AssertionError("cached EPO response must not authenticate")
    another = EPO("fixture-key", "fixture-password", store=store, sleep=clock.sleep,
                  client=httpx.Client(transport=httpx.MockTransport(fail)))
    cached = another.fetch("EP.1000000.A1")
    assert cached.cache_hit and original.provenance == cached.provenance


def test_epo_token_and_password_are_not_stored(tmp_path, clock):
    path = tmp_path / "epo.sqlite3"
    value = Store(path, clock=clock)
    page = epo(lambda r: patent_response(), value, clock).fetch("EP.1000000.A1")
    assert store_secret_free(page.provenance.to_dict())
    value.close()
    data = path.read_bytes()
    assert all(secret not in data for secret in (b"fixture-key", b"fixture-password", b"fixture-token"))


def store_secret_free(value):
    text = json.dumps(value)
    return not any(secret in text for secret in ("fixture-key", "fixture-password", "fixture-token"))


def test_epo_keeps_the_most_restrictive_rate_within_the_window(store, clock):
    source = epo(lambda r: patent_response(), store, clock)
    source._observe({"x-throttling-control": "overloaded (retrieval=red:10)"})
    source._observe({"x-throttling-control": "idle (retrieval=green:200)"})
    source.fetch("EP.1000000.A1")
    # Distinct fetches force actual service pacing; fixture must match each identity.
    def handler(request):
        ident = request.url.path.split("/")[-2]
        return patent_response([ident])
    source.client = httpx.Client(transport=httpx.MockTransport(handler))
    before = clock()
    source.fetch("EP.1000001.A1")
    assert clock() - before >= 5.9


def test_transport_retry_after_respects_provider_delay(store, clock):
    seen = [0]
    def handler(request):
        seen[0] += 1
        return httpx.Response(429, headers={"Retry-After": "3"}) if seen[0] == 1 else httpx.Response(200, json={})
    source = Transport("test", "https://example.org", store=store, sleep=clock.sleep,
                       client=httpx.Client(transport=httpx.MockTransport(handler)))
    source._request("GET", "/data")
    assert seen[0] == 2 and sum(clock.waits) >= 3


def test_exhausted_budget_is_not_retried(store, clock):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(429, headers={"X-RateLimit-Remaining": "0"})
    source = oa(handler, store, clock)
    with pytest.raises(BudgetExceeded):
        list(source.search("biology"))
    assert len(seen) == 1


def test_long_retry_after_returns_actionable_throttle(store, clock):
    source = epmc(lambda r: httpx.Response(429, headers={"Retry-After": "120"}), store, clock)
    with pytest.raises(Throttled) as error:
        list(source.search("biology"))
    assert error.value.retry_after == 120 and not any(wait > 60 for wait in clock.waits)


def test_transport_rejects_redirects_without_forwarding_credentials(store, clock):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://attacker.example/"})
    source = oa(handler, store, clock)
    with pytest.raises(SourceError, match="HTTP 302"):
        source.fetch("W1")
    assert len(seen) == 1


def test_compression_accounting_distinguishes_wire_and_decoded_bytes(store, clock):
    payload = json.dumps({"value": "x" * 10000}).encode()
    packed = gzip.compress(payload)
    source = Transport("test", "https://example.org", store=store, sleep=clock.sleep,
                       client=httpx.Client(transport=httpx.MockTransport(
                           lambda r: httpx.Response(200, content=packed, headers={"Content-Encoding": "gzip"}))))
    response = source._request("GET", "/data")
    assert response.body == payload
    assert source.usage()["decoded_bytes"] == len(payload)
    # Streaming real HTTP reports compressed wire bytes; MockTransport falls back to decoded size.
    assert response.wire_bytes > 0


def test_response_limit_stops_oversized_body(store, clock):
    source = Transport("test", "https://example.org", store=store, sleep=clock.sleep,
                       max_response_bytes=10,
                       client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"x" * 11))))
    with pytest.raises(InvalidResponse, match="size limit"):
        source._request("GET", "/data")


def test_retry_settlement_stays_in_original_budget_window(store, clock):
    clock.now = datetime(2026, 10, 1, 23, 59, 59, tzinfo=timezone.utc).timestamp()
    def handler(request):
        clock.sleep(2)
        return httpx.Response(200, json={}, headers={"X-RateLimit-Credits-Used": "0"})
    source = Transport("test", "https://example.org", store=store, sleep=clock.sleep,
                       client=httpx.Client(transport=httpx.MockTransport(handler)))
    source._request("GET", "/data", estimated_credits=10, credit_limit=10)
    with store.transaction() as db:
        used = db.execute("SELECT used FROM budgets WHERE unit='credits' AND window='2026-10-01'").fetchone()[0]
    assert used == 0


@pytest.mark.parametrize("source_type,maximum", [(OpenAlex, 101), (EuropePMC, 1001), (EPO, 101)])
def test_unsupported_page_sizes_fail_before_http(source_type, maximum, store, clock):
    source = source_type(store=store, sleep=clock.sleep)
    with pytest.raises(ValueError):
        next(source.search("biology", page_size=maximum))
    source.close()


def test_installed_core_needs_no_legacy_data_science_dependencies():
    import dedomena
    assert hasattr(dedomena, "sources")
    import sys
    assert not any(name in sys.modules for name in ("pandas", "pmlb", "pymed", "twintel"))


def test_form_request_cache_identity_includes_body(store, clock):
    seen = []
    def handler(request):
        seen.append(request.content)
        return httpx.Response(200, json={"input": request.content.decode()})
    source = Transport("test", "https://example.org", store=store, sleep=clock.sleep,
                       client=httpx.Client(transport=httpx.MockTransport(handler)))
    a = source._request("POST", "/search", form={"query": "gene A"})
    b = source._request("POST", "/search", form={"query": "gene B"})
    assert len(seen) == 2 and a.provenance.request_sha256 != b.provenance.request_sha256
    assert a.provenance.parameters["query"] == "gene A"
    assert source._request("POST", "/search", form={"query": "gene A"}).cache_hit


def test_europepmc_long_batches_use_native_post_without_truncation(store, clock):
    from urllib.parse import parse_qs
    identifiers = [str(10000000 + n) for n in range(100)]
    def handler(request):
        assert request.method == "POST" and request.url.path.endswith("/searchPOST")
        form = parse_qs(request.content.decode())
        assert all(ident in form["query"][0] for ident in identifiers)
        return epmc_response([epmc_row(int(value)) for value in identifiers])
    source = epmc(handler, store, clock)
    page = next(source.fetch_many(identifiers))
    assert len(page.records) == 100 and not page.unresolved
    assert page.provenance.method == "POST"


def test_epo_refreshes_expired_token_once_after_401(store, clock):
    auth_calls, fetch_calls = [0], [0]
    def handler(request):
        if request.url.path.endswith("accesstoken"):
            auth_calls[0] += 1
            return httpx.Response(200, json={"access_token": str(auth_calls[0]), "expires_in": 1199})
        fetch_calls[0] += 1
        if fetch_calls[0] == 1:
            return httpx.Response(401)
        assert request.headers["Authorization"] == "Bearer 2"
        return patent_response()
    source = EPO("key", "secret", store=store, sleep=clock.sleep,
                 client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert source.fetch("EP.1000000.A1").complete
    assert auth_calls[0] == fetch_calls[0] == 2


def test_epo_fulltext_is_on_demand_and_identity_checked(store, clock):
    xml = b'<ops:world-patent-data xmlns:ops="http://ops.epo.org"><fulltext-document country="EP" doc-number="1000000" kind="A1"><claims><claim>Biological claim</claim></claims></fulltext-document></ops:world-patent-data>'
    def handler(request):
        assert request.url.path.endswith("/claims")
        assert request.headers["Accept"] == "application/fulltext+xml"
        return httpx.Response(200, content=xml)
    source = epo(handler, store, clock)
    raw = source.full_text("EP.1000000.A1")
    assert raw.body == store.replay(raw.provenance.snapshot_id).body
    with pytest.raises(InvalidResponse):
        source.full_text("EP.2000000.A1")


def test_cli_benchmark_is_bounded_and_discloses_incomplete_search(monkeypatch, capsys, store, clock):
    from dedomena.sources import __main__ as cli
    source = epmc(lambda r: epmc_response([epmc_row()], 2, "next"), store, clock)
    monkeypatch.setattr(cli, "EuropePMC", lambda **kwargs: source)
    assert cli.main(["benchmark", "europepmc", "biology"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["pages"] == 1 and summary["records"] == 1
    assert not summary["complete"] and summary["next_cursor"] == "next"


def test_cli_source_failure_is_explicit_and_redacted(monkeypatch, capsys, store, clock):
    from dedomena.sources import __main__ as cli
    source = epmc(lambda r: httpx.Response(403, text="fixture-private-token"), store, clock)
    monkeypatch.setattr(cli, "EuropePMC", lambda **kwargs: source)
    assert cli.main(["search", "europepmc", "biology"]) == 2
    output = capsys.readouterr()
    assert "fixture-private-token" not in output.err
    assert not json.loads(output.err)["complete"]


def test_epo_missing_range_records_do_not_claim_complete(store, clock):
    source = epo(lambda r: patent_response(total=100, end=100), store, clock)
    with pytest.raises(InvalidResponse, match="fewer patent records"):
        next(source.search("ta=biology"))


def test_openalex_filtered_batch_reports_missing_work_ids(store, clock):
    source = oa(lambda r: oa_response([oa_row(1)], **{"X-RateLimit-Credits-Used": "1"}), store, clock)
    page = next(source.fetch_batch(["W1", "W2"]))
    assert page.unresolved == ("W2",)


def test_epo_epodoc_batch_discloses_missing_and_rejects_wrong_identity(store, clock):
    source = epo(lambda r: patent_response(), store, clock)
    page = next(source.fetch_many(["EP1000000.A1", "EP2000000.A1"], format="epodoc"))
    assert page.unresolved == ("EP2000000.A1",)
    with pytest.raises(InvalidResponse):
        source.fetch("EP2000000.A1", format="epodoc")


def test_epo_partitioned_cli_does_not_mark_first_partition_complete(monkeypatch, capsys, store, clock):
    from dedomena.sources import __main__ as cli
    def handler(request):
        query = request.url.params["q"]
        return patent_response(total=2001, end=100) if "20260101 20260104" in query else patent_response(total=1, end=1)
    source = epo(handler, store, clock)
    monkeypatch.setattr(cli, "EPO", lambda **kwargs: source)
    assert cli.main(["benchmark", "epo", "ta=biology", "--start-date", "2026-01-01",
                     "--end-date", "2026-01-04"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert not summary["complete"] and summary["next_cursor"] == "date:2026-01-03"


def test_openalex_doi_identity_retains_free_native_route_and_fixed_origin(store, clock):
    def handler(request):
        assert request.url.raw_path.startswith(b"/works/doi:10.1234/")
        assert request.url.host == "api.openalex.org"
        return httpx.Response(200, json=oa_row(1))
    source = oa(handler, store, clock)
    assert source.fetch("https://doi.org/10.1234/1").records[0]["id"].endswith("W1")


def test_injected_clients_cannot_forward_default_authorization_to_public_sources(store, clock):
    def handler(request):
        assert request.headers["Authorization"] == ""
        return epmc_response([epmc_row()])
    source = EuropePMC(store=store, sleep=clock.sleep,
                       client=httpx.Client(headers={"Authorization": "Bearer other-private-key"},
                                           transport=httpx.MockTransport(handler)))
    assert next(source.search("biology")).complete


def test_source_credentials_echoed_in_metadata_are_never_archived(store, clock):
    row = oa_row()
    row["title"] = "fixture-secret"
    source = oa(lambda r: oa_response([row]), store, clock)
    with pytest.raises(InvalidResponse, match="echoed a source credential"):
        next(source.search("biology"))
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0


def test_openalex_exact_lookup_does_not_silently_accept_changed_billing(store, clock):
    source = oa(lambda r: httpx.Response(200, json=oa_row(),
                                        headers={"X-RateLimit-Credits-Used": "1"}), store, clock)
    with pytest.raises(InvalidResponse, match="unexpectedly charged"):
        source.fetch("W1")


def test_doi_cannot_normalize_into_another_source_endpoint(store, clock):
    source = oa(lambda r: pytest.fail("invalid DOI must not dispatch"), store, clock)
    with pytest.raises(ValueError):
        source.fetch("10.1234/../../rate-limit")


@pytest.mark.parametrize("identifier", ["10.1234/1", "doi:10.1234/1", "https://doi.org/10.1234/1"])
def test_all_doi_spellings_normalize_to_verified_free_route(identifier, store, clock):
    def handler(request):
        assert request.url.path == "/works/doi:10.1234/1"
        return httpx.Response(200, json=oa_row(),
                              headers={"X-RateLimit-Credits-Used": "0"})
    assert oa(handler, store, clock).fetch(identifier).credits_used == 0


def test_source_transport_cannot_be_used_for_provider_mutations(store, clock):
    source = oa(lambda r: pytest.fail("write route must not dispatch"), store, clock)
    with pytest.raises(ValueError, match="read-only"):
        source._request("POST", "/authors/A1")
    with pytest.raises(ValueError, match="read-only"):
        source._request("GET", "/users/me")


def test_source_keys_cannot_enter_public_queries_or_provenance(store, clock):
    source = oa(lambda r: pytest.fail("key-bearing query must not dispatch"), store, clock)
    with pytest.raises(ValueError, match="public query data"):
        next(source.search("biology fixture-secret"))


def test_epo_mixed_xml_text_preserves_gene_names_and_surrounding_claims(store, clock):
    xml = b'<ops:world-patent-data xmlns:ops="http://ops.epo.org"><exchange-document country="EP" doc-number="1" kind="A1"><abstract><p>Genome <b>editing</b> evidence.</p></abstract></exchange-document></ops:world-patent-data>'
    source = epo(lambda r: httpx.Response(200, content=xml), store, clock)
    paragraph = source.fetch("EP.1.A1").records[0]["data"]["abstract"][0]["p"][0]
    assert paragraph["text"] == "Genome "
    assert paragraph["b"][0]["text"] == "editing"
    assert paragraph["b"][0]["tail"] == " evidence."


def test_normalized_paths_cannot_escape_read_only_source_routes(store, clock):
    source = oa(lambda r: pytest.fail("path escape must not dispatch"), store, clock)
    with pytest.raises(ValueError, match="read-only"):
        source._request("GET", "/works/../../users/me")
