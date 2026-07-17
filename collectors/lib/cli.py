"""Shared CLI conventions for collectors."""

from __future__ import annotations

import argparse
import os
import re
from getpass import getpass
from pathlib import Path


def base_parser(description: str, default_lookback: str = "30d") -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description=description,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory for evidence/, manifest.json and summary.json",
    )
    p.add_argument(
        "--report-only",
        action="store_true",
        help="Recompute summary.json from previously saved evidence; no API calls",
    )
    p.add_argument("--tar", action="store_true", help="Compress the output dir to .tar.gz")
    p.add_argument(
        "--lookback",
        default=default_lookback,
        help="Time window for volume figures (e.g. 7d, 30d)",
    )
    p.add_argument(
        "--insecure",
        action="store_true",
        help="Skip TLS certificate verification",
    )
    p.add_argument("--timeout", type=float, default=60.0, help="Per-request timeout (seconds)")
    p.add_argument(
        "--header",
        action="append",
        default=[],
        metavar="'Name: value'",
        help="Extra HTTP header (repeatable)",
    )
    return p


def parse_duration_s(s: str) -> int:
    """Parse 7d / 24h / 90m / 30s into seconds."""
    m = re.fullmatch(r"(\d+)([dhms])", s.strip().lower())
    if not m:
        raise ValueError(f"invalid duration: {s!r}")
    return int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def parse_duration_days(s: str) -> float:
    return parse_duration_s(s) / 86400.0


def parse_headers(raw: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for h in raw:
        name, _, value = h.partition(":")
        if not value:
            raise ValueError(f"invalid header (expected 'Name: value'): {h!r}")
        out[name.strip()] = value.strip()
    return out


def parse_go_duration_s(s: str) -> int | None:
    """Parse Go-style duration ('720h', '30d', '2h30m0s') into seconds."""
    s = s.strip()
    if not s:
        return None
    total = 0
    for m in re.finditer(r"(\d+)([dhms])", s):
        total += int(m.group(1)) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[m.group(2)]
    return total if total > 0 else None


def credential(
    flag_value: str | None,
    env_var: str,
    prompt: str,
    interactive_ok: bool = True,
) -> str | None:
    """Resolve a credential: CLI flag > env var > interactive prompt (TTY only)."""
    if flag_value:
        return flag_value
    if os.environ.get(env_var):
        return os.environ[env_var]
    if interactive_ok and os.isatty(0):
        value = getpass(f"{prompt} (or set {env_var}): ")
        return value or None
    return None
