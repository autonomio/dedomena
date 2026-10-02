import json

import httpx

from dedomena.sources import Store
from dedomena.sources.core import Transport
from dedomena.sources import __main__ as cli


def test_missing_finance_credentials_are_structured(monkeypatch, capsys):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    assert cli.main(["observations", "fred", "GDP"]) == 2
    output = capsys.readouterr()
    assert not output.out
    error = json.loads(output.err)
    assert error["error"] == "ValueError" and not error["complete"]
    assert "Traceback" not in output.err


def test_fred_replay_does_not_require_current_credentials(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FRED_API_KEY", raising=False)
    path = tmp_path / "sources.sqlite3"
    store = Store(path)
    with Transport("fred", "https://api.stlouisfed.org/fred", credential="a" * 32,
                   store=store, client=httpx.Client(transport=httpx.MockTransport(
                       lambda _: httpx.Response(200, json={"observations": []})))) as source:
        response = source._request("GET", "/series/observations",
                                   params={"series_id": "GDP"},
                                   private_params={"api_key": "a" * 32})
    store.close()
    assert cli.main(["replay", "fred", response.provenance.snapshot_id,
                     "--store", str(path)]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["provenance"]["source"] == "fred"
    assert json.loads(receipt["body"]) == {"observations": []}
    assert cli.main(["replay", "sec", response.provenance.snapshot_id,
                     "--store", str(path)]) == 2
    assert not json.loads(capsys.readouterr().err)["complete"]


def test_ecb_observation_flags_and_benchmark_receipt(monkeypatch, capsys):
    from dedomena.sources import ECB
    clock = [0.0]
    monkeypatch.setattr(cli.time, "perf_counter", lambda: clock[0])
    def constructor(**kwargs):
        def handler(request):
            clock[0] += 2
            assert request.url.params["lastNObservations"] == "2"
            assert request.url.params["startPeriod"] == "2025-01"
            assert request.url.params["endPeriod"] == "2025-03"
            return httpx.Response(200, text=(
                "KEY,TIME_PERIOD,OBS_VALUE,UNIT,UNIT_MULT\n"
                "EXR.M.USD.EUR.SP00.A,2025-03,1.08,USD,0\n"))
        return ECB(store=Store(), client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(cli, "ECB", constructor)
    assert cli.main(["benchmark", "ecb", "EXR/M.USD.EUR.SP00.A",
                     "--operation", "observations", "--last-n", "2",
                     "--start-period", "2025-01", "--end-period", "2025-03"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["elapsed_seconds"] == 2.0
    assert receipt["operation"] == "observations"
    assert receipt["complete"] and receipt["records"] == 1
    assert "records_seen" in receipt
    assert "records" not in receipt.get("provenance", {})


def test_release_benchmark_uses_native_bulk_endpoint(monkeypatch, capsys):
    from dedomena.sources import FRED
    def constructor(**kwargs):
        def handler(request):
            assert request.url.path == "/fred/v2/release/observations"
            assert request.url.params["release_id"] == "53"
            return httpx.Response(200, json={
                "release": {"release_id": 53}, "series": [], "has_more": False})
        return FRED("a" * 32, store=Store(),
                    client=httpx.Client(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(cli, "FRED", constructor)
    assert cli.main(["benchmark", "fred", "53", "--operation", "release"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["operation"] == "release" and receipt["complete"]
