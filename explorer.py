"""explorer.py

Interactive explorer for Wikipedia category/disambiguation graphs built by
wikihopper_multilingual.py or wikihopper_dumps.py. It imports nodes.csv/edges.csv
into an indexed SQLite database, then serves a web UI that starts from a searched
node and expands one capped neighborhood at a time.

Stdlib only (sqlite3 + http.server); the browser UI uses vis-network from a CDN.

Shared / local use (graph.db is enough — no Wikipedia dump required):
  python explorer.py
  then open http://127.0.0.1:8000/

Optional:
  python explorer.py --password secret   # Basic Auth (or WIKIHOPPER_PASSWORD)
  python explorer.py --low-mem           # ~4 GB RAM hosts
  python explorer.py --rebuild           # rebuild graph.db from CSVs (needs --out)
"""

from __future__ import annotations

import argparse
import base64
import bz2
import csv
import hmac
import html
import json
import os
import re
import heapq
import sqlite3
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

if __package__:
    from .wikihopper_config import CONFIG, EDGE_NAMES, EDGE_WEIGHTS, FORWARD_COSTS, REVERSE_COSTS, search_key, parse_search_seconds
    from .wikihopper_policy import normalize_policy, traversal_state
else:
    from wikihopper_config import CONFIG, EDGE_NAMES, EDGE_WEIGHTS, FORWARD_COSTS, REVERSE_COSTS, search_key, parse_search_seconds
    from wikihopper_policy import normalize_policy, traversal_state

csv.field_size_limit(10_000_000)

DEFAULT_OUT = CONFIG["paths"]["output"]
DEFAULT_DB = CONFIG["paths"]["database"]
DEFAULT_XML = CONFIG["paths"]["xml"]
DEFAULT_INDEX = CONFIG["paths"]["index"]
EDGE_TYPES = list(EDGE_NAMES)

# ---- regexes used for source snippets ---- #
_TEXT_RE = re.compile(r"<text[^>]*>(.*?)</text>", re.S)
_CAT_RE = re.compile(r"\[\[\s*[Cc]ategory\s*:\s*([^\]\|#]+)")
_LINK_RE = re.compile(r"\[\[\s*([^\[\]\|#]+)")
_WS_RE = re.compile(r"\s+")


def norm_title(t: str) -> str:
    """Normalize a wiki title/link for source-snippet lookup."""
    if not t:
        return ""
    if "&" in t:
        t = html.unescape(t)
    t = t.replace("_", " ").strip()
    if t.startswith(":"):
        t = t[1:].strip()
    t = _WS_RE.sub(" ", t)
    if not t:
        return ""
    low = t.lower()
    if low.startswith("category:"):
        name = t[9:].strip()
        return "Category:" + (name[:1].upper() + name[1:] if name else "")
    return t[:1].upper() + t[1:]


# --------------------------------------------------------------------------- #
# Database build
# --------------------------------------------------------------------------- #
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
            print(f"ERROR: missing {p} (import data with wikihopper_multilingual.py "
                  "or wikihopper_dumps.py first)", file=sys.stderr)
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


# --------------------------------------------------------------------------- #
# Query helpers
# --------------------------------------------------------------------------- #
def _connect_ro(db_path: str, low_mem: bool = False) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True,
                          check_same_thread=False)
    con.row_factory = sqlite3.Row
    if low_mem:
        # ~64 MiB page cache; spill temps to disk (4 GB mini-PC friendly)
        con.execute("PRAGMA cache_size = -65536")
        con.execute("PRAGMA temp_store = FILE")
    return con


def _columns(con, table):
    """Detect optional columns without mutating legacy shared databases."""
    return {r["name"] for r in con.execute(f"PRAGMA table_info({table})")}


def _node_fields(con, alias=""):
    prefix = alias + "." if alias else ""
    columns = _columns(con, "nodes")
    fields = [prefix + name for name in ("id", "title", "type")]
    for name, fallback in (("language", "'en'"), ("page_id", "NULL"),
                           ("wikidata_id", "NULL")):
        fields.append(prefix + name if name in columns else f"{fallback} AS {name}")
    return ", ".join(fields)


def q_search(con, query: str, limit: int = 30, language: str | None = None):
    raw = query.strip()
    q = raw.lower()
    if not q:
        return []
    # Exact (case-insensitive) matches. Distinct case variants that share a
    # lowercased key -- e.g. ".NET" (framework) vs ".net" (TLD), or
    # "'Tis the Season" vs "'Tis the SeaSon" -- are genuinely different pages
    # and are ALL returned here, never merged.
    language = language or None
    multilingual = "language" in _columns(con, "nodes")
    if language and not multilingual and language != "en":
        return []
    if language and multilingual:
        alternatives = [(language, search_key(raw, language))]
        excluded = [language]
    elif multilingual:
        alternatives = [(code, search_key(raw, code)) for code in CONFIG.get("languages", {})
                        if search_key(raw, code) != q]
        excluded = [code for code, _ in alternatives]
    else:
        alternatives, excluded = [], []

    def condition(prefix=False):
        operator = "LIKE" if prefix else "="
        suffix = "%" if prefix else ""
        terms, values = [], []
        if not (language and multilingual):
            default = f"tl {operator} ?"
            values.append(q + suffix)
            if excluded:
                default += " AND language NOT IN (" + ",".join("?" for _ in excluded) + ")"
                values.extend(excluded)
            terms.append("(" + default + ")")
        for code, key in alternatives:
            terms.append(f"(language = ? AND tl {operator} ?)")
            values.extend((code, key + suffix))
        return " OR ".join(terms), values

    fields = _node_fields(con)
    where, values = condition()
    exact = con.execute(f"SELECT {fields} FROM nodes WHERE {where} LIMIT ?",
                        (*values, limit)).fetchall()
    # Rank an exact case match to the typed query first, then articles ahead of
    # disambiguation/category, then the shorter title, for a stable ordering.
    type_rank = {"article": 0, "disambiguation": 1, "category": 2}

    def sort_key(r):
        return (0 if r["title"] == raw else 1,
                type_rank.get(r["type"], 3),
                len(r["title"]),
                r["title"], r["language"], r["id"])

    rows = sorted(exact, key=sort_key)
    seen = {r["id"] for r in rows}
    if len(rows) < limit:
        where, values = condition(prefix=True)
        for r in con.execute(
            f"SELECT {fields} FROM nodes WHERE {where} "
            "ORDER BY LENGTH(title), title, id LIMIT ?",
            (*values, limit),
        ):
            if r["id"] not in seen:
                rows.append(r)
                seen.add(r["id"])
            if len(rows) >= limit:
                break
    return [dict(r) for r in rows[:limit]]


def q_node(con, nid: int):
    r = con.execute(f"SELECT {_node_fields(con)} FROM nodes WHERE id = ?",
                    (nid,)).fetchone()
    return dict(r) if r else None


def q_dataset(con):
    """Describe the loaded database separately from enabled language profiles."""
    manifest = None
    if _columns(con, "metadata"):
        row = con.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()
        if row:
            manifest = json.loads(row["value"])
    if "language" in _columns(con, "nodes"):
        language_counts = {r["language"]: r["count"] for r in con.execute(
            "SELECT language, COUNT(*) count FROM nodes GROUP BY language ORDER BY language")}
    else:
        count = con.execute("SELECT COUNT(*) count FROM nodes").fetchone()["count"]
        language_counts = {"en": count} if count else {}
    return {"manifest": manifest, "language_counts": language_counts,
            "total_nodes": sum(language_counts.values()),
            "configured_languages": CONFIG.get("multilingual", {}).get("languages", ["en"]),
            "source_mode": manifest.get("source_mode") if isinstance(manifest, dict) else None,
            "bounded": manifest.get("bounded") if isinstance(manifest, dict) else None}


def q_neighbors(con, nid: int, limit: int, types, direction: str):
    node = q_node(con, nid)
    if node is None:
        return None
    type_filter = ""
    params_extra = []
    if types:
        placeholders = ",".join("?" for _ in types)
        type_filter = f" AND e.type IN ({placeholders})"
        params_extra = list(types)

    deg_out = con.execute("SELECT COUNT(*) c FROM edges WHERE src = ?",
                          (nid,)).fetchone()["c"]
    deg_in = con.execute("SELECT COUNT(*) c FROM edges WHERE dst = ?",
                         (nid,)).fetchone()["c"]

    edges = []
    fields = _node_fields(con, "n")
    if direction in ("out", "both"):
        rows = con.execute(
            f"SELECT {fields}, e.type etype, e.weight weight "
            "FROM edges e JOIN nodes n ON n.id = e.dst "
            f"WHERE e.src = ?{type_filter} LIMIT ?",
            (nid, *params_extra, limit),
        )
        for r in rows:
            edges.append({"dir": "out", "other": {"id": r["id"],
                          "title": r["title"], "type": r["type"],
                          "language": r["language"], "page_id": r["page_id"],
                          "wikidata_id": r["wikidata_id"]},
                          "type": r["etype"], "weight": EDGE_WEIGHTS[r["etype"]],
                          "stored_weight": r["weight"]})
    if direction in ("in", "both"):
        rows = con.execute(
            f"SELECT {fields}, e.type etype, e.weight weight "
            "FROM edges e JOIN nodes n ON n.id = e.src "
            f"WHERE e.dst = ?{type_filter} LIMIT ?",
            (nid, *params_extra, limit),
        )
        for r in rows:
            edges.append({"dir": "in", "other": {"id": r["id"],
                          "title": r["title"], "type": r["type"],
                          "language": r["language"], "page_id": r["page_id"],
                          "wikidata_id": r["wikidata_id"]},
                          "type": r["etype"], "weight": EDGE_WEIGHTS[r["etype"]],
                          "stored_weight": r["weight"]})

    return {"node": {**node, "deg_out": deg_out, "deg_in": deg_in},
            "edges": edges, "limit": limit}


# --------------------------------------------------------------------------- #
# Provenance: read the source wikitext line an edge was extracted from
# --------------------------------------------------------------------------- #
def fetch_wikitext(xml_path: str, title: str, offset: int) -> str:
    """Seek to the page's multistream sub-stream, decompress it, return the
    wikitext of the page with the given title."""
    with open(xml_path, "rb") as fh:
        fh.seek(offset)
        data = fh.read(8_000_000)
    text = bz2.BZ2Decompressor().decompress(data).decode("utf-8", "replace")
    needle = f"<title>{title}</title>"
    i = text.find(needle)
    if i < 0:
        # entities in title (rare) -> try escaped form
        i = text.find(f"<title>{html.escape(title)}</title>")
        if i < 0:
            return ""
    s = text.rfind("<page>", 0, i)
    e = text.find("</page>", i)
    m = _TEXT_RE.search(text[s:e])
    return m.group(1) if m else ""


def _resolves_to(con, link: str, dst_title: str, language="en") -> bool:
    t = norm_title(link)
    scoped = "language" in _columns(con, "redirects")
    if not scoped and language != "en":
        return t == dst_title
    for _ in range(8):
        if t == dst_title:
            return True
        row = con.execute("SELECT to_ FROM redirects WHERE frm = ?" +
                          (" AND language = ?" if scoped else ""),
                          (t, language) if scoped else (t,)).fetchone()
        if row is None:
            return False
        t = norm_title(row["to_"])
    return False


DAB_SUFFIX = " (disambiguation)"


def q_provenance(con, xml_path, src_id, dst_id, etype):
    src = q_node(con, src_id)
    dst = q_node(con, dst_id)
    if src is None or dst is None:
        return {"error": "node not found"}
    src["off"] = None
    if "off" in _columns(con, "nodes"):
        src["off"] = con.execute("SELECT off FROM nodes WHERE id=?",
                                 (src_id,)).fetchone()["off"]
    src_title, dst_title = src["title"], dst["title"]
    out = {"src": src_title, "src_type": src["type"], "dst": dst_title,
           "dst_type": dst["type"], "src_language": src["language"],
           "dst_language": dst["language"], "type": etype, "lines": [],
           "evidence": [], "note": ""}

    if _columns(con, "evidence"):
        out["evidence"] = [json.loads(r["evidence"]) for r in con.execute(
            "SELECT evidence FROM evidence WHERE src=? AND dst=? AND type=?",
            (src_id, dst_id, etype))]
        if out["evidence"]:
            for evidence in out["evidence"]:
                lines = evidence.get("lines", [])
                if isinstance(lines, str):
                    lines = [lines]
                out["lines"].extend(str(line) for line in lines)
                for key in ("line", "source_line", "wikitext"):
                    if evidence.get(key):
                        out["lines"].append(str(evidence[key]))
            out["lines"] = list(dict.fromkeys(out["lines"]))[:6]
            out["note"] = "Evidence recorded when this dataset was imported."
            return out

    if etype == "LANGUAGE_EQUIVALENT":
        out["note"] = "No imported Wikidata sitelink evidence is available for this edge."
        return out
    dump_source = False
    if _columns(con, "metadata"):
        row = con.execute("SELECT value FROM metadata WHERE key='manifest'").fetchone()
        manifest = json.loads(row["value"]) if row else {}
        if manifest.get("source_mode") == "wikipedia-dump":
            xml_path = manifest.get("sources", {}).get(src["language"], {}).get("xml", {}).get("path")
            dump_source = True
    if not dump_source and (src["language"] != "en" or dst["language"] != "en"):
        out["note"] = "No imported evidence is available for this language; the English dump cannot verify this edge."
        return out

    if not xml_path or not os.path.exists(xml_path):
        out["note"] = ("Provenance needs the Wikipedia multistream XML dump "
                       "(not included in the shared package). Search, expand, "
                       "and path finding still work without it.")
        return out

    if src["off"] is None:
        out["note"] = ("No source offset available (page not found in the "
                       "multistream index).")
        return out

    try:
        wt = fetch_wikitext(xml_path, src_title, src["off"])
    except Exception as e:
        out["note"] = f"Could not read source page: {e}"
        return out
    if not wt:
        out["note"] = "Source page text not found in the dump."
        return out

    matches = []
    category_prefix = dst_title.split(":", 1)[0] if dst["type"] == "category" else "Category"
    cat_pattern = re.compile(r"\[\[\s*(?:" + re.escape(category_prefix) + r"|Category)\s*:\s*([^\]\|#]+)", re.I)
    for raw_line in wt.split("\n"):
        line = raw_line.strip()
        if not line or "[[" not in line:
            continue
        hit = False
        if etype in ("HAS_CATEGORY", "SUBCATEGORY_OF"):
            for cat in cat_pattern.findall(line):
                if norm_title(category_prefix + ":" + cat.strip()) == dst_title:
                    hit = True
                    break
        else:  # HAS_DISAMBIGUATION / DISAMBIGUATES_TO
            for link in _LINK_RE.findall(line):
                if _resolves_to(con, link, dst_title, src["language"]):
                    hit = True
                    break
        if hit:
            matches.append(html.unescape(line))
            if len(matches) >= 6:
                break

    out["lines"] = matches
    if not matches:
        if etype == "HAS_DISAMBIGUATION" and dst_title.endswith(DAB_SUFFIX) \
                and dst_title[:-len(DAB_SUFFIX)].strip() == src_title:
            out["note"] = (f"Derived edge: '{dst_title}' is the standard "
                           f"'(disambiguation)' page for '{src_title}' "
                           f"(hatnote convention), so no explicit inline link "
                           f"is required.")
        else:
            out["note"] = ("No matching inline link found in the source "
                           "wikitext (the link may go through a redirect not "
                           "captured, or the source revision differs).")
    return out


# --------------------------------------------------------------------------- #
# Shortest path (relevance distance) via Dijkstra with directional costs
# --------------------------------------------------------------------------- #
# Stored direction stays fixed. Request policy controls the ambiguity guard;
# traversal costs come from the shared config, including existing databases.
_DAB_TYPES = ("HAS_DISAMBIGUATION", "DISAMBIGUATES_TO")


def _fwd_cost(etype):
    return FORWARD_COSTS[etype]


def _rev_cost(etype):
    return REVERSE_COSTS[etype]


def _traversal_state(source_type, target_type, etype, state, ambiguity_guard=None):
    """Apply the same ambiguity guard to discovered and user-selected routes."""
    guard = CONFIG["explorer"]["ambiguity_guard"] if ambiguity_guard is None else ambiguity_guard
    return traversal_state(source_type, target_type, etype, state, guard)


def _loaded_languages(con):
    if "language" not in _columns(con, "nodes"):
        return ("en",) if con.execute("SELECT 1 FROM nodes LIMIT 1").fetchone() else ()
    # Seek once per edition using the language index rather than counting or
    # scanning millions of nodes on every request. Small legacy fixtures need
    # no index for correctness.
    languages = []
    row = con.execute("SELECT language FROM nodes ORDER BY language LIMIT 1").fetchone()
    while row:
        languages.append(row["language"])
        row = con.execute("SELECT language FROM nodes WHERE language>? ORDER BY language LIMIT 1",
                          (row["language"],)).fetchone()
    return tuple(languages)


def _route_policy(con, policy=None):
    return normalize_policy(policy, _loaded_languages(con))


def _policy_node(node, policy, label):
    if node and node["language"] not in policy.allowed_languages:
        raise ValueError(f"{label} {node['id']} [{node['language']}] is outside Route languages")


class _PolicyAdjacency:
    """Request-local adjacency: policy filtering precedes probing and pruning."""

    def __init__(self, con, policy, hub_max, keep=()):
        self.con, self.policy, self.hub_max = con, policy, hub_max
        self.keep = tuple(sorted(keep))
        self.cache, self.pruned = {}, set()
        self.restrict_languages = ("language" in _columns(con, "nodes")
                                   and set(policy.allowed_languages) != set(_loaded_languages(con)))

    def rows(self, node, direction, *, hint=False):
        key = (node, direction)
        if not hint and key in self.cache:
            return self.cache[key]
        col, other = ("src", "dst") if direction == "fwd" else ("dst", "src")
        edges = self.policy.allowed_edge_types
        where = f"e.{col}=? AND e.type IN ({','.join('?' for _ in edges)})"
        values = (node, *edges)
        join = ""
        if self.restrict_languages:
            join = f" JOIN nodes n ON n.id=e.{other}"
            where += f" AND n.language IN ({','.join('?' for _ in self.policy.allowed_languages)})"
            values += self.policy.allowed_languages
        query = f"SELECT e.{other} neighbor,e.type FROM edges e{join} WHERE {where}"
        if hint:
            limit = 2 * self.hub_max + 1 if self.hub_max is not None else 5001
            return self.con.execute(query + " LIMIT ?", (*values, limit)).fetchall()
        rows = (self.con.execute(query, values).fetchall() if self.hub_max is None else
                self.con.execute(query + " LIMIT ?", (*values, self.hub_max + 1)).fetchall())
        if self.hub_max is not None and len(rows) > self.hub_max:
            keep_types = (("HAS_CATEGORY", "SUBCATEGORY_OF", "LANGUAGE_EQUIVALENT")
                          if direction == "fwd" else
                          ("SUBCATEGORY_OF", "HAS_DISAMBIGUATION", "DISAMBIGUATES_TO", "LANGUAGE_EQUIVALENT"))
            keep = f"e.type IN ({','.join('?' for _ in keep_types)})"
            extra = tuple(keep_types)
            if self.keep:
                keep += f" OR e.{other} IN ({','.join('?' for _ in self.keep)})"
                extra += self.keep
            keep = "(" + keep + ")"
            if self.con.execute(query + f" AND NOT {keep} LIMIT 1", (*values, *extra)).fetchone():
                self.pruned.add(key)
            rows = self.con.execute(query + f" AND {keep}", (*values, *extra)).fetchall()
        self.cache[key] = rows
        return rows


def _query_policy(qs):
    policy = {}
    for key in ("allowed_languages", "allowed_edge_types", "ambiguity_guard"):
        if key not in qs:
            continue
        if len(qs[key]) != 1:
            raise ValueError(f"Supply {key} once")
        value = qs[key][0]
        if key == "ambiguity_guard":
            if value not in ("true", "false"):
                raise ValueError("ambiguity_guard must be true or false")
            policy[key] = value == "true"
        else:
            policy[key] = value.split(",")
    return policy


def _rel_label(etype, direction):
    return {
        ("HAS_CATEGORY", "fwd"): "in category",
        ("HAS_CATEGORY", "rev"): "contains article",
        ("SUBCATEGORY_OF", "fwd"): "subcategory of",
        ("SUBCATEGORY_OF", "rev"): "has subcategory",
        ("HAS_DISAMBIGUATION", "fwd"): "has disambiguation",
        ("HAS_DISAMBIGUATION", "rev"): "disambiguation of",
        ("DISAMBIGUATES_TO", "fwd"): "disambiguates to",
        ("DISAMBIGUATES_TO", "rev"): "listed on",
        ("LANGUAGE_EQUIVALENT", "fwd"): "same concept in another language",
        ("LANGUAGE_EQUIVALENT", "rev"): "same concept in another language",
    }.get((etype, direction), etype)


def q_path(con, src_id, dst_id, hub_max=CONFIG["search"]["hub_max"],
           settle_cap=CONFIG["search"]["settle_cap"],
           time_budget=CONFIG["search"]["time_budget"], *, policy=None):
    """Dijkstra over the directional cost model and an immutable route policy.

    Costs come from wikihopper.toml. A state bit marks an article reached by
    exiting a disambiguation page; another disambiguation bridge is forbidden
    until a category step resets the state. With the guard off all states are 0;
    nonnegative costs and strict improvements still terminate zero-cost cycles."""
    search_started = time.perf_counter()
    policy = _route_policy(con, policy)
    seconds = parse_search_seconds(time_budget)
    deadline = search_started + seconds if seconds else None
    adjacency = _PolicyAdjacency(con, policy, hub_max, keep=(dst_id,))

    def finish(result):
        result["elapsed_seconds"] = round(time.perf_counter() - search_started, 3)
        result["effective_policy"] = policy.as_dict()
        result["hub_pruned"] = bool(adjacency.pruned)
        result["hub_pruned_nodes"] = len({node for node, _ in adjacency.pruned})
        return result

    _policy_node(q_node(con, src_id), policy, "Source node")
    _policy_node(q_node(con, dst_id), policy, "Target node")
    if con.execute("SELECT 1 FROM nodes WHERE id=?", (src_id,)).fetchone() is None:
        return finish({"found": False, "note": "source node not found"})
    if con.execute("SELECT 1 FROM nodes WHERE id=?", (dst_id,)).fetchone() is None:
        return finish({"found": False, "note": "target node not found"})
    if src_id == dst_id:
        return finish(_path_result(con, [src_id], 0, 0))

    # Prefer nodes adjacent to the target when distances tie. This preserves
    # Dijkstra correctness while avoiding a huge, arbitrary node-id-ordered
    # zero-cost closure before an obvious target-adjacent category is explored.
    target_neighbors = {r["neighbor"] for direction in ("fwd", "rev")
                        for r in adjacency.rows(dst_id, direction, hint=True)}

    def heap_item(distance, node_id, state):
        return (distance, 0 if node_id in target_neighbors else 1, node_id, state)

    typecache = {}

    def ntype(u):
        t = typecache.get(u)
        if t is None:
            r = con.execute("SELECT type FROM nodes WHERE id=?", (u,)).fetchone()
            t = r["type"] if r else "?"
            typecache[u] = t
        return t

    def successors(u):
        for direction, cost in (("fwd", _fwd_cost), ("rev", _rev_cost)):
            for r in adjacency.rows(u, direction):
                yield r["neighbor"], cost(r["type"]), r["type"], direction

    # state s: 0 = normal, 1 = article reached by exiting a disambiguation page
    start = (src_id, 0)
    dist = {start: 0}
    prev = {start: None}
    heap = [heap_item(0, src_id, 0)]
    done = set()
    settled = 0
    capped = False
    stop_reason = None
    best_dst = None                           # smallest distance found to dst

    while heap:
        d, _, u, su = heapq.heappop(heap)
        # Dijkstra pops in nondecreasing distance: once the frontier reaches the
        # best distance already found for the target, that distance is optimal.
        if best_dst is not None and d >= best_dst:
            break
        if (u, su) in done:
            continue
        if settle_cap is not None and settled >= settle_cap:
            capped = True
            stop_reason = "node limit"
            break
        if deadline is not None and time.perf_counter() >= deadline:
            capped = True
            stop_reason = "time limit"
            break
        done.add((u, su))
        settled += 1
        tu = ntype(u)
        # Nonnegative configured costs make the per-successor prune valid.
        for v, w, et, direction in successors(u):
            sv = _traversal_state(tu, ntype(v) if et in _DAB_TYPES else None, et, su,
                                  policy.ambiguity_guard)
            if sv is None:
                continue
            nd = d + w
            # once we have a candidate distance to dst, anything >= it can't
            # improve the answer, so don't bother exploring it (this stops the
            # expensive category-member frontier from being generated).
            if best_dst is not None and nd >= best_dst:
                continue
            key = (v, sv)
            if key not in dist or nd < dist[key]:
                dist[key] = nd
                prev[key] = ((u, su), (et, direction, w))
                heapq.heappush(heap, heap_item(nd, v, sv))
                if v == dst_id and (best_dst is None or nd < best_dst):
                    best_dst = nd

    cand = [(dist[k], k) for k in ((dst_id, 0), (dst_id, 1)) if k in dist]
    if not cand:
        hub_pruned = bool(adjacency.pruned)
        note = (f"No path found within the search {stop_reason} "
                f"({settled:,} nodes explored)."
                if capped else
                "No path found in the loaded graph under the selected route policy.")
        if hub_pruned:
            note += " Hub pruning omitted some edges; another route may exist."
        return finish({"found": False, "settled": settled, "note": note,
                       "timed_out": stop_reason == "time limit",
                       "limit_reason": stop_reason})
    _, endk = min(cand)

    seq, traversals = [], []
    cur = endk
    while cur is not None:
        seq.append(cur[0])
        previous = prev[cur]
        if previous is None:
            break
        cur, traversal = previous
        traversals.append(traversal)
    seq.reverse()
    traversals.reverse()
    # If we stopped on the budget and the answer is >0, optimality is not proven
    # (a shorter path might exist among unexplored 0-cost nodes).
    approx = (capped or adjacency.pruned) and dist[endk] > 0
    result = _path_result(con, seq, dist[endk], settled,
                          approx=bool(approx), traversals=traversals)
    result["timed_out"] = stop_reason == "time limit"
    result["limit_reason"] = stop_reason
    return finish(result)


def q_selected_path(con, node_ids, *, policy=None):
    """Measure a fixed ordered route, choosing the cheapest legal parallel edges.

    Only the supplied hops are evaluated. Reading outgoing edges by source
    avoids scanning all members of a high-degree category for a single hop.
    Two guard states suffice even when parallel relationships change legality.
    """
    if (not isinstance(node_ids, list) or not 1 <= len(node_ids) <= 500
            or any(type(nid) is not int or not 0 <= nid <= 9223372036854775807 for nid in node_ids)):
        raise ValueError("node_ids must be an ordered list of 1 to 500 nonnegative integer node IDs")
    started = time.perf_counter()
    policy = _route_policy(con, policy)
    meta = {nid: q_node(con, nid) for nid in set(node_ids)}
    for nid, node in meta.items():
        if node is None:
            raise ValueError(f"Selected node {nid} was not found in the loaded graph")
        _policy_node(node, policy, "Selected node")
    wanted = {nid: set() for nid in meta}
    for a, b in zip(node_ids, node_ids[1:]):
        wanted[a].add(b)
        wanted[b].add(a)
    adjacent = {}

    def outgoing(nid):
        if nid not in adjacent:
            matches = {}
            for row in con.execute("SELECT dst,type FROM edges WHERE src=?", (nid,)):
                if row["dst"] in wanted[nid] and row["type"] in policy.allowed_edge_types:
                    matches.setdefault(row["dst"], set()).add(row["type"])
            adjacent[nid] = matches
        return adjacent[nid]

    frontier = {0: (0.0, None)}
    for index, (a, b) in enumerate(zip(node_ids, node_ids[1:]), 1):
        choices = [(et, "fwd", _fwd_cost(et)) for et in outgoing(a).get(b, ())]
        choices += [(et, "rev", _rev_cost(et)) for et in outgoing(b).get(a, ())]
        next_frontier = {}
        for state, (distance, trace) in frontier.items():
            for et, direction, cost in sorted(choices, key=lambda step: (step[2], step[0], step[1])):
                next_state = _traversal_state(meta[a]["type"], meta[b]["type"], et, state,
                                             policy.ambiguity_guard)
                if next_state is None:
                    continue
                total = distance + cost
                if next_state not in next_frontier or total < next_frontier[next_state][0]:
                    next_frontier[next_state] = (total, (trace, (et, direction, cost)))
        if not next_frontier:
            reason = ("No direct relationship allowed by Route relationships connects these consecutive nodes."
                      if not choices else
                      "This step chains disambiguation bridges without a category step in between; a language switch preserves that guard.")
            return {"found": False, "valid": False, "failed_step": index,
                    "from_id": a, "to_id": b, "hops": len(node_ids) - 1,
                    "note": f"Step {index}: {meta[a]['title']} [{meta[a]['language']}] → {meta[b]['title']} [{meta[b]['language']}]. {reason}",
                    "elapsed_seconds": round(time.perf_counter() - started, 3),
                    "effective_policy": policy.as_dict()}
        frontier = next_frontier
    distance, trace = min(frontier.values(), key=lambda item: item[0])
    traversals = []
    while trace is not None:
        trace, traversal = trace
        traversals.append(traversal)
    traversals.reverse()
    result = _path_result(con, node_ids, distance, 0, traversals=traversals)
    result.update(valid=True, elapsed_seconds=round(time.perf_counter() - started, 3),
                  effective_policy=policy.as_dict())
    return result


def _edge_between(con, x, y):
    """Return (etype, direction, cost) for a single hop x -> y."""
    candidates = []
    for r in con.execute("SELECT type FROM edges WHERE src=? AND dst=?", (x, y)):
        candidates.append((r["type"], "fwd", _fwd_cost(r["type"])))
    for r in con.execute("SELECT type FROM edges WHERE src=? AND dst=?", (y, x)):
        candidates.append((r["type"], "rev", _rev_cost(r["type"])))
    return min(candidates, key=lambda step: step[2]) if candidates else (None, None, 0)



def _path_result(con, node_seq, distance, settled, approx=False, traversals=None):
    meta = {nid: q_node(con, nid) or {"id": nid, "title": str(nid), "type": "?",
                                    "language": "en", "wikidata_id": None,
                                    "page_id": None} for nid in node_seq}
    path = [meta[nid] for nid in node_seq]
    steps = []
    for index, (a, b) in enumerate(zip(node_seq, node_seq[1:])):
        # Preserve the edge actually chosen when parallel/reciprocal edges exist.
        et, direction, cost = (traversals[index] if traversals is not None
                               else _edge_between(con, a, b))
        step = {"from": meta[a]["title"], "to": meta[b]["title"], "type": et,
                      "from_id": a, "to_id": b,
                      "from_language": meta[a]["language"],
                      "to_language": meta[b]["language"],
                      "direction": direction, "rel": _rel_label(et, direction),
                      "cost": cost}
        if et == "LANGUAGE_EQUIVALENT":
            stored_src, stored_dst = (a, b) if direction == "fwd" else (b, a)
            provenance = q_provenance(con, None, stored_src, stored_dst, et)
            step["evidence"] = provenance["evidence"]
            qid = meta[a].get("wikidata_id")
            if qid and qid == meta[b].get("wikidata_id"):
                step["wikidata_id"] = qid
        steps.append(step)
    return {"found": True, "distance": distance, "hops": len(steps),
            "path": path, "steps": steps, "settled": settled, "approx": approx}


# --------------------------------------------------------------------------- #
# HTTP server
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    db_path = None
    xml_path = None
    password = None          # None => auth disabled (local/dev)
    low_mem = False
    rmst_slots = threading.BoundedSemaphore(1)

    def log_message(self, *args):
        pass

    def _send(self, code, body, ctype="application/json", extra_headers=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body)
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        if extra_headers:
            for k, v in extra_headers.items():
                self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _unauthorized(self):
        self._send(401, {"error": "unauthorized"},
                   extra_headers={"WWW-Authenticate": 'Basic realm="WikiHopper"'})

    def _authorized(self) -> bool:
        """If a password is configured, require HTTP Basic Auth (username ignored)."""
        if not self.password:
            return True
        hdr = self.headers.get("Authorization", "")
        if not hdr.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(hdr[6:].strip()).decode("utf-8")
        except Exception:
            return False
        # "user:password" — username is ignored; only the password must match
        pw = decoded.split(":", 1)[1] if ":" in decoded else decoded
        try:
            return hmac.compare_digest(pw, self.password)
        except (TypeError, ValueError):
            return False

    def do_POST(self):
        if not self._authorized():
            self._unauthorized()
            return
        path = urlparse(self.path).path
        selected_path = path == "/api/path/test"
        if not selected_path and (path not in ("/api/rmst", "/api/rmst/resolve")
                or not CONFIG["explorer"].get("rmst_enabled", False)):
            self._send(404, {"error": "not found"})
            return
        if self.headers.get_content_type() != "application/json":
            self._send(415, {"error": "Send an application/json request"})
            return
        origin = self.headers.get("Origin")
        if origin and urlparse(origin).netloc != self.headers.get("Host"):
            self._send(403, {"error": "Use the explorer's own origin"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                self._send(413, {"error": "Request must be between 1 byte and 64 KiB"})
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeError):
            self._send(400, {"error": "Invalid JSON request"})
            return
        if selected_path:
            con = None
            policy = None
            try:
                if not isinstance(payload, dict):
                    raise ValueError("Send an object with an ordered node_ids list")
                con = _connect_ro(self.db_path, low_mem=self.low_mem)
                if "policy" in payload and payload["policy"] is None:
                    raise ValueError("policy must be an object")
                policy = _route_policy(con, payload.get("policy"))
                self._send(200, q_selected_path(con, payload.get("node_ids"), policy=policy))
            except (ValueError, TypeError) as error:
                self._send(400, {"error": str(error),
                                 "effective_policy": policy.as_dict() if policy else None})
            except sqlite3.Error:
                self._send(500, {"error": "The selected path could not be read"})
            finally:
                if con is not None:
                    con.close()
            return
        if not self.rmst_slots.acquire(blocking=False):
            self._send(429, {"error": "Another R-MST calculation is running; try again shortly"})
            return
        con = None
        policy = None
        try:
            if __package__:
                from . import wikihopper_rmst as rmst
            else:
                import wikihopper_rmst as rmst
            con = _connect_ro(self.db_path, low_mem=self.low_mem)
            if path == "/api/rmst":
                if not isinstance(payload, dict):
                    raise ValueError("Input must be a JSON object")
                if "policy" in payload and payload["policy"] is None:
                    raise ValueError("policy must be an object")
                policy = _route_policy(con, payload.get("policy"))
                payload = {**payload, "policy": policy}
            options = {"api": sys.modules[__name__]}
            result = (rmst.resolve(con, payload, **options) if path.endswith("/resolve") else
                      rmst.evaluate(con, payload, low_mem=self.low_mem, **options))
            self._send(200, result)
        except (ValueError, TypeError) as error:
            self._send(400, {"error": str(error),
                             "effective_policy": policy.as_dict() if policy else None})
        except sqlite3.Error:
            self._send(500, {"error": "The graph could not be read for this calculation"})
        finally:
            if con is not None:
                con.close()
            self.rmst_slots.release()

    def do_GET(self):
        if not self._authorized():
            self._unauthorized()
            return

        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query, keep_blank_values=True)
        low = self.low_mem

        if path == "/" or path == "/index.html":
            self._send(200, _index_html(low_mem=low), "text/html")
            return

        if path == "/api/dataset":
            con = _connect_ro(self.db_path, low_mem=low)
            try:
                self._send(200, q_dataset(con))
            finally:
                con.close()
            return

        if path == "/api/search":
            con = _connect_ro(self.db_path, low_mem=low)
            try:
                q = qs.get("q", [""])[0]
                language = qs.get("language", [""])[0] or None
                self._send(200, q_search(con, q, language=language))
            finally:
                con.close()
            return

        if path == "/api/neighbors":
            con = _connect_ro(self.db_path, low_mem=low)
            try:
                nid = int(qs.get("id", ["-1"])[0])
                limit = max(1, min(1000, int(qs.get("limit", ["120"])[0])))
                direction = qs.get("dir", ["both"])[0]
                types = qs.get("types", [""])[0]
                types = [t for t in types.split(",") if t in EDGE_TYPES]
                res = q_neighbors(con, nid, limit, types, direction)
                if res is None:
                    self._send(404, {"error": "node not found"})
                else:
                    self._send(200, res)
            except Exception as e:
                self._send(400, {"error": str(e)})
            finally:
                con.close()
            return

        if path == "/api/provenance":
            con = _connect_ro(self.db_path, low_mem=low)
            try:
                src = int(qs.get("src", ["-1"])[0])
                dst = int(qs.get("dst", ["-1"])[0])
                etype = qs.get("type", [""])[0]
                if etype not in EDGE_TYPES:
                    self._send(400, {"error": "bad edge type"})
                else:
                    self._send(200, q_provenance(con, self.xml_path, src, dst, etype))
            except Exception as e:
                self._send(400, {"error": str(e)})
            finally:
                con.close()
            return

        if path == "/api/path":
            con = _connect_ro(self.db_path, low_mem=low)
            policy = None
            try:
                policy = _route_policy(con, _query_policy(qs))
                src = int(qs.get("src", ["-1"])[0])
                dst = int(qs.get("dst", ["-1"])[0])
                search = CONFIG["search_low_memory" if low else "search"]
                default_hub = search["hub_max"]
                hub = max(100, min(200000, int(qs.get("hub", [str(default_hub)])[0])))
                default_seconds = search["time_budget"]
                seconds = parse_search_seconds(qs.get("seconds", [str(default_seconds)])[0])
                full_graph = qs.get("exhaustive", ["0"])[0]
                if full_graph not in ("0", "1"):
                    raise ValueError("exhaustive must be 0 or 1")
                cap = search["settle_cap"]
                if full_graph == "1":
                    hub, cap = None, None
                res = q_path(con, src, dst, hub_max=hub, settle_cap=cap,
                             time_budget=seconds, policy=policy)
                self._send(200, res)
            except Exception as e:
                self._send(400, {"error": str(e),
                                 "effective_policy": policy.as_dict() if policy else None})
            finally:
                con.close()
            return

        self._send(404, {"error": "not found"})


class _Server(ThreadingHTTPServer):
    # Do NOT reuse the address: on Windows SO_REUSEADDR lets a second server
    # silently bind the same port and steal requests (serving stale code).
    # Binding must fail loudly if another instance is already running.
    allow_reuse_address = False
    daemon_threads = True


def serve(db_path, xml_path, host, port, password=None, low_mem=False):
    Handler.db_path = db_path
    Handler.xml_path = xml_path
    Handler.password = password or None
    Handler.low_mem = bool(low_mem)
    try:
        httpd = _Server((host, port), Handler)
    except OSError as e:
        print(f"[serve] ERROR: could not bind {host}:{port} ({e}). "
              f"Another server is probably still running on this port.",
              flush=True)
        sys.exit(1)
    auth_note = "Basic Auth ON" if Handler.password else "auth off"
    mem_note = "low-mem" if Handler.low_mem else "normal mem"
    print(f"[serve] http://{host}:{port}/  ({auth_note}, {mem_note}; Ctrl+C to stop)",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[serve] stopped", flush=True)
        httpd.shutdown()


# --------------------------------------------------------------------------- #
# Frontend (single page, vis-network from CDN)
# --------------------------------------------------------------------------- #
INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>WikiHopper Explorer</title>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, Segoe UI, Arial, sans-serif;
         background:#12151c; color:#e6e9ef; height:100vh; overflow:hidden; }
  #app { display:flex; height:100vh; }
  #side { width:340px; min-width:340px; background:#1a1e27; border-right:1px solid #2a2f3a;
          display:flex; flex-direction:column; padding:12px; gap:10px; overflow-y:auto; }
  #side > * { flex-shrink:0; }
  #net { flex:1; height:100vh; background:#0e1116; }
  h1 { font-size:16px; margin:0 0 4px; }
  .muted { color:#8b93a7; font-size:12px; }
  input[type=text] { width:100%; padding:8px 10px; border-radius:8px;
     border:1px solid #2a2f3a; background:#0e1116; color:#e6e9ef; font-size:14px; }
  .results { display:flex; flex-direction:column; gap:4px; max-height:230px; overflow-y:auto; }
  .res { padding:6px 8px; border-radius:6px; cursor:pointer; font-size:13px;
     border:1px solid transparent; display:flex; justify-content:space-between; gap:8px; }
  .res:hover { background:#232838; }
  .badge { font-size:10px; padding:1px 6px; border-radius:10px; white-space:nowrap; }
  .b-article { background:#1d3a5f; color:#8fc2ff; }
  .b-category { background:#1e4634; color:#8fe0b0; }
  .b-disambiguation { background:#4a3a17; color:#ffd88a; }
  .b-language { background:#3a2856; color:#d5b7ff; }
  .panel { border-top:1px solid #2a2f3a; padding-top:10px; }
  .row { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
  .btn { background:#243043; color:#e6e9ef; border:1px solid #33415a; border-radius:7px;
     padding:6px 9px; font-size:12px; cursor:pointer; }
  .btn:hover { background:#2d3b52; }
  .btn.primary { background:#2f6feb; border-color:#2f6feb; }
  label.chk { font-size:12px; display:flex; align-items:center; gap:5px; cursor:pointer; }
  .legend span { display:inline-flex; align-items:center; gap:5px; margin-right:10px; font-size:12px; }
  .dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
  .sel-title { font-weight:600; font-size:14px; word-break:break-word; }
  .kv { font-size:12px; color:#aab2c5; }
  #status { font-size:12px; color:#8b93a7; min-height:16px; }
  #datasetInfo { white-space:pre-wrap; overflow-wrap:anywhere; }
  .slider-row { display:flex; align-items:center; gap:8px; font-size:12px; }
  input[type=range] { flex:1; }
  select { padding:5px 7px; border-radius:7px; border:1px solid #33415a; background:#0e1116; color:#e6e9ef; }
  #rmstPanel summary { cursor:pointer; font-weight:600; font-size:13px; }
  #rmstPanel textarea { width:100%; min-height:110px; resize:vertical; padding:8px;
    border:1px solid #33415a; border-radius:7px; background:#0e1116; color:#e6e9ef; }
  #rmstPanel input[type=number] { width:65px; padding:5px; border:1px solid #33415a;
    border-radius:7px; background:#0e1116; color:#e6e9ef; }
  #rmstPanel .row { margin-top:7px; }
  #rmstPanel .rmst-entry { margin:4px 0; overflow-wrap:anywhere; }
  #rmstPanel .rmst-problem { color:#ffb3c1; }
  .btn:disabled { opacity:0.55; cursor:wait; }
  #selectedPathList { padding-left:22px; max-height:220px; overflow-y:auto; margin:8px 0; }
  #selectedPathList li { margin:5px 0; font-size:12px; overflow-wrap:anywhere; }
  #selectedPathList .btn { padding:2px 6px; margin-left:3px; }
</style>
</head>
<body>
<div id="app">
  <div id="side">
    <div>
      <h1>WikiHopper Explorer</h1>
      <div class="muted">Search a page, then click nodes to expand. Double-click a node to expand it.</div>
      <div id="datasetInfo" class="muted" style="margin-top:6px">Reading loaded dataset...</div>
    </div>
    <div class="row"><label for="searchLanguage" class="muted">Search language</label>
      <select id="searchLanguage"></select></div>
    <input id="q" type="text" placeholder="Search title (e.g. Apple)" autocomplete="off"/>
    <div class="results" id="results"></div>

    <div class="panel" id="routePolicyPanel">
      <div style="font-weight:600; font-size:13px">Route languages</div>
      <div id="routeLanguages" class="row">Reading loaded languages...</div>
      <div style="font-weight:600; font-size:13px; margin-top:8px">Route relationships</div>
      <div class="row"><label class="chk"><input class="routeEdge" type="checkbox" value="HAS_CATEGORY" checked/> Category membership</label></div>
      <div class="row"><label class="chk"><input class="routeEdge" type="checkbox" value="SUBCATEGORY_OF" checked/> Category hierarchy</label></div>
      <div class="row"><label class="chk"><input class="routeEdge" type="checkbox" value="HAS_DISAMBIGUATION,DISAMBIGUATES_TO" checked/> Disambiguation</label></div>
      <div class="row"><label class="chk"><input class="routeEdge" type="checkbox" value="LANGUAGE_EQUIVALENT" checked/> Language identity</label></div>
      <div class="row"><label class="chk"><input id="routeAmbiguityGuard" type="checkbox"/> Prevent consecutive ambiguity bridges</label></div>
      <div class="muted" style="margin-top:7px">Applies to path search, selected routes and R-MST. Suggestion languages and neighborhood filters are separate.</div>
    </div>

    <div class="panel">
      <div class="slider-row">
        <span>Max/expand</span>
        <input id="limit" type="range" min="10" max="500" step="10" value="120"/>
        <span id="limitVal">120</span>
      </div>
      <div class="row" style="margin-top:8px">
        <label class="chk"><input type="checkbox" class="etype" value="HAS_CATEGORY" checked/> categories</label>
        <label class="chk"><input type="checkbox" class="etype" value="SUBCATEGORY_OF" checked/> subcats</label>
      </div>
      <div class="row">
        <label class="chk"><input type="checkbox" class="etype" value="HAS_DISAMBIGUATION" checked/> disambig</label>
        <label class="chk"><input type="checkbox" class="etype" value="DISAMBIGUATES_TO" checked/> members</label>
      </div>
      <div class="row">
        <label class="chk"><input type="checkbox" class="etype" value="LANGUAGE_EQUIVALENT" checked/> language equivalents</label>
      </div>
    </div>

    <!-- RMST_PANEL_START -->
    <details class="panel" id="rmstPanel">
      <summary>R-MST / result-set connectivity</summary>
      <div class="muted" style="margin-top:7px">Add articles or paste one title per line in ranking order. Use language:title for mixed languages.</div>
      <div class="row"><label class="muted" for="rmstLanguage">Article language</label><select id="rmstLanguage"></select></div>
      <input id="rmstSearch" type="text" placeholder="Find an article to add..." autocomplete="off" style="margin-top:7px"/>
      <div class="results" id="rmstSearchRes" style="max-height:120px"></div>
      <div class="row"><button class="btn" id="rmstAddSearch">Add article</button><button class="btn" id="rmstAddSelected">Add selected node</button></div>
      <label for="rmstArticles" class="muted" style="display:block; margin:7px 0 4px">Ranked article list</label>
      <textarea id="rmstArticles" placeholder="en:Earth&#10;es:Tierra&#10;tr:Dünya" spellcheck="false"></textarea>
      <div class="row"><label for="rmstK" class="muted">Top-k</label><input type="number" id="rmstK" value="20" min="1" max="50"/>
        <label for="rmstSeconds" class="muted">Max time</label><select id="rmstSeconds"><option value="12">12 seconds</option><option value="30" selected>30 seconds</option><option value="60">1 minute</option></select>
        <input type="number" id="rmstCustomSeconds" min="0" step="any" value="3600" hidden aria-label="Custom R-MST maximum time in seconds" style="width:110px"/></div>
      <div class="row"><label class="chk"><input type="checkbox" id="rmstExhaustive" checked/> Full graph search</label></div>
      <div class="muted">Full graph search disables hub pruning and node limits. The time allowance still applies.</div>
      <div class="row"><button class="btn" id="rmstCheck">Check list</button><button class="btn primary" id="rmstGo">Calculate R-MST</button></div>
      <div id="rmstPreview" aria-live="polite" style="margin-top:7px; font-size:12px"></div>
      <div id="rmstResult" aria-live="polite" style="margin-top:7px; font-size:12px"></div>
      <div class="muted" style="margin-top:7px">R-MST is the mean tree-edge cost. Smaller values describe tighter graph structure. Tree links use the cheaper directional path and the shared route settings above.</div>
    </details>
    <!-- RMST_PANEL_END -->

    <div class="panel" id="selectedPathPanel">
      <div style="font-weight:600; font-size:13px; margin-bottom:6px">Selected path</div>
      <div class="muted">Ctrl-click expanded nodes in route order. Each consecutive pair must share a relationship. Use the arrows to change the order.</div>
      <ol id="selectedPathList" aria-label="Selected route order"></ol>
      <div class="row">
        <button class="btn primary" id="selectedPathTest" disabled>Test selected path</button>
        <button class="btn" id="selectedPathReverse" disabled>Reverse</button>
        <button class="btn" id="selectedPathClear" disabled>Clear selection</button>
      </div>
      <div id="selectedPathResult" aria-live="polite" style="margin-top:8px; font-size:12px">Select at least two nodes.</div>
    </div>

    <div class="panel" id="pathPanel">
      <div style="font-weight:600; font-size:13px; margin-bottom:6px">Distance / shortest path</div>
      <div class="row"><label for="pfFromLanguage" class="muted">From suggestion language</label>
        <select id="pfFromLanguage"></select></div>
      <input id="pfFrom" type="text" placeholder="From article..." autocomplete="off"/>
      <div class="results" id="pfFromRes" style="max-height:120px"></div>
      <div class="row"><label for="pfToLanguage" class="muted">To suggestion language</label>
        <select id="pfToLanguage"></select></div>
      <input id="pfTo" type="text" placeholder="To article..." autocomplete="off" style="margin-top:6px"/>
      <div class="results" id="pfToRes" style="max-height:120px"></div>
      <div class="row" style="margin-top:6px">
        <button class="btn" id="pfUseSel">Use selected as From</button>
        <button class="btn" id="pfUseSelTo">Use selected as To</button>
      </div>
      <div class="row" style="margin-top:6px">
        <label for="pfTimeout" class="muted">Max time</label>
        <select id="pfTimeout">
          <option value="5">5 seconds</option>
          <option value="12">12 seconds</option>
          <option value="30">30 seconds</option>
          <option value="60">1 minute</option>
          <option value="120">2 minutes</option>
          <option value="300">5 minutes</option>
        </select>
        <input type="number" id="pfCustomSeconds" min="0" step="any" value="3600" hidden aria-label="Custom shortest-path maximum time in seconds" style="width:110px"/>
        <button class="btn primary" id="pfGo">Find path</button>
      </div>
      <div class="row" style="margin-top:6px"><label class="chk"><input type="checkbox" id="pfExhaustive" checked/> Full graph search</label></div>
      <div class="muted">Full graph search disables hub pruning and node limits. The time allowance still applies.</div>
      <div id="pfResult" style="margin-top:8px; font-size:12px;"></div>
    </div>

    <div class="panel" id="selPanel" style="display:none">
      <div class="sel-title" id="selTitle"></div>
      <div class="kv" id="selMeta"></div>
      <div class="row" style="margin-top:8px">
        <button class="btn primary" id="btnExpand">Expand</button>
        <button class="btn" id="btnHide">Remove</button>
        <button class="btn" id="btnFocus">Focus</button>
        <button class="btn" id="btnRmst">Add to R-MST</button>
        <a class="btn" id="btnWiki" href="#" target="_blank" rel="noopener noreferrer">Wikipedia &#8599;</a>
      </div>
    </div>

    <div class="panel">
      <div id="status"></div>
      <div class="row"><button class="btn" id="btnClear">Clear canvas</button></div>
    </div>

    <div class="panel" id="provPanel" style="display:none">
      <div style="font-weight:600; font-size:13px; margin-bottom:4px">Edge provenance</div>
      <div class="kv" id="provHead"></div>
      <div id="provBody" style="margin-top:6px; font-size:12px;"></div>
    </div>

    <div class="panel legend">
      <span><span class="dot" style="background:#4a90e2"></span>article</span>
      <span><span class="dot" style="background:#40c07d"></span>category</span>
      <span><span class="dot" style="background:#e0a63a"></span>disambiguation</span>
      <div class="muted" style="margin-top:6px">Edge weight is the larger directional cost. Hover an edge to see both costs.</div>
    </div>
  </div>
  <div id="net"></div>
</div>

<script>
const NODE_COLORS = { article:"#4a90e2", category:"#40c07d", disambiguation:"#e0a63a" };
const EDGE_COLORS = { HAS_CATEGORY:"#40c07d", SUBCATEGORY_OF:"#2f8f63",
                      HAS_DISAMBIGUATION:"#e0a63a", DISAMBIGUATES_TO:"#c98a2a",
                      LANGUAGE_EQUIVALENT:"#ac7de8" };
const LANGUAGES = __LANGUAGES__;
const BASE_LANGUAGE = __BASE_LANGUAGE__;
const RMST_ENABLED = __RMST_ENABLED__;
const routePolicyListeners = [];
let routeLanguagesReady = false;
document.getElementById("routeAmbiguityGuard").checked = __AMBIGUITY_GUARD__;
function routePolicy() {
  if (!routeLanguagesReady) throw new Error("Loaded route languages are not available yet.");
  const allowed_languages = [...document.querySelectorAll(".routeLanguage:checked")].map(el => el.value);
  const allowed_edge_types = [...document.querySelectorAll(".routeEdge:checked")].flatMap(el => el.value.split(","));
  if (!allowed_languages.length) throw new Error("Select at least one Route language.");
  if (!allowed_edge_types.length) throw new Error("Select at least one Route relationship.");
  return {allowed_languages, allowed_edge_types,
          ambiguity_guard:document.getElementById("routeAmbiguityGuard").checked};
}
function routePolicyChanged() {
  pfRevision++;
  selectedPathRevision++;
  clearPathHighlights();
  selectedPathHighlighted = false;
  document.getElementById("pfResult").textContent = "Route settings changed. Find a path again.";
  document.getElementById("selectedPathResult").textContent = "Route settings changed. Test the selected route again.";
  for (const listener of routePolicyListeners) listener();
}
document.getElementById("routePolicyPanel").addEventListener("change", routePolicyChanged);
for (const id of ["searchLanguage", "pfFromLanguage", "pfToLanguage", "rmstLanguage"]) {
  const select = document.getElementById(id);
  if (!select) continue;
  if (id !== "rmstLanguage") select.add(new Option("All languages", ""));
  for (const [code, details] of Object.entries(LANGUAGES)) {
    select.add(new Option(`${details.name} (${code})`, code));
  }
  select.value = id === "searchLanguage" ? "" : BASE_LANGUAGE;
}
async function loadDatasetInfo() {
  const banner = document.getElementById("datasetInfo");
  try {
    const response = await fetch("/api/dataset");
    if (!response.ok) throw new Error("Dataset metadata unavailable");
    const dataset = await response.json();
    const counts = dataset.language_counts || {};
    const routeLanguages = document.getElementById("routeLanguages");
    routeLanguages.replaceChildren();
    for (const code of Object.keys(counts).sort()) {
      const label = document.createElement("label");
      label.className = "chk";
      const input = document.createElement("input");
      input.type = "checkbox"; input.className = "routeLanguage";
      input.value = code; input.checked = true;
      label.appendChild(input);
      label.appendChild(document.createTextNode(` ${LANGUAGES[code]?.name || code} (${code})`));
      routeLanguages.appendChild(label);
    }
    routeLanguagesReady = true;
    const label = code => LANGUAGES[code]?.name ? `${LANGUAGES[code].name} (${code})` : code;
    const loaded = Object.entries(counts).map(([code, count]) =>
      `    ${label(code)}: ${Number(count).toLocaleString()} nodes`);
    const scope = dataset.bounded === true ? "Loaded dataset (bounded sample)." : "Loaded dataset.";
    banner.textContent = scope + "\nLoaded languages:\n" + (loaded.join("\n") || "    none");
    for (const id of ["searchLanguage", "pfFromLanguage", "pfToLanguage", "rmstLanguage"]) {
      const select = document.getElementById(id);
      if (!select) continue;
      const previous = select.value;
      select.replaceChildren();
      if (id !== "rmstLanguage") select.add(new Option("All languages", ""));
      for (const code of Object.keys(counts)) select.add(new Option(label(code), code));
      select.value = counts[previous] || (previous === "" && id !== "rmstLanguage")
        ? previous : counts[BASE_LANGUAGE] ? BASE_LANGUAGE : Object.keys(counts)[0] || "";
    }
  } catch (error) {
    banner.textContent = "Dataset information unavailable.";
  }
}
loadDatasetInfo();

const nodes = new vis.DataSet([]);
const edges = new vis.DataSet([]);
const container = document.getElementById("net");
const network = new vis.Network(container, {nodes, edges}, {
  physics: { stabilization: false, barnesHut: { gravitationalConstant:-8000,
             springLength:130, springConstant:0.02, damping:0.4 } },
  interaction: { hover:true, multiselect:true, tooltipDelay:120 },
  nodes: { shape:"dot", size:14, font:{ color:"#e6e9ef", size:13 },
           borderWidth:1.5 },
  edges: { arrows:{ to:{ enabled:true, scaleFactor:0.5 } }, smooth:{ type:"dynamic" },
           color:{ opacity:0.6 }, font:{ size:9, color:"#8b93a7", strokeWidth:0 } }
});

let selectedId = null;
let selectedPathIds = [], selectedPathRevision = 0, selectedPathBusy = false;
let selectedPathHighlighted = false;
let rmstEdgeHandler = null;
const status = (m) => document.getElementById("status").textContent = m || "";
const limitEl = document.getElementById("limit");
const limitVal = document.getElementById("limitVal");
limitEl.oninput = () => limitVal.textContent = limitEl.value;

function selectedTypes() {
  return [...document.querySelectorAll(".etype:checked")].map(c => c.value).join(",");
}
function nodeStyle(n) {
  const language = n.language || "en";
  return { id:n.id, label:`${n.title} [${language}]`, pageTitle:n.title, language,
           wikidataId:n.wikidata_id, nodeType:n.type, title:`${n.title} [${language}; ${n.type}]`,
           color:{ background:NODE_COLORS[n.type]||"#888", border:"#20242e" } };
}
function addNode(n) {
  if (!nodes.get(n.id)) nodes.add(nodeStyle(n));
}
function edgeId(a,b,t){ return `${a}->${b}:${t}`; }
const EDGE_WEIGHTS = __EDGE_WEIGHTS__;
const EDGE_COSTS = __EDGE_COSTS__;
const defaultSeconds = __DEFAULT_SECONDS__;
function setupSearchTime(selectId, customId, initialSeconds) {
  const select = document.getElementById(selectId);
  if (!select) return;
  for (const [seconds, label] of [[300,"5 minutes"], [900,"15 minutes"], [3600,"1 hour"],
                                [21600,"6 hours"], [86400,"24 hours"], [0,"No time limit"]]) {
    if (![...select.options].some(o => Number(o.value) === seconds)) select.add(new Option(label, String(seconds)));
  }
  if (![...select.options].some(o => Number(o.value) === initialSeconds)) {
    select.add(new Option(initialSeconds + " seconds", String(initialSeconds)));
  }
  select.add(new Option("Custom seconds...", "custom"));
  select.value = String(initialSeconds);
  const custom = document.getElementById(customId);
  select.addEventListener("change", () => {
    custom.hidden = select.value !== "custom";
    if (!custom.hidden) custom.focus();
  });
}
function searchSeconds(selectId, customId) {
  const choice = document.getElementById(selectId).value;
  const raw = choice === "custom" ? document.getElementById(customId).value.trim() : choice;
  const seconds = Number(raw);
  if (!raw || !Number.isFinite(seconds) || seconds < 0) throw new Error("Enter finite seconds >= 0; use 0 for no time limit.");
  return seconds;
}
function searchAllowance(seconds) {
  return seconds === 0 ? "no time limit" : `search allowance: ${seconds} seconds`;
}
setupSearchTime("pfTimeout", "pfCustomSeconds", defaultSeconds);
setupSearchTime("rmstSeconds", "rmstCustomSeconds", 30);
const NORMAL_EDGE_FONT = { size:9, color:"#8b93a7", strokeWidth:0 };
let highlightedEdgeIds = new Set();

function edgeTooltip(type, weight) {
  const c = EDGE_COSTS[type] || { fwd:"?", rev:"?" };
  return `${type}: forward cost ${c.fwd}, reverse cost ${c.rev} ` +
         `(relevance weight ${weight}) - click edge for provenance`;
}
function baseEdgeData(id, from, to, type, weight, pathOnly=false) {
  return { id, from, to, etype:type, storedWeight:weight, pathOnly, width:1,
           color:{ color:EDGE_COLORS[type], opacity:0.6 },
           label:Number(weight) === 0 ? "0" : "",
           title:edgeTooltip(type, weight), dashes:Number(weight) === 0,
           font:NORMAL_EDGE_FONT };
}
function clearPathHighlights() {
  for (const eid of highlightedEdgeIds) {
    const e = edges.get(eid);
    if (!e) continue;
    if (e.pathOnly) {
      edges.remove(eid);
    } else {
      edges.update(baseEdgeData(eid, e.from, e.to, e.etype, e.storedWeight));
    }
  }
  highlightedEdgeIds.clear();
}

async function doSearch() {
  const q = document.getElementById("q").value.trim();
  const box = document.getElementById("results");
  if (!q) { box.innerHTML = ""; return; }
  const language = document.getElementById("searchLanguage").value;
  const r = await fetch("/api/search?q=" + encodeURIComponent(q) + "&language=" + encodeURIComponent(language));
  const items = await r.json();
  if (document.getElementById("q").value.trim() !== q ||
      document.getElementById("searchLanguage").value !== language) return;
  box.innerHTML = "";
  if (!items.length) { box.innerHTML = '<div class="muted">No matches.</div>'; return; }
  for (const it of items) {
    const d = document.createElement("div");
    d.className = "res";
    d.innerHTML = `<span>${esc(it.title)}</span><span><span class="badge b-language">${esc(it.language)}</span> <span class="badge b-${it.type}">${esc(it.type)}</span></span>`;
    d.onclick = () => { addNode(it); network.selectNodes([it.id]); syncSelectedPath(); onSelect(it.id);
                        expand(it.id); };
    box.appendChild(d);
  }
}
let searchTimer = null;
document.getElementById("q").addEventListener("input", () => {
  clearTimeout(searchTimer); searchTimer = setTimeout(doSearch, 220);
});
document.getElementById("searchLanguage").addEventListener("change", doSearch);

async function expand(id) {
  status("Loading neighbors...");
  const url = `/api/neighbors?id=${id}&limit=${limitEl.value}&types=${selectedTypes()}`;
  const r = await fetch(url);
  if (!r.ok) { status("Error loading node."); return; }
  const data = await r.json();
  addNode(data.node);
  let added = 0;
  for (const e of data.edges) {
    addNode(e.other);
    const a = e.dir === "out" ? id : e.other.id;
    const b = e.dir === "out" ? e.other.id : id;
    const eid = edgeId(a,b,e.type);
    const existing = edges.get(eid);
    if (!existing) {
      edges.add(baseEdgeData(eid, a, b, e.type, e.weight));
      added++;
    } else if (existing.pathOnly) {
      // It was introduced by a path; expansion makes it a normal canvas edge.
      edges.update({ id:eid, pathOnly:false, storedWeight:e.weight });
    }
  }
  const n = data.node;
  const shownOut = data.edges.filter(e=>e.dir==="out").length;
  const shownIn = data.edges.filter(e=>e.dir==="in").length;
  const moreOut = n.deg_out - shownOut, moreIn = n.deg_in - shownIn;
  status(`${n.title}: out ${n.deg_out} (shown ${shownOut}${moreOut>0?`, +${moreOut} more`:""}), ` +
         `in ${n.deg_in} (shown ${shownIn}${moreIn>0?`, +${moreIn} more`:""})`);
  onSelect(id, n);
}

function wikiUrl(title, language="en") {
  // Map a node title to its Wikipedia page URL (keeps the ":" of namespaces
  // like "Category:" and any "/" so the canonical link resolves directly).
  const t = String(title).replace(/ /g, "_");
  return "https://" + encodeURIComponent(language) + ".wikipedia.org/wiki/" +
         encodeURIComponent(t).replace(/%3A/gi, ":").replace(/%2F/gi, "/");
}

async function onSelect(id, meta) {
  selectedId = id;
  const node = nodes.get(id);
  if (!node) return;
  document.getElementById("selPanel").style.display = "block";
  document.getElementById("selTitle").textContent = node.label;
  const wiki = document.getElementById("btnWiki");
  wiki.href = wikiUrl(node.pageTitle, node.language);
  wiki.title = wiki.href;
  if (!meta) {
    const r = await fetch(`/api/neighbors?id=${id}&limit=1`);
    if (r.ok) meta = (await r.json()).node;
  }
  document.getElementById("selMeta").textContent = meta
    ? `${meta.language} | outgoing: ${meta.deg_out.toLocaleString()} | incoming: ${meta.deg_in.toLocaleString()}`
    : "";
}

network.on("click", (p) => {
  const hitNode = p.pointer?.DOM ? network.getNodeAt(p.pointer.DOM) : p.nodes[0];
  const hitEdge = p.pointer?.DOM ? network.getEdgeAt(p.pointer.DOM) : p.edges[0];
  if (hitNode != null) { syncSelectedPath(); onSelect(hitNode); }
  else if (hitEdge != null) {
    const edge = edges.get(hitEdge);
    if (edge && edge.rmstIndex != null && rmstEdgeHandler) rmstEdgeHandler(edge.rmstIndex);
    else showProvenance(hitEdge);
  }
  else syncSelectedPath();
});
network.on("selectNode", syncSelectedPath);
network.on("deselectNode", p => { if (p.nodes.length || !p.edges.length) syncSelectedPath(); });
network.on("doubleClick", (p) => {
  const id = p.pointer?.DOM ? network.getNodeAt(p.pointer.DOM) : p.nodes[0];
  if (id != null) expand(id);
});

function esc(s){ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;"); }
async function showProvenance(eid) {
  const e = edges.get(eid);
  if (!e || !e.etype) return;
  const panel = document.getElementById("provPanel");
  const body = document.getElementById("provBody");
  panel.style.display = "block";
  document.getElementById("provHead").textContent = "loading...";
  body.innerHTML = "";
  const r = await fetch(`/api/provenance?src=${e.from}&dst=${e.to}&type=${e.etype}`);
  const d = await r.json();
  if (d.error) { document.getElementById("provHead").textContent = d.error; return; }
  document.getElementById("provHead").innerHTML =
    `<b>${esc(d.src)}</b> <span class="muted">[${esc(d.src_language)}; ${esc(d.src_type)}]</span> ` +
    `&mdash;${esc(d.type)}&rarr; <b>${esc(d.dst)}</b> <span class="muted">[${esc(d.dst_language)}; ${esc(d.dst_type)}]</span>`;
  let hml = "";
  if (d.lines && d.lines.length) {
    hml += '<div class="muted" style="margin:4px 0">Source wikitext line(s) in "' +
           esc(d.src) + '":</div>';
    for (const ln of d.lines) {
      hml += `<pre style="white-space:pre-wrap; background:#0e1116; border:1px solid #2a2f3a;
              border-radius:6px; padding:6px; margin:4px 0; font-size:11px;">${esc(ln)}</pre>`;
    }
  }
  for (const evidence of d.evidence || []) {
    const qid = evidence.wikidata_id || evidence.qid;
    if (qid && /^Q[1-9][0-9]*$/.test(qid)) {
      hml += `<div>Shared Wikidata item: <a href="https://www.wikidata.org/wiki/${qid}" target="_blank" rel="noopener noreferrer">${qid}</a></div>`;
    }
    hml += `<pre style="white-space:pre-wrap; overflow-wrap:anywhere; font-size:11px">${esc(JSON.stringify(evidence, null, 2))}</pre>`;
  }
  if (d.note) hml += `<div class="muted" style="margin-top:4px">${esc(d.note)}</div>`;
  body.innerHTML = hml;
}

// ---- distance / shortest path ---- //
let pfFromId = null, pfToId = null, pfRevision = 0;
function pfChanged() {
  pfRevision++;
  clearPathHighlights();
  document.getElementById("pfResult").textContent = "Search settings changed. Find a path again.";
}
for (const id of ["pfFrom", "pfTo", "pfFromLanguage", "pfToLanguage", "pfTimeout", "pfCustomSeconds", "pfExhaustive"]) {
  document.getElementById(id).addEventListener("input", pfChanged);
  document.getElementById(id).addEventListener("change", pfChanged);
}
function makePicker(inputEl, resEl, languageEl, setId, articleOnly=false) {
  let timer = null;
  let version = 0;
  function search() {
    const currentVersion = ++version;
    setId(null);
    clearTimeout(timer);
    timer = setTimeout(async () => {
      const q = inputEl.value.trim();
      if (!q) { resEl.innerHTML = ""; return; }
      const items = await (await fetch("/api/search?q=" + encodeURIComponent(q) +
                                     "&language=" + encodeURIComponent(languageEl.value))).json();
      if (currentVersion !== version) return;
      resEl.innerHTML = "";
      for (const it of items) {
        if (articleOnly && it.type !== "article") continue;
        const d = document.createElement("div");
        d.className = "res";
        d.innerHTML = `<span>${esc(it.title)}</span><span><span class="badge b-language">${esc(it.language)}</span> <span class="badge b-${it.type}">${esc(it.type)}</span></span>`;
        d.onclick = () => { setId(it.id); inputEl.value = it.title; resEl.innerHTML = ""; };
        resEl.appendChild(d);
      }
    }, 220);
  }
  inputEl.addEventListener("input", search);
  languageEl.addEventListener("change", search);
}
makePicker(document.getElementById("pfFrom"), document.getElementById("pfFromRes"),
           document.getElementById("pfFromLanguage"), id => { pfFromId = id; pfChanged(); });
makePicker(document.getElementById("pfTo"), document.getElementById("pfToRes"),
           document.getElementById("pfToLanguage"), id => { pfToId = id; pfChanged(); });

document.getElementById("pfUseSel").onclick = () => {
  if (selectedId == null) { status("Select a node first."); return; }
  pfFromId = selectedId;
  pfChanged();
  const n = nodes.get(selectedId);
  document.getElementById("pfFrom").value = n ? n.pageTitle : selectedId;
  document.getElementById("pfFromLanguage").value = n ? n.language : "";
};

document.getElementById("pfUseSelTo").onclick = () => {
  if (selectedId == null) { status("Select a node first."); return; }
  pfToId = selectedId;
  pfChanged();
  const n = nodes.get(selectedId);
  document.getElementById("pfTo").value = n ? n.pageTitle : selectedId;
  document.getElementById("pfToLanguage").value = n ? n.language : "";
};

document.getElementById("pfGo").onclick = async () => {
  const res = document.getElementById("pfResult");
  if (pfFromId == null || pfToId == null) { res.textContent = "Pick both a From and a To node."; return; }
  let seconds, policy;
  try { seconds = searchSeconds("pfTimeout", "pfCustomSeconds"); policy = routePolicy(); }
  catch (error) { res.textContent = error.message; return; }
  const exhaustive = document.getElementById("pfExhaustive").checked ? 1 : 0;
  const revision = ++pfRevision;
  clearPathHighlights();
  res.textContent = `Searching (${searchAllowance(seconds)})...`;
  const params = new URLSearchParams({src:pfFromId, dst:pfToId, seconds, exhaustive,
    allowed_languages:policy.allowed_languages.join(","), allowed_edge_types:policy.allowed_edge_types.join(","),
    ambiguity_guard:String(policy.ambiguity_guard)});
  let d;
  try { d = await (await fetch(`/api/path?${params}`)).json(); }
  catch (error) { if (revision === pfRevision) res.textContent = error.message; return; }
  if (revision !== pfRevision) return;
  if (d.error) { res.textContent = d.error; return; }
  const elapsed = Number(d.elapsed_seconds || 0).toFixed(3);
  if (!d.found) {
    res.innerHTML = `<span class="muted">${esc(d.note || "No path found.")} (${elapsed}s)</span>`;
    return;
  }
  let h = `<div><b>Distance ${d.approx ? "\u2264 " : ""}${d.distance}</b> &middot; ${d.hops} hop(s) ` +
          `<span class="muted">(${d.settled.toLocaleString()} nodes explored in ${elapsed}s)</span></div>`;
  if (d.approx) {
    const reason = [d.limit_reason, d.hub_pruned ? "hub pruning" : null].filter(Boolean).join(" and ");
    h += `<div class="muted" style="margin-top:2px">Search used ${esc(reason)}; distance is an upper bound (a shorter route may exist).</div>`;
  }
  h += '<div style="margin-top:6px">';
  h += `<div>&bull; ${esc(d.path[0].title)} <span class="muted">[${esc(d.path[0].language)}; ${esc(d.path[0].type)}]</span></div>`;
  for (let i = 0; i < d.steps.length; i++) {
    const s = d.steps[i], nx = d.path[i + 1];
    const identity = s.wikidata_id ? `; ${esc(s.wikidata_id)}` : "";
    h += `<div style="margin-left:8px" class="muted">&darr; ${esc(s.rel)} (+${s.cost}${identity})</div>`;
    h += `<div>&bull; ${esc(nx.title)} <span class="muted">[${esc(nx.language)}; ${esc(nx.type)}]</span></div>`;
  }
  h += "</div>";
  res.innerHTML = h;
  drawPath(d);
};

function drawPath(d) {
  clearPathHighlights();
  selectedPathHighlighted = false;
  for (const n of d.path) addNode(n);
  for (let i = 0; i < d.steps.length; i++) {
    const s = d.steps[i], a = d.path[i].id, b = d.path[i + 1].id;
    const from_ = s.direction === "fwd" ? a : b, to_ = s.direction === "fwd" ? b : a;
    const eid = edgeId(from_, to_, s.type);
    const existing = edges.get(eid);
    const weight = EDGE_WEIGHTS[s.type] ?? 0;
    const pathOnly = existing ? Boolean(existing.pathOnly) : true;
    if (!existing) edges.add(baseEdgeData(eid, from_, to_, s.type, weight, true));
    const c = EDGE_COSTS[s.type] || { fwd:"?", rev:"?" };
    edges.update({ id:eid, from:from_, to:to_, etype:s.type,
                   storedWeight:weight, pathOnly,
                   label:`${s.rel} (${s.cost})`, width:3,
                   title:`${s.type}: ${s.rel}; this traversal costs ${s.cost}; ` +
                         `forward cost ${c.fwd}, reverse cost ${c.rev} ` +
                         `(relevance weight ${weight}) - click edge for provenance`,
                   color:{ color:"#f05a7a", opacity:1 }, dashes:s.cost === 0,
                   font:{ color:"#ffb3c1", size:11, strokeWidth:0 } });
    highlightedEdgeIds.add(eid);
  }
  network.fit({ nodes: d.path.map(n => n.id), animation: true });
}

// ---- measure an ordered route through selected canvas nodes ---- //
function renderSelectedPath() {
  const list = document.getElementById("selectedPathList");
  list.innerHTML = selectedPathIds.map((id, index) => {
    const n = nodes.get(id);
    return `<li>${esc(n.pageTitle)} <span class="muted">[${esc(n.language)}]</span> ` +
      `<button class="btn" data-route-move="-1" data-route-index="${index}" aria-label="Move node ${index + 1} earlier" ${index === 0 ? "disabled" : ""}>↑</button>` +
      `<button class="btn" data-route-move="1" data-route-index="${index}" aria-label="Move node ${index + 1} later" ${index === selectedPathIds.length - 1 ? "disabled" : ""}>↓</button>` +
      `<button class="btn" data-route-remove="${index}" aria-label="Remove node ${index + 1} from route">×</button></li>`;
  }).join("");
  document.getElementById("selectedPathTest").disabled = selectedPathIds.length < 2 || selectedPathBusy;
  document.getElementById("selectedPathReverse").disabled = selectedPathIds.length < 2;
  document.getElementById("selectedPathClear").disabled = selectedPathIds.length === 0;
}

function setSelectedPath(ids, selectOnCanvas=false) {
  ids = [...new Set(ids)].filter(id => nodes.get(id));
  if (ids.length === selectedPathIds.length && ids.every((id, index) => id === selectedPathIds[index])) return;
  selectedPathIds = ids;
  selectedPathRevision++;
  if (selectedPathHighlighted) {
    clearPathHighlights();
    selectedPathHighlighted = false;
  }
  if (selectOnCanvas) network.selectNodes(ids, false);
  document.getElementById("selectedPathResult").textContent = ids.length < 2
    ? "Select at least two nodes." : `${ids.length} nodes in route order. Test to measure ${ids.length - 1} hops.`;
  renderSelectedPath();
}

function syncSelectedPath() {
  const selected = network.getSelectedNodes();
  const keep = new Set(selected);
  // Retain click order; vis may enumerate its selected-node set by node ID.
  setSelectedPath([...selectedPathIds.filter(id => keep.has(id)),
                   ...selected.filter(id => !selectedPathIds.includes(id))]);
}

document.getElementById("selectedPathList").onclick = event => {
  const button = event.target.closest("button");
  if (!button || button.disabled) return;
  const ids = [...selectedPathIds];
  if (button.dataset.routeRemove != null) ids.splice(Number(button.dataset.routeRemove), 1);
  else {
    const index = Number(button.dataset.routeIndex), next = index + Number(button.dataset.routeMove);
    [ids[index], ids[next]] = [ids[next], ids[index]];
  }
  setSelectedPath(ids, true);
};
document.getElementById("selectedPathReverse").onclick = () => setSelectedPath([...selectedPathIds].reverse(), true);
document.getElementById("selectedPathClear").onclick = () => setSelectedPath([], true);
document.getElementById("selectedPathTest").onclick = async () => {
  if (selectedPathIds.length < 2 || selectedPathBusy) return;
  const revision = selectedPathRevision, ids = [...selectedPathIds];
  const result = document.getElementById("selectedPathResult");
  selectedPathBusy = true;
  renderSelectedPath();
  result.textContent = "Testing selected route...";
  try {
    const policy = routePolicy();
    const response = await fetch("/api/path/test", {method:"POST", headers:{"Content-Type":"application/json"},
                                                 body:JSON.stringify({node_ids:ids, policy})});
    const d = await response.json();
    if (revision !== selectedPathRevision) return;
    if (!response.ok) throw new Error(d.error || "Could not test the selected path");
    if (!d.valid) {
      result.textContent = d.note;
      return;
    }
    let html = `<div><b>Selected length ${d.distance}</b> · ${d.hops} hop(s)</div>`;
    for (const step of d.steps) {
      html += `<div class="muted" style="margin-top:4px">${esc(step.from)} [${esc(step.from_language)}] → ` +
              `${esc(step.to)} [${esc(step.to_language)}]: ${esc(step.rel)} (+${step.cost})</div>`;
    }
    result.innerHTML = html;
    drawPath(d);
    selectedPathHighlighted = true;
    network.selectNodes(ids, false);
    status(`Selected route: length ${d.distance}; ${d.hops} hops. Click a relationship for provenance.`);
  } catch (error) {
    if (revision === selectedPathRevision) result.textContent = error.message;
  } finally {
    selectedPathBusy = false;
    renderSelectedPath();
  }
};

// ---- retrieved-set minimum spanning tree ---- //
if (RMST_ENABLED) {
let rmstPicked = null, rmstLast = null, rmstRevision = 0, rmstBusy = false;
const rmstList = document.getElementById("rmstArticles");
const rmstResult = document.getElementById("rmstResult");
const rmstPreview = document.getElementById("rmstPreview");
makePicker(document.getElementById("rmstSearch"), document.getElementById("rmstSearchRes"),
           document.getElementById("rmstLanguage"), id => rmstPicked = id, true);

function rmstChanged() {
  rmstRevision++;
  rmstLast = null;
  rmstResult.textContent = "";
  rmstPreview.textContent = "List changed. Check it before calculating.";
  for (const edge of edges.get()) if (edge.rmstIndex != null) edges.remove(edge.id);
}
routePolicyListeners.push(rmstChanged);
for (const id of ["rmstArticles", "rmstK", "rmstLanguage", "rmstSeconds", "rmstCustomSeconds", "rmstExhaustive"]) {
  document.getElementById(id).addEventListener("input", rmstChanged);
  document.getElementById(id).addEventListener("change", rmstChanged);
}
function rmstPayload() {
  return { retrieved_articles:rmstList.value.split(/\r?\n/).map(s => s.trim()).filter(Boolean),
           top_k:Number(document.getElementById("rmstK").value),
           language:document.getElementById("rmstLanguage").value || BASE_LANGUAGE,
           seconds:searchSeconds("rmstSeconds", "rmstCustomSeconds"),
           exhaustive:document.getElementById("rmstExhaustive").checked, policy:routePolicy() };
}
async function rmstPost(path, payload) {
  const response = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(payload)});
  const data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || "Calculation request failed");
  return data;
}
function rmstSetBusy(busy) {
  rmstBusy = busy;
  for (const id of ["rmstCheck", "rmstGo"]) document.getElementById(id).disabled = busy;
}
async function rmstAdd(id) {
  if (id == null) { status("Choose an article first."); return; }
  const response = await fetch(`/api/neighbors?id=${id}&limit=1`);
  if (!response.ok) { status("The selected article could not be loaded."); return; }
  const node = (await response.json()).node;
  if (node.type !== "article") { status("R-MST terminals must be articles. Choose an article node."); return; }
  rmstList.value = [rmstList.value.trim(), `${node.language}:${node.title}`].filter(Boolean).join("\n");
  rmstChanged();
  document.getElementById("rmstPanel").open = true;
  document.getElementById("rmstPanel").scrollIntoView({block:"nearest"});
}
document.getElementById("rmstAddSearch").onclick = () => rmstAdd(rmstPicked).catch(error => status(error.message));
document.getElementById("rmstAddSelected").onclick = () => rmstAdd(selectedId).catch(error => status(error.message));
document.getElementById("btnRmst").onclick = () => rmstAdd(selectedId).catch(error => status(error.message));

function renderRmstPreview(data) {
  let html = `<div>${data.included_count} ranked entries within top-${data.top_k}; <b>${data.unique_article_count} unique articles</b>.</div>`;
  if (data.input_count > data.included_count) html += '<div class="muted">Entries after top-k are excluded.</div>';
  for (const entry of data.entries) {
    const text = entry.node ? `${entry.node.language}:${entry.node.title}` : String(entry.requested);
    const detail = entry.status === "duplicate" ? " — duplicate; counted once" : entry.reason ? ` — ${entry.reason}` : "";
    html += `<div class="rmst-entry ${entry.status === "unresolved" ? "rmst-problem" : "muted"}">${entry.rank}. ${esc(text + detail)}</div>`;
  }
  if (!data.ready) html += '<div class="rmst-problem">Correct unresolved entries before calculating.</div>';
  rmstPreview.innerHTML = html;
}
document.getElementById("rmstCheck").onclick = async () => {
  const revision = rmstRevision;
  rmstSetBusy(true);
  rmstPreview.textContent = "Checking article titles...";
  try {
    const data = await rmstPost("/api/rmst/resolve", rmstPayload());
    if (revision === rmstRevision) renderRmstPreview(data);
  } catch (error) { if (revision === rmstRevision) rmstPreview.textContent = error.message; }
  finally { rmstSetBusy(false); }
};

function renderRmst(data) {
  let html = data.certified
    ? `<div><b>R-MST: ${Number(data.mean_edge_cost).toFixed(4)}</b></div><div>Total cost ${data.total_cost} · ${data.unique_article_count} unique articles · ${data.edges.length} tree edges</div>`
    : `<div class="rmst-problem"><b>${data.status === "disconnected" ? "Disconnected in the loaded graph" : "Incomplete calculation"}</b> — a complete R-MST score is unavailable.</div>`;
  if (!data.certified) html += `<div>${data.components.length} component(s); ${data.available_pair_count || 0}/${data.expected_pair_count || 0} pairs have path witnesses.</div>`;
  if (data.limit_reasons?.length) html += `<div class="muted">Search limits: ${esc(data.limit_reasons.join(", "))}.</div>`;
  html += `<div class="muted">${Number(data.elapsed_seconds).toFixed(3)} seconds. ${data.dataset.bounded ? "Bounded imported sample." : "Loaded graph."}</div>`;
  if (data.edges.length || data.terminals.length) html += '<button class="btn" id="rmstShowTree" style="margin-top:7px">Show tree / forest</button>';
  html += '<div class="muted" style="margin:7px 0">Click a tree edge or a path button to inspect its Wikipedia route.</div>';
  for (let index = 0; index < data.edges.length; index++) {
    const edge = data.edges[index], first = edge.path[0], last = edge.path[edge.path.length - 1];
    html += `<div class="rmst-entry">${esc(first.title)} [${esc(first.language)}] ↔ ${esc(last.title)} [${esc(last.language)}]: ${edge.distance}${edge.approx ? " (path upper bound)" : ""} <button class="btn" data-rmst-path="${index}">Inspect path</button></div>`;
  }
  rmstResult.innerHTML = html;
  document.getElementById("rmstShowTree")?.addEventListener("click", showRmstTree);
  for (const button of rmstResult.querySelectorAll("[data-rmst-path]")) button.onclick = () => showRmstPath(Number(button.dataset.rmstPath));
}
function clearRmstCanvas() {
  nodes.clear(); edges.clear(); highlightedEdgeIds.clear(); selectedId = null;
  setSelectedPath([]);
  document.getElementById("selPanel").style.display = "none";
  document.getElementById("provPanel").style.display = "none";
}
function showRmstTree() {
  if (!rmstLast) return;
  clearRmstCanvas();
  for (const node of rmstLast.terminals) addNode(node);
  rmstLast.edges.forEach((edge, index) => edges.add({id:`rmst:${index}`, from:edge.source_id, to:edge.target_id,
    rmstIndex:index, label:String(edge.distance), width:3, dashes:edge.approx,
    arrows:{to:{enabled:false}}, color:{color:"#ac7de8", opacity:1},
    font:{color:"#d5b7ff", size:12, strokeWidth:0}, title:"Terminal connection — click to inspect the Wikipedia path"}));
  network.fit({animation:true});
  status("R-MST terminal tree / forest. Each edge represents a Wikipedia path.");
}
function showRmstPath(index) {
  const edge = rmstLast?.edges[index];
  if (!edge) return;
  clearRmstCanvas();
  for (const node of edge.path) addNode(node);
  for (const step of edge.steps) {
    const from = step.direction === "fwd" ? step.from_id : step.to_id;
    const to = step.direction === "fwd" ? step.to_id : step.from_id;
    const id = edgeId(from, to, step.type);
    edges.update({...baseEdgeData(id, from, to, step.type, EDGE_WEIGHTS[step.type] ?? 0, true),
      label:`${step.rel} (${step.cost})`, width:3, color:{color:"#f05a7a", opacity:1}});
    highlightedEdgeIds.add(id);
  }
  network.fit({animation:true});
  status(`Tree-edge path: cost ${edge.distance}; ${edge.hops} hops. Click a relationship for provenance.`);
  const existing = document.getElementById("rmstPathDetail");
  if (existing) existing.remove();
  const detail = document.createElement("div");
  detail.id = "rmstPathDetail";
  detail.style.marginTop = "8px";
  detail.innerHTML = '<b>Selected path</b>' + edge.path.map((node, i) =>
    `<div class="rmst-entry">${i ? `<span class="muted">${esc(edge.steps[i-1].rel)} (+${edge.steps[i-1].cost})${edge.steps[i-1].wikidata_id ? "; " + esc(edge.steps[i-1].wikidata_id) : ""}</span><br/>` : ""}${esc(node.title)} [${esc(node.language)}]</div>`).join("");
  rmstResult.appendChild(detail);
}
document.getElementById("rmstGo").onclick = async () => {
  const revision = rmstRevision;
  let payload;
  try { payload = rmstPayload(); }
  catch (error) { rmstResult.textContent = error.message; return; }
  rmstSetBusy(true);
  rmstLast = null;
  rmstResult.textContent = "Checking article list...";
  try {
    const preview = await rmstPost("/api/rmst/resolve", payload);
    if (revision !== rmstRevision) return;
    renderRmstPreview(preview);
    if (!preview.ready) { rmstResult.textContent = "Fix the article list to continue."; return; }
    rmstResult.textContent = `Calculating pairwise paths and R-MST (${searchAllowance(payload.seconds)})...`;
    const data = await rmstPost("/api/rmst", payload);
    if (revision !== rmstRevision) return;
    rmstLast = data;
    renderRmst(data);
    showRmstTree();
  } catch (error) { if (revision === rmstRevision) rmstResult.textContent = error.message; }
  finally { rmstSetBusy(false); }
};

rmstEdgeHandler = showRmstPath;
}

document.getElementById("btnExpand").onclick = () => selectedId != null && expand(selectedId);
document.getElementById("btnFocus").onclick = () =>
  selectedId != null && network.focus(selectedId, { scale:1.1, animation:true });
document.getElementById("btnHide").onclick = () => {
  if (selectedId == null) return;
  const rm = edges.get().filter(e => e.from===selectedId || e.to===selectedId).map(e=>e.id);
  edges.remove(rm); nodes.remove(selectedId);
  for (const eid of rm) highlightedEdgeIds.delete(eid);
  selectedId = null; document.getElementById("selPanel").style.display = "none";
  setSelectedPath(selectedPathIds.filter(id => nodes.get(id)), true);
};
document.getElementById("btnClear").onclick = () => {
  nodes.clear(); edges.clear(); selectedId = null;
  highlightedEdgeIds.clear();
  setSelectedPath([]);
  document.getElementById("selPanel").style.display = "none"; status("");
};
</script>
</body>
</html>
"""


def _index_html(*, low_mem=False):
    """Render the current model into the UI, including its search-time default."""
    search = CONFIG["search_low_memory" if low_mem else "search"]
    costs = {name: {"fwd": FORWARD_COSTS[name], "rev": REVERSE_COSTS[name]}
             for name in EDGE_TYPES}
    profiles = CONFIG.get("languages", {"en": {"name": "English", "wiki": "enwiki"}})
    languages = profiles
    rmst_enabled = CONFIG["explorer"].get("rmst_enabled", False)
    rendered = INDEX_HTML
    if not rmst_enabled:
        rendered = re.sub(r"<!-- RMST_PANEL_START -->.*?<!-- RMST_PANEL_END -->", "", rendered, flags=re.S)
        rendered = rendered.replace('<button class="btn" id="btnRmst">Add to R-MST</button>', "")
    return (rendered.replace("__EDGE_WEIGHTS__", json.dumps(EDGE_WEIGHTS))
            .replace("__RMST_ENABLED__", json.dumps(rmst_enabled))
            .replace("__AMBIGUITY_GUARD__", json.dumps(CONFIG["explorer"]["ambiguity_guard"]))
            .replace("__EDGE_COSTS__", json.dumps(costs))
            .replace("__LANGUAGES__", json.dumps(languages).replace("</", "<\\/"))
            .replace("__BASE_LANGUAGE__", json.dumps(CONFIG.get("multilingual", {}).get("base_language", "en")))
            .replace("__DEFAULT_SECONDS__", json.dumps(search["time_budget"])))


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
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
              f"  or rebuild with: python explorer.py --rebuild --out DIR",
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
