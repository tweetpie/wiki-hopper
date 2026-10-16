"""Title normalization and disambiguation-entry parsing shared by the builders."""

import html
import re
import urllib.parse


_HEADING = re.compile(r"^(=+)\s*(.*?)\s*\1\s*$")
_LEADING_LINK = re.compile(r"^\[\[\s*([^\[\]|#]+)")
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
