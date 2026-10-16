"""Download dated Wikipedia inputs for the configured WikiHopper languages.

Python 3.11+, standard library only. Nothing downloads on import.
"""

import argparse
from datetime import date, timedelta
import hashlib
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from wikihopper_config import CONFIG_PATH, load_config


SUFFIXES = (
    "pages-articles-multistream.xml.bz2",
    "pages-articles-multistream-index.txt.bz2",
    "page_props.sql.gz",
)
BASE_URL = "https://dumps.wikimedia.org"
USER_AGENT = "WikiHopper-dump-downloader/1.0"
CHUNK_SIZE = 1024 * 1024


def previous_month(today=None):
    today = today or date.today()
    return (today.replace(day=1) - timedelta(days=1)).replace(day=1).strftime("%Y%m%d")


def dump_date(value):
    try:
        parsed = date(int(value[:4]), int(value[4:6]), int(value[6:]))
    except ValueError:
        raise argparse.ArgumentTypeError("Date must be YYYYMM01") from None
    if len(value) != 8 or not value.isdigit() or parsed.day != 1:
        raise argparse.ArgumentTypeError("Date must be YYYYMM01")
    return value


def open_url(url):
    return urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=120)


def checksum(path):
    digest = hashlib.md5()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def download(url, destination, expected):
    if destination.exists() and checksum(destination) == expected:
        print(f"Already verified: {destination.name}", flush=True)
        return
    partial = destination.with_name(destination.name + ".part")
    print(f"Downloading: {url}", flush=True)
    for attempt in range(1, 4):
        try:
            digest = hashlib.md5()
            received = 0
            with open_url(url) as response, partial.open("wb") as stream:
                length = response.headers.get("Content-Length")
                total = int(length) if length else None
                last_report = time.monotonic()
                while chunk := response.read(CHUNK_SIZE):
                    stream.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if time.monotonic() - last_report >= 10:
                        progress = f" / {total / CHUNK_SIZE:,.0f} MiB" if total else ""
                        print(f"  {received / CHUNK_SIZE:,.0f} MiB{progress}", flush=True)
                        last_report = time.monotonic()
            if total is not None and received != total:
                raise OSError(f"Incomplete transfer: {received} of {total} bytes")
            if digest.hexdigest() != expected:
                raise OSError("Checksum mismatch")
            partial.replace(destination)
            print(f"Verified: {destination.name}", flush=True)
            return
        except (OSError, URLError) as exc:
            if isinstance(exc, HTTPError) and 400 <= exc.code < 500 and exc.code != 429:
                raise
            if attempt == 3:
                raise
            print(f"  Attempt {attempt} failed: {exc}. Restarting in {attempt * 5}s.", flush=True)
            time.sleep(attempt * 5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=CONFIG_PATH,
                        help="WikiHopper TOML configuration")
    parser.add_argument("--date", type=dump_date,
                        help="Override the previous-month snapshot (YYYYMM01)")
    parser.add_argument("--output", type=Path,
                        help="Download root (default: configured output/dumps)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print URLs and destinations without network access or writes")
    args = parser.parse_args(argv)
    config = load_config(args.config)
    snapshot = args.date or previous_month()
    root = args.output or Path(config["paths"]["output"]) / "dumps"
    failures = []
    print(f"Snapshot: {snapshot}; languages: {', '.join(config['multilingual']['languages'])}", flush=True)
    for language in config["multilingual"]["languages"]:
        wiki = config["languages"][language]["wiki"]
        base = f"{BASE_URL}/{wiki}/{snapshot}"
        directory = root / snapshot / wiki
        filenames = [f"{wiki}-{snapshot}-{suffix}" for suffix in SUFFIXES]
        if args.dry_run:
            for filename in filenames:
                print(f"{base}/{filename}\n  -> {directory / filename}")
            continue
        try:
            # Require published checksums for this exact dated snapshot.
            manifest_name = f"{wiki}-{snapshot}-md5sums.txt"
            with open_url(f"{base}/{manifest_name}") as response:
                manifest = response.read().decode("utf-8")
            sums = {}
            for line in manifest.splitlines():
                fields = line.split(maxsplit=1)
                if len(fields) == 2:
                    digest, filename = fields
                    if len(digest) == 32 and all(c in "0123456789abcdefABCDEF" for c in digest):
                        sums[filename.lstrip("*")] = digest.lower()
            missing = [filename for filename in filenames if filename not in sums]
            if missing:
                raise ValueError(f"Snapshot has no published checksums for: {', '.join(missing)}")
            directory.mkdir(parents=True, exist_ok=True)
            (directory / manifest_name).write_text(manifest, encoding="utf-8")
        except (OSError, URLError, ValueError) as exc:
            failures.append(f"{wiki}: {exc}")
            continue
        for filename in filenames:
            try:
                download(f"{base}/{filename}", directory / filename, sums[filename])
            except (OSError, URLError, ValueError) as exc:
                failures.append(f"{wiki}/{filename}: {exc}")
    if failures:
        print("Some downloads failed:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print("Rerun to retry. Verified files will be skipped; partial files restart.", file=sys.stderr)
        return 1
    print("Download plan printed." if args.dry_run else "All dump files verified.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nDownload interrupted. Rerun to retry; partial files restart.", file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError) as exc:
        print(f"Configuration or setup error: {exc}", file=sys.stderr)
        sys.exit(1)
