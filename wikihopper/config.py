"""Shared configuration, loaded once per process (Python 3.11+).

The configuration file is ``wikihopper.toml`` in the repository root, beside
this package. It is read at import time; a missing or invalid file is an error.
"""

from pathlib import Path
import math
import re
import tomllib

CONFIG_PATH = Path(__file__).resolve().parent.parent / "wikihopper.toml"
EDGE_NAMES = ("HAS_CATEGORY", "SUBCATEGORY_OF", "HAS_DISAMBIGUATION",
              "DISAMBIGUATES_TO", "LANGUAGE_EQUIVALENT")
COST_DIRECTIONS = {
    "HAS_CATEGORY": ("forward", "reverse"),
    "SUBCATEGORY_OF": ("forward", "reverse"),
    "DISAMBIGUATION": ("article_to_disambiguation", "disambiguation_to_article"),
    "LANGUAGE_EQUIVALENT": ("forward", "reverse"),
}

DEFAULT_LANGUAGES = {
    "en": {"name": "English", "wiki": "enwiki", "disambiguation_suffixes": [" (disambiguation)"], "excluded_disambiguation_sections":
           ["See also", "References", "External links", "Further reading", "Notes", "Sources", "Bibliography"]},
    "es": {"name": "Español", "wiki": "eswiki", "disambiguation_suffixes": [" (desambiguación)"], "excluded_disambiguation_sections":
           ["Véase también", "Referencias", "Enlaces externos", "Bibliografía", "Notas"]},
    "tr": {"name": "Türkçe", "wiki": "trwiki", "disambiguation_suffixes": [" (anlam ayrımı)"], "excluded_disambiguation_sections":
           ["Ayrıca bakınız", "Kaynakça", "Dış bağlantılar", "Notlar"],
           "case_replacements": {"I": "ı", "İ": "i"}},
}


def parse_search_seconds(value):
    """Nonnegative finite seconds, with zero meaning no time limit."""
    message = "Max time must be finite seconds >= 0 (0 means no time limit)"
    if isinstance(value, bool):
        raise ValueError(message)
    try:
        seconds = float(value)
    except (ValueError, TypeError, OverflowError):
        raise ValueError(message) from None
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(message)
    return seconds


def load_config(path=CONFIG_PATH):
    """Read a complete config; reject unknown keys and invalid values."""
    path = Path(path).resolve()
    try:
        with path.open("rb") as stream:
            config = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"Cannot read WikiHopper config {path}: {exc}") from exc

    # Older English-only configurations remain valid. Explicit language tables
    # replace these defaults, so additional editions need no code changes.
    config.setdefault("languages", {code: {**profile,
        "excluded_disambiguation_sections": list(profile["excluded_disambiguation_sections"]),
        "case_replacements": dict(profile.get("case_replacements", {}))}
        for code, profile in DEFAULT_LANGUAGES.items()})
    config.setdefault("multilingual", {"base_language": "en", "languages": ["en", "es", "tr"]})
    if isinstance(config.get("costs"), dict):
        config["costs"].setdefault("LANGUAGE_EQUIVALENT", {"forward": 0.5, "reverse": 0.5})
    if isinstance(config.get("explorer"), dict):
        config["explorer"].setdefault("rmst_enabled", False)
        config["explorer"].setdefault("ambiguity_guard", True)

    def keys(table, expected, name):
        if not isinstance(table, dict) or set(table) != set(expected):
            raise ValueError(f"{path}: {name} must contain exactly: {', '.join(expected)}")

    def number(value, name, minimum=0, maximum=math.inf, integer=False):
        types = (int,) if integer else (int, float)
        if (type(value) not in types or not math.isfinite(value)
                or not minimum <= value <= maximum):
            raise ValueError(f"{path}: {name} must be a finite "
                             f"{'integer' if integer else 'number'} "
                             f"between {minimum} and {maximum}")

    schema = {
        "paths": ("output", "database", "xml", "index"),
        "costs": COST_DIRECTIONS,
        "explorer": ("host", "port", "low_memory", "rmst_enabled", "ambiguity_guard"),
        "search": ("hub_max", "settle_cap", "time_budget"),
        "search_low_memory": ("hub_max", "settle_cap", "time_budget"),
        "multilingual": ("base_language", "languages"),
    }
    # Accept existing local configs with settings for the retired English builder.
    if "builder" in config:
        schema["builder"] = ("workers", "sample", "shard_directory")
    keys(config, (*schema, "languages"), "sections")
    for section, expected in schema.items():
        keys(config[section], expected, section)
    for edge, costs in config["costs"].items():
        keys(costs, COST_DIRECTIONS[edge], f"costs.{edge}")
        for direction, value in costs.items():
            number(value, f"costs.{edge}.{direction}")
    languages = config["languages"]
    if not isinstance(languages, dict) or not languages:
        raise ValueError(f"{path}: languages must contain at least one language profile")
    wiki_ids = set()
    for code, profile in languages.items():
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,29}", code):
            raise ValueError(f"{path}: invalid Wikipedia language code {code!r}")
        if not isinstance(profile, dict) or not {"name", "wiki"} <= set(profile) or set(profile) - {
                "name", "wiki", "excluded_disambiguation_sections", "case_replacements", "disambiguation_suffixes"}:
            raise ValueError(f"{path}: languages.{code} requires name and wiki; "
                             "excluded_disambiguation_sections, case_replacements and disambiguation_suffixes are optional")
        if not isinstance(profile["name"], str) or not profile["name"].strip():
            raise ValueError(f"{path}: languages.{code}.name must be a nonempty string")
        if not isinstance(profile["wiki"], str) or not re.fullmatch(r"[a-z][a-z0-9_-]*wiki", profile["wiki"]):
            raise ValueError(f"{path}: languages.{code}.wiki must be a Wikimedia site ID such as eswiki")
        if profile["wiki"] in wiki_ids:
            raise ValueError(f"{path}: language profiles must identify distinct Wikipedia editions")
        wiki_ids.add(profile["wiki"])
        excluded = profile.setdefault("excluded_disambiguation_sections", [])
        if not isinstance(excluded, list) or any(not isinstance(value, str) or not value.strip() for value in excluded):
            raise ValueError(f"{path}: languages.{code}.excluded_disambiguation_sections must be a list of nonempty strings")
        suffixes = profile.setdefault("disambiguation_suffixes",
            list(DEFAULT_LANGUAGES.get(code, {}).get("disambiguation_suffixes", [])))
        if not isinstance(suffixes, list) or any(
                not isinstance(value, str) or not value.strip() for value in suffixes):
            raise ValueError(f"{path}: languages.{code}.disambiguation_suffixes must be a list of nonempty strings")
        replacements = profile.setdefault("case_replacements", {})
        if not isinstance(replacements, dict) or any(
                not isinstance(key, str) or len(key) != 1 or not isinstance(value, str) or not value
                for key, value in replacements.items()):
            raise ValueError(f"{path}: languages.{code}.case_replacements must map single characters to nonempty strings")
    active = config["multilingual"]["languages"]
    if not isinstance(active, list) or not active or any(not isinstance(code, str) for code in active):
        raise ValueError(f"{path}: multilingual.languages must be a nonempty list of language codes")
    if len(active) != len(set(active)) or any(code not in languages for code in active):
        raise ValueError(f"{path}: multilingual.languages must contain unique configured language codes")
    if not isinstance(config["multilingual"]["base_language"], str) or config["multilingual"]["base_language"] not in active:
        raise ValueError(f"{path}: multilingual.base_language must be enabled in multilingual.languages")
    for name, value in config["paths"].items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{path}: paths.{name} must be a nonempty path string")
        config["paths"][name] = str((path.parent / value).resolve())
    if "builder" in config:
        for name in ("workers", "sample"):
            number(config["builder"][name], f"builder.{name}", integer=True)
        shard = config["builder"]["shard_directory"]
        if not isinstance(shard, str):
            raise ValueError(f"{path}: builder.shard_directory must be a string")
        config["builder"]["shard_directory"] = str((path.parent / shard).resolve()) if shard else None
    explorer = config["explorer"]
    if not isinstance(explorer["host"], str) or not explorer["host"].strip():
        raise ValueError(f"{path}: explorer.host must be a nonempty string")
    for name in ("low_memory", "rmst_enabled", "ambiguity_guard"):
        if type(explorer[name]) is not bool:
            raise ValueError(f"{path}: explorer.{name} must be true or false")
    number(explorer["port"], "explorer.port", 1, 65535, integer=True)
    for section in ("search", "search_low_memory"):
        number(config[section]["hub_max"], f"{section}.hub_max", 100, 200000, integer=True)
        number(config[section]["settle_cap"], f"{section}.settle_cap", 1, integer=True)
        number(config[section]["time_budget"], f"{section}.time_budget", 0)
    return config


CONFIG = load_config()


def search_key(title, language="en"):
    """Normalize search text using a language profile; never change identity."""
    replacements = CONFIG["languages"].get(language, {}).get("case_replacements", {})
    return title.translate(str.maketrans(replacements)).lower()


# Keep the stored edge types, but derive both from the same traversal policy.
# HAS_DISAMBIGUATION is article -> dab; DISAMBIGUATES_TO is dab -> article.
_dab_costs = CONFIG["costs"]["DISAMBIGUATION"]
_cost_pairs = {name: (CONFIG["costs"][name]["forward"], CONFIG["costs"][name]["reverse"])
               for name in ("HAS_CATEGORY", "SUBCATEGORY_OF", "LANGUAGE_EQUIVALENT")}
_cost_pairs["HAS_DISAMBIGUATION"] = (_dab_costs["article_to_disambiguation"],
                                      _dab_costs["disambiguation_to_article"])
_cost_pairs["DISAMBIGUATES_TO"] = (_dab_costs["disambiguation_to_article"],
                                   _dab_costs["article_to_disambiguation"])
FORWARD_COSTS = {name: float(cost[0]) for name, cost in _cost_pairs.items()}
REVERSE_COSTS = {name: float(cost[1]) for name, cost in _cost_pairs.items()}
# A single exported/display weight summarizes the two directional costs.
EDGE_WEIGHTS = {name: max(FORWARD_COSTS[name], REVERSE_COSTS[name]) for name in EDGE_NAMES}
