"""Provenance: read the source wikitext line an edge was extracted from."""

import bz2
import html
import json
import os
import re

from .queries import _columns, q_node

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
