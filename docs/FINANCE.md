# Finance sources

Four official APIs cover corporate fundamentals, dated macroeconomic data,
monetary statistics, reference exchange rates and international indicators.
Every adapter shares the research-source transport, native records, durable
snapshots, verified replay and JSON CLI.

## Access and retrieval weight

| Source | Access | Maximum retrieval unit | Rate policy |
| --- | --- | --- | --- |
| SEC EDGAR | No key; identifying organization/contact User-Agent | Company facts, exact concept, cross-company frame, complete filing history | Provider maximum 10 requests/s across clients and machines |
| FRED/ALFRED | Free personal API key | 100,000 series observations or 500,000 release observations/request | v1 120/min; v2 2/s |
| ECB SDMX | No key | Batched dimension values, selected series and history/revision deltas | No published numerical allowance found; local default 2/s |
| World Bank Indicators v2 | No key | 60 indicators/query; 32,767 records/page accepted in live probes | No published numerical allowance found; local default 5/s |

Rates are ceilings or local policies, not guaranteed daily entitlements.
SEC counts other clients and machines too; coordinate traffic and use a shared
store. SEC, ECB and World Bank pacing shares a source namespace; FRED shares a
key namespace. All adapters honor 429/Retry-After and bounded retries.

Default decoded-response ceilings: SEC 64 MB, FRED 128 MB, ECB 16 MB,
World Bank 32 MB. Set `max_response_bytes` if needed; narrow dates, concepts or
page sizes to reduce memory/transfer weight. A response exceeding its cap fails
explicitly. Each response is decoded, parsed and archived in memory before
delivery; page streaming bounds acquisition between requests.

Primary references: [SEC APIs](https://www.sec.gov/search-filings/edgar-application-programming-interfaces),
[SEC fair access](https://www.sec.gov/about/developer-resources),
[FRED v2 bulk](https://fred.stlouisfed.org/docs/api/fred/v2/release_observations.html),
[FRED v2 rates](https://fred.stlouisfed.org/docs/api/fred/v2/errors.html),
[FRED v1 rates](https://fred.stlouisfed.org/docs/api/fred/errors.html),
[ECB API](https://data.ecb.europa.eu/help/api/data),
[World Bank access](https://datahelpdesk.worldbank.org/knowledgebase/articles/889392),
[World Bank paging/batches](https://datahelpdesk.worldbank.org/knowledgebase/articles/898581).

## SEC: scope the read to the question

Set `SEC_USER_AGENT` to your organization/application and actual contact email.
The adapter requires an explicit identity; it does not borrow a default contact.

~~~python
from dedomena.sources import SEC
with SEC() as sec:
    facts = sec.company_facts(320193)
    assets = sec.company_concept(320193, "us-gaap", "Assets")
    frame = sec.frame("us-gaap", "Assets", "USD", "CY2025Q4I")
    for page in sec.submissions(320193):
        consume(page.records, page.provenance.to_dict())
~~~

`fetch` aliases company facts; `search` enumerates all filings for an exact CIK.
`fetch_many` offers bounded parallel company reads, stable caller order and
window deduplication. All SEC clients in one store share pacing across User-Agents.

Facts, concepts and frames are one native envelope per Page record, preserving
taxonomies, units, accession numbers, fiscal/report periods and amendments.
Filing pages become rows of native columns; original company metadata and
columnar envelopes remain in snapshots. Additional history files are traversed
by default; requesting recent filings alone is explicitly incomplete when
history exists. Resume with filename `next_cursor` and `records_seen`.

Frames select SEC's latest filing matching a calendar period. Fiscal dates can
differ; a frame is not a historical knowledge-date query. Selecting an amendment
or latest value from company facts remains a downstream research decision.
Standard taxonomy facts do not represent every custom filing concept/full document.

For corpus scale, SEC recommends its
[nightly companyfacts/submissions ZIP archives](https://www.sec.gov/search-filings/edgar-application-programming-interfaces).
`quota()` exposes their official URLs; this adapter implements bounded JSON reads.

## FRED: bulk acquisition and dated evidence

Set `FRED_API_KEY` using your own free FRED account key.
[Authentication](https://fred.stlouisfed.org/docs/api/fred/v2/api_key.html)
requires a key for each user.

~~~python
from dedomena.sources import FRED
with FRED() as fred:
    metadata = fred.fetch("GDP", as_of="2020-01-01")
    for page in fred.observations("GDP", as_of="2020-01-01"):
        consume(page.records, page.provenance.to_dict())
    for page in fred.release_observations(53):
        consume(page.records, page.provenance.to_dict())
~~~

V1 search, metadata and observations pin default real-time bounds to today's
UTC date in the receipt. Use `as_of` for ALFRED knowledge-date evidence, or explicit
`realtime_start`/`realtime_end` for revision intervals. Observation dates differ
from knowledge dates. `vintages` enumerates publication/revision dates.
The [observations contract](https://fred.stlouisfed.org/docs/api/fred/series_observations.html)
defines transformations and aggregation; raw levels are the default.
Decimal strings and missing `"."` remain native. Fetch metadata for units,
frequency, seasonality, rights and source notes.

V2 release pages contain series fragments, metadata and observations. A series
may span pages. `page_size` counts observations; `Page.records`, `records_seen`
and CLI `records` count series fragments. Usage separately tracks
`observations_delivered`. Resume v1 with offset/records_seen, v2 with cursor/records_seen.
A repeated series changing version during traversal fails explicitly; different
series can still have mixed update times. V2 serves latest release data rather
than an ALFRED vintage. The raw envelope retains release/source metadata.

V1 mandates a query-string key. Only the HTTP send receives it; public provenance
and archived requests exclude it, and caches use hashed key namespaces.
A stable filter redacts `api_key` from HTTPX's URL response log without retaining
a credential registry. V2 uses Bearer authentication. Caller-supplied hooks and
logging remain the caller's responsibility. Echoed credentials are rejected.

## ECB: batched dimensions and statistical meaning

~~~python
from dedomena.sources import ECB
with ECB() as ecb:
    datasets = ecb.dataflows()
    metadata = ecb.series("EXR", "M..EUR.SP00.A")
    rates = ecb.fx(["USD", "GBP", "JPY"], frequency="M",
                   start_period="2025-01", end_period="2025-12")
    delta = ecb.observations("EXR", "D..EUR.SP00.A",
                            updated_after="2026-10-01T00:00:00Z")
~~~

`fx` batches ISO currencies: `OBS_VALUE` is currency units per one EUR.
General `observations(flow, key)` preserves native SDMX keys, blank wildcards
and `+` unions. CSV strings preserve precision, units, `UNIT_MULT`, status and
every returned attribute. No scaling, currency inversion or imputation occurs.
`series` returns keys/attributes without observation payloads.

`last_n` selects observations separately per series. `updated_after` deltas can
include additions, revisions and deletions: reconcile with prior data.
`include_history=True` requests prior versions where supported; avoid collapsing
rows solely by series and period. [Parameter semantics](https://data.ecb.europa.eu/help/api/data).

There is no documented cursor pagination. A Page covers the selected request;
narrow explicit date windows when the response cap is exceeded. A valid future
interval returned HTTP 200 with an empty body in live probes: complete empty
result, receipt and warning. HTTP 404 stays an explicit error because invalid
series and no matching data cannot be distinguished. Missing calendar dates
do not themselves establish truncated retrieval.

## World Bank: counted global indicators

~~~python
from dedomena.sources import WorldBank
with WorldBank() as wb:
    metadata = wb.fetch("NY.GDP.MKTP.CD")
    for page in wb.search(["NY.GDP.MKTP.CD", "FP.CPI.TOTL.ZG"],
                          countries=["USA", "FIN"], date="1970:2024",
                          footnotes=True):
        consume(page.records, page.provenance.to_dict())
~~~

`indicators()` discovers metadata; `fetch` returns exact indicator metadata.
`search`/`observations` return values, defaulting to WDI (source 2); select other
datasets with numeric `source`. IDs, dates, units, decimals, nulls and requested
footnotes remain native. `countries="all"` includes regional/income aggregates;
those are not independent countries. Fetch units/source notes before comparison.
Current revised indicators do not supply historical vintages.

Count, page size and update metadata are checked across pages. Resume with numeric
page cursor and `records_seen=(page-1)*page_size`. 32,767/page succeeded live;
32,768 and 50,000 returned HTTP 400. This local cap records observed behavior,
not a published provider promise.

## Agent CLI and replay

~~~sh
python -m dedomena.sources fetch sec 320193
python -m dedomena.sources search sec 320193 --max-pages 1
python -m dedomena.sources benchmark fred 53 --operation release
python -m dedomena.sources observations fred GDP --as-of 2020-01-01
python -m dedomena.sources catalogue ecb
python -m dedomena.sources benchmark ecb EXR/M.USD+GBP.EUR.SP00.A --last-n 12
python -m dedomena.sources catalogue worldbank
python -m dedomena.sources benchmark worldbank 'NY.GDP.MKTP.CD;FP.CPI.TOTL.ZG' --period 1970:2024
python -m dedomena.sources replay fred SNAPSHOT_ID --store /path/to/sources.sqlite3
~~~

Benchmarks default to one page, emitting metrics/checkpoints without records.
Other collection operations emit pages and a final receipt. `--max-pages` keeps
the real completion state. SDK methods accept the reported resume checkpoints;
the CLI reports checkpoints but does not yet expose resume options.
Replay needs no provider credentials and rejects source mismatches.

Every successful response includes public query semantics, adapter version,
HTTP status, source URL, retrieval/completion times, hashes, license context and
snapshot ID. Replay verifies saved bytes offline. Fresh queries against evolving
providers cannot guarantee identical results; deterministic evidence uses snapshots.

## Rights and bounded verification

Free access does not establish universal redistribution rights.
[FRED terms](https://fred.stlouisfed.org/legal/terms/), native copyright fields,
[ECB reuse](https://www.ecb.europa.eu/stats/ecb_statistics/governance_and_quality_framework/html/usage_policy.en.html),
[World Bank data terms](https://data.worldbank.org/summary-terms-of-use),
and underlying SEC filing rights govern reuse. Code is MIT; data rights differ.
Coverage is fundamentals, macro and reference rates; it is not a comprehensive
exchange-level real-time securities feed.

Bounded probes on 2026-10-02 verified:

- SEC: 2,257 Apple filings across both files; 25,135 company-fact observations.
  All facts: 3,789,099 decoded / 271,819 wire bytes. Assets concept: 19,972 /
  2,472 bytes, 99.47% smaller decoded payload with narrower coverage.
  Assets frame: 6,157 companies; USD-per-shares frame also verified.
- ECB: 36 monthly observations across three currencies, 44 series metadata
  records, 104 dataflows and a complete empty future interval; raw replay verified.
- World Bank: 29,150 GDP/inflation observations for 1970–2024 in one request;
  6,472,841 decoded / 363,141 wire bytes. Cache used zero upstream bytes; raw
  replay, metadata and the 1,498-indicator WDI catalog verified.
- FRED: official v1/v2 protocol fixtures pass offline. No key was configured;
  authenticated live acquisition and sustained key yield remain unmeasured.

These are selected-query probes, not daily harvest measurements. Sanitized local
metrics/snapshots live under ignored `out/finance-probes-2026-10-02/`.
