# Hyperliquid market data

`Hyperliquid` retrieves public market data from the official
[POST `/info` endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint).
No key or wallet is required. The transport allows only the supported public
query schemas; account queries, signing and `/exchange` are excluded.

## Choose the retrieval unit

| SDK method | Native result | Request weight |
| --- | --- | --- |
| `all_mids(dex="")` | One coin-to-price map | 2 |
| `markets(spot=False, dex="")` | One metadata/context envelope | 20 |
| `order_book(coin)`; `fetch(coin)` alias | One L2 snapshot, at most 20 levels/side | 2 |
| `candles(coin, interval, start_time, end_time)` | Available OHLCV rows | Reserve 104; settle 20 + ceil(rows/60) |
| `funding_history(coin, start_time, end_time=None, cursor=None)` | Funding rows in timestamp pages | Reserve 45; settle 20 + ceil(rows/20) |

Metadata retains native universe, token indices, margin tables and asset
contexts. Perpetual contexts follow universe positions. Spot contexts and pair
metadata can have different lengths; both remain intact without an inferred join.
Decimals remain strings; units, nulls and additional native fields are unchanged.
Native schemas: [perpetuals](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals),
[spot](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/spot).

Use provider asset names: `BTC` for a perpetual, `xyz:XYZ100` for a builder DEX,
`PURR/USDC` or `@<index>` for spot. Resolve spot indices from the native universe;
UI labels can differ from API names. `dex` selects a perpetual DEX for mids or
metadata; spot metadata does not accept it.
[Asset naming](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#perpetuals-vs-spot).

~~~python
import time
from dedomena.sources import Hyperliquid

end = int(time.time() * 1000)
start = end - 86_400_000
with Hyperliquid() as api:
    mids = api.all_mids()
    perpetuals = api.markets()
    spot = api.markets(spot=True)
    book = api.order_book("BTC")
    candles = api.candles("BTC", "1h", start, end)
    for page in api.funding_history("BTC", start, end):
        consume(page.records, page.provenance.to_dict())
~~~

Times are inclusive epoch milliseconds. Candle intervals are `1m`, `3m`, `5m`,
`15m`, `30m`, `1h`, `2h`, `4h`, `8h`, `12h`, `1d`, `3d`, `1w`, `1M`.
Only the latest 5,000 candles are available. Every candle Page has
`complete=False`: short or empty results cannot prove historical coverage.
The current candle can be unfinished. [Candle contract](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#candle-snapshot).

## Funding checkpoints and receipts

Funding uses the provider's 500-item time-range ceiling. Each following request
starts at the preceding page's final timestamp; exact boundary-row hashes remove
inclusive repeats. A full page that cannot advance past its starting timestamp
raises `SearchLimitExceeded` before delivering that page. Malformed identities,
reordered timestamps and duplicate rows fail explicitly.
[Provider pagination](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint#pagination).

The opaque `next_cursor` contains the exact coin, start/end bounds, emitted
count and boundary hashes. Resume with the same coin/start and cursor; supply
the same end or omit it to use the cutoff stored in the checkpoint. An omitted
initial end is pinned once to retrieval time. `complete=True` finishes enumeration
of provider-available funding for that range.

~~~python
with Hyperliquid() as api:
    start = end - 30 * 86_400_000
    stream = api.funding_history("BTC", start, end)
    checkpoint = next(stream)
    consume(checkpoint.records, checkpoint.provenance.to_dict())
    stream.close()
    if checkpoint.next_cursor is not None:
        for page in api.funding_history("BTC", start, cursor=checkpoint.next_cursor):
            consume(page.records, page.provenance.to_dict())
~~~

Every Page carries the canonical public JSON request body, hashes, retrieval
and completion times, HTTP status and durable snapshot ID. Replay verifies the
stored response hash. Mutable snapshots default to `cache_ttl=0`; explicit cache
TTL or `refresh=True` follows the [shared source contract](SOURCES.md).

## CLI

~~~sh
python -m dedomena.sources markets hyperliquid
python -m dedomena.sources markets hyperliquid --spot
python -m dedomena.sources mids hyperliquid --dex xyz
python -m dedomena.sources book hyperliquid BTC
python -m dedomena.sources fetch hyperliquid BTC
python -m dedomena.sources candles hyperliquid BTC --interval 1h --start-time 1790812800000 --end-time 1790899200000
python -m dedomena.sources funding hyperliquid BTC --start-time 1790812800000 --end-time 1790899200000
python -m dedomena.sources quota hyperliquid
~~~

The CLI emits JSON Pages followed by retrieval totals and checkpoint fields.
`--max-pages` bounds funding acquisition; checkpoint continuation uses the SDK.

## IP capacity and measured validation

Hyperliquid publishes an aggregate REST limit of **1,200 weight/minute per IP**.
The shared limiter reserves each request's conservative maximum, then settles
valid list responses by returned item count. Failed or uncertain sends retain
the reservation; 429 cooldowns apply to the affected IP. The local 1,000
requests/s dispatch ceiling is not a provider allowance. Other clients outside
the shared store still consume the provider's quota.
[Official rate limits](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits).

Use the same [generic IPPool](IP_ROUTING.md) with Hyperliquid and OpenAlex.
Ten distinct owned egress IPs have a theoretical ceiling of 6,000 weight-2
requests/minute, subject to other traffic, latency and provider availability.
This is capacity arithmetic, not a sustained throughput measurement or daily
entitlement.

Bounded live adapter probes on **2026-10-02**: five successful queries covered
mids, perpetual metadata, spot metadata, candles and funding. All returned HTTP
200; all five snapshots passed verified replay. Combined transfer was 399,814
decoded bytes, with 191 weight reserved and 85 locally accounted weight.
Item-count costs are rounded up; provider billing was not reported. Candles
returned three rows with `complete=False`; funding returned 24 rows with
`complete=True`. No sustained high-volume or multi-IP benchmark was performed.
