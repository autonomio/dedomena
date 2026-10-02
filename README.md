# Dedomena

Consistent research data access for agents and scientists across biology and finance.
OpenAlex, Europe PMC, EPO, SEC EDGAR, FRED/ALFRED, ECB, World Bank and Hyperliquid share a
streaming page and provenance contract. Queries retain their native source semantics.

## Install

Python 3.10 or later:

~~~sh
pip install .
~~~

The core requires only HTTPX and defusedxml. Historical dataset/API functions remain
available with `pip install '.[legacy]'`.

## Research sources

~~~python
from dedomena.sources import OpenAlex, EuropePMC, EPO

with OpenAlex() as source:  # OPENALEX_API_KEY
    for page in source.search('CRISPR AND biology'):
        consume(page.records, page.provenance.to_dict())

with EuropePMC() as source:  # 1,000 records per request, no key
    for page in source.search('TITLE:CRISPR'):
        consume(page.records, page.provenance.to_dict())

with EPO() as source:  # EPO_OPS_KEY and EPO_OPS_SECRET
    for page in source.search('ta="CRISPR"'):
        consume(page.records, page.provenance.to_dict())
~~~

`consume` represents your application's page consumer. Every page includes stable
source identifiers, exact request provenance, SHA-256 hashes, a saved-response
snapshot ID, retrieval times, completeness, cost and transfer size. Partial
enumeration fails explicitly. Native records retain source-specific fields.

Research and traditional finance caches persist for 24 hours at `~/.cache/dedomena/sources.sqlite3`.
`refresh=True` fetches new data while keeping previous snapshots for offline replay.
`DEDOMENA_SOURCE_STORE` selects a shared store, including across Canary workers.

## Finance sources

~~~python
from dedomena.sources import SEC, FRED, ECB, WorldBank

with SEC() as source:  # SEC_USER_AGENT: your organization and contact email
    page = source.company_facts(320193)  # All concepts, units, and filing vintages
    consume(page.records, page.provenance.to_dict())

with FRED() as source:  # FRED_API_KEY, free registration
    for page in source.release_observations(53):  # Up to 500,000 observations/request
        consume(page.records, page.provenance.to_dict())
    for page in source.observations("GDP", as_of="2020-01-01"):
        consume(page.records, page.provenance.to_dict())

with ECB() as source:  # No key; currency units per EUR
    page = source.fx(["USD", "GBP", "JPY"], frequency="M",
                     start_period="2025-01", end_period="2025-12")
    consume(page.records, page.provenance.to_dict())

with WorldBank() as source:  # No key; up to 60 indicators in one query
    for page in source.search(["NY.GDP.MKTP.CD", "FP.CPI.TOTL.ZG"],
                              countries="all", date="1970:2024"):
        consume(page.records, page.provenance.to_dict())
~~~

Metadata, units, scaling, missing values, status and filing dates remain native.
FRED supports ALFRED knowledge dates; SEC retains amendments; ECB exposes
revisions; World Bank serves current revised indicators.
See [finance access, throughput and semantics](docs/FINANCE.md).

~~~python
from dedomena.sources import Hyperliquid

with Hyperliquid() as source:  # Public market data, no key or wallet
    markets = source.markets()  # Native metadata and asset contexts
    book = source.order_book("BTC")
    consume(book.records, book.provenance.to_dict())
~~~

Hyperliquid adds mids, spot/perpetual metadata, books, candles and funding history.
Market snapshots default to a zero cache TTL. Candle retention is explicitly
incomplete; funding supports timestamp checkpoints.
See [Hyperliquid retrieval and weight](docs/HYPERLIQUID.md).

`IPPool` routes the same source clients through owned local IPs or proxies, with
shared weighted per-IP admission, key budgets and cooldowns. Reuse one pool for
Hyperliquid and OpenAlex; extra IPs do not multiply OpenAlex's key allowance.
See [shared IP routing examples](docs/IP_ROUTING.md).

## Agent CLI

~~~sh
python -m dedomena.sources benchmark openalex 'CRISPR'
python -m dedomena.sources benchmark europepmc 'TITLE:CRISPR'
python -m dedomena.sources search epo 'ta="CRISPR"' --max-pages 1
python -m dedomena.sources quota openalex
python -m dedomena.sources markets hyperliquid
python -m dedomena.sources book hyperliquid BTC
python -m dedomena.sources fetch sec 320193
python -m dedomena.sources benchmark fred 53 --operation release
python -m dedomena.sources observations fred GDP --as-of 2020-01-01
python -m dedomena.sources benchmark worldbank 'NY.GDP.MKTP.CD;FP.CPI.TOTL.ZG' --period 1970:2024
python -m dedomena.sources benchmark ecb EXR/M.USD+GBP+JPY.EUR.SP00.A --start-period 2025-01 --end-period 2025-12
~~~

Benchmarks default to one page; `--max-pages` controls acquisition explicitly.
Collection operations stream JSON pages and a final receipt. Source failures emit structured
JSON to stderr with a nonzero exit status. Credentials stay in environment variables. Offline replay requires no current API key.

See [source limits, usage and Canary integration](docs/SOURCES.md).
Downstream services can expose these same clients to their agents.

## Legacy API

~~~python
import dedomena as da
data = da.datasets.autonomio('icu_mortality')
~~~

Legacy `datasets`, `apis`, and `generators` namespaces remain available; legacy
adapters retain their historical behavior. New source guarantees apply to `sources`.

## Verification

~~~sh
pip install . 'pytest>=8,<9'
python -m pytest -q tests
~~~

CI runs offline provider contracts on Python 3.10, 3.12 and 3.13.
[MIT license](LICENSE).
