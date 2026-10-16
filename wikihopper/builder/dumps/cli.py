"""Extract configured Wikipedia editions from downloaded structural dumps.

python -m wikihopper extract --check-only
python -m wikihopper extract
python -m wikihopper explorer --build-only --rebuild

Uses disk-backed SQLite staging and streams compressed inputs. Categories are
explicit wikitext memberships, not categories introduced by template expansion.
The index supplies disambiguation titles; page_props supplies page types and
Wikidata identity. Disambiguation links include explicit wikilinks and the
edition-local primary-topic title convention. No network
access or third-party dependencies are needed.
"""

import argparse
from pathlib import Path
import sqlite3
import sys
import xml.etree.ElementTree as ET

from ...config import CONFIG
from .graph import export_graph, materialize
from .inputs import input_identity, select_inputs
from .staging import create_staging, import_edition, reuse_import, save_import_state, site_info


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dumps", type=Path, default=Path(CONFIG["paths"]["output"]) / "dumps",
                        help="download root (default: configured output/dumps)")
    parser.add_argument("--date", help="snapshot YYYYMM01 (default: newest local snapshot)")
    parser.add_argument("--languages", nargs="+", help="configured languages (default: all enabled editions)")
    parser.add_argument("--out", type=Path, default=Path(CONFIG["paths"]["output"]))
    parser.add_argument("--check-only", action="store_true", help="check input paths and XML editions; do not extract or write")
    parser.add_argument("--max-pages", type=int, default=0, help="XML pages per edition; 0 extracts the full dumps")
    parser.add_argument("--rebuild", action="store_true", help="replace existing extracted CSVs and staging database")
    parser.add_argument("--skip-import", action="store_true", help="reuse completed staging; rebuild only the graph and exports")
    args = parser.parse_args(argv)
    codes = args.languages or CONFIG["multilingual"]["languages"]
    if args.max_pages < 0:
        parser.error("--max-pages must be nonnegative")
    if args.skip_import and args.rebuild:
        parser.error("--skip-import and --rebuild cannot be combined")
    if len(codes) != len(set(codes)) or any(c not in CONFIG["languages"] for c in codes):
        parser.error("--languages must contain unique configured language codes")
    languages = {c: CONFIG["languages"][c] for c in codes}
    try:
        snapshot, inputs = select_inputs(args.dumps, args.date, languages)
        for code, paths in inputs.items():
            site_info(paths["xml"], languages[code]["wiki"])
            print(f"[{code}] {languages[code]['wiki']} / {snapshot}", flush=True)
            for name, path in paths.items():
                print(f"  {name}: {path} ({path.stat().st_size:,} bytes)", flush=True)
        if args.check_only:
            print("Inputs are present and XML editions match. No extraction was run. Checksums were not recomputed.")
            return 0
        output = args.out.resolve()
        stage = output / "dump_staging.sqlite"
        artifacts = [output / n for n in ("nodes.csv", "edges.csv", "redirects.csv", "provenance.jsonl", "manifest.json", "snapshots.json")]
        existing = [p for p in [stage, *artifacts] if p.exists()]
        if existing and not args.rebuild and not args.skip_import:
            raise ValueError(f"Existing extraction at {output}; use --rebuild to replace it or --out for another directory")
        output.mkdir(parents=True, exist_ok=True)
        if args.rebuild:
            for path in [stage, stage.with_name(stage.name + "-wal"), stage.with_name(stage.name + "-shm"), *artifacts]:
                path.unlink(missing_ok=True)
        if args.skip_import:
            if not stage.is_file():
                raise ValueError(f"Staging database not found: {stage}")
            con = sqlite3.connect(stage.as_uri() + "?mode=rw", uri=True)
            con.execute("PRAGMA temp_store=FILE")
            con.execute("PRAGMA cache_size=-65536")
        else:
            con = create_staging(stage)
        try:
            identity = input_identity(snapshot, inputs, args.max_pages)
            if args.skip_import:
                state = reuse_import(con, identity, inputs, languages)
                statistics = state["statistics"]
                print("[graph] reusing completed imports; XML/property extraction skipped", flush=True)
            else:
                state = {"inputs": identity, "complete": False, "canonical_titles": True, "statistics": {}}
                save_import_state(con, state)
                statistics = state["statistics"]
                for code in codes:
                    statistics[code] = import_edition(con, code, languages[code], inputs[code], args.max_pages)
                    save_import_state(con, state)
                state["complete"] = True
                save_import_state(con, state)
            conflicts = materialize(con)
            manifest = export_graph(con, output, snapshot, inputs, statistics, args.max_pages, conflicts, state)
        finally:
            con.close()
        stats = manifest["statistics"]
        print(f"Built {stats['nodes']:,} nodes and {stats['edges']:,} edges in {output}")
        print(f'Next: python -m wikihopper explorer --out "{output}" --db "{output / "graph.db"}" --build-only --rebuild')
        return 0
    except (OSError, ValueError, sqlite3.Error, ET.ParseError) as exc:
        print(f"Extraction error: {exc}", file=sys.stderr)
        return 1


def run():
    """Console entry point with the extractor's exit codes and messages."""
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nExtraction interrupted. If imports completed, rerun with --skip-import; otherwise use --rebuild.", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    run()
