"""Agent-friendly JSON CLI. Credentials are read exclusively from the environment."""
import argparse
from contextlib import ExitStack
from datetime import date
import json
import os
from pathlib import Path
import sys
import time

from . import (ECB, EPO, FRED, SEC, EuropePMC, Hyperliquid, IPPool, IPRoute,
               OpenAlex, SourceError, Store, WorldBank)


def _ip_pool(path):
    try:
        with Path(path).expanduser().open("rb") as handle:
            data = handle.read(65537)
        if len(data) > 65536:
            raise ValueError
        routes = json.loads(data)
    except (OSError, ValueError, UnicodeDecodeError):
        raise ValueError("IP routes require a readable JSON file of at most 64 KiB") from None
    if (not isinstance(routes, list) or not 1 <= len(routes) <= 1000
            or any(not isinstance(route, dict) or "public_ip" not in route
                   or not set(route).issubset({"public_ip", "local_address", "proxy"})
                   for route in routes)):
        raise ValueError("IP routes require public_ip and one local_address or proxy per entry")
    return IPPool(IPRoute(**route) for route in routes)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    market_operations = ("markets", "mids", "book", "candles", "funding")
    operations = ("search", "observations", "release", "catalogue", *market_operations)
    parser.add_argument("action", choices=(*operations, "fetch", "quota", "usage", "replay", "benchmark"))
    parser.add_argument("source", choices=("openalex", "europepmc", "epo", "sec", "fred", "ecb", "worldbank", "hyperliquid"))
    parser.add_argument("query", nargs="?")
    parser.add_argument("--store", help="Shared SQLite snapshot/allowance store")
    parser.add_argument("--ip-routes", help="JSON file of owned public IP routes and local bindings/proxies")
    parser.add_argument("--start-time", type=int, help="Hyperliquid start time in epoch milliseconds")
    parser.add_argument("--end-time", type=int, help="Hyperliquid end time in epoch milliseconds")
    parser.add_argument("--interval", help="Hyperliquid candle interval, e.g. 1h")
    parser.add_argument("--spot", action="store_true", help="Hyperliquid spot market metadata")
    parser.add_argument("--dex", help="Hyperliquid perpetual DEX name")
    parser.add_argument("--profile", choices=("discovery", "evidence"), default="discovery")
    parser.add_argument("--filter")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    parser.add_argument("--operation", choices=operations, default="search", help="Operation to benchmark")
    parser.add_argument("--as-of", help="FRED/ALFRED knowledge date YYYY-MM-DD")
    parser.add_argument("--countries", help="World Bank codes joined by semicolons, or all")
    parser.add_argument("--period", help="World Bank year/month/quarter or range, e.g. 2020:2025")
    parser.add_argument("--start-period", help="ECB ISO date or SDMX period")
    parser.add_argument("--end-period", help="ECB ISO date or SDMX period")
    parser.add_argument("--updated-after", help="ECB revision delta since ISO timestamp with timezone")
    parser.add_argument("--last-n", type=int, help="ECB latest observations per series")
    args = parser.parse_args(argv)
    operation = args.operation if args.action == "benchmark" else args.action
    if args.max_pages is not None and args.max_pages < 1:
        parser.error("--max-pages must be positive")
    if args.filter and args.source != "openalex":
        parser.error("--filter is supported by openalex only")
    if args.as_of and (args.source != "fred" or operation not in ("search", "fetch", "observations")):
        parser.error("--as-of requires FRED series search, fetch, or observations")
    if (args.countries or args.period) and (args.source != "worldbank" or operation not in ("search", "observations")):
        parser.error("--countries and --period require World Bank search or observations")
    if (args.start_period or args.end_period or args.updated_after or args.last_n is not None) and (args.source != "ecb" or operation not in ("search", "observations")):
        parser.error("SDMX period/delta/last-n options require ECB search or observations")
    if args.action != "benchmark" and args.operation != "search":
        parser.error("--operation selects the benchmark operation only")
    if operation in market_operations and args.source != "hyperliquid":
        parser.error("market operations require hyperliquid")
    if args.source == "hyperliquid" and operation in ("search", "observations", "release"):
        parser.error("hyperliquid supports markets, mids, book, candles, funding, and fetch")
    if (args.start_time is not None or args.end_time is not None or args.interval) and (
            args.source != "hyperliquid" or operation not in ("candles", "funding")):
        parser.error("epoch time and interval options require Hyperliquid candles or funding")
    if args.interval and operation != "candles":
        parser.error("--interval requires candles")
    if operation in ("candles", "funding") and args.start_time is None:
        parser.error("historical market operations require --start-time")
    if operation == "candles" and (args.end_time is None or not args.interval):
        parser.error("candles require --end-time and --interval")
    if args.spot and (args.source != "hyperliquid" or operation not in ("markets", "catalogue")):
        parser.error("--spot requires Hyperliquid markets or catalogue")
    if args.dex is not None and (args.source != "hyperliquid" or operation not in ("markets", "catalogue", "mids")):
        parser.error("--dex requires Hyperliquid markets or mids")
    if operation == "release" and args.source != "fred":
        parser.error("release requires fred")
    if operation == "catalogue" and args.source not in ("ecb", "worldbank", "hyperliquid"):
        parser.error("catalogue requires ecb, worldbank or hyperliquid")
    if operation == "observations" and args.source not in ("fred", "ecb", "worldbank"):
        parser.error("observations requires fred, ecb, or worldbank")
    if operation in ("fetch", "replay", "release", "observations", "book", "candles", "funding") and not args.query:
        parser.error("this action needs an identifier")
    if operation == "search" and not args.query and not (args.source == "openalex" and args.filter):
        parser.error("search needs a query, or an OpenAlex filter")
    if bool(args.start_date) != bool(args.end_date) or (args.start_date and args.source != "epo"):
        parser.error("date partitions require both dates and the epo source")
    store = None
    resources = ExitStack()

    def emit(value):
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))

    def pages(source):
        if args.source == "hyperliquid":
            if operation in ("markets", "catalogue"):
                return iter((source.markets(spot=args.spot, dex=args.dex or "", refresh=args.refresh),))
            if operation == "mids":
                return iter((source.all_mids(dex=args.dex or "", refresh=args.refresh),))
            if operation == "book":
                return iter((source.order_book(args.query, refresh=args.refresh),))
            if operation == "candles":
                return iter((source.candles(args.query, args.interval, start_time=args.start_time,
                                            end_time=args.end_time, refresh=args.refresh),))
            return source.funding_history(args.query, start_time=args.start_time,
                                          end_time=args.end_time, refresh=args.refresh)
        if operation == "release":
            if not args.query.isascii() or not args.query.isdecimal():
                raise ValueError("release identifier must be a positive integer")
            return source.release_observations(int(args.query), refresh=args.refresh)
        if operation == "catalogue":
            return iter((source.dataflows(refresh=args.refresh),)) if args.source == "ecb" else source.indicators(refresh=args.refresh)
        if args.source == "fred":
            method = source.observations if operation == "observations" else source.search
            return method(args.query, as_of=args.as_of, refresh=args.refresh)
        if args.source == "worldbank":
            options = {"refresh": args.refresh}
            if args.countries:
                options["countries"] = args.countries
            if args.period:
                options["date"] = args.period
            return source.search(args.query, **options)
        if args.source == "ecb":
            flow, key = source._query(args.query)
            return iter((source.observations(flow, key, start_period=args.start_period,
                         end_period=args.end_period, updated_after=args.updated_after,
                         last_n=args.last_n, refresh=args.refresh),))
        if args.source == "openalex":
            return source.search(args.query, filter=args.filter, refresh=args.refresh)
        if args.source == "epo" and args.start_date:
            return source.search_partitioned(args.query, args.start_date, args.end_date,
                                               refresh=args.refresh,
                                               biblio=args.profile == "evidence")
        if args.source == "epo":
            return source.search(args.query, refresh=args.refresh, biblio=args.profile == "evidence")
        return source.search(args.query, refresh=args.refresh)

    try:
        if args.action == "replay":
            store = Store(args.store or os.environ.get("DEDOMENA_SOURCE_STORE",
                          str(Path.home() / ".cache" / "dedomena" / "sources.sqlite3")))
            raw = store.replay(args.query)
            if raw.provenance.source != args.source:
                raise SourceError("snapshot belongs to a different source")
            emit({"provenance": raw.provenance.to_dict(), "body": raw.body.decode("utf-8")})
            return 0
        store = Store(args.store) if args.store else None
        constructors = {"openalex": OpenAlex, "europepmc": EuropePMC, "epo": EPO,
                        "sec": SEC, "fred": FRED, "ecb": ECB, "worldbank": WorldBank,
                        "hyperliquid": Hyperliquid}
        kwargs = {"store": store} if store else {}
        if args.ip_routes:
            kwargs["ip_pool"] = resources.enter_context(_ip_pool(args.ip_routes))
        if args.source == "openalex":
            kwargs["profile"] = args.profile
        elif args.source == "europepmc":
            kwargs["result_type"] = "core" if args.profile == "evidence" else "lite"
        with constructors[args.source](**kwargs) as source:
            if args.action == "quota":
                emit(source.quota())
            elif args.action == "usage":
                emit(source.usage())
            elif args.action == "fetch":
                options = {"refresh": args.refresh}
                if args.source == "fred":
                    options["as_of"] = args.as_of
                emit(source.fetch(args.query, **options).to_dict())
            else:
                started = time.perf_counter()
                stream = pages(source)
                limit = args.max_pages or (1 if args.action == "benchmark" else None)
                totals = {"pages": 0, "records": 0, "credits_used": 0,
                          "wire_bytes": 0, "decoded_bytes": 0, "cache_hits": 0}
                last = None
                for page in stream:
                    last = page
                    totals["pages"] += 1
                    totals["records"] += len(page.records)
                    totals["credits_used"] += page.credits_used
                    totals["wire_bytes"] += page.wire_bytes
                    totals["decoded_bytes"] += page.decoded_bytes
                    totals["cache_hits"] += int(page.cache_hit)
                    if args.action != "benchmark":
                        emit(page.to_dict())
                    if limit and totals["pages"] >= limit:
                        break
                emit({"source": args.source, "operation": operation, **totals,
                      "elapsed_seconds": round(time.perf_counter() - started, 3),
                      "complete": bool(last and last.complete),
                      "next_cursor": last.next_cursor if last else None,
                      "records_seen": last.records_seen if last else 0,
                      "last_parameters": dict(last.provenance.parameters) if last else {},
                      "usage": source.usage()})
    except (SourceError, ValueError, KeyError) as error:
        message = "snapshot not found" if isinstance(error, KeyError) else str(error)
        emit_error = {"source": args.source, "error": type(error).__name__,
                      "message": message, "complete": False}
        print(json.dumps(emit_error), file=sys.stderr)
        return 2
    finally:
        resources.close()
        if store:
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
