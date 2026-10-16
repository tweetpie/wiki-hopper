"""Page sources for the bounded importer: offline fixtures and the MediaWiki API."""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

from ..wikitext import title_key


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
