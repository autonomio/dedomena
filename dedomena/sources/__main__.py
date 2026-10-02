"""Agent-friendly JSON CLI. Credentials are read exclusively from the environment."""
import argparse
from datetime import date
import json
import sys
import time

from . import EPO, EuropePMC, OpenAlex, SourceError, Store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("search", "fetch", "quota", "usage", "replay", "benchmark"))
    parser.add_argument("source", choices=("openalex", "europepmc", "epo"))
    parser.add_argument("query", nargs="?")
    parser.add_argument("--store", help="Shared SQLite snapshot/allowance store")
    parser.add_argument("--profile", choices=("discovery", "evidence"), default="discovery")
    parser.add_argument("--filter")
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--max-pages", type=int)
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--end-date", type=date.fromisoformat)
    args = parser.parse_args(argv)
    if args.max_pages is not None and args.max_pages < 1:
        parser.error("--max-pages must be positive")
    if args.filter and args.source != "openalex":
        parser.error("--filter is supported by openalex only")
    if args.action in ("fetch", "replay") and not args.query:
        parser.error("this action needs an identifier")
    if args.action in ("search", "benchmark") and not args.query and not (args.source == "openalex" and args.filter):
        parser.error("search needs a query, or an OpenAlex filter")
    if bool(args.start_date) != bool(args.end_date) or (args.start_date and args.source != "epo"):
        parser.error("date partitions require both dates and the epo source")
    store = Store(args.store) if args.store else None
    constructors = {"openalex": OpenAlex, "europepmc": EuropePMC, "epo": EPO}
    kwargs = {"store": store} if store else {}
    if args.source == "openalex":
        kwargs["profile"] = args.profile
    elif args.source == "europepmc":
        kwargs["result_type"] = "core" if args.profile == "evidence" else "lite"
    source = constructors[args.source](**kwargs)
    def emit(value):
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
    try:
        with source:
            if args.action == "quota":
                emit(source.quota())
            elif args.action == "usage":
                emit(source.usage())
            elif args.action == "replay":
                raw = source.store.replay(args.query)
                emit({"provenance": raw.provenance.to_dict(), "body": raw.body.decode("utf-8")})
            elif args.action == "fetch":
                emit(source.fetch(args.query, refresh=args.refresh).to_dict())
            else:
                if args.source == "openalex":
                    pages = source.search(args.query, filter=args.filter, refresh=args.refresh)
                elif args.source == "epo" and args.start_date:
                    pages = source.search_partitioned(args.query, args.start_date, args.end_date,
                                                       refresh=args.refresh,
                                                       biblio=args.profile == "evidence")
                elif args.source == "epo":
                    pages = source.search(args.query, refresh=args.refresh, biblio=args.profile == "evidence")
                else:
                    pages = source.search(args.query, refresh=args.refresh)
                limit = args.max_pages if args.max_pages else (1 if args.action == "benchmark" else None)
                started = time.perf_counter()
                totals = {"pages": 0, "records": 0, "credits_used": 0,
                          "wire_bytes": 0, "decoded_bytes": 0, "cache_hits": 0}
                last = None
                for page in pages:
                    last = page
                    totals["pages"] += 1
                    totals["records"] += len(page.records)
                    totals["credits_used"] += page.credits_used
                    totals["wire_bytes"] += page.wire_bytes
                    totals["decoded_bytes"] += page.decoded_bytes
                    totals["cache_hits"] += int(page.cache_hit)
                    if args.action == "search":
                        emit(page.to_dict())
                    if limit and totals["pages"] >= limit:
                        break
                emit({"source": args.source, **totals,
                      "elapsed_seconds": round(time.perf_counter() - started, 3),
                      "complete": bool(last and last.complete),
                      "next_cursor": last.next_cursor if last else None,
                      "records_seen": last.records_seen if last else 0,
                      "last_parameters": dict(last.provenance.parameters) if last else {},
                      "usage": source.usage()})
    except (SourceError, ValueError, KeyError) as error:
        # KeyError is a snapshot ID, never a raw upstream body or credential.
        message = "snapshot not found" if isinstance(error, KeyError) else str(error)
        print(json.dumps({"source": args.source, "error": type(error).__name__,
                          "message": message, "complete": False}), file=sys.stderr)
        return 2
    finally:
        if store:
            store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
