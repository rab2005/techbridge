"""Command-line entry point.

    python -m techbridge scrape --limit 25     # Phase 1: ingest listings
    python -m techbridge enrich --limit 25     # Phase 2: AI summaries
    python -m techbridge embed                 # Phase 4: vectors for reverse search
    python -m techbridge serve                 # Phase 3: run the web app
    python -m techbridge update                # all of the above for new listings (runs daily)
    python -m techbridge stats
"""

import argparse
import os
import sys
from datetime import datetime

from . import config, db


def main(argv=None):
    parser = argparse.ArgumentParser(prog="techbridge")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("scrape", help="Ingest technologies from TTO websites")
    p.add_argument("--institution", "-i", action="append", choices=list(config.INSTITUTIONS),
                   help="Only scrape this institution (repeatable). Default: all.")
    p.add_argument("--limit", type=int, default=25,
                   help="Max NEW technologies per institution this run (default 25, 0 = no limit)")
    p.add_argument("--refresh", action="store_true", help="Re-fetch technologies already in the DB")

    p = sub.add_parser("enrich", help="Generate AI business summaries with Claude")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--force", action="store_true", help="Re-enrich records that already have summaries")

    p = sub.add_parser("embed", help="Compute embeddings for reverse search (runs locally)")
    p.add_argument("--force", action="store_true")

    p = sub.add_parser("serve", help="Run the web app")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")

    sub.add_parser("update", help="Scrape new listings, summarize them (if a Claude key is set), "
                                  "and refresh search. This is what the daily schedule runs.")

    sub.add_parser("stats", help="Show database counts")

    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn
        uvicorn.run("techbridge.api:app", port=args.port, reload=args.reload)
        return

    conn = db.connect()
    if args.command == "scrape":
        from .scraper import ScrapeInProgress, scrape_all
        try:
            n = scrape_all(conn, args.institution, args.limit or None, args.refresh)
        except ScrapeInProgress as e:
            sys.exit(f"Not scraping: {e}.")
        print(f"Saved {n} technologies.")
    elif args.command == "update":
        update(conn)
    elif args.command == "enrich":
        import anthropic
        from .enrich import enrich_pending
        try:
            print(f"Enriched {enrich_pending(conn, limit=args.limit, force=args.force)} technologies.")
        except (anthropic.AuthenticationError, TypeError):
            sys.exit("Claude API key missing or invalid. Copy .env.example to .env and set ANTHROPIC_API_KEY.")
        except anthropic.NotFoundError:
            sys.exit(f"Model {config.CLAUDE_MODEL!r} not found. Check CLAUDE_MODEL in .env.")
    elif args.command == "embed":
        from .embeddings import embed_pending
        print(f"Embedded {embed_pending(conn, force=args.force)} technologies.")
    elif args.command == "stats":
        for r in conn.execute("""SELECT institution, COUNT(*), COUNT(enriched_at), COUNT(embedding)
                                 FROM technologies GROUP BY institution"""):
            print(f"{config.INSTITUTIONS.get(r[0], r[0]):32} {r[1]:5} scraped "
                  f"{r[2]:5} enriched {r[3]:5} embedded")


def update(conn):
    """Scrape -> enrich -> embed, for new listings only. Safe to run as often as you like."""
    from .embeddings import embed_pending
    from .scraper import ScrapeInProgress, scrape_all

    print(f"=== TechBridge update {datetime.now():%Y-%m-%d %H:%M} ===")
    try:
        print(f"Scraped {scrape_all(conn)} new technologies.")
    except ScrapeInProgress as e:
        print(f"Skipping scrape: {e}.")

    if os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN"):
        import anthropic
        from .enrich import enrich_pending
        try:
            print(f"Enriched {enrich_pending(conn)} technologies.")
        except (anthropic.AuthenticationError, anthropic.NotFoundError, TypeError) as e:
            print(f"Skipping AI summaries: {e}")
    else:
        print("Skipping AI summaries (no ANTHROPIC_API_KEY in .env).")

    print(f"Embedded {embed_pending(conn)} technologies.")
    print("Done.")


if __name__ == "__main__":
    main()
