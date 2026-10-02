"""Regressions for explicit field validation and persisted singleton billing."""
from datetime import datetime, timezone

import httpx
import pytest

from dedomena.sources import InvalidResponse, OpenAlex, Store


@pytest.fixture
def store():
    value = Store()
    yield value
    value.close()


def make_source(handler, store):
    return OpenAlex("", store=store,
                    client=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize("billing", ["1", "-1", "not-a-number"])
def test_exact_lookup_refuses_original_billing_across_cache_and_restart(tmp_path, billing):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"id": "https://openalex.org/W1"},
                              headers={"X-RateLimit-Credits-Used": billing})

    path = tmp_path / "sources.sqlite3"
    first_store = Store(path)
    try:
        source = make_source(handler, first_store)
        with pytest.raises(InvalidResponse, match="lookup.*(charged|billing)"):
            source.fetch("W1")
        with pytest.raises(InvalidResponse, match="lookup.*(charged|billing)"):
            source.fetch("W1")
    finally:
        first_store.close()
    reopened_store = Store(path)
    try:
        source = make_source(handler, reopened_store)
        with pytest.raises(InvalidResponse, match="lookup.*(charged|billing)"):
            source.fetch("W1")
    finally:
        reopened_store.close()
    assert len(requests) == 1


@pytest.mark.parametrize("method", ["search", "fetch", "fetch_doi", "fetch_many", "fetch_batch"])
@pytest.mark.parametrize("fields", ["", " ", False, 0, []])
def test_explicit_invalid_fields_fail_before_any_network_request(method, fields, store):
    requests = []

    def handler(request):
        requests.append(request)
        raise AssertionError("field validation must precede every upstream request")

    source = make_source(handler, store)
    with pytest.raises(ValueError, match="fields"):
        if method == "search":
            list(source.search("biology", fields=fields))
        elif method == "fetch":
            source.fetch("W1", fields=fields)
        elif method == "fetch_doi":
            source.fetch("10.1234/test", fields=fields)
        elif method == "fetch_many":
            list(source.fetch_many([], fields=fields))
        else:
            list(source.fetch_batch([], fields=fields))
    assert requests == []


def test_none_fields_uses_default_and_zero_billing_cache_remains_usable(store):
    requests = []

    def handler(request):
        requests.append(request)
        assert request.url.params["select"] == "id,doi,title,publication_year,ids,type"
        return httpx.Response(200, json={"id": "https://openalex.org/W1"},
                              headers={"X-RateLimit-Credits-Used": "0"})

    source = make_source(handler, store)
    fresh = source.fetch("W1", fields=None)
    cached = source.fetch("W1", fields=None)
    assert fresh.credits_used == cached.credits_used == 0
    assert cached.cache_hit and cached.records == fresh.records
    assert len(requests) == 1


def test_exact_doi_fields_must_retain_doi_before_network(store):
    source = make_source(lambda request: pytest.fail("unexpected request"), store)
    with pytest.raises(ValueError, match="exact identity"):
        source.fetch("10.1234/test", fields="id,title")


def test_quota_json_observation_retains_request_day_across_midnight():
    class Clock:
        now = datetime(2026, 10, 1, 23, 59, 59, tzinfo=timezone.utc).timestamp()

        def __call__(self):
            return self.now

    clock = Clock()
    value = Store(clock=clock)

    def handler(request):
        assert request.url.path == "/rate-limit"
        clock.now += 2
        return httpx.Response(200, json={"rate_limit": {
            "credits_remaining": 9990, "credits_limit": 10000}})

    try:
        source = make_source(handler, value)
        source.quota()
        assert value.used(source.scope, "credits", "day") == 0
        clock.now -= 2
        assert value.used(source.scope, "credits", "day") == 10
    finally:
        value.close()
