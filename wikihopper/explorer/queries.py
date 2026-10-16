"""Read-only queries: search, node lookup, dataset description and neighborhoods."""

import json
import sqlite3

from ..config import CONFIG, EDGE_NAMES, EDGE_WEIGHTS, search_key

EDGE_TYPES = list(EDGE_NAMES)


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
