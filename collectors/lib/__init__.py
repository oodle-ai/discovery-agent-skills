"""Shared library for discovery collectors.

Collectors are standalone PEP 723 scripts (run via `uv run`). They import this
package by inserting the `collectors/` directory into sys.path:

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from lib import ...
"""
