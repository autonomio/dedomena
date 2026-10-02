# Source acquisition contracts — 1 October 2026

## Provider limits and yield

| Source | Supported page/batch size | Account constraint | Strategy |
| --- | --- | --- | --- |
| OpenAlex | 100 works/page; 100 IDs/filter | Free key: 10,000 credits/day, reset 00:00 UTC; 100 requests/s | Compact discovery; cursor enumeration; free exact-ID enrichment; persistent reuse |
| Europe PMC / PubMed | 1,000 records/page; 100-ID batches in this client | No API key or published daily allowance | Lite discovery, core enrichment, cursor enumeration, OA JATS XML on demand |
| EPO OPS | 100 search hits/page; 100 publication references/POST | Free 4 GB/calendar week, reset Monday 00:00 UTC; approximately 450 MB/rolling hour; dynamic service RPM | Compact references, bulk biblio/abstracts, publication-date partitions, claims on demand |

OpenAlex's free daily budget yields up to **100,000 keyword-search result rows**
(1,000 calls × 100) or **1,000,000 filtered result rows** (10,000 calls × 100).
These are alternatives drawing on the same budget, not additive allowances.
Exact OpenAlex ID lookups and native doi: lookups cost zero credits and share
the 100 requests/s ceiling.
At that ceiling, 8.64 million singleton requests/day is a mathematical upper bound,
not measured throughput or a guarantee of unique records. Research uses a mix.

OpenAlex still accepts legacy 200-record pages but explicitly deprecates them.
This client uses the supported maximum of 100. Each cursor page is separately billed.
Semantic search and paid hosted-content downloads are absent from the current
adapter. Use the public snapshot for entire-corpus ingestion rather than crawling.
The 1,500-character search guard comes from Canary's observed key limit; the
documented encoded-URL limit is 4 KB. Queries are never shortened automatically.

Europe PMC's 10 requests/s default is a **local setting**, not a provider entitlement.
It is configurable and respects 429/Retry-After. A live probe accepted 1,000
records/page and returned an error envelope for 1,001. No daily record yield is
asserted. Fetch abstracts and licensed full text only when needed; prefer bulk
distribution for corpus mirroring. Long exact-ID queries use the native /searchPOST API
rather than overflowing an intermediary's URL limit.

EPO has a weekly byte allowance, not a fixed daily record allowance. The free
weekly volume can in principle be consumed within one day, subject to hourly
and dynamic service constraints. Record yield depends on bytes per response.
There is no automatic paid-tier upgrade. Search defaults to compact references;
`biblio=True` adds richer metadata including abstracts.
Search is capped at 2,000 hits **per query**. `search_partitioned` bisects an
explicit publication-date interval until every partition fits. One overflowing
day requires a narrower CQL query. The first 2,000 hits are never quietly marked
complete.

The live billing probe found that /works/doi:10.1038/nbt.2647 costs zero credits,
while /works/https://doi.org/10.1038/nbt.2647 costs one, despite resolving the
same work. The adapter normalizes DOI inputs to the verified free doi: route.
Unexpected charges on an exact lookup raise instead of silently accumulating.

Primary references:
- [OpenAlex authentication, headers and bounds](https://help.openalex.org/api/authentication/)
- [OpenAlex operation costs and daily yields](https://help.openalex.org/access/example-costs/)
- [OpenAlex paging and legacy 200-record deprecation](https://help.openalex.org/api/paging/)
- [OpenAlex snapshot](https://help.openalex.org/access/snapshot/)
- [Europe PMC REST service](https://europepmc.org/RestfulWebService)
- [Europe PMC bulk downloads](https://europepmc.org/downloads)
- [EPO OPS weekly allowance](https://www.epo.org/en/searching-for-patents/data/web-services/ops)
- [EPO fair use charter](https://www.epo.org/en/service-support/ordering/fair-use)
- [EPO guide 1.3.20, sections 2.3 and 3.1](https://link.epo.org/web/searching-for-patents/data/en-ops-v3.2-documentation-version-1.3.20.pdf)
- [EPO OpenAPI specification](https://ops.epo.org/wsdl/ops.yaml)
- [EPO OPS redistribution terms](https://www.epo.org/en/service-support/ordering/terms-and-conditions/ops-terms-and-conditions)

## Common contract

`OpenAlex`, `EuropePMC`, and `EPO` are synchronous context-managed HTTP clients:
- `search(...)` streams pages without collecting the corpus.
- `fetch(identifier)` retrieves metadata and validates exact source identity.
- `fetch_many(...)` uses bounded retrieval: OpenAlex preserves input order with
  parallel free singletons; Europe PMC/EPO batch explicit identifiers.
- `quota()` reports OpenAlex account allowance, Europe PMC local/public policy,
  or latest EPO provider headers plus shared byte accounting.
- `usage()` reports UTC requests, credits, bytes, cache hits and delivered records.

OpenAlex also provides `fetch_batch(...)` for 100-ID, one-credit lookup.
Missing IDs in batched operations appear in `Page.unresolved`. Exact singleton
failures raise rather than becoming a silent search or a different record.

`Page.complete` is true only on the final page. EPO partitioned retrieval can finish
only in its final date partition; native hit counts remain partition-specific.
Exceptions end incomplete enumeration. Earlier pages retain valid receipts,
but do not establish complete coverage. `allow_partial=True` is an explicit EPO
escape hatch; capped or missing-record pages warn and remain incomplete.

Provenance captures method, URL, parameters, relevant public headers and POST body,
UTC acquisition interval, adapter version, rights description, request/response
SHA-256 and a durable snapshot ID. Every source record retains its stable ID.
Snapshots preserve original decoded response bytes; compression is transport-only.
EPO record trees retain XML attributes and repeated elements; original XML remains
in the snapshot. Rights can differ across records.

The 24-hour cache is credential-scoped by a hash. Separate keys do not reuse each
other's licensed records or allowances. Refresh preserves old snapshots.
Use one shared store path across processes; anonymous Europe PMC shares a source
namespace. Snapshots accumulate: retain the store with your research artifact
and manage its disk lifetime explicitly.

~~~python
from dedomena.sources import Store

store = Store("research-sources.sqlite3")
raw = store.replay(snapshot_id)  # verifies the original response hash internally
original_bytes = raw.body
store.close()
~~~

Replay reproduces captured bytes offline. Live queries cannot promise tomorrow's
database state; multi-page retrieval is not a provider transaction. Archive the
responses and query plans. Private storage and exports must respect source rights;
EPO raw data is not an unrestricted public mirror.

## Throughput controls

Clients accept an injected `httpx.Client`, shared `Store`, `cache_ttl`,
`requests_per_second`, `max_response_bytes`, `max_attempts` and `max_wait`.
The default decoded response ceiling is 16 MB. Pools allow 100 connections on
demand; free OpenAlex singleton workers default to 16 and can be raised to 100.

Retries cover timeouts and transient 429/5xx. Budget exhaustion stops immediately.
Long Retry-After returns `Throttled` with its delay instead of indefinite sleep.
Unknown transport failures retain reservations because the source may have charged
the call. Successful replies reconcile actual credits. EPO reserves maximum
response bytes before dispatch, settles transfer counts, and adopts reported
weekly usage. This leaves headroom near its allowance boundary.
EPO follows the most restrictive service RPM observed in 60 seconds and a
1 Mbit/s byte schedule.

OpenAlex checks account allowance before the first uncached paid request each UTC
day, observes later allowance headers, and defaults to a 10,000-credit local cap.
It does not automatically expand into prepaid usage. Unrelated clients sharing
the key can race local checks; route callers through the same store/service for
coordinated admission. Usage counters measure store traffic, not the whole account.
Credentials and OAuth/quota responses are never saved as source snapshots.

## Canary starting point and integration

Inspected `~/dev/Canary/src/canary/retrieval.py`, `question_retrieval.py`,
`primary_sources.py`, `boundary/openalex_usage.py`, `boundary/server.py`,
and `docs/OPENALEX.md`.

Canary already had 100-record pages, selected fields, cursor enumeration, DOI OR
batches, retries, session metadata reuse, and a service-owned Bearer key.
Dedomena adds persistent snapshots and key budgets, cheaper discovery fields,
free parallel singleton enrichment, operation/byte metrics, and two additional
providers with the same receipt contract.

~~~python
from dedomena.sources import OpenAlex, Store

store = Store("/srv/canary/source-cache.sqlite3")
with OpenAlex(store=store, profile="evidence") as source:
    for page in source.search(compiled_query, fields=CANARY_NET_FIELDS):
        papers = parse_openalex({"results": list(page.records)})
        checkpoint(papers, page.provenance.to_dict(), page.next_cursor, page.records_seen)

with OpenAlex(store=store) as source:
    for page in source.fetch_many(verified_dois, workers=100):
        consume(page)
store.close()
~~~

Keep the key in Canary's trusted service and serve these adapters through its
existing narrow routes. Canary was inspected read-only; gateway migration follows
later. Existing native OpenAlex work dictionaries still fit Canary's parser.

Resume OpenAlex/Europe PMC with `cursor=page.next_cursor` and
`records_seen=page.records_seen`. The count validates the final page against the
full hit count. EPO uses integer range offsets. A completed nonfinal date partition
returns `date:YYYY-MM-DD` for the next start date; preserve the original query,
final date bound and completed receipts.

## Live measurements

Bounded probes on 1 October 2026 used Canary's OpenAlex key and public Europe PMC.
These are point measurements, not sustained throughput tests. Initial probes
consumed ten OpenAlex credits plus one free singleton; SDK verification follows.

| Request | Rows | Decoded bytes | Wire bytes | Time | Credits |
| --- | ---: | ---: | ---: | ---: | ---: |
| OpenAlex CRISPR, discovery fields | 100 | 40,549 | 7,190 | 1.220 s | 10 |
| Europe PMC TITLE:CRISPR, lite | 1,000 | 754,830 | 149,570 | 6.677 s | Unmetered |
| OpenAlex singleton | 1 | Not recorded | Not recorded | Not recorded | 0 |

No EPO credentials were configured. OAuth, quota, XML, ranges, bulk and partition
behavior are tested offline; live account yield remains unmeasured.

~~~sh
python -m dedomena.sources benchmark epo 'ta="CRISPR"'
python -m dedomena.sources benchmark openalex 'CRISPR' --max-pages 2 --refresh
python -m dedomena.sources benchmark europepmc 'TITLE:CRISPR' --profile evidence
~~~

Benchmarks emit metrics and a resume checkpoint, defaulting to one page. Distinguish
cached and fresh measurements. Usage also counts authentication/quota and partition
probes, so overhead remains visible.

SDK verification confirmed 100-ID Europe PMC POST retrieval with no missing IDs,
and exact DOI lookup plus offline replay through the free doi: route. A cached
100-record discovery page returned in approximately 1 ms with zero upstream
requests, wire bytes or credits. On the same CRISPR page, the discovery profile
used 40,548 decoded / 7,161 wire bytes, versus 2,328,924 decoded / 192,455 wire
bytes for the evidence profile: reductions of 98.3% decoded and 96.3% on the wire.
These profiles intentionally carry different fields; select richer evidence only
when needed. This comparison measures transfer weight, not improved recall.
Probe reports and raw snapshots remain locally under ignored
out/source-probes-2026-10-01/. EPO still requires a live credentialed probe.
