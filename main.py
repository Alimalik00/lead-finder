#!/usr/bin/env python3
"""Lead Finder -- discover local businesses that need a website or software.

Pipeline
--------
1. Resolve the search area (Geocoding API).
2. Discover businesses (Places Text/Nearby Search, paginated).
3. Enrich them with phone, website and opening status (Places Details).
4. Filter by review count, rating and operating status.
5. Audit each website (technical, UX and business-capability checks).
6. Measure performance (PageSpeed Insights).
7. Score and prioritise every lead.
8. Write outreach copy (Claude, or deterministic templates).
9. Export a formatted Excel workbook.

Run it with::

    python main.py --country "United States" --city Austin --category Dentists

or, on Windows::

    py main.py --country "United States" --city Austin --category Dentists
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence

# Make the project importable no matter which directory the script is run from.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from tqdm import tqdm  # noqa: E402

from ai_summary import generate_summaries  # noqa: E402
from cache import JsonCache  # noqa: E402
from config import AppConfig, load_config  # noqa: E402
from export import export_leads  # noqa: E402
from google_places import (  # noqa: E402
    GooglePlacesClient,
    PlacesError,
    filter_places,
)
from http_client import build_client  # noqa: E402
from lead_scoring import LeadScorer  # noqa: E402
from logging_setup import setup_logging  # noqa: E402
from models import Lead, Place  # noqa: E402
from pagespeed import PageSpeedClient  # noqa: E402
from website_audit import WebsiteAuditor, merge_pagespeed_signals  # noqa: E402

BANNER = r"""
  _                    _   _____ _           _
 | |    ___  __ _  __| | |  ___(_)_ __   __| | ___ _ __
 | |   / _ \/ _` |/ _` | | |_  | | '_ \ / _` |/ _ \ '__|
 | |__|  __/ (_| | (_| | |  _| | | | | | (_| |  __/ |
 |_____\___|\__,_|\__,_| |_|   |_|_| |_|\__,_|\___|_|
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="lead_finder",
        description=(
            "Find local businesses that likely need a new website or software "
            "services, score them as leads, and export to Excel."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python main.py --country \"United States\" --city Austin --category Dentists\n"
            "  python main.py --config config.yaml\n"
            "  python main.py --city Dubai --country UAE --category Restaurants \\\n"
            "                 --min-reviews 50 --radius 10000 --no-website-only\n"
            "  python main.py --interactive\n"
        ),
    )

    search = parser.add_argument_group("search")
    search.add_argument("--country", help="Country name, e.g. \"United States\"")
    search.add_argument("--state", help="State or province (optional)")
    search.add_argument("--city", help="City name")
    search.add_argument(
        "--category",
        help="Business category, e.g. Dentists / Restaurants / Roofing / HVAC",
    )
    search.add_argument("--keyword", help="Extra keyword appended to the query")
    search.add_argument(
        "--country-code", help="Two-letter country code used to bias results (e.g. us)"
    )
    search.add_argument(
        "--place-type",
        help="Google Places type override for nearby search (e.g. dentist)",
    )
    search.add_argument(
        "--search-mode",
        choices=("text", "nearby"),
        help="text = Places Text Search (default), nearby = strict radius search",
    )
    search.add_argument("--radius", type=int, help="Search radius in metres")
    search.add_argument(
        "--max-results", type=int, help="Maximum number of businesses to fetch"
    )
    search.add_argument("--min-reviews", type=int, help="Minimum Google review count")
    search.add_argument("--min-rating", type=float, help="Minimum Google rating (0-5)")
    search.add_argument(
        "--no-website-only",
        action="store_true",
        help="Keep only businesses with no website (the hottest leads)",
    )

    stages = parser.add_argument_group("pipeline stages")
    stages.add_argument(
        "--no-audit", action="store_true", help="Skip the website audit stage"
    )
    stages.add_argument(
        "--no-pagespeed", action="store_true", help="Skip PageSpeed Insights"
    )
    stages.add_argument(
        "--no-ai",
        action="store_true",
        help="Skip the AI model and use template copy (still fully populated)",
    )
    stages.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover and list businesses only; no audits, scoring or export",
    )

    output = parser.add_argument_group("output")
    output.add_argument("--output", help="Explicit output .xlsx path")
    output.add_argument("--output-dir", help="Directory for generated files")
    output.add_argument("--csv", action="store_true", help="Also write a CSV copy")
    output.add_argument("--json", action="store_true", help="Also write a JSON dump")

    runtime = parser.add_argument_group("runtime")
    runtime.add_argument("--config", help="Path to a YAML config file")
    runtime.add_argument("--env-file", help="Path to a .env file")
    runtime.add_argument(
        "--api-version",
        choices=("legacy", "v1"),
        help="Which Google Places API generation to call",
    )
    runtime.add_argument("--no-cache", action="store_true", help="Bypass the disk cache")
    runtime.add_argument(
        "--clear-cache", action="store_true", help="Delete cached responses and exit"
    )
    runtime.add_argument(
        "--interactive", action="store_true", help="Prompt for the search parameters"
    )
    runtime.add_argument("--log-level", help="DEBUG / INFO / WARNING / ERROR")
    runtime.add_argument("-v", "--verbose", action="store_true", help="Shorthand for DEBUG")
    runtime.add_argument("-q", "--quiet", action="store_true", help="Only warnings and errors")

    return parser


def apply_overrides(config: AppConfig, args: argparse.Namespace) -> None:
    """Layer command-line flags on top of the loaded configuration."""
    search = config.search

    if args.country:
        search.country = args.country
    if args.state:
        search.state = args.state
    if args.city:
        search.city = args.city
    if args.category:
        search.category = args.category
    if args.keyword:
        search.keyword = args.keyword
    if args.country_code:
        search.country_code = args.country_code
    if args.place_type:
        search.place_type = args.place_type
    if args.search_mode:
        search.search_mode = args.search_mode
    if args.radius is not None:
        search.radius_m = args.radius
    if args.max_results is not None:
        search.max_results = args.max_results
    if args.min_reviews is not None:
        search.min_reviews = args.min_reviews
    if args.min_rating is not None:
        search.min_rating = args.min_rating
    if args.no_website_only:
        search.require_no_website = True

    if args.no_audit:
        config.audit.enabled = False
    if args.no_pagespeed:
        config.pagespeed.enabled = False
    if args.no_ai:
        config.ai.enabled = False

    if args.output_dir:
        config.output.directory = args.output_dir
    if args.csv:
        config.output.write_csv = True
    if args.json:
        config.output.write_json = True

    if args.api_version:
        config.google.api_version = args.api_version
    if args.no_cache:
        config.cache.enabled = False

    if args.verbose:
        config.log_level = "DEBUG"
    elif args.log_level:
        config.log_level = args.log_level.upper()


def prompt_for_missing(config: AppConfig, force: bool) -> None:
    """Interactively fill in the required search fields.

    Only prompts when attached to a terminal, so the tool stays usable in cron
    jobs and CI where a blocked ``input()`` would hang the process.
    """
    search = config.search
    missing = search.validate()
    if not force and not missing:
        return

    if not sys.stdin.isatty():
        return

    print("\nEnter the search parameters (press Enter to accept the default).\n")

    def ask(label: str, current: str) -> str:
        suffix = f" [{current}]" if current else ""
        answer = input(f"  {label}{suffix}: ").strip()
        return answer or current

    search.country = ask("Country", search.country)
    search.state = ask("State / Province (optional)", search.state or "") or None
    search.city = ask("City", search.city)
    search.category = ask("Business category", search.category)

    if force:
        search.radius_m = _ask_int("Search radius in metres", search.radius_m)
        search.max_results = _ask_int("Maximum businesses to fetch", search.max_results)
        search.min_reviews = _ask_int("Minimum review count", search.min_reviews)
        search.min_rating = _ask_float("Minimum rating (0-5)", search.min_rating)
    print()


def _ask_int(label: str, current: int) -> int:
    answer = input(f"  {label} [{current}]: ").strip()
    if not answer:
        return current
    try:
        return int(answer)
    except ValueError:
        print(f"    '{answer}' is not a whole number; keeping {current}.")
        return current


def _ask_float(label: str, current: float) -> float:
    answer = input(f"  {label} [{current}]: ").strip()
    if not answer:
        return current
    try:
        return float(answer)
    except ValueError:
        print(f"    '{answer}' is not a number; keeping {current}.")
        return current


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------


def run_pipeline(
    config: AppConfig,
    dry_run: bool = False,
    quiet: bool = False,
) -> Optional[Path]:
    """Execute the full pipeline. Returns the workbook path, or ``None``."""
    logger = setup_logging(config.log_level, config.log_path(), quiet=quiet)
    cache = JsonCache(
        config.cache_dir(), config.cache.ttl_hours, config.cache.enabled
    )

    search = config.search
    logger.info(
        "Searching for '%s' in %s (radius %dm, up to %d results)",
        search.category, search.location_string, search.radius_m, search.max_results,
    )

    # -- 1 & 2: discover -----------------------------------------------------
    places_http = build_client(config.http, config.google.requests_per_second)
    try:
        places_client = GooglePlacesClient(config.google, places_http, cache)
        places = places_client.search(search)

        if not places:
            logger.warning(
                "No businesses found. Try a wider radius, a different category, "
                "or --search-mode text."
            )
            return None

        # -- 3: enrich -------------------------------------------------------
        logger.info("Fetching contact details for %d business(es)", len(places))
        with tqdm(total=len(places), desc="Place details", unit="biz", leave=False) as bar:
            places = places_client.enrich(places, progress=bar)
    finally:
        places_http.close()

    # -- 4: filter -----------------------------------------------------------
    places = filter_places(places, search)
    if not places:
        logger.warning("Every result was removed by the filters. Loosen them and retry.")
        return None
    logger.info("%d business(es) passed the filters", len(places))

    if dry_run:
        _print_dry_run(places)
        return None

    leads: List[Lead] = [Lead(place=place) for place in places]

    # -- 5: audit websites ---------------------------------------------------
    website_leads = [lead for lead in leads if lead.place.has_website]
    logger.info(
        "%d business(es) have a website, %d have none",
        len(website_leads), len(leads) - len(website_leads),
    )

    if config.audit.enabled and website_leads:
        # Each audited site spawns up to `asset_check_workers` extra probes,
        # so size the connection pool for the full fan-out.
        audit_http = build_client(
            config.http,
            pool_size=max(
                32, config.audit.max_workers * (config.audit.asset_check_workers + 2)
            ),
        )
        try:
            auditor = WebsiteAuditor(config.audit, audit_http)
            urls = [lead.place.website for lead in website_leads]
            with tqdm(total=len(urls), desc="Website audit", unit="site", leave=False) as bar:
                audits = auditor.audit_many(urls, progress=bar)
            for lead in website_leads:
                lead.audit = audits.get(lead.place.website)
        finally:
            audit_http.close()
    elif website_leads:
        logger.info("Website audit disabled (--no-audit)")

    # -- 6: PageSpeed --------------------------------------------------------
    if config.pagespeed.enabled and website_leads:
        # Only measure sites that actually loaded -- PSI on a dead host burns
        # quota and returns nothing useful.
        measurable = [
            lead for lead in website_leads
            if lead.audit is None or lead.audit.reachable
        ]
        skipped = len(website_leads) - len(measurable)
        if skipped:
            logger.info("Skipping PageSpeed for %d unreachable site(s)", skipped)

        if measurable:
            psi_http = build_client(
                config.http,
                config.pagespeed.requests_per_second,
                pool_size=max(10, config.pagespeed.max_workers * 2),
            )
            try:
                psi_client = PageSpeedClient(config.pagespeed, psi_http, cache)
                urls = [lead.place.website for lead in measurable]
                total = len(urls)
                with tqdm(total=total, desc="PageSpeed", unit="site", leave=False) as bar:
                    results = psi_client.analyze_many(urls, progress=bar)
                for lead in measurable:
                    lead.pagespeed = results.get(lead.place.website)
            finally:
                psi_http.close()
    elif website_leads:
        logger.info("PageSpeed Insights disabled (--no-pagespeed)")

    # -- 6b: let real Lighthouse data override the HTML-only heuristics ------
    for lead in website_leads:
        if lead.audit is not None:
            merge_pagespeed_signals(lead.audit, lead.pagespeed)

    # -- 7: score ------------------------------------------------------------
    scorer = LeadScorer(config.scoring)
    for lead in leads:
        lead.score = scorer.score(lead.place, lead.audit, lead.pagespeed)
    leads.sort(key=lambda l: (l.score.score, l.place.review_count), reverse=True)

    # -- 8: outreach copy ----------------------------------------------------
    with tqdm(total=len(leads), desc="Lead summaries", unit="lead", leave=False) as bar:
        generate_summaries(leads, config.ai, progress=bar)

    # -- 9: export -----------------------------------------------------------
    output_path = export_leads(leads, config, _explicit_output_path(config))

    _print_report(leads, output_path, cache)
    return output_path


def _explicit_output_path(config: AppConfig) -> Optional[Path]:
    """Honour ``--output`` when it was supplied."""
    explicit = getattr(config, "_explicit_output", None)
    return Path(explicit) if explicit else None


# ---------------------------------------------------------------------------
# Console reporting
# ---------------------------------------------------------------------------


def _print_dry_run(places: Sequence[Place]) -> None:
    print(f"\nFound {len(places)} business(es):\n")
    print(f"  {'#':>3}  {'Business':<38} {'Rating':>6} {'Reviews':>8}  Website")
    print(f"  {'-' * 3}  {'-' * 38} {'-' * 6} {'-' * 8}  {'-' * 30}")
    for index, place in enumerate(places, start=1):
        rating = f"{place.rating:.1f}" if place.rating is not None else "-"
        website = place.website or "(none)"
        print(
            f"  {index:>3}  {_truncate(place.name, 38):<38} {rating:>6} "
            f"{place.review_count:>8}  {_truncate(website, 44)}"
        )
    print("\nDry run: no audits, scoring or export were performed.\n")


def _print_report(leads: Sequence[Lead], output_path: Path, cache: JsonCache) -> None:
    counts: Dict[str, int] = {"Hot": 0, "Warm": 0, "Cold": 0}
    for lead in leads:
        counts[lead.score.priority] = counts.get(lead.score.priority, 0) + 1

    no_website = sum(1 for lead in leads if not lead.place.has_website)
    with_email = sum(1 for lead in leads if lead.email)

    print("\n" + "=" * 78)
    print(f"  {len(leads)} lead(s) scored")
    print(
        f"  Hot: {counts['Hot']}   Warm: {counts['Warm']}   Cold: {counts['Cold']}"
        f"   |   No website: {no_website}   Emails found: {with_email}"
    )
    print("=" * 78)

    top = leads[:10]
    if top:
        print(f"\n  Top {len(top)} lead(s):\n")
        print(f"  {'Score':>5}  {'Priority':<8}  {'Business':<34}  Why")
        print(f"  {'-' * 5}  {'-' * 8}  {'-' * 34}  {'-' * 40}")
        for lead in top:
            reason = lead.score.reasons[0].detail if lead.score.reasons else "-"
            print(
                f"  {lead.score.score:>5}  {lead.score.priority:<8}  "
                f"{_truncate(lead.place.name, 34):<34}  {_truncate(reason, 46)}"
            )

    print(f"\n  {cache.stats()}")
    print(f"  Workbook: {output_path}\n")


def _truncate(text: str, width: int) -> str:
    text = str(text or "")
    return text if len(text) <= width else text[: width - 1] + "…"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config, args.env_file)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    apply_overrides(config, args)

    if args.clear_cache:
        cache = JsonCache(config.cache_dir(), config.cache.ttl_hours, enabled=True)
        removed = cache.clear()
        print(f"Cleared {removed} cached response(s) from {config.cache_dir()}")
        return 0

    if not args.quiet:
        print(BANNER)

    prompt_for_missing(config, force=args.interactive)

    problems = config.search.validate()
    if problems:
        print("Missing or invalid search parameters:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print(
            "\nSupply them as flags, in a --config file, or run with --interactive.\n"
            "Example:\n"
            '  python main.py --country "United States" --city Austin --category Dentists',
            file=sys.stderr,
        )
        return 2

    if not config.google.api_key:
        print(
            "No Google API key found.\n"
            "  1. cp .env.example .env\n"
            "  2. Set GOOGLE_MAPS_API_KEY in .env\n"
            "  3. Enable the Places, Geocoding and PageSpeed Insights APIs for that key.",
            file=sys.stderr,
        )
        return 2

    # Stash --output so export_leads can pick it up without widening AppConfig.
    if args.output:
        setattr(config, "_explicit_output", args.output)

    try:
        run_pipeline(config, dry_run=args.dry_run, quiet=args.quiet)
    except PlacesError as exc:
        print(f"\nGoogle Places error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted. No output was written.", file=sys.stderr)
        return 130
    except Exception as exc:  # pragma: no cover - top-level safety net
        import logging

        logging.getLogger("lead_finder").exception("Run failed")
        print(f"\nUnexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("See the log file for the full traceback.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
