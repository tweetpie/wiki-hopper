"""Build a bounded, reproducible Wikipedia graph in configured languages.

This proof-of-concept imports MediaWiki Action API snapshots, rather than full
dumps. Pages keep their own edition's categories and disambiguation structure;
only ordinary articles with the same verified Wikidata item are joined.

Examples::

    python -m wikihopper sample --seed en:Apple --seed es:Manzana
    python -m wikihopper sample --offline-fixture fixture.json --seed en:Apple

An offline fixture is {"pages": [PAGE, ...], "redirects": [REDIRECT, ...]}.
PAGE has language, title, page_id, namespace (0 or 14), wikidata_id,
disambiguation (boolean), categories (localized full titles), langlinks
({language: title}), category_members (titles for category pages), links
(ordinary article titles), wikitext, revision_id, and optionally timestamp.
Only disambiguation wikitext's leading bullet links create membership edges;
an optional "members" array is deliberately not treated as evidence.
REDIRECT has language, frm, to_. Values come from actual edition snapshots;
fabricated fixtures are useful for tests, but are labelled fixture data.
--export-fixture saves fetched, normalized snapshots for later offline builds.
"""

import argparse
import json
from pathlib import Path

from ...config import CONFIG
from ..wikitext import title_key
from .graph import build_graph
from .sources import ApiSource, FixtureSource


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--seed", action="append", required=True, metavar="LANG:TITLE")
    parser.add_argument("--languages", nargs="+", help="Configured editions (default: multilingual.languages)")
    parser.add_argument("--max-pages", type=int, default=300)
    parser.add_argument("--depth", type=int, default=2)
    parser.add_argument("--max-category-members", type=int, default=20)
    parser.add_argument("--max-article-links", type=int, default=20,
        help="Inspect this many article links per page to discover explicit disambiguation targets")
    parser.add_argument("--max-categories", type=int, default=50)
    parser.add_argument("--max-disambiguation-members", type=int, default=100)
    parser.add_argument("--max-discovered-pages", type=int, default=3000,
        help="Cap all discovered page identities, including missing pages and redirects")
    parser.add_argument("--max-api-requests", type=int, default=1000)
    parser.add_argument("--max-continuation-pages", type=int, default=5)
    parser.add_argument("--request-delay", type=float, default=1.0,
        help="Minimum seconds between HTTP attempts (default: 1)")
    parser.add_argument("--retries", type=int, default=3, help="Additional retries for HTTP 429/503 (maximum: 5)")
    parser.add_argument("--user-agent", help="Descriptive HTTP User-Agent, preferably including a project/contact URL")
    parser.add_argument("--output", default=CONFIG["paths"]["output"])
    parser.add_argument("--offline-fixture", type=Path)
    parser.add_argument("--export-fixture", type=Path, help="Save normalized fetched snapshots for replay")
    args = parser.parse_args(argv)
    limits = {name: getattr(args, name) for name in ("max_pages", "depth", "max_category_members",
        "max_article_links", "max_categories", "max_disambiguation_members", "max_discovered_pages",
        "max_api_requests", "max_continuation_pages", "request_delay", "retries")}
    for name, value in limits.items():
        if value < (1 if name in ("max_pages", "max_discovered_pages", "max_api_requests", "max_continuation_pages") else 0):
            parser.error(f"--{name.replace('_', '-')} has an invalid negative or zero value")
    if not 0 <= args.request_delay <= 60:
        parser.error("--request-delay must be between 0 and 60 seconds")
    if not 0 <= args.retries <= 5:
        parser.error("--retries must be between 0 and 5")
    if args.user_agent and (not args.user_agent.strip() or "\n" in args.user_agent or "\r" in args.user_agent):
        parser.error("--user-agent must be a nonempty single line")
    codes = args.languages or CONFIG["multilingual"]["languages"]
    unknown = set(codes) - CONFIG["languages"].keys()
    if unknown:
        parser.error("Add language configuration before importing: " + ", ".join(sorted(unknown)))
    languages = {code: CONFIG["languages"][code] for code in dict.fromkeys(codes)}
    base_language = CONFIG["multilingual"]["base_language"]
    if base_language not in languages:
        base_language = next(iter(languages))
    seeds = []
    for seed in args.seed:
        language, colon, title = seed.partition(":")
        if not colon or language not in languages or not title_key(title):
            parser.error("Each --seed must be LANG:TITLE in the selected languages")
        seeds.append((language, title_key(title)))
    try:
        source = FixtureSource(args.offline_fixture, limits) if args.offline_fixture else ApiSource(languages, limits, args.user_agent)
        manifest, fetched = build_graph(source, seeds, languages, limits, args.output,
            base_language)
        if args.export_fixture:
            fixture = {"pages": [fetched[key] for key in sorted(fetched)],
                "redirects": [{"language": language, "frm": title, "to_": target}
                    for (language, title), target in sorted(source.redirects.items())],
                "namespace_prefixes": source.namespace_prefixes}
            args.export_fixture.parent.mkdir(parents=True, exist_ok=True)
            args.export_fixture.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Import failed: {exc}\n")
    stats = manifest["statistics"]
    print(f"Built {stats['nodes']} nodes and {stats['edges']} edges in {args.output}")
    print("Bounded sample; inspect manifest.json for source, limits, and truncations.")


if __name__ == "__main__":
    main()
