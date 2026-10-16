"""Import nodes.csv/edges.csv and optional evidence into an indexed SQLite database."""

import bz2
import csv
import json
import os
import sqlite3
import sys
import time

from ..config import EDGE_WEIGHTS, search_key
from .provenance import norm_title

csv.field_size_limit(10_000_000)


def _load_offsets(index_path: str) -> dict:
    """title -> byte offset of its multistream sub-stream (for provenance)."""
    print(f"[db] loading page offsets from index ({index_path})...", flush=True)
    t = time.time()
    off = {}
    with bz2.open(index_path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            p = line.rstrip("\n").split(":", 2)
            if len(p) == 3:
                try:
                    off[norm_title(p[2])] = int(p[0])
                except ValueError:
                    pass
    print(f"[db] {len(off):,} offsets loaded ({time.time() - t:.0f}s)", flush=True)
    return off


def build_db(out_dir: str, db_path: str, index_path: str | None = None):
    nodes_csv = os.path.join(out_dir, "nodes.csv")
    edges_csv = os.path.join(out_dir, "edges.csv")
    redirects_csv = os.path.join(out_dir, "redirects.csv")
    for p in (nodes_csv, edges_csv):
        if not os.path.exists(p):
            print(f"ERROR: missing {p} (import data with python -m wikihopper sample "
                  "or python -m wikihopper extract first)", file=sys.stderr)
            sys.exit(1)

    if os.path.exists(db_path):
        os.remove(db_path)

    with open(nodes_csv, "r", encoding="utf-8", newline="") as f:
        csv_offsets = "offset" in (csv.DictReader(f).fieldnames or [])
    offsets = (_load_offsets(index_path)
               if not csv_offsets and index_path and os.path.exists(index_path) else {})

    print(f"[db] building {db_path}", flush=True)
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;
        PRAGMA temp_store = MEMORY;
        PRAGMA cache_size = -1048576;   -- ~1 GiB page cache
        CREATE TABLE nodes (id INTEGER PRIMARY KEY, title TEXT, type TEXT,
                            tl TEXT, off INTEGER, language TEXT NOT NULL,
                            page_id INTEGER, wikidata_id TEXT);
        CREATE TABLE edges (src INTEGER, dst INTEGER, type TEXT, weight REAL);
        CREATE TABLE redirects (frm TEXT, to_ TEXT, language TEXT NOT NULL);
        CREATE TABLE evidence (src INTEGER, dst INTEGER, type TEXT, evidence TEXT);
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT);
        """
    )

    # ---- nodes ---- #
    t0 = time.time()
    n = 0
    with open(nodes_csv, "r", encoding="utf-8", newline="") as f:
        r = csv.DictReader(f)
        batch = []
        for row in r:
            if not all(row.get(k) is not None for k in ("id", "title", "type")):
                continue
            nid, title, typ = row["id"], row["title"], row["type"]
            language = row.get("language") or "en"
            batch.append((int(nid), title, typ, search_key(title, language),
                          (int(row["offset"]) if row.get("offset") else None) if csv_offsets
                          else (offsets.get(title) if language == "en" else None),
                          language, int(row["page_id"]) if row.get("page_id") else None,
                          row.get("wikidata_id") or None))
            if len(batch) >= 50000:
                cur.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?)", batch)
                n += len(batch)
                batch.clear()
        if batch:
            cur.executemany("INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?)", batch)
            n += len(batch)
    con.commit()
    print(f"[db] inserted {n:,} nodes ({time.time() - t0:.0f}s)", flush=True)

    # ---- redirects (for provenance link resolution) ---- #
    if os.path.exists(redirects_csv):
        tr = time.time()
        m = 0
        with open(redirects_csv, "r", encoding="utf-8", newline="") as f:
            r = csv.DictReader(f)
            batch = []
            for row in r:
                frm = row.get("from_title", row.get("from", row.get("frm")))
                to = row.get("to_title", row.get("to", row.get("to_")))
                if frm is None or to is None:
                    continue
                batch.append((frm, to, row.get("language") or "en"))
                if len(batch) >= 100000:
                    cur.executemany("INSERT INTO redirects VALUES (?,?,?)", batch)
                    m += len(batch)
                    batch.clear()
            if batch:
                cur.executemany("INSERT INTO redirects VALUES (?,?,?)", batch)
                m += len(batch)
        con.commit()
        print(f"[db] inserted {m:,} redirects ({time.time() - tr:.0f}s)", flush=True)

    # ---- edges ---- #
    t1 = time.time()
    m = 0
    with open(edges_csv, "r", encoding="utf-8", newline="") as f:
        r = csv.reader(f)
        next(r, None)
        batch = []
        for row in r:
            if len(row) != 4:
                continue
            src, dst, typ, w = row
            # Rebuild under the current cost model, including older exports.
            weight = EDGE_WEIGHTS[typ]
            batch.append((int(src), int(dst), typ, weight))
            if len(batch) >= 100000:
                cur.executemany("INSERT INTO edges VALUES (?,?,?,?)", batch)
                m += len(batch)
                batch.clear()
                if m % 5_000_000 == 0:
                    print(f"[db] {m:,} edges ({time.time() - t1:.0f}s)", flush=True)
        if batch:
            cur.executemany("INSERT INTO edges VALUES (?,?,?,?)", batch)
            m += len(batch)
    con.commit()
    print(f"[db] inserted {m:,} edges ({time.time() - t1:.0f}s)", flush=True)

    provenance_path = os.path.join(out_dir, "provenance.jsonl")
    if os.path.exists(provenance_path):
        batch = []
        with open(provenance_path, encoding="utf-8") as f:
            for line_number, line in enumerate(f, 1):
                if not line.strip():
                    continue
                record = json.loads(line)
                if not isinstance(record.get("evidence"), dict):
                    raise ValueError(f"{provenance_path}:{line_number}: evidence must be an object")
                batch.append((int(record["src"]), int(record["dst"]), record["type"],
                              json.dumps(record["evidence"], ensure_ascii=False)))
                if len(batch) >= 50000:
                    cur.executemany("INSERT INTO evidence VALUES (?,?,?,?)", batch)
                    batch.clear()
        if batch:
            cur.executemany("INSERT INTO evidence VALUES (?,?,?,?)", batch)
    manifest_path = os.path.join(out_dir, "manifest.json")
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
        cur.execute("INSERT INTO metadata VALUES (?,?)",
                    ("manifest", json.dumps(manifest, ensure_ascii=False)))
    con.commit()

    # ---- indexes ---- #
    t2 = time.time()
    print("[db] building indexes (this is the slow part)...", flush=True)
    cur.executescript(
        """
        CREATE INDEX idx_nodes_tl ON nodes(tl);
        CREATE INDEX idx_nodes_language_tl ON nodes(language, tl);
        CREATE INDEX idx_edges_src ON edges(src);
        CREATE INDEX idx_edges_dst ON edges(dst);
        CREATE INDEX idx_redirects_frm ON redirects(frm);
        CREATE INDEX idx_redirects_language_frm ON redirects(language, frm);
        CREATE INDEX idx_evidence_edge ON evidence(src, dst, type);
        """
    )
    con.commit()
    print(f"[db] indexes built ({time.time() - t2:.0f}s)", flush=True)
    con.close()
    size = os.path.getsize(db_path) / 1e9
    print(f"[db] done: {db_path} ({size:.1f} GB)", flush=True)
