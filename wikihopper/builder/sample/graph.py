"""Capped breadth-first import and export of evidenced relationships."""

from collections import deque
import csv
from datetime import datetime, timezone
import hashlib
from itertools import combinations
import json
from pathlib import Path
import re
import sys

from ...config import EDGE_WEIGHTS
from ..disambiguation import primary_topic_title, primary_topic_evidence
from ..wikitext import disambiguation_members, page_type, source_url, title_key
from .sources import FixtureSource, SourceLimit

_QID = re.compile(r"^Q[1-9][0-9]*$")


def build_graph(source, seeds, languages, limits, output, base_language="en"):
    """Fetch capped BFS snapshots, then materialize only evidenced relations."""
    if len(set(seeds)) > limits["max_pages"]:
        raise ValueError("max-pages must be at least the number of distinct seeds")
    if len(set(seeds)) > limits["max_discovered_pages"]:
        raise ValueError("max-discovered-pages must be at least the number of distinct seeds")
    output = Path(output)
    queue = deque((language, title_key(title), 0) for language, title in seeds)
    queued = {(language, title_key(title)) for language, title in seeds}
    fetched = {}
    requested = {}
    depths = {}
    stats = {"missing_pages": 0, "unsupported_namespace_pages": 0,
             "counterpart_qid_mismatches": 0, "counterpart_unavailable": 0,
             "counterpart_nonarticles": 0, "duplicate_page_requests": 0,
             "max_pages_reached": False, "max_depth_reached": False,
             "max_api_requests_reached": False, "max_discovered_pages_reached": False}

    def dab_entries(page):
        cache_key = (page["language"], page["title"])
        if cache_key in dab_entry_cache:
            return dab_entry_cache[cache_key]
        entries = list(disambiguation_members(page["wikitext"],
            languages[page["language"]].get("excluded_disambiguation_sections", []),
            source.namespace_prefixes.get(page["language"], [])))
        cap = limits["max_disambiguation_members"]
        if len(entries) > cap:
            source.truncations["disambiguation_members"] = source.truncations.get("disambiguation_members", 0) + 1
        dab_entry_cache[cache_key] = entries[:cap]
        return dab_entry_cache[cache_key]

    dab_entry_cache = {}

    while queue:
        if len(fetched) >= limits["max_pages"]:
            stats["max_pages_reached"] = True
            break
        language, title, depth = queue.popleft()
        key = (language, title)
        try:
            page = source.fetch(language, title)
        except SourceLimit:
            stats["max_api_requests_reached"] = True
            break
        if page is None:
            stats["missing_pages"] += 1
            requested[key] = None
            continue
        canonical = (language, page["title"])
        requested[key] = canonical
        if page_type(page) is None:
            stats["unsupported_namespace_pages"] += 1
            continue
        if canonical in fetched:
            stats["duplicate_page_requests"] += 1
            continue
        fetched[canonical] = page
        depths[canonical] = depth
        if source.mode == "mediawiki-api" and len(fetched) % 10 == 0:
            print(f"Imported {len(fetched)} pages; {source.requests} API requests", file=sys.stderr)
        neighbors = []
        # Prefer the user's configured multilingual connections before broad
        # local expansion when a small global page budget is selected.
        if page_type(page) == "article":
            neighbors.extend((code, counterpart) for code, counterpart in page["langlinks"].items()
                if code in languages and code != language)
        neighbors.extend((language, title) for title in page["categories"])
        if page_type(page) == "category":
            neighbors.extend((language, title) for title in page["category_members"])
        elif page_type(page) == "disambiguation":
            neighbors.extend((language, title) for title in dab_entries(page))
            primary = primary_topic_title(page["title"], languages[language])
            if primary:
                neighbors.append((language, primary))
        else:
            # Prioritize the small primary-topic bridge before broad article
            # links so bounded API imports do not miss template-only hatnotes.
            neighbors.extend((language, page["title"] + suffix)
                for suffix in languages[language].get("disambiguation_suffixes", []))
            neighbors.extend((language, title) for title in page["links"])
        if depth >= limits["depth"]:
            if neighbors:
                stats["max_depth_reached"] = True
            continue
        for next_language, next_title in neighbors:
            next_key = (next_language, title_key(next_title))
            if next_key not in queued:
                if len(queued) >= limits["max_discovered_pages"]:
                    stats["max_discovered_pages_reached"] = True
                    continue
                queued.add(next_key)
                queue.append((*next_key, depth + 1))

    def resolve(language, title):
        key = (language, title_key(title))
        seen = set()
        while key in source.redirects:
            if key in seen:
                return None
            seen.add(key)
            key = (language, source.redirects[key])
        key = requested.get(key, key)
        return key if key in fetched else None

    node_keys = sorted(fetched)
    node_ids = {key: index for index, key in enumerate(node_keys)}
    edges = {}

    def evidence(page, relation, **extra):
        return {"source_url": source_url(page["language"], page["title"]),
            "source_language": page["language"], "revision_id": page["revision_id"],
            "source_timestamp": page["timestamp"], "retrieved_at": page.get("retrieved_at", ""), "relation": relation,
            "source_mode": source.mode, **extra}

    def add_edge(src, dst, kind, proof):
        if src is None or dst is None or src == dst:
            return
        edge = (node_ids[src], node_ids[dst], kind)
        if edge not in edges:
            edges[edge] = proof

    for key in node_keys:
        page = fetched[key]
        kind = page_type(page)
        for category in page["categories"]:
            target = resolve(page["language"], category)
            if target is not None and page_type(fetched[target]) == "category":
                add_edge(key, target, "SUBCATEGORY_OF" if kind == "category" else "HAS_CATEGORY",
                    evidence(page, "category-membership", category_title=category))
        if kind == "category":
            for title in page["category_members"]:
                member = resolve(page["language"], title)
                if member is not None:
                    add_edge(member, key, "SUBCATEGORY_OF" if page_type(fetched[member]) == "category" else "HAS_CATEGORY",
                        evidence(page, "incoming-category-member", member_title=title))
        elif kind == "disambiguation":
            primary = primary_topic_title(page["title"], languages[page["language"]])
            article = resolve(page["language"], primary) if primary else None
            if article is not None and page_type(fetched[article]) == "article":
                proof = evidence(page, "primary-topic-title-convention")
                proof.update(primary_topic_evidence(page["language"], fetched[article]["title"], page["title"]))
                add_edge(article, key, "HAS_DISAMBIGUATION", proof)
            for title in dab_entries(page):
                target = resolve(page["language"], title)
                if target is not None and page_type(fetched[target]) == "article":
                    source_lines = [line.strip() for line in page["wikitext"].splitlines()
                        if title in list(disambiguation_members(line, [], source.namespace_prefixes.get(page["language"], [])))]
                    add_edge(key, target, "DISAMBIGUATES_TO", evidence(page,
                        "leading-bullet-wikilink", target_title=title, lines=source_lines))
        else:
            for title in page["links"]:
                target = resolve(page["language"], title)
                if target is not None and page_type(fetched[target]) == "disambiguation":
                    add_edge(key, target, "HAS_DISAMBIGUATION", evidence(page,
                        "article-link-to-disambiguation", target_title=title))
            for code, title in sorted(page["langlinks"].items()):
                if code not in languages or code == page["language"]:
                    continue
                target = resolve(code, title)
                if target is None:
                    stats["counterpart_unavailable"] += 1
                    continue
                counterpart = fetched[target]
                if page_type(counterpart) != "article":
                    stats["counterpart_nonarticles"] += 1
                    continue
                qid = page.get("wikidata_id", "")
                if not _QID.fullmatch(qid) or qid != counterpart.get("wikidata_id"):
                    stats["counterpart_qid_mismatches"] += 1
                    continue
                first, second = sorted((key, target))
                add_edge(first, second, "LANGUAGE_EQUIVALENT", evidence(page, "verified-same-wikidata-item",
                    wikidata_id=qid, wikidata_url=f"https://www.wikidata.org/wiki/{qid}",
                    counterpart_url=source_url(code, counterpart["title"]), counterpart_language=code,
                    counterpart_revision_id=counterpart["revision_id"]))

    # Pageprops independently verifies each loaded article's Wikidata item.
    # Join all loaded editions of that item even when API langlink pagination
    # did not return every pair (or the user explicitly seeded counterparts).
    qid_groups = {}
    for key in node_keys:
        page = fetched[key]
        qid = page.get("wikidata_id", "")
        if page_type(page) == "article" and _QID.fullmatch(qid):
            qid_groups.setdefault(qid, []).append(key)
    for qid, keys in sorted(qid_groups.items()):
        for first, second in combinations(keys, 2):
            if first[0] == second[0]:
                continue
            original, counterpart = fetched[first], fetched[second]
            add_edge(first, second, "LANGUAGE_EQUIVALENT", evidence(original,
                "verified-same-wikidata-item", wikidata_id=qid,
                wikidata_url=f"https://www.wikidata.org/wiki/{qid}",
                counterpart_url=source_url(second[0], counterpart["title"]),
                counterpart_language=second[0], counterpart_revision_id=counterpart["revision_id"]))

    output.mkdir(parents=True, exist_ok=True)
    with (output / "nodes.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("id", "title", "type", "language", "page_id", "wikidata_id"))
        for key in node_keys:
            page = fetched[key]
            writer.writerow((node_ids[key], page["title"], page_type(page),
                page["language"], page["page_id"], page["wikidata_id"]))
    with (output / "edges.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("src", "dst", "type", "weight"))
        for edge in sorted(edges):
            writer.writerow((*edge, EDGE_WEIGHTS[edge[2]]))
    with (output / "redirects.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(("from_title", "to_title", "language"))
        for (language, old_title), target_title in sorted(source.redirects.items()):
            target = resolve(language, target_title)
            if target is not None and old_title != target[1]:
                writer.writerow((old_title, target[1], language))
    with (output / "provenance.jsonl").open("w", encoding="utf-8") as stream:
        for (src, dst, kind), proof in sorted(edges.items()):
            stream.write(json.dumps({"src": src, "dst": dst, "type": kind, "evidence": proof},
                ensure_ascii=False, sort_keys=True) + "\n")
    fixture = {"pages": [fetched[key] for key in node_keys],
        "redirects": [{"language": language, "frm": title, "to_": target}
            for (language, title), target in sorted(source.redirects.items())],
        "namespace_prefixes": source.namespace_prefixes}
    (output / "snapshots.json").write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    manifest = {"schema_version": 2, "builder": "wikihopper_multilingual", "bounded": True,
        "languages": list(languages), "base_language": base_language, "source_mode": source.mode,
        "built_at": datetime.now(timezone.utc).isoformat(), "limits": dict(limits),
        "seeds": [{"language": language, "title": title} for language, title in seeds],
        "statistics": {**stats, "nodes": len(node_keys), "edges": len(edges),
            "api_requests": source.requests, "truncations": source.truncations,
            "unfetched_discovered_pages": len(queue)},
        "snapshot_timestamps": sorted({p["timestamp"] for p in fetched.values() if p["timestamp"]}),
        "snapshot_retrieved_at": sorted({p["retrieved_at"] for p in fetched.values() if p.get("retrieved_at")}),
        "language_edge_policy": "ordinary articles with matching verified Wikidata IDs; stored once",
        "category_policy": "MediaWiki categories API with hidden categories excluded (clshow=!hidden)",
        "metadata_policy": "API metadata captured at import time; revision ID identifies content, not a historical metadata reconstruction",
        "search_completeness": "bounded imported sample, not complete Wikipedia editions"}
    if isinstance(source, FixtureSource):
        manifest["fixture_sha256"] = source.sha256
    for filename in ("nodes.csv", "edges.csv", "redirects.csv", "provenance.jsonl", "snapshots.json"):
        manifest.setdefault("artifact_sha256", {})[filename] = hashlib.sha256((output / filename).read_bytes()).hexdigest()
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest, fetched
