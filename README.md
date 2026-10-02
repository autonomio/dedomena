# Dedomena

Consistent research data access for agents and scientists, starting with biology.
OpenAlex, Europe PMC (including PubMed), and EPO patent data share a streaming page
and provenance contract. Queries retain their native source semantics.

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

Default caches persist for 24 hours at `~/.cache/dedomena/sources.sqlite3`.
`refresh=True` fetches new data while keeping previous snapshots for offline replay.
`DEDOMENA_SOURCE_STORE` selects a shared store, including across Canary workers.

## Agent CLI

~~~sh
python -m dedomena.sources benchmark openalex 'CRISPR'
python -m dedomena.sources benchmark europepmc 'TITLE:CRISPR'
python -m dedomena.sources search epo 'ta="CRISPR"' --max-pages 1
python -m dedomena.sources quota openalex
~~~

Benchmarks default to one page; `--max-pages` controls acquisition explicitly.
Search streams JSON pages and a final receipt. Source failures emit structured
JSON to stderr with a nonzero exit status. Credentials stay in environment variables.

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
