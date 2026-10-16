"""Extract configured Wikipedia editions from downloaded structural dumps.

python wikihopper_dumps.py --check-only
python wikihopper_dumps.py
python explorer.py --build-only --rebuild

Uses disk-backed SQLite staging and streams compressed inputs. Categories are
explicit wikitext memberships, not categories introduced by template expansion.
The index supplies disambiguation titles; page_props supplies page types and
Wikidata identity. Disambiguation links include explicit wikilinks and the
edition-local primary-topic title convention. No network
access or third-party dependencies are needed.
"""

from __future__ import annotations

import argparse
import bz2
import csv
from datetime import datetime, timezone
import gzip
import html
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import xml.etree.ElementTree as ET

from wikihopper_config import CONFIG, CONFIG_PATH, EDGE_WEIGHTS
from wikihopper_multilingual import disambiguation_members, title_key
from wikihopper_disambiguation import staged_primary_topics, primary_topic_evidence

SUFFIXES = ("pages-articles-multistream.xml.bz2",
            "pages-articles-multistream-index.txt.bz2", "page_props.sql.gz")
LINK = re.compile(r"\[\[\s*([^\[\]|#]+)")
IGNORED = re.compile(r"<!--.*?-->|<(?:nowiki|pre|source|syntaxhighlight)\b[^>]*>.*?</(?:nowiki|pre|source|syntaxhighlight)\s*>", re.I | re.S)
QID = re.compile(r"Q[1-9][0-9]*\Z")


def select_inputs(root, snapshot, languages):
    root = Path(root).resolve()
    if snapshot is None:
        dates = sorted((p.name for p in root.iterdir()
                        if p.is_dir() and re.fullmatch(r"\d{6}01", p.name)), reverse=True)
        if not dates:
            raise ValueError(f"No dated snapshots found in {root}")
        # Do not silently use an older snapshot if the latest is incomplete.
        snapshot = dates[0]
    if not re.fullmatch(r"\d{6}01", snapshot):
        raise ValueError("--date must be YYYYMM01")
    datetime.strptime(snapshot, "%Y%m%d")
    inputs = {}
    for language, profile in languages.items():
        wiki = profile["wiki"]
        paths = [root / snapshot / wiki / f"{wiki}-{snapshot}-{suffix}" for suffix in SUFFIXES]
        for path in paths:
            if not path.is_file() or path.stat().st_size == 0:
                raise ValueError(f"Missing or empty dump input: {path}")
        inputs[language] = dict(zip(("xml", "index", "page_props"), paths))
    return snapshot, inputs


def sql_rows(values, insert_only=False):
    """Parse mysqldump VALUES tuples without executing SQL from a dump.

    Accept a string or an iterable of byte/text chunks. State spans chunks, so
    multiline INSERTs and quoted binary values remain intact. In insert_only
    mode, chunks must be physical SQL lines and other statements are skipped.
    Binary chunks use a one-to-one byte mapping: SQL syntax, relevant property
    names and Wikidata IDs are ASCII; unrelated BLOB values need no text decoding.
    """
    row, field = None, []
    quoted = False
    string_field = False
    escaped = False
    quote_pending = False
    active = not insert_only
    header = "INSERT INTO `page_props` VALUES"
    escapes = {"0": "\0", "n": "\n", "r": "\r", "t": "\t", "b": "\b", "Z": "\x1a"}
    chunks = (values,) if isinstance(values, (str, bytes)) else values
    for chunk in chunks:
        if isinstance(chunk, bytes):
            chunk = chunk.decode("latin-1")
        if not active:
            if not chunk.startswith(header):
                continue
            chunk = chunk[len(header):]
            active = True
        for char in chunk:
            if quote_pending:
                quote_pending = False
                if char == "'":
                    field.append(char)
                    continue
                else:
                    quoted = False
            if quoted:
                if escaped:
                    field.append(escapes.get(char, char))
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == "'":
                    quote_pending = True
                else:
                    field.append(char)
                continue
            if row is None:
                if char == "(":
                    row, field = [], []
                    string_field = False
                elif char == ";" and insert_only:
                    active = False
                    break
                continue
            if char == "'":
                quoted = True
                string_field = True
            elif char in ",)":
                value = "".join(field) if string_field else "".join(field).strip()
                row.append(None if not string_field and value == "NULL" else value)
                field, string_field = [], False
                if char == ")":
                    yield row
                    row = None
            elif not (string_field and char.isspace()):
                field.append(char)
    if quoted or escaped or row is not None or (insert_only and active):
        raise ValueError("Incomplete page_props SQL tuple")


def normalize(title, language, category="Category", case="first-letter", decode_entities=True):
    if decode_entities:
        title = html.unescape(title)
    title = re.sub(r"[ \t\r\n]+", " ", title.split("#", 1)[0].lstrip(":").replace("_", " ")).strip(" \t\r\n")
    prefix, colon, rest = title.partition(":")
    if colon and prefix.casefold() in {category.casefold(), "category"}:
        prefix, title = category + ":", rest.strip()
    else:
        prefix = ""
    if title and case == "first-letter":
        first = {"i": "İ", "ı": "I"}.get(title[0], title[0].upper()) if language in {"tr", "az"} else title[0].upper()
        title = first + title[1:]
    return prefix + title


def create_staging(path):
    con = sqlite3.connect(path)
    con.executescript("""
        PRAGMA journal_mode = WAL;
        PRAGMA synchronous = NORMAL;
        PRAGMA temp_store = FILE;
        PRAGMA cache_size = -65536;
        CREATE TABLE props(language TEXT, page_id INTEGER, dab INTEGER, hidden INTEGER, qid TEXT,
                           PRIMARY KEY(language, page_id)) WITHOUT ROWID;
        CREATE TABLE pages(id INTEGER PRIMARY KEY, language TEXT, page_id INTEGER,
                           title TEXT, type TEXT, redirect TEXT, qid TEXT, hidden INTEGER, off INTEGER);
        CREATE TABLE links(src INTEGER, language TEXT, target TEXT, type TEXT);
        CREATE TABLE import_state(key TEXT PRIMARY KEY,value TEXT);
    """)
    return con


def import_properties(con, language, path):
    statement = """INSERT INTO props VALUES (?,?,?,?,?)
        ON CONFLICT(language,page_id) DO UPDATE SET
        dab=MAX(dab,excluded.dab), hidden=MAX(hidden,excluded.hidden),
        qid=COALESCE(excluded.qid,qid)"""
    batch = []
    count = 0
    with gzip.open(path, "rb") as stream:
        for row in sql_rows(stream, insert_only=True):
            if len(row) != 4:
                raise ValueError(f"Unsupported page_props schema in {path}")
            page_id, name, value, _ = row
            if name not in {"disambiguation", "hiddencat", "wikibase_item"}:
                continue
            qid = value if name == "wikibase_item" and QID.fullmatch(value or "") else None
            batch.append((language, int(page_id), int(name == "disambiguation"),
                          int(name == "hiddencat"), qid))
            count += 1
            if len(batch) >= 20000:
                con.executemany(statement, batch)
                con.commit()
                batch.clear()
            if count % 1000000 == 0:
                print(f"[{language}] {count:,} relevant page properties", flush=True)
    if batch:
        con.executemany(statement, batch)
        con.commit()
    if not count:
        raise ValueError(f"No relevant page properties found in {path}")


def site_info(path, wiki):
    with bz2.open(path, "rb") as stream:
        for _, elem in ET.iterparse(stream, events=("end",)):
            if elem.tag.rsplit("}", 1)[-1] != "siteinfo":
                continue
            prefix = elem.tag[:-len("siteinfo")]
            if elem.findtext(prefix + "dbname") != wiki:
                raise ValueError(f"XML edition does not match configured {wiki}: {path}")
            namespaces = {int(n.attrib["key"]): n.text or ""
                          for n in elem.findall(prefix + "namespaces/" + prefix + "namespace")}
            if not namespaces.get(14):
                raise ValueError(f"XML has no category namespace: {path}")
            case = elem.findtext(prefix + "case", "first-letter")
            return namespaces, case
    raise ValueError(f"XML has no siteinfo: {path}")


def import_edition(con, language, profile, inputs, max_pages=0):
    print(f"[{language}] importing page properties", flush=True)
    import_properties(con, language, inputs["page_props"])
    namespaces, case = site_info(inputs["xml"], profile["wiki"])
    category = namespaces[14]
    norm = lambda title, entities=True: normalize(title, language, category, case, entities)
    # Only disambiguation titles need to reside in memory. Keep the much larger
    # Wikidata mappings and page/edge inventories in SQLite.
    dab_ids = {r[0] for r in con.execute("SELECT page_id FROM props WHERE language=? AND dab=1", (language,))}
    dab_titles = set()
    con.execute("CREATE TABLE page_index(page_id INTEGER PRIMARY KEY,off INTEGER)")
    index_batch = []
    with bz2.open(inputs["index"], "rt", encoding="utf-8") as stream:
        for line in stream:
            fields = line.rstrip("\n").split(":", 2)
            if len(fields) != 3:
                raise ValueError(f"Invalid multistream index record in {inputs['index']}")
            if int(fields[1]) in dab_ids:
                dab_titles.add(fields[2])
            index_batch.append((int(fields[1]), int(fields[0])))
            if len(index_batch) >= 20000:
                con.executemany("INSERT INTO page_index VALUES (?,?)", index_batch)
                con.commit()
                index_batch.clear()
    con.executemany("INSERT INTO page_index VALUES (?,?)", index_batch)
    con.commit()
    del dab_ids
    cat_pattern = re.compile(r"\[\[\s*(?:" + "|".join(re.escape(p) for p in {category, "Category"}) + r")\s*:\s*([^\]|#]+)", re.I)
    prefixes = [p for p in namespaces.values() if p]
    page_batch, link_batch = [], []
    next_id = con.execute("SELECT COALESCE(MAX(id),-1)+1 FROM pages").fetchone()[0]
    count, scanned, limited = 0, 0, False
    start = time.monotonic()

    def flush():
        con.executemany("INSERT INTO pages VALUES (?,?,?,?,?,?,?,?,?)", page_batch)
        con.executemany("INSERT INTO links VALUES (?,?,?,?)", link_batch)
        con.commit()
        page_batch.clear()
        link_batch.clear()

    with bz2.open(inputs["xml"], "rb") as stream:
        context = ET.iterparse(stream, events=("start", "end"))
        _, root = next(context)
        prefix = root.tag[:-len("mediawiki")]
        for event, elem in context:
            if event != "end" or elem.tag != prefix + "page":
                continue
            scanned += 1
            ns = int(elem.findtext(prefix + "ns", "-1"))
            if ns in (0, 14):
                page_id = int(elem.findtext(prefix + "id"))
                title = elem.findtext(prefix + "title")
                if not title:
                    raise ValueError(f"Page {page_id} has no title")
                metadata = con.execute("""SELECT COALESCE(p.dab,0),COALESCE(p.hidden,0),p.qid,i.off
                    FROM page_index i LEFT JOIN props p ON p.language=? AND p.page_id=i.page_id WHERE i.page_id=?""",
                                       (language, page_id)).fetchone()
                if metadata is None:
                    raise ValueError(f"XML page {page_id} is missing from {inputs['index']}")
                dab, hidden, qid, offset = metadata
                typ = "category" if ns == 14 else "disambiguation" if dab else "article"
                red = elem.find(prefix + "redirect")
                target = red.attrib["title"].split("#", 1)[0] if red is not None else None
                nid = next_id
                next_id += 1
                # XML titles are authoritative identities, not user-entered links.
                page_batch.append((nid, language, page_id, title, typ, target, qid, hidden, offset))
                count += 1
                if target is None and not (ns == 14 and hidden):
                    text = elem.findtext(prefix + "revision/" + prefix + "text", "")
                    text = IGNORED.sub("", text)
                    relationships = {(norm(category + ":" + c), "HAS_CATEGORY" if ns == 0 else "SUBCATEGORY_OF")
                                     for c in cat_pattern.findall(text)}
                    if typ == "disambiguation":
                        relationships.update((norm(t, False), "DISAMBIGUATES_TO") for t in disambiguation_members(
                            text, profile.get("excluded_disambiguation_sections", []), prefixes))
                    elif typ == "article":
                        relationships.update((norm(t), "HAS_DISAMBIGUATION") for t in LINK.findall(text)
                                             if norm(t) in dab_titles)
                    link_batch.extend((nid, language, t, kind) for t, kind in relationships)
                if len(page_batch) >= 5000 or len(link_batch) >= 50000:
                    flush()
            elem.clear()
            root.clear()
            if scanned % 100000 == 0:
                print(f"[{language}] {scanned:,} XML pages scanned ({time.monotonic()-start:.0f}s)", flush=True)
            if max_pages and scanned >= max_pages:
                limited = True
                break
    flush()
    con.execute("DROP TABLE page_index")
    con.execute("DELETE FROM props WHERE language=?", (language,))
    con.commit()
    print(f"[{language}] {count:,} article/category records", flush=True)
    if not count:
        raise ValueError(f"No article/category pages in {inputs['xml']}")
    return {"pages_scanned": scanned, "page_records": count, "limited": limited}


def input_identity(snapshot, inputs, max_pages):
    return {"snapshot": snapshot, "languages": list(inputs), "max_pages": max_pages,
            "sources": {code: {key: {"path": str(path.resolve()), "bytes": path.stat().st_size}
                               for key, path in paths.items()} for code, paths in inputs.items()}}


def save_import_state(con, state):
    con.execute("CREATE TABLE IF NOT EXISTS import_state(key TEXT PRIMARY KEY,value TEXT)")
    con.execute("INSERT OR REPLACE INTO import_state VALUES ('import',?)", (json.dumps(state),))
    con.commit()


def restore_legacy_titles(con, inputs, languages):
    """Restore canonical names in older staging without reparsing article XML.

    Old versions treated stored page titles as links and folded Unicode space.
    The multistream index preserves the canonical title for each page ID.
    """
    con.execute("DROP INDEX IF EXISTS page_titles")
    con.execute("CREATE TEMP TABLE title_repairs(language TEXT,page_id INTEGER,title TEXT,PRIMARY KEY(language,page_id)) WITHOUT ROWID")
    for code, paths in inputs.items():
        namespaces, case = site_info(paths["xml"], languages[code]["wiki"])
        batch = []
        count = 0
        print(f"[{code}] checking canonical titles in the index", flush=True)
        with bz2.open(paths["index"], "rt", encoding="utf-8") as stream:
            for line in stream:
                _, page_id, title = line.rstrip("\r\n").split(":", 2)
                # Reproduce the old normalization only to find changed names.
                old = title_key(title.split("#", 1)[0].lstrip(":"))
                old = normalize(old, code, namespaces[14], case, False)
                if old != title:
                    batch.append((code, int(page_id), title))
                    count += 1
                if len(batch) >= 20000:
                    con.executemany("INSERT OR REPLACE INTO title_repairs VALUES (?,?,?)", batch)
                    batch.clear()
        con.executemany("INSERT OR REPLACE INTO title_repairs VALUES (?,?,?)", batch)
        print(f"[{code}] {count:,} index titles need their original spelling", flush=True)
    cur = con.execute("""UPDATE pages SET title=r.title FROM title_repairs r
        WHERE pages.language=r.language AND pages.page_id=r.page_id AND pages.title!=r.title""")
    repaired = cur.rowcount
    con.execute("DROP TABLE title_repairs")
    con.commit()
    return repaired


def reuse_import(con, identity, inputs, languages):
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"pages", "links", "props"} <= tables:
        raise ValueError("Staging is missing imported pages/links; run a fresh extraction")
    row = con.execute("SELECT value FROM import_state WHERE key='import'").fetchone() if "import_state" in tables else None
    if row:
        state = json.loads(row[0])
        if state.get("inputs") != identity:
            raise ValueError("Staging inputs differ; use the original --date, --languages, --max-pages and --dumps settings")
        if not state.get("complete"):
            raise ValueError("Dump import was interrupted before completion; --skip-import requires completed imports")
        return state
    # Compatibility with completed imports made before recovery metadata existed.
    if "page_index" in tables or con.execute("SELECT 1 FROM props LIMIT 1").fetchone():
        raise ValueError("Legacy staging has an unfinished import; --skip-import cannot reuse it")
    counts = dict(con.execute("SELECT language,COUNT(*) FROM pages GROUP BY language"))
    if set(counts) != set(inputs):
        raise ValueError("Staged languages differ from the requested languages")
    print("[graph] legacy staging has no import settings; using the supplied snapshot and limits", flush=True)
    repaired = restore_legacy_titles(con, inputs, languages)
    state = {"inputs": identity, "complete": True, "canonical_titles": True,
             "legacy_staging": True, "restored_titles": repaired,
             "statistics": {code: {"pages_scanned": None, "page_records": count,
                                    "limited": bool(identity["max_pages"])} for code, count in counts.items()}}
    save_import_state(con, state)
    return state


def materialize(con):
    print("[graph] resolving redirects and local relationships", flush=True)
    con.executescript("""
        DROP TABLE IF EXISTS graph_edges;
        DROP TABLE IF EXISTS graph_nodes;
        DROP TABLE IF EXISTS resolved;
        DROP TABLE IF EXISTS selected_pages;
        DROP INDEX IF EXISTS page_titles;
        DROP INDEX IF EXISTS redirect_targets;
        CREATE INDEX page_titles ON pages(language,title);
        CREATE TABLE selected_pages(id INTEGER PRIMARY KEY);
        INSERT INTO selected_pages
          SELECT id FROM (SELECT id,ROW_NUMBER() OVER
            (PARTITION BY language,title ORDER BY (redirect IS NOT NULL),page_id,id) AS priority FROM pages)
          WHERE priority=1;
        CREATE TABLE graph_nodes(id INTEGER PRIMARY KEY, original_id INTEGER UNIQUE,
                                 language TEXT, title TEXT, type TEXT, page_id INTEGER, wikidata_id TEXT, off INTEGER);
        INSERT INTO graph_nodes
          SELECT ROW_NUMBER() OVER (ORDER BY p.id)-1,p.id,language,title,type,page_id,qid,off
          FROM pages p JOIN selected_pages s ON s.id=p.id
          WHERE redirect IS NULL AND NOT(type='category' AND hidden=1);
        CREATE TABLE resolved(language TEXT,title TEXT,node_id INTEGER,
                              PRIMARY KEY(language,title)) WITHOUT ROWID;
        INSERT INTO resolved SELECT language,title,id FROM graph_nodes;
        CREATE INDEX redirect_targets ON pages(language,redirect) WHERE redirect IS NOT NULL;
    """)
    # Each iteration resolves one more redirect hop. Cycles and missing targets
    # never acquire a node_id and consequently cannot create spurious edges.
    while True:
        cur = con.execute("""INSERT OR IGNORE INTO resolved
            SELECT p.language,p.title,r.node_id FROM pages p JOIN selected_pages s ON s.id=p.id
            JOIN resolved r ON r.language=p.language AND r.title=p.redirect
            WHERE p.redirect IS NOT NULL""")
        con.commit()
        if not cur.rowcount:
            break
    con.executescript("""
        CREATE TABLE graph_edges(src INTEGER,dst INTEGER,type TEXT,
                                 PRIMARY KEY(src,dst,type)) WITHOUT ROWID;
        INSERT OR IGNORE INTO graph_edges
          SELECT s.id,d.id,l.type FROM links l
          JOIN graph_nodes s ON s.original_id=l.src
          JOIN resolved r ON r.language=l.language AND r.title=l.target
          JOIN graph_nodes d ON d.id=r.node_id
          WHERE s.id!=d.id AND
          ((l.type='HAS_CATEGORY' AND d.type='category') OR
           (l.type='SUBCATEGORY_OF' AND s.type='category' AND d.type='category') OR
           (l.type='DISAMBIGUATES_TO' AND s.type='disambiguation' AND d.type='article') OR
           (l.type='HAS_DISAMBIGUATION' AND s.type='article' AND d.type='disambiguation'));
        CREATE INDEX identity_items ON graph_nodes(wikidata_id,language) WHERE type='article' AND wikidata_id IS NOT NULL;
        INSERT OR IGNORE INTO graph_edges
          SELECT a.id,b.id,'LANGUAGE_EQUIVALENT' FROM graph_nodes a
          JOIN graph_nodes b ON a.wikidata_id=b.wikidata_id AND a.language<b.language
          WHERE a.type='article' AND b.type='article' AND a.wikidata_id IS NOT NULL;
    """)
    # Restore the original builder's primary-topic bridge. A hatnote such as
    # {{About|...|other uses}} need not contain an explicit [[DAB]] link.
    for article, dab, _, _, _ in staged_primary_topics(con, CONFIG["languages"]):
        con.execute("INSERT OR IGNORE INTO graph_edges VALUES (?,?,'HAS_DISAMBIGUATION')",
                    (article, dab))
    con.commit()
    staged = con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    selected = con.execute("SELECT COUNT(*) FROM selected_pages").fetchone()[0]
    conflicts = {"discarded_records": staged-selected,
                 "policy": "Within each edition/title, prefer a nonredirect, then the lowest page ID, then the lowest staging ID. Discarded records contribute no nodes or edges."}
    print(f"[graph] selected {selected:,} page records; {staged-selected:,} duplicate-title records excluded", flush=True)
    return conflicts


def export_graph(con, output, snapshot, inputs, statistics, max_pages, conflicts=None, import_state=None):
    def write_csv(name, header, rows):
        with (output / name).open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(header)
            writer.writerows(rows)

    # Publish through .part files so an interrupted export cannot be mistaken
    # for a complete graph. The manifest is the final completion marker.
    write_csv("nodes.csv.part", ("id", "title", "type", "language", "page_id", "wikidata_id", "offset"),
              con.execute("SELECT id,title,type,language,page_id,wikidata_id,off FROM graph_nodes ORDER BY id"))
    write_csv("edges.csv.part", ("src", "dst", "type", "weight"),
              ((s, d, t, EDGE_WEIGHTS[t]) for s, d, t in con.execute("SELECT src,dst,type FROM graph_edges")))
    write_csv("redirects.csv.part", ("from_title", "to_title", "language"),
              con.execute("""SELECT p.title,n.title,p.language FROM pages p JOIN selected_pages s ON s.id=p.id
                  JOIN resolved r ON r.language=p.language AND r.title=p.title
                  JOIN graph_nodes n ON n.id=r.node_id WHERE p.redirect IS NOT NULL"""))
    # Record identity alignment and the derivation of primary-topic bridges.
    with (output / "provenance.jsonl.part").open("w", encoding="utf-8") as stream:
        for src, dst, qid, a, ap, b, bp in con.execute("""SELECT e.src,e.dst,a.wikidata_id,a.language,a.page_id,b.language,b.page_id
            FROM graph_edges e JOIN graph_nodes a ON a.id=e.src JOIN graph_nodes b ON b.id=e.dst
            WHERE e.type='LANGUAGE_EQUIVALENT'"""):
            record = {"src": src, "dst": dst, "type": "LANGUAGE_EQUIVALENT", "evidence": {
                "source_mode": "wikipedia-dump", "snapshot": snapshot, "wikidata_id": qid,
                "alignment": "matching wikibase_item page properties on ordinary articles",
                "source": {"language": a, "page_id": ap, "file": str(inputs[a]["page_props"])},
                "target": {"language": b, "page_id": bp, "file": str(inputs[b]["page_props"])}}}
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        for src, dst, article_title, dab_title, language in staged_primary_topics(con, CONFIG["languages"]):
            record = {"src": src, "dst": dst, "type": "HAS_DISAMBIGUATION", "evidence":
                primary_topic_evidence(language, article_title, dab_title,
                    source_mode="wikipedia-dump", snapshot=snapshot)}
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    for name in ("nodes.csv", "edges.csv", "redirects.csv", "provenance.jsonl"):
        (output / (name + ".part")).replace(output / name)
    manifest = {"schema_version": 2, "builder": "wikihopper_dumps", "source_mode": "wikipedia-dump",
                "snapshot": snapshot, "languages": list(inputs), "base_language": (
                    CONFIG["multilingual"]["base_language"] if CONFIG["multilingual"]["base_language"] in inputs else next(iter(inputs))),
                "built_at": datetime.now(timezone.utc).isoformat(), "bounded": bool(max_pages),
                "limits": {"max_pages_per_language": max_pages or None},
                "duplicate_titles": conflicts,
                "import_recovery": {key: (import_state or {}).get(key) for key in ("legacy_staging", "restored_titles")},
                "statistics": {"editions": statistics, "nodes": con.execute("SELECT COUNT(*) FROM graph_nodes").fetchone()[0],
                               "edges": con.execute("SELECT COUNT(*) FROM graph_edges").fetchone()[0]},
                "sources": {code: {key: {"path": str(path), "bytes": path.stat().st_size}
                                   for key, path in paths.items()} for code, paths in inputs.items()},
                "limitations": ["Explicit wikitext categories only; template-generated categories are not expanded.",
                                "Article-to-disambiguation links use explicit wikilinks and edition-local primary-topic title conventions; other template-generated links are not expanded.",
                                "Disambiguation entries use leading bullet links; complex markup may be omitted."]}
    (output / "manifest.json.part").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "manifest.json.part").replace(output / "manifest.json")
    return manifest


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
        print(f'Next: python explorer.py --out "{output}" --db "{output / "graph.db"}" --build-only --rebuild')
        return 0
    except (OSError, ValueError, sqlite3.Error, ET.ParseError) as exc:
        print(f"Extraction error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nExtraction interrupted. If imports completed, rerun with --skip-import; otherwise use --rebuild.", file=sys.stderr)
        sys.exit(130)
