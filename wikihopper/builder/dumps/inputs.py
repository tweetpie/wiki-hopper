"""Locate dated dump inputs and describe them for staging reuse checks."""

from datetime import datetime
from pathlib import Path
import re

SUFFIXES = ("pages-articles-multistream.xml.bz2",
            "pages-articles-multistream-index.txt.bz2", "page_props.sql.gz")


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


def input_identity(snapshot, inputs, max_pages):
    return {"snapshot": snapshot, "languages": list(inputs), "max_pages": max_pages,
            "sources": {code: {key: {"path": str(path.resolve()), "bytes": path.stat().st_size}
                               for key, path in paths.items()} for code, paths in inputs.items()}}
