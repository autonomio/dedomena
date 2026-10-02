import json

import httpx
import pytest

from dedomena.sources.core import InvalidResponse, Store, Transport
from dedomena.sources.ecb import ECB


CSV = ("KEY,FREQ,CURRENCY,CURRENCY_DENOM,TIME_PERIOD,OBS_VALUE,UNIT,UNIT_MULT,OBS_STATUS\r\n"
       "EXR.M.USD.EUR.SP00.A,M,USD,EUR,2025-01,1.0353727272727,USD,0,A\r\n"
       "EXR.M.GBP.EUR.SP00.A,M,GBP,EUR,2025-01,0.8390809090909,GBP,0,A\r\n")


def source(handler):
    return ECB(client=httpx.Client(transport=httpx.MockTransport(handler)),
               store=Store(), sleep=lambda _: None)


def test_fx_batch_preserves_precision_units_and_query_receipt():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.url.path == "/service/data/EXR/M.USD+GBP.EUR.SP00.A"
        assert request.url.params["startPeriod"] == "2025-01"
        assert request.url.params["endPeriod"] == "2025-03"
        assert request.url.params["detail"] == "full"
        return httpx.Response(200, text=CSV, headers={"ETag": '"edition-1"'})
    with source(handler) as ecb:
        page = ecb.fx(["USD", "GBP", "USD"], frequency="M",
                      start_period="2025-01", end_period="2025-03")
        assert page.complete and page.total == 2
        assert page.records[0]["OBS_VALUE"] == "1.0353727272727"
        assert page.records[0]["UNIT_MULT"] == "0"
        assert ecb.store.replay(page.provenance.snapshot_id).body == CSV.encode()
        cached = ecb.fx(["USD", "GBP"], frequency="M",
                        start_period="2025-01", end_period="2025-03")
        assert cached.cache_hit and len(calls) == 1
        assert ecb.store.replay(page.provenance.snapshot_id).headers["etag"] == '"edition-1"'


def test_revision_delta_and_history_are_explicit():
    def handler(request):
        assert request.url.params["includeHistory"] == "true"
        assert request.url.params["updatedAfter"] == "2025-01-01T00:00:00Z"
        assert request.url.params["lastNObservations"] == "2"
        return httpx.Response(200, text=CSV)
    with source(handler) as ecb:
        page = ecb.observations("EXR", "M..EUR.SP00.A", include_history=True,
                                updated_after="2025-01-01T00:00:00Z", last_n=2)
        assert len(page.warnings) == 4


def test_discovery_retains_series_attributes_without_values():
    def handler(request):
        assert request.url.params["detail"] == "nodata"
        return httpx.Response(200, text="KEY,UNIT,UNIT_MULT\r\nEXR.M.USD.EUR.SP00.A,USD,0\r\n")
    with source(handler) as ecb:
        page = ecb.series("EXR", "M..EUR.SP00.A")
        assert page.records[0]["UNIT"] == "USD"
        assert "OBS_VALUE" not in page.records[0]


@pytest.mark.parametrize("body", [
    "KEY,TIME_PERIOD,OBS_VALUE\nEXR.M.USD,2025-01\n",
    "KEY,TIME_PERIOD,OBS_VALUE\nOTHER.M.USD,2025-01,1\n",
    "KEY,TIME_PERIOD,OBS_VALUE\nEXR.M.USD,,1\n",
    "KEY,TIME_PERIOD,OBS_VALUE\nEXR.M.USD,2025-01,1,extra\n",
    "KEY,KEY,TIME_PERIOD,OBS_VALUE\nEXR.M.USD,EXR.M.USD,2025-01,1\n",
    '{"error":"unexpected format"}',
])
def test_malformed_and_wrong_dataset_fail(body):
    with source(lambda _: httpx.Response(200, text=body)) as ecb:
        with pytest.raises(InvalidResponse):
            ecb.fetch("EXR/M.USD.EUR.SP00.A")


@pytest.mark.parametrize("kwargs", [
    {"flow": "../users", "key": ""},
    {"flow": "EXR", "key": ".."},
    {"flow": "EXR", "key": "D.USD/EUR"},
    {"flow": "EXR", "key": "", "updated_after": "2025-01-01"},
    {"flow": "EXR", "key": "", "start_period": "2025-02-30"},
    {"flow": "EXR", "key": "", "start_period": "2025-03", "end_period": "2025-01"},
    {"flow": "EXR", "key": "", "include_history": "false"},
])
def test_invalid_queries_fail_before_network(kwargs):
    with source(lambda _: pytest.fail("unexpected network")) as ecb:
        with pytest.raises(ValueError):
            ecb.observations(**kwargs)


def test_metadata_xml_keeps_source_ids_names_and_raw_snapshot():
    xml = ('<s:Structure xmlns:s="urn:sdmx"><s:Dataflows>'
           '<s:Dataflow id="EXR" agencyID="ECB" version="1.0">'
           '<s:Name xml:lang="en">Exchange rates</s:Name></s:Dataflow>'
           '</s:Dataflows></s:Structure>')
    with source(lambda _: httpx.Response(200, text=xml)) as ecb:
        page = ecb.dataflows()
        assert page.records[0]["id"] == "EXR"
        assert page.records[0]["names"][0]["text"] == "Exchange rates"
        assert ecb.store.replay(page.provenance.snapshot_id).body == xml.encode()


def test_fred_query_key_is_sent_but_never_archived():
    secret = "a" * 32
    def handler(request):
        assert request.url.params["api_key"] == secret
        return httpx.Response(200, json={"observations": []})
    with Transport("fred", "https://api.stlouisfed.org/fred", credential=secret,
                   client=httpx.Client(transport=httpx.MockTransport(handler)),
                   store=Store(), sleep=lambda _: None) as fred:
        response = fred._request("GET", "/series/observations", params={"series_id": "GDP"},
                                 private_params={"api_key": secret})
        assert secret not in json.dumps(response.provenance.to_dict())
        assert "api_key" not in response.provenance.parameters
        assert secret.encode() not in fred.store.replay(response.provenance.snapshot_id).body
        with pytest.raises(ValueError):
            fred._request("GET", "/series/observations", private_params={"api_key": "b" * 32})


def test_query_credentials_are_not_a_general_transport_escape_hatch():
    with source(lambda _: pytest.fail("unexpected network")) as ecb:
        with pytest.raises(ValueError):
            ecb._request("GET", "/data/EXR", private_params={"api_key": ""})
        with pytest.raises(ValueError):
            ecb._request("GET", "/data/EXR/../../users")


def test_empty_ecb_interval_has_a_receipt_and_is_complete():
    with source(lambda _: httpx.Response(200, content=b"")) as ecb:
        page = ecb.fx("USD", start_period="2099-01-01", end_period="2099-01-05")
        assert page.complete and page.total == 0 and page.warnings
        assert page.provenance.http_status == 200
        assert ecb.store.replay(page.provenance.snapshot_id).body == b""


def test_exact_series_query_rejects_another_currency():
    with source(lambda _: httpx.Response(200, text=CSV)) as ecb:
        with pytest.raises(InvalidResponse):
            ecb.fx("EUR", frequency="M")


def test_fred_private_key_is_redacted_from_httpx_info_logs(caplog):
    import logging
    secret = "c" * 32
    caplog.set_level(logging.INFO, logger="httpx")
    with Transport("fred", "https://api.stlouisfed.org/fred", credential=secret,
                   store=Store(), client=httpx.Client(transport=httpx.MockTransport(
                       lambda _: httpx.Response(200, json={"observations": []})))) as fred:
        fred._request("GET", "/series/observations",
                      params={"series_id": "GDP"}, private_params={"api_key": secret})
    assert "[REDACTED]" in caplog.text
    assert secret not in caplog.text


def test_fred_logging_redaction_survives_parallel_keys(caplog):
    from concurrent.futures import ThreadPoolExecutor
    import logging
    caplog.set_level(logging.INFO, logger="httpx")
    keys = [str(index).zfill(32) for index in range(20)]
    def request(secret):
        with Transport("fred", "https://api.stlouisfed.org/fred", credential=secret,
                       store=Store(), client=httpx.Client(transport=httpx.MockTransport(
                           lambda _: httpx.Response(200, json={"observations": []})))) as fred:
            fred._request("GET", "/series/observations", private_params={"api_key": secret})
    with ThreadPoolExecutor(max_workers=10) as workers:
        list(workers.map(request, keys))
    assert all(key not in caplog.text for key in keys)
    assert caplog.text.count("[REDACTED]") == len(keys)


@pytest.mark.parametrize("secret", ["s" + "1" * 31, "1" * 15 + "s" + "1" * 16])
def test_httpx_redaction_replaces_entire_parameter_and_keeps_url_shape(secret):
    import logging
    from dedomena.sources.core import _PrivateQueryLogFilter
    record = logging.LogRecord("httpx", logging.INFO, "", 0,
                               'HTTP Request: GET %s "HTTP/1.1 200 OK"',
                               ("https://api.stlouisfed.org/fred/series?api_key="
                                + secret + "&series_id=GDP",), None)
    assert _PrivateQueryLogFilter().filter(record)
    assert record.getMessage() == (
        'HTTP Request: GET https://api.stlouisfed.org/fred/series?api_key='
        '[REDACTED]&series_id=GDP "HTTP/1.1 200 OK"')
