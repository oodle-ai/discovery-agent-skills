# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Datadog estimated-usage 6-month summary (from the Estimated Usage Overview).

Mirrors Datadog's built-in "Estimated Usage Overview" dashboard: for each of the
dashboard's summary widgets it queries the underlying `datadog.estimated_usage.*`
metric per calendar month over the last N months, producing one value per widget
per month. This is NOT a per-SKU breakdown — it's the same product-level numbers
you see on the dashboard (and the Plan & Usage page), reconstructed month by
month so they can be trended.

Each widget's metric is reduced per month by the aggregation that matches how
Datadog counts it:
    counts (*.as_count())            -> summed over the month
    host / container gauges          -> max over the month (peak concurrent)
    custom-metric gauges             -> averaged over the month (billing basis)

Reads with metrics/timeseries scope via /api/v1/query (no usage_read /
billing_read). Point it at its OWN output dir.

Examples:
    DD_API_KEY=... DD_APP_KEY=... \\
    uv run collectors/datadog/estimated_usage_monthly.py --site us5 --months 6 \\
        --output-dir ./discovery-output/datadog-estimated-usage --tar

    uv run collectors/datadog/estimated_usage_monthly.py --report-only \\
        --output-dir ./discovery-output/datadog-estimated-usage
"""

from __future__ import annotations

import csv
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1]))  # collectors/  (for lib.*)

import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("dd_est_sibling", _HERE / "collect.py")
dd = _ilu.module_from_spec(_spec)
sys.modules["dd_est_sibling"] = dd
_spec.loader.exec_module(dd)

from lib.auth import datadog_headers  # noqa: E402
from lib.cli import base_parser, credential, parse_headers  # noqa: E402
from lib.evidence import EvidenceWriter  # noqa: E402
from lib.http import HttpClient  # noqa: E402
from lib.summary import SummaryWriter  # noqa: E402

COLLECTOR = "datadog_estimated_usage_monthly"
VERSION = "1.0.0"

MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# The Estimated Usage Overview dashboard's summary widgets. `agg` is the monthly
# reduction: sum for cumulative counts, max for concurrent host/container gauges,
# avg for custom-metric gauges (Datadog's billing basis).
METRICS = [
    {"key": "infra_hosts", "label": "Infra hosts", "unit": "hosts", "agg": "max",
     "query": "sum:datadog.estimated_usage.hosts{*}"},
    {"key": "apm_hosts", "label": "APM hosts", "unit": "hosts", "agg": "max",
     "query": "sum:datadog.estimated_usage.apm_hosts{*}"},
    {"key": "containers", "label": "Containers", "unit": "containers", "agg": "max",
     "query": "sum:datadog.estimated_usage.containers{*}"},
    {"key": "custom_metrics", "label": "Custom metrics", "unit": "metrics", "agg": "avg",
     "query": "sum:datadog.estimated_usage.metrics.custom{*}"},
    {"key": "custom_metrics_ingested", "label": "Ingested custom metrics",
     "unit": "metrics", "agg": "avg",
     "query": "sum:datadog.estimated_usage.metrics.custom.ingested{*}"},
    {"key": "logs_ingested", "label": "Ingested logs", "unit": "events", "agg": "sum",
     "query": "sum:datadog.estimated_usage.logs.ingested_events{*}.as_count()"},
    {"key": "logs_ingested_bytes", "label": "Ingested log volume", "unit": "GB", "agg": "sum",
     "query": "sum:datadog.estimated_usage.logs.ingested_bytes{*}.as_count()"},
    {"key": "logs_indexed", "label": "Indexed logs", "unit": "events", "agg": "sum",
     "query": "sum:datadog.estimated_usage.logs.ingested_events"
              "{datadog_index:*,datadog_is_excluded:false}.as_count()"},
    {"key": "apm_ingested_bytes", "label": "Ingested spans", "unit": "GB", "agg": "sum",
     "query": "sum:datadog.estimated_usage.apm.ingested_bytes{*}.as_count()"},
    {"key": "apm_indexed_spans", "label": "Indexed spans", "unit": "spans", "agg": "sum",
     "query": "sum:datadog.estimated_usage.apm.indexed_spans{*}.as_count()"},
]


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


def month_bounds(key: str) -> tuple[int, int]:
    """(from_epoch, to_epoch) for the whole calendar month `key` (YYYY-MM)."""
    y, m = int(key[:4]), int(key[5:7])
    start = datetime(y, m, 1, tzinfo=UTC)
    end = datetime(y + 1, 1, 1, tzinfo=UTC) if m == 12 else datetime(y, m + 1, 1, tzinfo=UTC)
    return int(start.timestamp()), int(end.timestamp())


def reduce_points(points: list[tuple[int, float]], agg: str) -> float | None:
    """Reduce a query pointlist by the month's aggregation. None if no points."""
    if not points:
        return None
    vals = [v for _, v in points]
    if agg == "sum":
        return sum(vals)
    if agg == "max":
        return max(vals)
    return sum(vals) / len(vals)  # avg


def aggregate_monthly(
    results: dict[str, Any], months: list[str]
) -> list[dict[str, Any]]:
    """One row per metric with a per-month reduced value (None where no data).
    Pure function over stored /api/v1/query responses, so it is unit-testable."""
    rows: list[dict[str, Any]] = []
    for metric in METRICS:
        values: dict[str, float | None] = {}
        for mk in months:
            pts = dd.query_points(results.get(f"est_{metric['key']}_{mk}"))
            v = reduce_points(pts, metric["agg"])
            if v is not None and metric["unit"] == "GB":
                v = v / 1e9
            values[mk] = v
        rows.append({
            "key": metric["key"], "label": metric["label"],
            "unit": metric["unit"], "aggregation": metric["agg"], "values": values,
        })
    return rows


def _fmt(value: float | None, unit: str) -> str:
    if value is None:
        return ""
    if unit == "GB":
        return f"{value:.3f}"
    return f"{value:.0f}" if abs(value - round(value)) < 1e-6 else f"{value:.2f}"


def write_csv(output_dir: Path, rows: list[dict[str, Any]], months: list[str]) -> Path:
    path = output_dir / "datadog_estimated_usage_monthly.csv"
    labels = [month_label(m) for m in months]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["metric", "unit", "aggregation"] + labels)
        for r in rows:
            w.writerow([r["label"], r["unit"], r["aggregation"]]
                       + [_fmt(r["values"][m], r["unit"]) for m in months])
    return path


def inventory_rows(rows: list[dict[str, Any]], months: list[str]) -> list[dict[str, Any]]:
    """Report-ready rows: display strings per month label, matrix-compatible with
    the report's monthly-usage section (metric in the 'usage_type' slot)."""
    labels = [month_label(m) for m in months]
    out: list[dict[str, Any]] = []
    for r in rows:
        d: dict[str, Any] = {
            "product_family": "estimated_usage", "usage_type": r["label"],
            "unit": r["unit"], "aggregation": r["aggregation"],
        }
        for m, lab in zip(months, labels, strict=True):
            d[lab] = _fmt(r["values"][m], r["unit"])
        out.append(d)
    return out


def write_summary(
    output_dir: Path, rows: list[dict[str, Any]], months: list[str],
    domain: str, args_redacted: dict[str, Any],
) -> Path:
    summary = SummaryWriter(
        collector=COLLECTOR, collector_version=VERSION, expected=[],
        target=f"https://api.{domain}",
        lookback=f"{len(months)} full calendar months ({months[0]}..{months[-1]})",
        args_redacted=args_redacted,
    )
    summary.environment = {
        "detected_backend": "datadog", "version": None,
        "detection_method": "site flag / API reachability", "site": domain,
    }
    summary.inventory["monthly_usage_months"] = [month_label(m) for m in months]
    summary.inventory["monthly_usage_by_sku"] = inventory_rows(rows, months)
    summary.inventory["monthly_usage_note"] = (
        "Datadog Estimated Usage Overview metrics per calendar month. Counts "
        "(logs/spans) are summed; host/container gauges are the monthly max "
        "(peak concurrent); custom-metric gauges are the monthly average."
    )
    return summary.write(output_dir)


def main() -> int:
    parser = base_parser("Datadog estimated-usage monthly summary", default_lookback="180d")
    parser.add_argument("--api-key", help="Datadog API key (or env DD_API_KEY)")
    parser.add_argument("--app-key", help="Datadog application key (or env DD_APP_KEY)")
    parser.add_argument("--site", default="us1",
                        help=f"Datadog site: {', '.join(dd.DD_SITES)} or a full domain")
    parser.add_argument("--months", type=int, default=6,
                        help="Number of full calendar months back to report")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ev = EvidenceWriter(args.output_dir)
    results: dict[str, Any] = {}
    now = datetime.now(UTC)
    months = month_keys(now, args.months)
    domain = dd.resolve_site(args.site)

    if args.report_only:
        results = ev.load_all()
        if not results:
            print(f"ERROR: --report-only but no evidence under {ev.evidence_dir}")
            return 2
    else:
        api_key = credential(args.api_key, "DD_API_KEY", "Datadog API key")
        app_key = credential(args.app_key, "DD_APP_KEY", "Datadog application key")
        if not api_key or not app_key:
            print("ERROR: missing credentials. Pass --api-key/--app-key or set "
                  "DD_API_KEY / DD_APP_KEY.")
            return 2
        headers = datadog_headers(api_key, app_key)
        headers.update(parse_headers(args.header))
        print(f"Datadog estimated usage: {month_label(months[0])} .. {month_label(months[-1])} "
              f"({len(months)} months) from https://api.{domain}")
        with HttpClient(f"https://api.{domain}", headers=headers,
                        timeout_s=args.timeout, verify=not args.insecure) as client:
            for mk in months:
                frm, to = month_bounds(mk)
                for metric in METRICS:
                    key = f"est_{metric['key']}_{mk}"
                    res = client.get_json("/api/v1/query", params={
                        "from": str(frm), "to": str(to), "query": metric["query"]})
                    if res.ok:
                        results[key] = res.data
                        ev.write(key, res.data,
                                 source_api=f"GET /api/v1/query?query={metric['query']}")
                    else:
                        print(f"  WARN {key}: {res.error}")

    rows = aggregate_monthly(results, months)
    csv_path = write_csv(args.output_dir, rows, months)
    summary_path = write_summary(
        args.output_dir, rows, months, domain,
        args_redacted={"site": args.site, "months": args.months},
    )
    ev.finalize()
    if args.tar:
        ev.tar()

    print(f"\nWrote {csv_path}")
    print(f"Wrote {summary_path}")
    for r in rows:
        cells = "  ".join(f"{month_label(m)}={_fmt(r['values'][m], r['unit'])}" for m in months)
        print(f"  {r['label']:>24} ({r['unit']}): {cells}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
