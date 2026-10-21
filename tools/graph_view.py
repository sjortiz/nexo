"""Standalone entry point for the graph page; same as `nexo visualize`.

uv run python tools/graph_view.py [--out graph.html] [--open]
"""

import sys

from nexo.cli import main

if __name__ == "__main__":
    sys.exit(main(["visualize", *sys.argv[1:]]))
