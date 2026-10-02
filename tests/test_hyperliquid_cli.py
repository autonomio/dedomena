"""Public market operations, route configuration and offline replay in JSON CLI."""
import json

import httpx
import pytest

from dedomena.sources import Hyperliquid, IPPool, IPRoute, Store
from dedomena.sources import __main__ as cli


@pytest.fixture
def market_cli(monkeypatch):
    store, requests = Store(), []
    def handler(request):
        body = json.loads(request.content)
        requests.append(body)
        kind = body["type"]
        if kind == "allMids":
            data = {"BTC": "100.00000001"}
        elif kind in ("metaAndAssetCtxs", "spotMetaAndAssetCtxs"):
            data = [{"universe": [{"name": "BTC"}], "tokens": []}, [{"markPx": "100.1"}]]
        elif kind == "l2Book":
            data = {"coin": body["coin"], "time": 1234, "levels": [[], []]}
        elif kind == "candleSnapshot":
            data = [{"s": "BTC", "i": "1h", "t": 0, "T": 3599999, "n": 2,
                     "o": "100.01", "h": "101", "l": "99", "c": "100.02", "v": "1.1"}]
        else:
            assert kind == "fundingHistory"
            data = [{"coin": "BTC", "time": 1234, "fundingRate": "0.0001", "premium": "0.001"}]
        return httpx.Response(200, json=data)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    def constructor(**options):
        options.pop("store", None)
        if "ip_pool" not in options:
            options["client"] = client
        return Hyperliquid(store=store, **options)
    monkeypatch.setattr(cli, "Hyperliquid", constructor)
    yield requests, client, store
    client.close()
    store.close()


@pytest.mark.parametrize("args,kind,complete", [
    (["mids", "hyperliquid", "--dex", "xyz"], "allMids", True),
    (["markets", "hyperliquid"], "metaAndAssetCtxs", True),
    (["catalogue", "hyperliquid", "--spot"], "spotMetaAndAssetCtxs", True),
    (["book", "hyperliquid", "BTC"], "l2Book", True),
    (["candles", "hyperliquid", "BTC", "--interval", "1h", "--start-time", "0",
      "--end-time", "3600000"], "candleSnapshot", False),
    (["funding", "hyperliquid", "BTC", "--start-time", "0", "--end-time", "3600000"],
     "fundingHistory", True),
])
def test_market_operations_emit_native_page_and_truthful_receipt(market_cli, capsys, args, kind, complete):
    assert cli.main(args) == 0
    requests, _, _ = market_cli
    page, receipt = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert requests[0]["type"] == kind
    assert page["provenance"]["method"] == "POST"
    assert json.loads(page["provenance"]["request_body"]) == requests[0]
    assert page["complete"] == receipt["complete"] == complete
    assert receipt["usage"]["ip_weight_used"] == (2 if kind in ("allMids", "l2Book") else
                                                   21 if kind in ("candleSnapshot", "fundingHistory") else 20)


def test_fetch_alias_and_benchmark_mids(market_cli, capsys):
    assert cli.main(["fetch", "hyperliquid", "BTC"]) == 0
    page = json.loads(capsys.readouterr().out)
    assert page["records"][0]["coin"] == "BTC"
    assert cli.main(["benchmark", "hyperliquid", "--operation", "mids"]) == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["operation"] == "mids" and receipt["complete"]
    assert receipt["records"] == 1


def test_route_file_configures_shared_pool_and_receipt(tmp_path, monkeypatch, market_cli, capsys):
    _, client, _ = market_cli
    configured, pools = [], []
    def pool_factory(routes):
        routes = list(routes)
        configured.extend(routes)
        pool = IPPool([IPRoute(route.public_ip, client=client) for route in routes])
        pools.append(pool)
        return pool
    monkeypatch.setattr(cli, "IPPool", pool_factory)
    path = tmp_path / "routes.json"
    path.write_text(json.dumps([{"public_ip": "203.0.113.1", "local_address": "10.0.0.1"}]))
    assert cli.main(["mids", "hyperliquid", "--ip-routes", str(path)]) == 0
    page = json.loads(capsys.readouterr().out.splitlines()[0])
    assert configured[0].local_address == "10.0.0.1"
    assert page["provenance"]["egress_ip_sha256"] is not None
    assert pools[0]._closed and not client.is_closed


@pytest.mark.parametrize("contents", ["bad JSON", "{}", "[]",
    '[{"public_ip":"203.0.113.1","client":"not a client"}]',
    '[{"public_ip":"203.0.113.1","proxy":"http://user:secret@proxy.invalid/?bad"}]',
])
def test_bad_route_file_is_structured_and_never_dispatches(tmp_path, market_cli, capsys, contents):
    path = tmp_path / "routes.json"
    path.write_text(contents)
    assert cli.main(["mids", "hyperliquid", "--ip-routes", str(path)]) == 2
    output = capsys.readouterr()
    assert not output.out and not market_cli[0]
    assert json.loads(output.err)["error"] == "ValueError"
    assert "secret" not in output.err


def test_missing_route_file_is_structured(tmp_path, market_cli, capsys):
    assert cli.main(["mids", "hyperliquid", "--ip-routes", str(tmp_path / "absent")]) == 2
    assert not market_cli[0]
    assert json.loads(capsys.readouterr().err)["error"] == "ValueError"


def test_hyperliquid_replay_needs_no_routes(tmp_path, capsys):
    path = tmp_path / "saved.sqlite3"
    store = Store(path)
    client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"BTC": "100.00001"})))
    with Hyperliquid(store=store, client=client) as source:
        page = source.all_mids()
    store.close()
    client.close()
    assert cli.main(["replay", "hyperliquid", page.provenance.snapshot_id,
                     "--store", str(path), "--ip-routes", "not-needed.json"]) == 0
    replay = json.loads(capsys.readouterr().out)
    assert json.loads(replay["body"]) == {"BTC": "100.00001"}
