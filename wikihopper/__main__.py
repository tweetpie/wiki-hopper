"""Command-line entry point: ``python -m wikihopper <command> [options]``."""

import importlib
import sys

# Modules are imported only when their command runs; each reads the
# configuration at import time.
COMMANDS = {
    "download": ("wikihopper.download", "run",
                 "Download dated Wikipedia dumps for the configured languages"),
    "sample": ("wikihopper.builder.sample.cli", "main",
               "Build a bounded graph from the MediaWiki API or an offline fixture"),
    "extract": ("wikihopper.builder.dumps.cli", "run",
                "Extract a graph from downloaded dumps"),
    "explorer": ("wikihopper.explorer.cli", "main",
                 "Build graph.db from the exports and serve the web explorer"),
}


def usage():
    lines = ["usage: python -m wikihopper <command> [options]", "", "commands:"]
    lines += [f"  {name:<9} {summary}" for name, (_, _, summary) in COMMANDS.items()]
    lines += ["", "Run 'python -m wikihopper <command> --help' for a command's options."]
    return "\n".join(lines)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(usage())
        return 0
    if argv[0] not in COMMANDS:
        print(f"Unknown command: {argv[0]}\n\n{usage()}", file=sys.stderr)
        return 2
    module, function, _ = COMMANDS[argv[0]]
    # Commands parse sys.argv themselves; name the program for their --help.
    sys.argv = [f"wikihopper {argv[0]}", *argv[1:]]
    getattr(importlib.import_module(module), function)()
    return 0


if __name__ == "__main__":
    sys.exit(main())
