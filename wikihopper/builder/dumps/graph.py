"""Resolve staged pages into graph nodes/edges and export the graph artifacts."""

import csv
from datetime import datetime, timezone
import json

from ...config import CONFIG, EDGE_WEIGHTS
from ..disambiguation import staged_primary_topics, primary_topic_evidence


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
