import json

import httpx
import pytest

from dedomena.sources.core import InvalidResponse, SearchLimitExceeded, Store, canonical
from dedomena.sources.hyperliquid import Hyperliquid, info_policy


BOOK = {"coin": "BTC", "time": 1000,
        "levels": [[{"px": "113377.0123456789", "sz": "7.6699", "n": 17}], []]}
META = [{"universe": [{"name": "BTC", "szDecimals": 5}],
         "marginTables": [[50, {"marginTiers": [{"lowerBound": "0.0", "maxLeverage": 50}]}]],
         "collateralToken": 0},
        [{"markPx": "113377.0123456789", "premium": None, "impactPxs": None, "midPx": None}]]
CANDLE = {"s": "BTC", "i": "1m", "t": 1000, "T": 60999, "n": 1,
          "o": "100.0123456789", "h": "100.3", "l": "99.9", "c": "100.1", "v": "0.00123"}


def source(handler, **kwargs):
    return Hyperliquid(client=httpx.Client(transport=httpx.MockTransport(handler)),
                       store=kwargs.pop("store", Store()), sleep=lambda _: None, **kwargs)


def funding(time, rate="0.0000125"):
    return {"coin": "BTC", "time": time, "fundingRate": rate, "premium": "-0.00031774"}


def test_book_preserves_native_precision_receipt_and_replay():
    calls = []
    def handler(request):
        calls.append(request)
        assert request.method == "POST"
        assert request.url == "https://api.hyperliquid.xyz/info"
        assert json.loads(request.content) == {"type": "l2Book", "coin": "BTC", "nSigFigs": 5, "mantissa": 2}
        return httpx.Response(200, json=BOOK)
    with source(handler, cache_ttl=86400) as liquid:
        page = liquid.order_book("BTC", n_sig_figs=5, mantissa=2)
        assert page.complete and page.total == 1
        assert page.records == (BOOK,)
        assert page.records[0]["levels"][0][0]["px"] == "113377.0123456789"
        assert json.loads(page.provenance.request_body)["mantissa"] == 2
        assert json.loads(liquid.store.replay(page.provenance.snapshot_id).body) == BOOK
        again = liquid.order_book("BTC", n_sig_figs=5, mantissa=2)
        assert again.cache_hit and len(calls) == 1


def test_metadata_retains_margin_tables_nulls_and_context_positions():
    with source(lambda _: httpx.Response(200, json=META)) as liquid:
        page = liquid.markets()
        assert page.records == ({"meta": META[0], "assetCtxs": META[1]},)
        assert page.records[0]["assetCtxs"][0]["premium"] is None
        assert page.complete and page.warnings


def test_spot_contexts_are_not_zipped_or_truncated_to_pair_universe():
    data = [{"universe": [{"name": "PURR/USDC", "tokens": [1, 0], "index": 0}],
             "tokens": [{"name": "USDC", "index": 0}, {"name": "PURR", "index": 1}]},
            [{"coin": "PURR/USDC", "midPx": "0.15"}, {"coin": "@999", "midPx": None}]]
    def handler(request):
        assert json.loads(request.content) == {"type": "spotMetaAndAssetCtxs"}
        return httpx.Response(200, json=data)
    with source(handler) as liquid:
        page = liquid.markets(spot=True)
        assert len(page.records[0]["assetCtxs"]) == 2
        assert page.records[0]["meta"]["tokens"] == data[0]["tokens"]


def test_all_mids_is_one_native_map_including_spot_and_prediction_asset_ids():
    data = {"BTC": "113377.0123456789", "@107": "36.456", "#14720": "0.385"}
    with source(lambda _: httpx.Response(200, json=data)) as liquid:
        page = liquid.all_mids()
        assert page.records == (data,)
        assert page.complete and page.records_seen == 1


def test_candles_preserve_precision_and_never_claim_complete_history():
    def handler(request):
        assert json.loads(request.content) == {"type": "candleSnapshot", "req": {
            "coin": "BTC", "interval": "1m", "startTime": 1000, "endTime": 2000}}
        return httpx.Response(200, json=[CANDLE])
    with source(handler) as liquid:
        page = liquid.candles("BTC", "1m", 1000, 2000)
        assert page.records == (CANDLE,)
        assert not page.complete and page.next_cursor is None
        assert any("5,000" in warning for warning in page.warnings)


def test_empty_candles_do_not_prove_historical_coverage():
    with source(lambda _: httpx.Response(200, json=[])) as liquid:
        assert not liquid.candles("BTC", "1d", 0, 1000).complete


def test_funding_pagination_inclusive_overlap_and_count_are_exact():
    calls = []
    first = [funding(time) for time in range(1, 501)]
    second = [funding(500), funding(501)]
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert body["endTime"] == 1000
        return httpx.Response(200, json=first if body["startTime"] == 1 else second)
    with source(handler) as liquid:
        pages = list(liquid.funding_history("BTC", 1, 1000))
        assert len(pages) == 2
        assert len(pages[0].records) == 500 and not pages[0].complete
        assert pages[1].records == (funding(501),)
        assert pages[1].complete and pages[1].records_seen == 501
        assert pages[1].next_cursor is None
        assert [body["startTime"] for body in calls] == [1, 500]
        assert liquid.usage()["upstream_records"] == 502
        assert liquid.usage()["records_delivered"] == 501
        assert liquid.usage()["boundary_repeats"] == 1


def test_funding_checkpoint_pins_default_end_and_deduplicates_resume():
    store = Store(clock=lambda: 1.0)
    bodies = []
    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        return httpx.Response(200, json=[funding(time) for time in range(1, 501)]
                              if body["startTime"] == 1 else [funding(500), funding(501)])
    with source(handler, store=store) as liquid:
        page = next(liquid.funding_history("BTC", 1))
        store.clock = lambda: 2.0
        resumed = list(liquid.funding_history("BTC", 1, cursor=page.next_cursor))
        assert resumed[0].records == (funding(501),)
        assert resumed[0].records_seen == 501
        assert resumed[0].complete
        assert bodies[-1]["endTime"] == 1000
        with pytest.raises(ValueError):
            list(liquid.funding_history("ETH", 1, cursor=page.next_cursor))
        with pytest.raises(ValueError):
            list(liquid.funding_history("BTC", 2, cursor=page.next_cursor))
        with pytest.raises(ValueError):
            list(liquid.funding_history("BTC", 1, 2000, cursor=page.next_cursor))


def test_full_same_timestamp_funding_fails_before_partial_page_is_yielded():
    rows = [funding(1, str(index)) for index in range(500)]
    with source(lambda _: httpx.Response(200, json=rows)) as liquid:
        with pytest.raises(SearchLimitExceeded):
            next(liquid.funding_history("BTC", 1, 1000))
        assert liquid.usage().get("records_delivered", 0) == 0


def test_multiple_distinct_boundary_rows_are_deduplicated_without_losing_new_row():
    first = [funding(time) for time in range(1, 499)] + [funding(499, "0.1"), funding(499, "0.2")]
    second = [funding(499, "0.1"), funding(499, "0.2"), funding(499, "0.3"), funding(500)]
    with source(lambda request: httpx.Response(200, json=first
                if json.loads(request.content)["startTime"] == 1 else second)) as liquid:
        pages = list(liquid.funding_history("BTC", 1, 1000))
        assert pages[-1].records == (funding(499, "0.3"), funding(500))
        assert pages[-1].records_seen == 502


@pytest.mark.parametrize("method,args,kwargs", [
    ("order_book", ("../BTC",), {}),
    ("order_book", ("BTC",), {"n_sig_figs": True}),
    ("order_book", ("BTC",), {"mantissa": 2}),
    ("order_book", ("BTC",), {"n_sig_figs": 5, "mantissa": 3}),
    ("all_mids", (), {"dex": "../test"}),
    ("markets", (), {"spot": "true"}),
    ("markets", (), {"spot": True, "dex": "xyz"}),
    ("candles", ("BTC", "2m", 1, 2), {}),
    ("candles", ("BTC", "1m", True, 2), {}),
    ("candles", ("BTC", "1m", 3, 2), {}),
    ("funding_history", ("BTC", 1, 2), {"cursor": "{}"}),
    ("funding_history", ("BTC", -1, 2), {}),
    ("funding_history", ("BTC", 1, True), {}),
])
def test_invalid_queries_fail_before_network(method, args, kwargs):
    with source(lambda _: pytest.fail("unexpected network")) as liquid:
        with pytest.raises(ValueError):
            result = getattr(liquid, method)(*args, **kwargs)
            if method == "funding_history":
                list(result)


@pytest.mark.parametrize("method,body", [
    ("all_mids", {"BTC": 123.4}),
    ("all_mids", {"BTC": "NaN"}),
    ("order_book", {**BOOK, "coin": "ETH"}),
    ("order_book", {**BOOK, "time": True}),
    ("order_book", {**BOOK, "levels": [[{"px": "1", "sz": "-1", "n": 1}], []]}),
    ("markets", [{"universe": [{"name": "BTC"}]}, []]),
    ("markets", [{"universe": [{"name": "BTC"}, {"name": "BTC"}]}, [{}, {}]]),
])
def test_malformed_snapshot_responses_fail_closed(method, body):
    with source(lambda _: httpx.Response(200, json=body)) as liquid:
        with pytest.raises(InvalidResponse):
            getattr(liquid, method)("BTC") if method == "order_book" else getattr(liquid, method)()


@pytest.mark.parametrize("row", [
    {**CANDLE, "s": "ETH"}, {**CANDLE, "i": "1d"}, {**CANDLE, "t": True},
    {**CANDLE, "t": 3000}, {**CANDLE, "c": 100.1}, {**CANDLE, "v": "Infinity"},
])
def test_malformed_candle_identity_and_values_fail_closed(row):
    with source(lambda _: httpx.Response(200, json=[row])) as liquid:
        with pytest.raises(InvalidResponse):
            liquid.candles("BTC", "1m", 1000, 2000)


@pytest.mark.parametrize("rows", [
    [funding(2), funding(1)], [funding(1), funding(1)],
    [{**funding(1), "coin": "ETH"}], [funding(3000)],
    [{**funding(1), "fundingRate": 0.1}], [{**funding(1), "time": True}],
])
def test_malformed_funding_order_identity_and_values_fail_closed(rows):
    with source(lambda _: httpx.Response(200, json=rows)) as liquid:
        with pytest.raises(InvalidResponse):
            list(liquid.funding_history("BTC", 1, 2000))


@pytest.mark.parametrize("body", [
    {"type": "order", "coin": "BTC"}, {"type": "userFills", "user": "0xabc"},
    {"type": "recentTrades", "coin": "BTC"}, {"type": []},
    {"type": "allMids", "user": "0xabc"}, {"type": "l2Book", "coin": "BTC", "action": {}},
    {"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "1m", "startTime": 0}},
    {"type": "fundingHistory", "coin": "BTC", "startTime": False, "endTime": 1000},
])
def test_transport_policy_rejects_account_actions_unknown_types_and_fields(body):
    with pytest.raises(ValueError):
        info_policy(canonical(body).encode())
    with source(lambda _: pytest.fail("unexpected network")) as liquid:
        with pytest.raises(ValueError):
            liquid._request("POST", "/info", content=canonical(body).encode())


@pytest.mark.parametrize("path", ["/exchange", "/info/../exchange", "//elsewhere/info"])
def test_transport_cannot_reach_exchange_even_directly(path):
    with source(lambda _: pytest.fail("unexpected network")) as liquid:
        with pytest.raises(ValueError):
            liquid._request("POST", path, content=b'{"type":"allMids"}')


@pytest.mark.parametrize("body,maximum,actual_count,actual", [
    ({"type": "allMids"}, 2, None, 2),
    ({"type": "metaAndAssetCtxs"}, 20, None, 20),
    ({"type": "fundingHistory", "coin": "BTC", "startTime": 0, "endTime": 1000}, 45, 21, 22),
    ({"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "1m", "startTime": 0, "endTime": 1000}}, 104, 61, 22),
])
def test_provider_policy_reserves_maximum_and_settles_by_count(body, maximum, actual_count, actual):
    reserved, count = info_policy(canonical(body).encode())
    assert reserved == maximum
    if actual_count is not None:
        assert count(canonical([{}] * actual_count).encode()) == actual
        with pytest.raises(ValueError):
            count(b'{}')
    else:
        assert count is None


@pytest.mark.parametrize("limit", [(1201, 60), (1200, 59), None, (True, 60), (1200, float("nan"))])
def test_provider_ip_policy_cannot_be_weakened(limit):
    with pytest.raises(ValueError):
        source(lambda _: pytest.fail("unexpected network"), ip_limit=limit)


def test_quota_and_fetch_alias_are_agent_accessible():
    with source(lambda _: httpx.Response(200, json=BOOK)) as liquid:
        assert liquid.quota()["ip_weight_per_minute"] == 1200
        assert liquid.quota()["api_key_required"] is False
        assert liquid.ip_throttle_only
        assert liquid.fetch("BTC").records == (BOOK,)



def test_direct_transport_cannot_undercharge_a_count_weighted_endpoint():
    body = {"type": "candleSnapshot", "req": {"coin": "BTC", "interval": "1m",
                                             "startTime": 0, "endTime": 1000}}
    with source(lambda _: httpx.Response(200, json=[])) as liquid:
        liquid._request("POST", "/info", content=canonical(body).encode(),
                        rate_weight=1, actual_weight=lambda _: 1)
        usage = liquid.usage()
        assert usage["ip_weight_reserved"] == 104
        assert usage["ip_weight_used"] == 20


@pytest.mark.parametrize("kwargs", [{"params": {"type": "allMids"}},
                                     {"form": {"type": "allMids"}},
                                     {"auth": ("user", "pass")},
                                     {"private_params": {"api_key": "secret"}}])
def test_hyperliquid_transport_only_accepts_public_json_body(kwargs):
    with source(lambda _: pytest.fail("unexpected network")) as liquid:
        with pytest.raises(ValueError):
            liquid._request("POST", "/info", content=b'{"type":"allMids"}', **kwargs)


def test_malformed_response_keeps_conservative_weight_reservation():
    with source(lambda _: httpx.Response(200, json={"unexpected": "schema"})) as liquid:
        with pytest.raises(InvalidResponse):
            liquid.candles("BTC", "1m", 0, 1000)
        usage = liquid.usage()
        assert usage["ip_weight_reserved"] == usage["ip_weight_used"] == 104
