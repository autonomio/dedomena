"""Regression contracts for EPO review: identity, traversal coverage and XML order."""
import json

import httpx
import pytest

from dedomena.sources import EPO, InvalidResponse, Store
from dedomena.sources.epo import local


class Clock:
    def __init__(self):
        self.now = 1_790_812_800.0

    def __call__(self):
        return self.now

    def sleep(self, amount):
        self.now += amount


@pytest.fixture
def source_factory():
    clock = Clock()
    store = Store(clock=clock)
    sources = []

    def create(handler):
        def wrapped(request):
            if request.url.path == "/3.2/auth/accesstoken":
                return httpx.Response(200, json={"access_token": "fixture-token", "expires_in": "1199"})
            return handler(request)
        source = EPO("fixture-key", "fixture-secret", store=store, sleep=clock.sleep,
                     client=httpx.Client(transport=httpx.MockTransport(wrapped)))
        sources.append(source)
        return source

    yield create
    for source in sources:
        source.close()
    store.close()


def fulltext(*attributes):
    documents = "".join(
        "<fulltext-document " + " ".join(f'{key}="{value}"' for key, value in attrs.items()) +
        "><claims><claim>Genome evidence</claim></claims></fulltext-document>"
        for attrs in attributes)
    return httpx.Response(200, content=("<world-patent-data>" + documents + "</world-patent-data>").encode())


IDENTITY = {"country": "EP", "doc-number": "1000000", "kind": "A1"}


@pytest.mark.parametrize("identifier,format", [
    ("EP.1000000.A1", "docdb"), ("EP1000000.A1", "epodoc"), ("EP1000000", "epodoc")])
def test_fulltext_matching_envelope_is_returned_and_replayable(source_factory, identifier, format):
    source = source_factory(lambda request: fulltext(IDENTITY))
    response = source.full_text(identifier, format=format)
    assert response.body == source.store.replay(response.provenance.snapshot_id).body


@pytest.mark.parametrize("change", [
    {"country": "US"}, {"doc-number": "2000000"}, {"kind": "B1"}])
def test_epodoc_fulltext_rejects_foreign_envelope(source_factory, change):
    source = source_factory(lambda request: fulltext({**IDENTITY, **change}))
    with pytest.raises(InvalidResponse, match="different patent"):
        source.full_text("EP1000000.A1", format="epodoc")


@pytest.mark.parametrize("missing", ["country", "doc-number", "kind"])
def test_epodoc_fulltext_rejects_missing_envelope_identity(source_factory, missing):
    source = source_factory(lambda request: fulltext({k: v for k, v in IDENTITY.items() if k != missing}))
    with pytest.raises(InvalidResponse, match="different patent"):
        source.full_text("EP1000000", format="epodoc")


def test_epodoc_fulltext_checks_every_document(source_factory):
    source = source_factory(lambda request: fulltext(IDENTITY, {**IDENTITY, "doc-number": "2000000"}))
    with pytest.raises(InvalidResponse, match="different patent"):
        source.full_text("EP1000000", format="epodoc")


def search_response(ids, begin, end):
    documents = "".join(
        f'<exchange-document country="EP" doc-number="{number}" kind="A1"/>' for number in ids)
    xml = (f'<world-patent-data><biblio-search total-result-count="4">'
           f'<range begin="{begin}" end="{end}"/><search-result>{documents}'
           '</search-result></biblio-search></world-patent-data>')
    return httpx.Response(200, content=xml.encode())


def test_epo_underfilled_earlier_range_keeps_terminal_page_incomplete(source_factory):
    def handler(request):
        if request.url.params["Range"] == "1-2":
            return search_response([1], 1, 2)
        assert request.url.params["Range"] == "3-4"
        return search_response([3, 4], 3, 4)
    source = source_factory(handler)
    pages = list(source.search("ta=biology", page_size=2, allow_partial=True))
    assert [page.complete for page in pages] == [False, False]
    assert [page.next_cursor for page in pages] == ["3", None]
    assert [record["id"] for page in pages for record in page.records] == ["EP.1.A1", "EP.3.A1", "EP.4.A1"]
    assert pages[-1].warnings == pages[0].warnings
    assert "range 1-2" in pages[-1].warnings[0]
    with pytest.raises(InvalidResponse, match="fewer patent records"):
        list(source.search("ta=biology", page_size=2))


def test_epo_complete_ranges_still_mark_terminal_page_complete(source_factory):
    def handler(request):
        return search_response([1, 2], 1, 2) if request.url.params["Range"] == "1-2" else search_response([3, 4], 3, 4)
    pages = list(source_factory(handler).search("ta=biology", page_size=2, allow_partial=True))
    assert [page.complete for page in pages] == [False, True]
    assert pages[-1].warnings == ()


def reconstruct_text(value):
    # A consumer can reconstruct mixed XML text after JSON serialization using
    # the compact order references, without relying on dictionary insertion order.
    result = value["text"]
    for descriptor in value.get("child_order", []):
        child = value[local(descriptor["tag"])][descriptor["index"]]
        result += reconstruct_text(child) + child.get("tail", "")
    return result


def test_epo_mixed_children_reconstruct_original_sequence_from_json(source_factory):
    xml = (b'<world-patent-data><exchange-document country="EP" doc-number="1000000" kind="A1">'
           b'<abstract><p>Start <b>A</b>, <i>B <b>nested</b></i>, <b>C</b> end.</p></abstract>'
           b'</exchange-document></world-patent-data>')
    page = source_factory(lambda request: httpx.Response(200, content=xml)).fetch("EP.1000000.A1")
    paragraph = json.loads(json.dumps(page.records[0]["data"]))["abstract"][0]["p"][0]
    assert reconstruct_text(paragraph) == "Start A, B nested, C end."
    assert paragraph["child_order"] == [{"tag": "b", "index": 0}, {"tag": "i", "index": 0}, {"tag": "b", "index": 1}]
    # Grouped access remains compatible with the original record structure.
    assert [child["text"] for child in paragraph["b"]] == ["A", "C"]


def test_epo_child_order_retains_qualified_tags(source_factory):
    xml = (b'<world-patent-data xmlns:a="urn:a" xmlns:b="urn:b">'
           b'<exchange-document country="EP" doc-number="1000000" kind="A1">'
           b'<abstract><p><a:em>A</a:em><b:em>B</b:em><a:em>C</a:em></p></abstract>'
           b'</exchange-document></world-patent-data>')
    page = source_factory(lambda request: httpx.Response(200, content=xml)).fetch("EP.1000000.A1")
    paragraph = page.records[0]["data"]["abstract"][0]["p"][0]
    assert reconstruct_text(paragraph) == "ABC"
    assert [item["tag"] for item in paragraph["child_order"]] == ["{urn:a}em", "{urn:b}em", "{urn:a}em"]


@pytest.mark.parametrize("kind,identifier", [
    ("A1", "EP1000000"), ("B1", "EP1000000"), ("B1", "EP1000000B"),
    ("C", "EP1000000C"), ("U", "EP1000000U"), ("Y1", "EP1000000Y.Y1")])
def test_epodoc_fulltext_accepts_documented_kind_letter_forms(source_factory, kind, identifier):
    source = source_factory(lambda request: fulltext({**IDENTITY, "kind": kind}))
    assert source.full_text(identifier, format="epodoc").body


@pytest.mark.parametrize("kind,identifier", [("A1", "EP1000000A"), ("U", "EP1000000"), ("Y1", "EP1000000U")])
def test_epodoc_fulltext_rejects_wrong_or_missing_required_kind_letter(source_factory, kind, identifier):
    source = source_factory(lambda request: fulltext({**IDENTITY, "kind": kind}))
    with pytest.raises(InvalidResponse, match="different patent"):
        source.full_text(identifier, format="epodoc")
