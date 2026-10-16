# WikiHopper

WikiHopper builds multilingual Wikipedia graphs and explores paths through
categories, disambiguation pages, and articles in a local browser interface.
Each edition keeps its own categories and disambiguation pages as separate,
typed nodes; articles connect across editions only when they share a verified
Wikidata item. The example configuration enables English, Spanish, and Turkish.

With WikiHopper you can:

- build multilingual graphs with configurable, directional edge costs;
- find minimum-cost routes between articles under a route policy (allowed
  languages, allowed relationship types, and a guard against chaining
  ambiguity bridges);
- connect several articles at once (R-MST: pairwise routes joined by a
  minimum spanning tree, the usual 2-approximation of a Steiner tree);
- test an exact route you pick on the canvas;
- trace every relationship back to its evidence: the source wikitext line,
  the shared Wikidata item, or the rule that derived it.

## Contents

- [Setup](#setup)
- [Try a small graph](#try-a-small-graph)
- [Build a larger graph from dumps](#build-a-larger-graph-from-dumps)
- [How it works](#how-it-works)
- [Configuration](#configuration)
- [Repository layout](#repository-layout)

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

All commands run from the repository root through one entry point:

```sh
python -m wikihopper --help
```

| Command    | Component     | Purpose                                                        |
|------------|---------------|----------------------------------------------------------------|
| `download` | Dump handler  | Download dated, checksum-verified dumps for configured editions |
| `sample`   | Graph builder | Import a bounded graph from the MediaWiki API or a fixture      |
| `extract`  | Graph builder | Extract a graph from downloaded dumps                           |
| `explorer` | Explorer      | Build `graph.db` from the exports and serve the web interface   |

Add `--help` after any command to list its options. Optionally, `pip install -e .`
also installs a `wikihopper` console command (e.g. `wikihopper explorer`). Keep
the install editable: `wikihopper.toml` is read from the repository root.

## Try a small graph

Fetch a bounded sample using the public MediaWiki API, import it into SQLite,
and start the explorer:

```sh
python -m wikihopper sample --seed "en:Earth" --seed "es:Tierra" --seed "tr:Dünya" --max-pages 60 --depth 2 --max-category-members 2 --max-article-links 2 --max-categories 3 --max-api-requests 150 --output data/multilingual
python -m wikihopper explorer --out data/multilingual --db data/multilingual/graph.db --build-only
python -m wikihopper explorer --db data/multilingual/graph.db
```

Open <http://127.0.0.1:8000>. Search for articles, expand their neighborhoods,
and inspect relationship evidence. Use **Distance / shortest path** to find
routes, Ctrl-click nodes to test a selected route, or use **R-MST** to measure
connectivity among a list of articles. Route language and relationship filters,
the ambiguity guard, and time limits are available in the browser.

The sample is incomplete, so some routes may be missing. API requests are
paced; use `--user-agent` with your application name and contact information
for shared use. `--export-fixture` saves the fetched snapshots, and
`--offline-fixture` replays them without network access.

## Build a larger graph from dumps

The downloader uses the configured languages and the previous month's
first-of-month snapshot by default. Preview the plan before downloading:

```sh
python -m wikihopper download --dry-run
python -m wikihopper download
python -m wikihopper extract --check-only
python -m wikihopper extract
python -m wikihopper explorer --build-only
python -m wikihopper explorer
```

Full editions require substantial time and disk space. The extractor selects
the newest local snapshot. Use `--date YYYYMM01` to choose a specific snapshot.
For a smaller extraction, pass `--max-pages 1000 --out data/dump_sample`,
then pass matching `--out data/dump_sample --db data/dump_sample/graph.db`
arguments to the explorer. If an extraction is interrupted after its imports
completed, `extract --skip-import` rebuilds only the graph and exports.

## How it works

```
 Configuration ──► Dump handler ──► Graph builder ──► Explorer
 wikihopper.toml   download          sample / extract   explorer
 languages, costs  dated dumps,      typed pages and    graph.db, route search,
 search limits     MD5-verified      relations (CSV)    provenance, web UI
```

### Graph model

Nodes are pages of one edition: **articles**, **categories** (by namespace),
and **disambiguation pages** (by page property). Redirects are resolved within
an edition and are not nodes; hidden categories are excluded. Five relationship
types are stored:

| Relationship          | Direction                              | Source of evidence                          |
|-----------------------|----------------------------------------|---------------------------------------------|
| `HAS_CATEGORY`        | article / disambiguation → category    | explicit `[[Category:…]]` wikitext links     |
| `SUBCATEGORY_OF`      | category → parent category             | explicit category links on category pages   |
| `HAS_DISAMBIGUATION`  | article → disambiguation page          | wikilinks, or the primary-topic title rule  |
| `DISAMBIGUATES_TO`    | disambiguation page → listed article   | leading bullet links outside excluded sections |
| `LANGUAGE_EQUIVALENT` | article ↔ article in another edition   | the same verified Wikidata item             |

Categories and disambiguation pages are never merged across editions, even when
they have Wikidata items. Ordinary hyperlinks are not stored as edges.

### Costs and route search

Every stored edge can be traversed in either direction. A route costs the sum
of its directional step costs, read from `wikihopper.toml` at start-up (a cost
change needs a restart, not a rebuild). The defaults:

| Relationship                                | Forward | Reverse |
|---------------------------------------------|--------:|--------:|
| `HAS_CATEGORY`                              | 0       | 1       |
| `SUBCATEGORY_OF`                            | 0.5     | 0.5     |
| `HAS_DISAMBIGUATION`, `DISAMBIGUATES_TO`    | 0       | 0       |
| `LANGUAGE_EQUIVALENT`                       | 0.5     | 0.5     |

Search is a uniform-cost (Dijkstra) search over (page, ambiguity-state) pairs.
With **Prevent consecutive ambiguity bridges** on, a route that leaves a
disambiguation page for an article must traverse a category before entering
another disambiguation page; language switches do not reset this. Unless
**Full graph search** is selected, hub pruning and a node limit bound the
search; a time allowance always applies. A route found under a limit is an
upper bound, and a bounded search cannot prove that no route exists.

### Explorer HTTP API

The web interface uses these JSON endpoints, which can also be scripted:

| Endpoint                    | Purpose                                                 |
|-----------------------------|---------------------------------------------------------|
| `GET /api/dataset`          | loaded languages, node counts, and the build manifest   |
| `GET /api/search`           | title search (`q`, optional `language`)                 |
| `GET /api/neighbors`        | a node's capped neighborhood (`id`, `limit`, `types`, `dir`) |
| `GET /api/provenance`       | evidence for one edge (`src`, `dst`, `type`)            |
| `GET /api/path`             | shortest route (`src`, `dst`, `seconds`, `exhaustive`, route policy) |
| `POST /api/path/test`       | measure an ordered route (`node_ids`, `policy`)         |
| `POST /api/rmst/resolve`    | resolve a ranked article list for R-MST                 |
| `POST /api/rmst`            | compute the R-MST over the resolved articles            |

The R-MST endpoints are available when `explorer.rmst_enabled` is true.

## Configuration

The comments in [wikihopper.example.toml](wikihopper.example.toml) describe
the settings. To add an edition, add its code to `multilingual.languages` and
define a matching `[languages.<code>]` profile. Costs must be finite and
nonnegative. Restart running processes after configuration changes.

When source data changes, rebuild its exports and rebuild the explorer database
with `--build-only --rebuild`. Use a separate output directory for a new sample.
For optional password protection, set `WIKIHOPPER_PASSWORD` before starting
the explorer or supply `--password`.

Each builder writes `nodes.csv`, `edges.csv`, `redirects.csv`,
`provenance.jsonl`, and `manifest.json` to its output directory; the bounded
sample also writes `snapshots.json`. The explorer imports these into `graph.db`.

## Repository layout

```
wikihopper.example.toml       configuration template (copy to wikihopper.toml)
pyproject.toml                package metadata and the `wikihopper` console command
wikihopper/
├── __main__.py               `python -m wikihopper <command>` dispatcher
├── config.py                 load and validate wikihopper.toml; directional costs
├── policy.py                 route policy and the ambiguity-guard state machine
├── download.py               dump handler: dated, MD5-verified dump downloads
├── builder/                  graph builders
│   ├── wikitext.py           title normalization, disambiguation-entry parsing
│   ├── disambiguation.py     primary-topic title rule shared by both builders
│   ├── sample/               bounded import (`sample` command)
│   │   ├── sources.py        offline fixture and MediaWiki API page sources
│   │   ├── graph.py          capped BFS import and artifact export
│   │   └── cli.py
│   └── dumps/                full-dump extraction (`extract` command)
│       ├── inputs.py         locate dated dump files
│       ├── sql.py            streaming page_props SQL parser
│       ├── staging.py        page, property, and link import into SQLite staging
│       ├── graph.py          redirect resolution, edges, and artifact export
│       └── cli.py
└── explorer/                 explorer (`explorer` command)
    ├── database.py           CSV/JSONL → indexed graph.db
    ├── queries.py            search, node lookup, dataset info, neighborhoods
    ├── provenance.py         edge evidence and source wikitext snippets
    ├── routing.py            Dijkstra route search and selected-route tests
    ├── rmst.py               R-MST over ranked article lists
    ├── graph_api.py          query functions the R-MST evaluator calls
    ├── server.py             HTTP handler and JSON endpoints
    ├── frontend.py           renders the web page from web/
    ├── web/                  index.html, style.css, js/*.js (one inline page)
    └── cli.py
```
