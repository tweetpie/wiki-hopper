# WikiHopper

WikiHopper builds multilingual Wikipedia graphs and explores paths through
categories, disambiguation pages, and articles in a local browser interface.
The example enables English, Spanish, and Turkish. Articles connect across
editions when they share a verified Wikidata item.

## Setup

Install **Python 3.11+**, download or clone this repository, and open a terminal
in its directory. No additional Python packages are needed. Internet access is
needed for imports/downloads and for the browser's vis-network library.

Create your local configuration in PowerShell:

```powershell
Copy-Item wikihopper.example.toml wikihopper.toml
```

Or in a POSIX shell:

```sh
cp wikihopper.example.toml wikihopper.toml
```

Edit `wikihopper.toml` to choose languages, paths, traversal costs, and explorer
settings. Relative paths resolve beside the configuration file. Your local
configuration and generated data are ignored by Git.

## Try a small graph

Fetch a bounded sample using the public MediaWiki API, import it into SQLite,
and start the explorer:

```sh
python wikihopper_multilingual.py --seed "en:Earth" --seed "es:Tierra" --seed "tr:Dünya" --max-pages 60 --depth 2 --max-category-members 2 --max-article-links 2 --max-categories 3 --max-api-requests 150 --output data/multilingual
python explorer.py --out data/multilingual --db data/multilingual/graph.db --build-only
python explorer.py --db data/multilingual/graph.db
```

Open <http://127.0.0.1:8000>. Search for articles, expand their neighborhoods,
and inspect relationship evidence. Use **Distance / shortest path** to find
routes, Shift-click nodes to test a selected route, or use **R-MST** to measure
connectivity among a list of articles. Route language and relationship filters,
the ambiguity guard, and time limits are available in the browser.

The sample is incomplete, so some routes may be missing. API requests are
paced; use `--user-agent` with your application name and contact information
for shared use. Each script's `--help` lists its options.

## Build a larger graph from dumps

The downloader uses the configured languages and the previous month's
first-of-month snapshot by default. Preview the plan before downloading:

```sh
python download_dumps.py --dry-run
python download_dumps.py
python wikihopper_dumps.py --check-only
python wikihopper_dumps.py
python explorer.py --build-only
python explorer.py
```

Full editions require substantial time and disk space. The extractor selects
the newest local snapshot. Use `--date YYYYMM01` to choose a specific snapshot.
For a smaller extraction, pass `--max-pages 1000 --out data/dump_sample`,
then pass matching `--out data/dump_sample --db data/dump_sample/graph.db`
arguments to the explorer.

## Configuration

The comments in [wikihopper.example.toml](wikihopper.example.toml) describe
the settings. To add an edition, add its code to `multilingual.languages` and
define a matching `[languages.<code>]` profile. Costs must be finite and
nonnegative. Restart running processes after configuration changes.

When source data changes, rebuild its exports and rebuild the explorer database
with `--build-only --rebuild`. Use a separate output directory for a new sample.
For optional password protection, set `WIKIHOPPER_PASSWORD` before starting
the explorer or supply `--password`.
