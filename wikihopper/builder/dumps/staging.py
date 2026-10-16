"""Import page properties, XML pages and raw links into a disk-backed staging database."""

import bz2
import gzip
import json
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
import html

from ..wikitext import disambiguation_members, title_key
from .sql import sql_rows

LINK = re.compile(r"\[\[\s*([^\[\]|#]+)")
IGNORED = re.compile(r"<!--.*?-->|<(?:nowiki|pre|source|syntaxhighlight)\b[^>]*>.*?</(?:nowiki|pre|source|syntaxhighlight)\s*>", re.I | re.S)
QID = re.compile(r"Q[1-9][0-9]*\Z")


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
