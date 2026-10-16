"""Interactive explorer for Wikipedia category/disambiguation graphs built by
``python -m wikihopper sample`` or ``python -m wikihopper extract``. It imports
nodes.csv/edges.csv into an indexed SQLite database, then serves a web UI that
starts from a searched node and expands one capped neighborhood at a time.

Stdlib only (sqlite3 + http.server); the browser UI uses vis-network from a CDN.

Shared / local use (graph.db is enough — no Wikipedia dump required):
  python -m wikihopper explorer
  then open http://127.0.0.1:8000/

Optional:
  python -m wikihopper explorer --password secret   # Basic Auth (or WIKIHOPPER_PASSWORD)
  python -m wikihopper explorer --low-mem           # ~4 GB RAM hosts
  python -m wikihopper explorer --rebuild           # rebuild graph.db from CSVs (needs --out)
"""

import argparse
import os
import sys

from ..config import CONFIG
from .database import build_db
from .server import serve

DEFAULT_OUT = CONFIG["paths"]["output"]
DEFAULT_DB = CONFIG["paths"]["database"]
DEFAULT_XML = CONFIG["paths"]["xml"]
DEFAULT_INDEX = CONFIG["paths"]["index"]


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="directory holding nodes.csv / edges.csv (rebuild only)")
    ap.add_argument("--db", default=DEFAULT_DB,
                    help=f"sqlite db path (default {DEFAULT_DB})")
    ap.add_argument("--xml", default=None,
                    help="multistream .xml.bz2 for provenance (optional; omitted if missing)")
    ap.add_argument("--index", default=None,
                    help="multistream index .txt.bz2 (rebuild offsets only; optional)")
    ap.add_argument("--rebuild", action="store_true", help="force rebuild of graph.db from CSVs")
    ap.add_argument("--build-only", action="store_true", help="build db and exit")
    ap.add_argument("--host", default=CONFIG["explorer"]["host"],
                    help="bind address (default localhost)")
    ap.add_argument("--port", type=int, default=CONFIG["explorer"]["port"])
    ap.add_argument("--password", default=None,
                    help="shared Basic Auth password (or set WIKIHOPPER_PASSWORD); "
                         "if unset, auth is disabled")
    ap.add_argument("--low-mem", action=argparse.BooleanOptionalAction,
                    default=CONFIG["explorer"]["low_memory"],
                    help="small SQLite cache + tighter path budgets (for ~4 GB RAM hosts)")
    args = ap.parse_args(argv)

    db_path = args.db
    xml_path = args.xml if args.xml is not None else DEFAULT_XML
    index_path = args.index if args.index is not None else DEFAULT_INDEX

    if args.rebuild or args.build_only:
        if not os.path.exists(db_path) or args.rebuild:
            build_db(args.out, db_path, index_path if os.path.exists(index_path) else None)
        else:
            print(f"[db] {db_path} already exists; pass --rebuild to refresh", flush=True)
        if args.build_only:
            return
    elif not os.path.exists(db_path):
        print(f"ERROR: database not found: {db_path}\n"
              f"  Place graph.db there (shared package), or pass --db PATH,\n"
              f"  or rebuild with: python -m wikihopper explorer --rebuild --out DIR",
              file=sys.stderr)
        sys.exit(1)
    else:
        print(f"[db] using existing {db_path} "
              f"({os.path.getsize(db_path)/1e9:.1f} GB)",
              flush=True)

    if not os.path.exists(xml_path):
        xml_path = None  # provenance disabled; browsing still works

    password = args.password or os.environ.get("WIKIHOPPER_PASSWORD") or None
    serve(db_path, xml_path, args.host, args.port,
          password=password, low_mem=args.low_mem)


if __name__ == "__main__":
    main()
