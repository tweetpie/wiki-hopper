"""WikiHopper: ambiguity-aware path finding over multilingual Wikipedia graphs.

Components (see README.md):

* ``wikihopper.config``    -- configuration loaded from ``wikihopper.toml``
* ``wikihopper.download``  -- dump handler for dated, checksummed Wikimedia dumps
* ``wikihopper.builder``   -- graph builders (bounded API sample, full dumps)
* ``wikihopper.explorer``  -- SQLite graph database, route search and web UI

Run ``python -m wikihopper --help`` for the command-line interface.
"""

__version__ = "0.1.0"
