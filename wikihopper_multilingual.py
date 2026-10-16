"""Build a bounded, reproducible Wikipedia graph in configured languages.

This proof-of-concept imports MediaWiki Action API snapshots, rather than full
dumps. Pages keep their own edition's categories and disambiguation structure;
only ordinary articles with the same verified Wikidata item are joined.

Examples::

    python wikihopper_multilingual.py --seed en:Apple --seed es:Manzana
    python wikihopper_multilingual.py --offline-fixture fixture.json --seed en:Apple

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

from __future__ import annotations

import argparse
from collections import deque
import csv
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import html
from itertools import combinations
import json
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

if __package__:
    from .wikihopper_config import CONFIG, EDGE_WEIGHTS
    from .wikihopper_disambiguation import primary_topic_title, primary_topic_evidence
else:
    from wikihopper_config import CONFIG, EDGE_WEIGHTS
    from wikihopper_disambiguation import primary_topic_title, primary_topic_evidence


_HEADING = re.compile(r"^(=+)\s*(.*?)\s*\1\s*$")
_LEADING_LINK = re.compile(r"^\[\[\s*([^\[\]|#]+)")
_QID = re.compile(r"^Q[1-9][0-9]*$")
_DEFAULT_EXCLUDED = {"see also", "references", "external links", "further reading"}


def title_key(title):
    """Leave locale-sensitive capitalization to MediaWiki, not Python."""
    return " ".join(str(title).replace("_", " ").strip().split())


def page_type(page):
    if page["namespace"] == 14:
        return "category"
    if page["namespace"] == 0:
        return "disambiguation" if page.get("disambiguation") else "article"
    return None


def source_url(language, title):
    return "https://" + language + ".wikipedia.org/wiki/" + urllib.parse.quote(
        title.replace(" ", "_"), safe="/:()")


def disambiguation_members(wikitext, excluded_sections, namespace_prefixes=()):
    """Read entry-leading links; ignore prose links and non-entry sections."""
    excluded = _DEFAULT_EXCLUDED | {s.strip().casefold() for s in excluded_sections}
    prefixes = {p.casefold() for p in namespace_prefixes}
    skip_level = None
    seen = set()
    for line in wikitext.splitlines():
        line = line.strip()
        heading = _HEADING.match(line)
        if heading:
            level = len(heading.group(1))
            if skip_level is not None and level <= skip_level:
                skip_level = None
            if heading.group(2).strip().casefold() in excluded:
                skip_level = level
            continue
        if skip_level is not None or not line or line[0] not in "*#":
            continue
        body = line.lstrip("*#:; ").lstrip("'\" ")
        match = _LEADING_LINK.match(body)
        if not match:
            continue
        title = title_key(html.unescape(match.group(1).lstrip(":")))
        prefix, colon, _ = title.partition(":")
        if not title or (colon and prefix.casefold() in prefixes) or title in seen:
            continue
        seen.add(title)
        yield title


class SourceLimit(Exception):
    """An explicitly configured source request budget was exhausted."""


class FixtureSource:
    mode = "offline-fixture"

    def __init__(self, path, limits):
        self.path = Path(path)
        raw = self.path.read_bytes()
        data = json.loads(raw.decode("utf-8"))
        pages = data.get("pages") if isinstance(data, dict) else data
        if not isinstance(pages, list):
            raise ValueError("Fixture must contain a pages array")
        self.pages = {}
        self.redirects = {}
        self.namespace_prefixes = {}
        self.truncations = {}
        self.requests = 0
        self.limits = limits
        self.sha256 = hashlib.sha256(raw).hexdigest()
        for raw_page in pages:
            page = self._normalize(raw_page)
            key = (page["language"], page["title"])
            if key in self.pages:
                raise ValueError(f"Duplicate fixture page: {key}")
            self.pages[key] = page
        for redirect in data.get("redirects", []) if isinstance(data, dict) else []:
            self.redirects[(redirect["language"], title_key(redirect.get("frm", redirect.get("from_title", ""))))] = title_key(redirect.get("to_", redirect.get("to_title", "")))
        if isinstance(data, dict):
            self.namespace_prefixes = data.get("namespace_prefixes", {})
        # Namespace IDs on retrieved pages remain authoritative. Localized
        # category prefixes can additionally reject nonarticle DAB entries.
        for page in self.pages.values():
            if page["namespace"] == 14 and ":" in page["title"]:
                self.namespace_prefixes.setdefault(page["language"], []).append(page["title"].split(":", 1)[0])

    @staticmethod
    def _normalize(raw):
        if not isinstance(raw, dict) or not all(k in raw for k in ("language", "title", "namespace")):
            raise ValueError("Fixture pages need language, title, and namespace")
        page = dict(raw)
        page["title"] = title_key(page["title"])
        if type(page["namespace"]) is not int or type(page.get("disambiguation", False)) is not bool:
            raise ValueError("Fixture namespace must be an integer and disambiguation a boolean")
        page.setdefault("page_id", "")
        page.setdefault("wikidata_id", "")
        page.setdefault("disambiguation", False)
        page.setdefault("revision_id", "")
        page.setdefault("timestamp", "")
        page.setdefault("retrieved_at", "")
        page.setdefault("wikitext", "")
        for name in ("categories", "category_members", "links"):
            values = page.get(name, [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ValueError(f"Fixture {name} must be an array of titles")
            page[name] = list(dict.fromkeys(title_key(v) for v in values))
        if not isinstance(page.get("langlinks", {}), dict):
            raise ValueError("Fixture langlinks must map language codes to titles")
        page["langlinks"] = {k: title_key(v) for k, v in page.get("langlinks", {}).items()}
        return page

    def fetch(self, language, title):
        key = (language, title_key(title))
        seen = set()
        while key in self.redirects:
            if key in seen:
                raise ValueError(f"Redirect cycle in fixture: {key}")
            seen.add(key)
            key = (language, self.redirects[key])
        original = self.pages.get(key)
        if original is None:
            return None
        page = dict(original)
        for name, limit in (("category_members", self.limits["max_category_members"]),
                            ("links", self.limits["max_article_links"]),
                            ("categories", self.limits["max_categories"])):
            page[name] = original[name][:limit]
            if len(original[name]) > limit:
                self.truncations[name] = self.truncations.get(name, 0) + 1
        return page


class ApiSource:
    mode = "mediawiki-api"

    def __init__(self, languages, limits, user_agent=None):
        self.languages = languages
        self.limits = limits
        self.requests = 0
        self.redirects = {}
        self.namespace_prefixes = {}
        self.truncations = {}
        self.pages = {}
        self.user_agent = user_agent or "WikiHopper/0.1 (bounded multilingual research demonstration)"
        self._last_request = None

    @staticmethod
    def _retry_delay(header, attempt):
        delay = min(2 ** attempt, 60)
        if header:
            try:
                delay = float(header)
            except (TypeError, ValueError):
                try:
                    retry_at = parsedate_to_datetime(header)
                    if retry_at.tzinfo is None:
                        retry_at = retry_at.replace(tzinfo=timezone.utc)
                    delay = (retry_at - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    pass
        if not 0 <= delay <= 60:
            if delay < 0:
                return 0
            raise ValueError("MediaWiki Retry-After exceeds 60 seconds; try again later or replay an offline snapshot")
        return delay

    def _request(self, language, params):
        url = f"https://{language}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(
            {"format": "json", "formatversion": 2, **params})
        request = urllib.request.Request(url, headers={
            "User-Agent": self.user_agent,
            "Accept": "application/json"})
        retries = self.limits.get("retries", 3)
        for attempt in range(retries + 1):
            if self.requests >= self.limits["max_api_requests"]:
                raise SourceLimit("max_api_requests")
            if self._last_request is not None:
                spacing = self.limits.get("request_delay", 1.0) - (time.monotonic() - self._last_request)
                if spacing > 0:
                    time.sleep(spacing)
            self.requests += 1
            self._last_request = time.monotonic()
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    result = json.load(response)
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 503) or attempt >= retries:
                    raise ValueError(f"MediaWiki request failed for {language}: HTTP {exc.code}; "
                        "try again later, set --user-agent with a project/contact URL, or use --offline-fixture") from exc
                self.truncations["http_retries"] = self.truncations.get("http_retries", 0) + 1
                delay = self._retry_delay(exc.headers.get("Retry-After") if exc.headers else None, attempt)
                if self.requests >= self.limits["max_api_requests"]:
                    raise SourceLimit("max_api_requests") from exc
                if delay > 0:
                    time.sleep(delay)
            except (urllib.error.URLError, json.JSONDecodeError) as exc:
                raise ValueError(f"MediaWiki request failed for {language}: {exc}") from exc
        if "error" in result:
            raise ValueError(f"MediaWiki error for {language}: {result['error']}")
        return result

    def _siteinfo(self, language):
        if language in self.namespace_prefixes:
            return
        query = self._request(language, {"action": "query", "meta": "siteinfo",
            "siprop": "namespaces|namespacealiases|interwikimap"}).get("query", {})
        prefixes = set(self.languages)
        namespaces = query.get("namespaces", {})
        if isinstance(namespaces, dict):
            namespaces = namespaces.values()
        for namespace in namespaces:
            if namespace.get("id", 0) != 0:
                prefixes.update(str(namespace[k]) for k in ("name", "canonical", "*") if namespace.get(k))
        for alias in query.get("namespacealiases", []):
            prefixes.add(alias.get("alias", alias.get("*", "")))
        prefixes.update(entry["prefix"] for entry in query.get("interwikimap", []))
        self.namespace_prefixes[language] = sorted(prefixes)

    def fetch(self, language, title):
        self._siteinfo(language)
        params = {"action": "query", "titles": title, "redirects": 1,
            "prop": "info|pageprops|categories|langlinks|links|revisions",
            "ppprop": "wikibase_item|disambiguation", "cllimit": 500, "clshow": "!hidden",
            "lllimit": 500, "pllimit": min(500, max(1, self.limits["max_article_links"])),
            "plnamespace": 0, "rvprop": "ids|timestamp|content", "rvslots": "main", "rvlimit": 1}
        page = None
        continuation = {}
        for batch in range(self.limits["max_continuation_pages"]):
            result = self._request(language, {**params, **continuation})
            query = result.get("query", {})
            for redirect in query.get("redirects", []) + query.get("normalized", []):
                self.redirects[(language, title_key(redirect["from"]))] = title_key(redirect["to"])
            records = query.get("pages", [])
            if not records or "missing" in records[0] or "invalid" in records[0]:
                return None
            record = records[0]
            if page is None:
                revision = (record.get("revisions") or [{}])[0]
                slot = revision.get("slots", {}).get("main", {})
                props = record.get("pageprops", {})
                page = {"language": language, "title": title_key(record["title"]),
                    "page_id": record.get("pageid", ""), "namespace": record["ns"],
                    "wikidata_id": props.get("wikibase_item", ""),
                    "disambiguation": "disambiguation" in props,
                    "revision_id": revision.get("revid", ""), "timestamp": revision.get("timestamp", ""),
                    "retrieved_at": datetime.now(timezone.utc).isoformat(),
                    "wikitext": slot.get("content", slot.get("*", revision.get("*", ""))),
                    "categories": [], "langlinks": {}, "category_members": [], "links": []}
            for name, property_name, limit in (("categories", "categories", self.limits["max_categories"]),
                                              ("links", "links", self.limits["max_article_links"])):
                for entry in record.get(property_name, []):
                    value = title_key(entry["title"])
                    if value not in page[name]:
                        if len(page[name]) < limit:
                            page[name].append(value)
                        else:
                            self.truncations[name] = self.truncations.get(name, 0) + 1
            for entry in record.get("langlinks", []):
                if entry["lang"] in self.languages:
                    page["langlinks"][entry["lang"]] = title_key(entry.get("title", entry.get("*", "")))
            continuation = result.get("continue", {})
            if not continuation:
                break
            # One revision is enough for a frozen snapshot. Drop exhausted
            # properties as well as their continuation keys so unrelated
            # continuations never fetch their first page repeatedly.
            properties = params["prop"].split("|")
            stop = {"revisions": "rvcontinue"}
            if len(page["links"]) >= self.limits["max_article_links"]:
                stop["links"] = "plcontinue"
            if len(page["categories"]) >= self.limits["max_categories"]:
                stop["categories"] = "clcontinue"
            for property_name, continuation_key in stop.items():
                if continuation_key in continuation and property_name != "revisions":
                    self.truncations[property_name] = self.truncations.get(property_name, 0) + 1
                if property_name in properties:
                    properties.remove(property_name)
                continuation.pop(continuation_key, None)
            params["prop"] = "|".join(properties)
            if set(continuation) <= {"continue"}:
                self.truncations["property_continuation"] = self.truncations.get("property_continuation", 0) + 1
                break
        else:
            if continuation:
                self.truncations["property_continuation"] = self.truncations.get("property_continuation", 0) + 1
        if page["namespace"] == 14 and self.limits["max_category_members"]:
            self._category_members(language, page)
        self.pages[(language, page["title"])] = page
        return page

    def _category_members(self, language, page):
        continuation = {}
        limit = self.limits["max_category_members"]
        for _ in range(self.limits["max_continuation_pages"]):
            result = self._request(language, {"action": "query", "list": "categorymembers",
                "cmtitle": page["title"], "cmnamespace": "0|14", "cmsort": "sortkey", "cmdir": "asc",
                "cmlimit": min(500, limit - len(page["category_members"])), **continuation})
            page["category_members"].extend(title_key(item["title"])
                for item in result.get("query", {}).get("categorymembers", []))
            continuation = result.get("continue", {})
            if not continuation:
                return
            if len(page["category_members"]) >= limit:
                break
        self.truncations["category_members"] = self.truncations.get("category_members", 0) + 1


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
