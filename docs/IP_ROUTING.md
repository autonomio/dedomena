# Shared IP routing

`IPPool` connects any source adapter to the same owned egress routes. Each route
pairs its **actual public IP** with a local interface address, an HTTP(S) proxy,
or an already routed `httpx.Client`. Declaring an IP identifies its allowance;
it does not configure or verify network routing. Dedomena never discovers IPs
through an external service.

Two local addresses behind the same NAT share one public IP and one allowance.
Duplicate public IPs, including IPv4-mapped IPv6 aliases, are rejected within a
pool. Configure every worker with the same declared IP for the same egress and
use the same SQLite `Store` to coordinate across threads, processes and restarts.
Other applications using that IP still consume the provider's allowance.

## One pool, multiple sources

Replace all example addresses with interfaces and public IPs you own. The
`203.0.113.*` addresses below are reserved documentation examples; local addresses
must exist on the machine running Dedomena.

~~~python
from dedomena.sources import Hyperliquid, IPPool, IPRoute, OpenAlex, Store

store = Store("sources.sqlite3")
try:
    with IPPool([
        IPRoute("203.0.113.10", local_address="10.0.0.10"),
        IPRoute("203.0.113.11", local_address="10.0.0.11"),
    ]) as routes:
        with OpenAlex(store=store, ip_pool=routes) as papers, \
             Hyperliquid(store=store, ip_pool=routes) as markets:
            paper = papers.fetch("W2741809807")
            book = markets.order_book("BTC")
finally:
    store.close()
~~~

`OPENALEX_API_KEY` supplies the paper source's key. Hyperliquid public market
reads require no key. Closing a source leaves the caller's pool and store open;
closing the pool closes only clients it created. Local bindings and proxies use
pooled connections with `trust_env=False`, so environment proxy settings cannot
replace the configured route. An injected client must already implement that
route and cannot be combined with `local_address` or `proxy`.

For a proxy, keep its URL and credentials in the caller's environment:

~~~python
import os

route = IPRoute("203.0.113.10", proxy=os.environ["RESEARCH_PROXY_1"])
# RESEARCH_PROXY_1 is an http:// or https:// proxy origin, optionally with auth.
~~~

## Allowances and retrieval weight

The pool admits weighted rolling windows, shared provider/key pacing and
cooldowns atomically. It chooses a ready route in deterministic round-robin
order, or waits for the earliest eligible route within `max_wait`. Waiting does
not reserve a future dispatch slot. A positive `max_wait` bounds admission,
including budget work and lock wait; zero disables scheduled quota sleeps. A rolling window's duration remains fixed
for its quota scope so changing a worker's configuration cannot hide prior use.

- **Hyperliquid:** 1,200 aggregate REST weight per minute per public IP.
  Mid-price and order-book requests cost 2; market metadata costs 20.
  Ten distinct IPs therefore have a theoretical combined ceiling of 6,000
  cheap requests/minute, versus 600 for one IP. This is allowance arithmetic,
  subject to RTT, local pacing and other traffic; it is not a throughput benchmark.
  [Official rate policy](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/rate-limits-and-user-limits).
- **OpenAlex:** Dedomena retains the 100 requests/second ceiling across the key,
  plus a conservative IP guard and the shared daily credit budget. The current
  help page does not establish an IP-only scope for that rate, so adding IPs
  does not increase the key's request or credit allowance.
  [Official authentication and limits](https://help.openalex.org/api/authentication/).

Response-sized Hyperliquid reads reserve their maximum estimated weight before
sending, then reduce only that request's reservation after a verified response
establishes its returned item count. Uncertain sends and failed responses retain
the reservation. Budget rejection before a send consumes no IP allowance.
A 429 persists a cooldown on the affected provider/IP; shared provider or key
exhaustion still applies across the whole pool. Provider errors and retries never
supply extra allowances.

Without explicit routes, adapters with IP limits use the conservative unknown-IP
scope `0.0.0.0`. All such instances of that provider share its IP guard in the
same store. No extra IP allowance is inferred from separate clients or keys.

For another adapter, pass `ip_pool=routes` and `ip_limit=(capacity, seconds)` through
its transport configuration. The provider's own key, daily, service and global
limits remain in effect. `ip_throttle_only=True` is appropriate only when that
provider's 429 is known to describe an IP allowance; otherwise the transport also
retains its shared cooldown. A pool with `ip_limit=None` provides routing and IP
cooldowns while keeping the adapter's existing shared pacing.

Request hashes and cache identity exclude routing, so a query can reuse the same
snapshot across IPs. Network receipts carry optional `egress_ip_sha256` for the
chosen declared public IP; unknown-IP and older receipts leave it unset. Public
provenance, pool representations and admission state exclude proxy credentials.

## CLI route file

Save the same routes as a JSON array in `ip-routes.json`:

~~~json
[
  {"public_ip": "203.0.113.10", "local_address": "10.0.0.10"},
  {"public_ip": "203.0.113.11", "local_address": "10.0.0.11"}
]
~~~

~~~sh
python -m dedomena.sources fetch hyperliquid BTC --store sources.sqlite3 --ip-routes ip-routes.json
python -m dedomena.sources fetch openalex W2741809807 --store sources.sqlite3 --ip-routes ip-routes.json
~~~

Each JSON route accepts `public_ip` and exactly one of `local_address` or `proxy`;
Python client objects cannot appear in the file. Use the same route identities
and store path across CLI workers.

Routing admission does not schedule parallel work. A generic bounded executor,
with stable ordering, shared admission and durable checkpoints, is tracked in
[issue #18](https://github.com/autonomio/dedomena/issues/18).
