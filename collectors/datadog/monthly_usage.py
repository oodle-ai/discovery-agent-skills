# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Datadog monthly usage-by-SKU export (for pricing / usage reviews).

Pulls the last N full calendar months (default 6) of Datadog usage from the
same v2 hourly-usage API the discovery collector uses, then buckets every
product_family / usage_type ("SKU") measurement by calendar month and writes
a CSV with one row per SKU and one column per month.

Outputs two artifacts into its OWN output dir (keep it separate from the
discovery collector's dir so the two summary.json files don't collide):
  - datadog_monthly_usage_by_sku.csv    — the standalone CSV
  - summary.json                        — the SKU matrix under
      inventory.monthly_usage_by_sku, which report/generate_report.py renders
      as a "Last N Months Usage by SKU" section (it carries no scalar figures)
Raw API responses are written to evidence/ (redacted) and, with --tar, packed
into the output tarball, so every figure is re-derivable offline.

Examples:
    DD_API_KEY=... DD_APP_KEY=... \\
    uv run collectors/datadog/monthly_usage.py --site us5 \\
        --output-dir ./discovery-output/datadog-monthly --tar

    # re-derive the CSV + summary from previously saved evidence, no API calls:
    uv run collectors/datadog/monthly_usage.py --report-only \\
        --output-dir ./discovery-output/datadog-monthly

Aggregation per usage_type:
    *_bytes            -> summed over the month, reported in GB (bytes / 1e9)
    gauge counts       -> averaged over the month (host_count, container_count,
                          num_custom_timeseries) — these are concurrent counts,
                          not per-hour increments, so a sum would be meaningless
    everything else    -> summed over the month (events, sessions, spans, ...)
The unit and aggregation used are written into the CSV so every number is
self-documenting and matches Datadog's Plan & Usage page for the same window.

SKU rows with zero/no usage in every month (e.g. host counts for clouds you
don't run) are dropped by default from both artifacts so the report shows only
SKUs you actually use; pass --include-empty to keep the full matrix.
"""

from __future__ import annotations

import csv
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1]))  # collectors/  (for lib.*)

# The sibling discovery collector supplies the fetch machinery and constants
# verbatim (paginated v2 hourly usage, chunking, evidence naming) so the two
# stay in lockstep. Load it under a unique module name (not the bare `collect`)
# so it never collides with another collector's collect.py in one process.
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("datadog_collect_sibling", _HERE / "collect.py")
dd = _ilu.module_from_spec(_spec)
sys.modules["datadog_collect_sibling"] = dd
_spec.loader.exec_module(dd)

from lib.auth import datadog_headers  # noqa: E402
from lib.cli import base_parser, credential, parse_headers  # noqa: E402
from lib.evidence import EvidenceWriter  # noqa: E402
from lib.http import HttpClient  # noqa: E402
from lib.summary import SummaryWriter  # noqa: E402

COLLECTOR = "datadog_monthly"
VERSION = "1.0.0"

MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# usage_types that are point-in-time (concurrent) counts, not per-hour
# increments: averaged over the month rather than summed.
GAUGE_USAGE_TYPES = {
    "host_count",
    "container_count",
    "num_custom_timeseries",
    "apm_host_count",
    "avg_apm_host_count",
    "fargate_container_count",
    "npm_host_count",
}

_GAUGE_UNIT = {
    "host_count": "hosts (avg)",
    "container_count": "containers (avg)",
    "num_custom_timeseries": "custom metrics (avg)",
}


def month_keys(now: datetime, n: int) -> list[str]:
    """The n most recent FULL calendar months as 'YYYY-MM', oldest first."""
    y, m = now.year, now.month  # current (partial) month is excluded
    keys: list[str] = []
    for _ in range(n):
        m -= 1
        if m == 0:
            m, y = 12, y - 1
        keys.append(f"{y:04d}-{m:02d}")
    keys.reverse()
    return keys


def month_label(key: str) -> str:
    y, m = key.split("-")
    return f"{MONTH_ABBR[int(m)]} {y}"


def classify(usage_type: str) -> tuple[str, str]:
    """(unit, aggregation) for a usage_type. aggregation is 'sum' or 'avg'."""
    if usage_type.endswith("_bytes"):
        return "GB", "sum"
    if usage_type in GAUGE_USAGE_TYPES:
        return _GAUGE_UNIT.get(usage_type, "count (avg)"), "avg"
    return "count", "sum"


def aggregate_monthly(
    results: dict[str, Any], families: list[str], months: list[str]
) -> list[dict[str, Any]]:
    """Bucket hourly usage_type measurements into monthly per-SKU rows.

    Pure function (no I/O) so it is unit-testable. Returns one row per
    (product_family, usage_type) that has at least one measurement in-window.
    """
    buckets: dict[tuple[str, str], dict[str, list[float]]] = {}
    for fam in families:
        data = results.get(f"usage_hourly_{fam}")
        if not data:
            continue
        for entry in data.get("data", []):
            attrs = entry.get("attributes", {})
            month = str(attrs.get("timestamp", ""))[:7]
            if month not in months:
                continue
            for meas in attrs.get("measurements", []):
                ut = meas.get("usage_type")
                val = meas.get("value")
                if ut is None or val is None:
                    continue
                buckets.setdefault((fam, ut), {}).setdefault(month, []).append(float(val))

    fam_order = {f: i for i, f in enumerate(families)}
    rows: list[dict[str, Any]] = []
    for (fam, ut), by_month in buckets.items():
        unit, agg = classify(ut)
        values: dict[str, float | None] = {}
        for month in months:
            samples = by_month.get(month)
            if not samples:
                values[month] = None
                continue
            v = sum(samples) / len(samples) if agg == "avg" else sum(samples)
            if unit == "GB":
                v = v / 1e9
            values[month] = v
        rows.append(
            {"product_family": fam, "usage_type": ut, "unit": unit,
             "aggregation": agg, "values": values}
        )
    rows.sort(key=lambda r: (fam_order.get(r["product_family"], 99), r["usage_type"]))
    return rows


def has_usage(row: dict[str, Any]) -> bool:
    """True if the SKU has any nonzero measurement across the window.

    Rows that are all-None or all-zero (e.g. `aws_host_count` for an account
    that runs nothing on AWS) are pure noise in the report, so they are dropped
    by default. `None` (no data that month) and exact `0` both count as "no
    usage"; a tiny-but-nonzero value is kept.
    """
    return any(v not in (None, 0) for v in row["values"].values())


def drop_empty_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if has_usage(r)]


def _fmt(value: float | None, unit: str, agg: str) -> str:
    if value is None:
        return ""
    if unit == "GB":
        return f"{value:.3f}"
    if agg == "avg":
        return f"{value:.1f}"
    return f"{value:.0f}" if abs(value - round(value)) < 1e-6 else f"{value:.2f}"


def write_csv(
    output_dir: Path, rows: list[dict[str, Any]], months: list[str]
) -> Path:
    path = output_dir / "datadog_monthly_usage_by_sku.csv"
    labels = [month_label(m) for m in months]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["product_family", "usage_type", "unit", "aggregation"] + labels)
        for r in rows:
            w.writerow(
                [r["product_family"], r["usage_type"], r["unit"], r["aggregation"]]
                + [_fmt(r["values"][m], r["unit"], r["aggregation"]) for m in months]
            )
    return path


def inventory_rows(
    rows: list[dict[str, Any]], months: list[str]
) -> list[dict[str, Any]]:
    """The SKU matrix as report-ready inventory: one dict per SKU with a
    display string per month label (empty for months with no data). Values are
    formatted identically to the CSV cells so the report and CSV stay in
    lockstep — the report generator renders these verbatim, never recomputes.
    """
    labels = [month_label(m) for m in months]
    out: list[dict[str, Any]] = []
    for r in rows:
        d: dict[str, Any] = {
            "product_family": r["product_family"],
            "usage_type": r["usage_type"],
            "unit": r["unit"],
            "aggregation": r["aggregation"],
        }
        for m, lab in zip(months, labels, strict=True):
            d[lab] = _fmt(r["values"][m], r["unit"], r["aggregation"])
        out.append(d)
    return out


def write_summary(
    output_dir: Path,
    rows: list[dict[str, Any]],
    months: list[str],
    domain: str,
    args_redacted: dict[str, Any],
) -> Path:
    """Emit a summary.json carrying the SKU matrix in `inventory` (no scalar
    figures). report/generate_report.py discovers it via the per-collector
    summary glob and renders the monthly-usage section from it."""
    summary = SummaryWriter(
        collector=COLLECTOR,
        collector_version=VERSION,
        expected=[],  # this collector reports a matrix, not scalar figures
        target=f"https://api.{domain}",
        lookback=f"{len(months)} full calendar months ({months[0]}..{months[-1]})",
        args_redacted=args_redacted,
    )
    summary.environment = {
        "detected_backend": "datadog",
        "version": None,
        "detection_method": "site flag / API reachability",
        "site": domain,
    }
    summary.inventory["monthly_usage_months"] = [month_label(m) for m in months]
    summary.inventory["monthly_usage_by_sku"] = inventory_rows(rows, months)
    summary.inventory["monthly_usage_note"] = (
        "*_bytes SKUs are summed and reported in GB; concurrent-count gauges "
        "(hosts, containers, custom metrics) are averaged; everything else is "
        "summed. Matches Datadog's Plan & Usage page for the same window."
    )
    return summary.write(output_dir)


def main() -> int:
    parser = base_parser("Datadog monthly usage-by-SKU export", default_lookback="180d")
    parser.add_argument("--api-key", help="Datadog API key (or env DD_API_KEY)")
    parser.add_argument("--app-key", help="Datadog application key (or env DD_APP_KEY)")
    parser.add_argument(
        "--site", default="us1",
        help=f"Datadog site: {', '.join(dd.DD_SITES)} or a full domain",
    )
    parser.add_argument(
        "--months", type=int, default=6,
        help="Number of full calendar months back to report",
    )
    parser.add_argument(
        "--include-empty", action="store_true",
        help="Keep SKU rows with zero/no usage in every month "
             "(default: drop them so the report only shows SKUs you actually use)",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ev = EvidenceWriter(args.output_dir)
    results: dict[str, Any] = {}
    families = dd.HOURLY_FAMILIES
    now = datetime.now(UTC)
    months = month_keys(now, args.months)
    domain = dd.resolve_site(args.site)  # pure; needed by both paths for summary.json

    if args.report_only:
        results = ev.load_all()
        if not results:
            print(f"ERROR: --report-only but no evidence under {ev.evidence_dir}")
            return 2
    else:
        api_key = credential(args.api_key, "DD_API_KEY", "Datadog API key")
        app_key = credential(args.app_key, "DD_APP_KEY", "Datadog application key")
        if not api_key or not app_key:
            print(
                "ERROR: missing credentials. Pass --api-key/--app-key or set "
                "DD_API_KEY / DD_APP_KEY.\n"
                "Keys: https://app.datadoghq.com/organization-settings/api-keys"
            )
            return 2
        headers = datadog_headers(api_key, app_key)
        headers.update(parse_headers(args.header))
        start = datetime(int(months[0][:4]), int(months[0][5:7]), 1, tzinfo=UTC)
        end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        print(f"Datadog monthly usage: {month_label(months[0])} .. {month_label(months[-1])} "
              f"({len(months)} months) from https://api.{domain}")
        with HttpClient(
            f"https://api.{domain}", headers=headers,
            timeout_s=args.timeout, verify=not args.insecure,
        ) as client:
            dd.fetch_hourly_usage(client, families, start, end, ev, results)

    all_rows = aggregate_monthly(results, families, months)
    rows = all_rows if args.include_empty else drop_empty_rows(all_rows)
    dropped = len(all_rows) - len(rows)
    csv_path = write_csv(args.output_dir, rows, months)
    summary_path = write_summary(
        args.output_dir, rows, months, domain,
        args_redacted={
            "site": args.site, "months": args.months,
            "include_empty": args.include_empty,
        },
    )
    ev.finalize()
    if args.tar:
        ev.tar()

    print(f"\nWrote {csv_path}  ({len(rows)} SKU rows x {len(months)} months)")
    print(f"Wrote {summary_path}  (SKU matrix in inventory.monthly_usage_by_sku)")
    if dropped:
        print(f"Omitted {dropped} all-zero SKU row(s); pass --include-empty to keep them.")
    if not rows:
        print("No usage rows produced. Check credentials/site, or the account "
              "may have no usage in these product families.")
    else:
        for r in rows:
            latest = _fmt(r["values"][months[-1]], r["unit"], r["aggregation"])
            print(f"  {r['product_family']:>15} / {r['usage_type']:<28} "
                  f"{month_label(months[-1])}={latest} {r['unit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
