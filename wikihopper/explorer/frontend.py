"""Render the single-page explorer UI from the templates in ``web/``.

``web/index.html`` holds the markup; ``__STYLE__`` and ``__SCRIPT__`` are
replaced by ``web/style.css`` and the ``web/js`` files, concatenated in
``SCRIPTS`` order into one inline script that shares a global scope.
Other ``__NAME__`` placeholders are filled from the configuration per request.
"""

import json
import re
from pathlib import Path

from ..config import CONFIG, EDGE_WEIGHTS, FORWARD_COSTS, REVERSE_COSTS
from .queries import EDGE_TYPES

WEB_DIR = Path(__file__).with_name("web")
SCRIPTS = ("settings.js", "canvas.js", "shortest-path.js",
           "selected-path.js", "rmst.js", "controls.js")


def _read(name):
    return (WEB_DIR / name).read_text(encoding="utf-8")


INDEX_HTML = (_read("index.html")
              .replace("__STYLE__", _read("style.css"))
              .replace("__SCRIPT__", "".join(_read("js/" + name) for name in SCRIPTS)))


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
