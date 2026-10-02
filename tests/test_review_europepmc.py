"""Review regressions for PMC aliases and explicit source options."""
import httpx
import pytest

from dedomena.sources import EuropePMC, Store


def source(handler):
    return EuropePMC(store=Store(), client=httpx.Client(transport=httpx.MockTransport(handler)),
                     sleep=lambda _: None)


@pytest.mark.parametrize("identifier", ["PMC123", "PMC:123", "PMC:PMC123"])
def test_exact_pmc_aliases_request_and_verify_canonical_pmcid(identifier):
    def handler(request):
        assert request.url.params["query"] == "PMCID:PMC123"
        return httpx.Response(200, json={"hitCount": 1, "resultList": {"result": [
            {"id": "123", "source": "PMC", "pmcid": "PMC123"}]}})
    with source(handler) as europepmc:
        page = europepmc.fetch(identifier)
        assert page.complete and page.records[0]["pmcid"] == "PMC123"


def test_mixed_pmc_alias_batch_does_not_claim_valid_identifiers_unresolved():
    def handler(request):
        assert request.url.params["query"].count("PMCID:PMC123") == 2
        return httpx.Response(200, json={"hitCount": 1, "resultList": {"result": [
            {"id": "123", "source": "PMC", "pmcid": "PMC123"}]}})
    with source(handler) as europepmc:
        page, = europepmc.fetch_many(["PMC:123", "PMC:PMC123"])
        assert page.complete and not page.unresolved


@pytest.mark.parametrize("kind", ["", 0, False])
def test_explicit_invalid_result_type_is_rejected_before_network(kind):
    with source(lambda _: pytest.fail("unexpected network")) as europepmc:
        with pytest.raises(ValueError):
            list(europepmc.search("biology", result_type=kind))


def test_explicit_none_retains_default_result_type():
    def handler(request):
        assert request.url.params["resultType"] == "lite"
        return httpx.Response(200, json={"hitCount": 0, "resultList": {"result": []}})
    with source(handler) as europepmc:
        page, = europepmc.search("biology", result_type=None)
        assert page.complete


def test_invalid_batch_option_is_rejected_even_for_empty_input():
    with source(lambda _: pytest.fail("unexpected network")) as europepmc:
        with pytest.raises(ValueError):
            list(europepmc.fetch_many([], result_type=""))


def test_invalid_pmc_alias_cannot_be_forwarded_as_another_source_identifier():
    with pytest.raises(ValueError):
        EuropePMC._id("PMC:ABC")
