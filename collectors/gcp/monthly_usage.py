# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""GCP monthly observability-usage-by-SKU export (for usage reviews).

Reconstructs per-signal observability volume broken out by **calendar month**
from the same Cloud Monitoring `billing/*` timeSeries the discovery collector
reads, then writes a CSV with one row per SKU and one column per month, plus a
summary.json whose `inventory.monthly_usage_by_sku` the report generator renders
as a "Monthly Usage by SKU" section (it carries no scalar figures).

The reconstructed "SKUs":
    metrics / samples_ingested   (monitoring.googleapis.com/billing/samples_ingested)
    logs    / bytes_ingested     (logging.googleapis.com/billing/bytes_ingested, -> GB)
    traces  / spans_ingested     (cloudtrace.googleapis.com/billing/spans_ingested)
summed across the selected projects, bucketed by the calendar month of each
DELTA point.

RETENTION CAVEAT: Cloud Monitoring retains billing timeSeries for only ~6 weeks,
so months older than that return no data and appear blank — NOT zero usage. For
a full multi-month history, use the Cloud Billing BigQuery export instead. This
caveat is written into the report so no blank month is read as "no usage".

Auth: `gcloud auth print-access-token` (no google-cloud SDK dependency).

Examples:
    uv run collectors/gcp/monthly_usage.py --project my-project-1 --months 6 \\
        --output-dir ./discovery-output/gcp-monthly --tar

    # re-derive CSV + summary from previously saved evidence, no API calls:
    uv run collectors/gcp/monthly_usage.py --report-only \\
        --output-dir ./discovery-output/gcp-monthly

Point it at its OWN output dir so its summary.json does not collide with the
discovery collector's.
"""

from __future__ import annotations

import csv
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parents[1]))  # collectors/  (for lib.*)

# The sibling discovery collector supplies the timeSeries fetch, project
# discovery, and evidence-naming helpers so the two stay in lockstep. Load it
# under a unique module name (not the bare `collect`) so it never collides with
# another collector's collect.py when several are imported in one process.
import importlib.util as _ilu  # noqa: E402

_spec = _ilu.spec_from_file_location("gcp_collect_sibling", _HERE / "collect.py")
gcp = _ilu.module_from_spec(_spec)
sys.modules["gcp_collect_sibling"] = gcp
_spec.loader.exec_module(gcp)

from lib.auth import bearer_headers, gcloud_access_token  # noqa: E402
from lib.cli import base_parser, parse_headers  # noqa: E402
from lib.evidence import EvidenceWriter  # noqa: E402
from lib.http import HttpClient  # noqa: E402
from lib.summary import SummaryWriter  # noqa: E402

COLLECTOR = "gcp_monthly"
VERSION = "1.0.0"

MONTH_ABBR = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]

# The observability "SKUs" we can reconstruct by month from Cloud Monitoring
# billing timeSeries. All are DELTA counters summed over the month; log bytes
# are divided to GB. usage_type mirrors the metric's short name.
GCP_SKUS = [
    {"key": "samples_ingested",
     "metric": "monitoring.googleapis.com/billing/samples_ingested",
     "product_family": "metrics", "usage_type": "samples_ingested",
     "unit": "samples", "divisor": 1.0},
    {"key": "log_bytes_ingested",
     "metric": "logging.googleapis.com/billing/bytes_ingested",
     "product_family": "logs", "usage_type": "bytes_ingested",
     "unit": "GB", "divisor": 1e9},
    {"key": "spans_ingested",
     "metric": "cloudtrace.googleapis.com/billing/spans_ingested",
     "product_family": "traces", "usage_type": "spans_ingested",
     "unit": "spans", "divisor": 1.0},
]

RETENTION_NOTE = (
    "Reconstructed from Cloud Monitoring billing/* timeSeries, which GCP retains "
    "for only ~6 weeks. Months older than that return no data and appear blank "
    "— that is a retention limit, not zero usage. For a full multi-month "
    "history, use the Cloud Billing BigQuery export."
)


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


def bucket_by_month(
    ts_list: list[dict[str, Any]], months: list[str], divisor: float
) -> dict[str, float | None]:
    """Sum DELTA points into calendar-month buckets (by point endTime).

    Returns one entry per in-window month: the summed value (divided by
    `divisor`), or None if that month had no points at all — so the report can
    show a blank rather than a fabricated 0.
    """
    sums: dict[str, float] = {}
    seen: set[str] = set()
    for ts in ts_list:
        for point in ts.get("points", []):
            end = str(point.get("interval", {}).get("endTime", ""))
            month = end[:7]
            if month not in months:
                continue
            val = point.get("value", {})
            raw = val.get("int64Value", val.get("doubleValue", 0))
            sums[month] = sums.get(month, 0.0) + float(raw or 0)
            seen.add(month)
    out: dict[str, float | None] = {}
    for month in months:
        out[month] = (sums.get(month, 0.0) / divisor) if month in seen else None
    return out


def aggregate_monthly(
    results: dict[str, Any], projects: list[str], months: list[str]
) -> list[dict[str, Any]]:
    """Bucket each billing SKU's timeSeries into monthly per-SKU rows, summed
    across projects. Pure function (no I/O) so it is unit-testable."""
    rows: list[dict[str, Any]] = []
    for sku in GCP_SKUS:
        all_series: list[dict[str, Any]] = []
        for p in projects:
            data = results.get(f"monthly_ts_{sku['key']}_{gcp.safe_project(p)}")
            if isinstance(data, dict):
                all_series.extend(data.get("timeSeries", []))
        values = bucket_by_month(all_series, months, sku["divisor"])
        rows.append({
            "product_family": sku["product_family"],
            "usage_type": sku["usage_type"],
            "unit": sku["unit"],
            "aggregation": "sum",
            "values": values,
        })
    return rows


def has_usage(row: dict[str, Any]) -> bool:
    """True if the SKU has any nonzero measurement across the window. All-None
    or all-zero rows are dropped by default (see drop_empty_rows)."""
    return any(v not in (None, 0) for v in row["values"].values())


def drop_empty_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r for r in rows if has_usage(r)]


def _fmt(value: float | None, unit: str) -> str:
    if value is None:
        return ""
    if unit == "GB":
        return f"{value:.3f}"
    return f"{value:.0f}" if abs(value - round(value)) < 1e-6 else f"{value:.2f}"


def inventory_rows(
    rows: list[dict[str, Any]], months: list[str]
) -> list[dict[str, Any]]:
    """The SKU matrix as report-ready inventory: one dict per SKU with a display
    string per month label (empty for months with no data), formatted the same
    as the CSV cells so report and CSV stay in lockstep."""
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
            d[lab] = _fmt(r["values"][m], r["unit"])
        out.append(d)
    return out


def write_csv(
    output_dir: Path, rows: list[dict[str, Any]], months: list[str]
) -> Path:
    path = output_dir / "gcp_monthly_usage_by_sku.csv"
    labels = [month_label(m) for m in months]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["product_family", "usage_type", "unit", "aggregation"] + labels)
        for r in rows:
            w.writerow(
                [r["product_family"], r["usage_type"], r["unit"], r["aggregation"]]
                + [_fmt(r["values"][m], r["unit"]) for m in months]
            )
    return path


def write_summary(
    output_dir: Path,
    rows: list[dict[str, Any]],
    months: list[str],
    projects: list[str],
    args_redacted: dict[str, Any],
) -> Path:
    """Emit a summary.json carrying the SKU matrix in `inventory` (no scalar
    figures). report/generate_report.py discovers it via the per-collector
    summary glob and renders the monthly-usage section from it."""
    summary = SummaryWriter(
        collector=COLLECTOR,
        collector_version=VERSION,
        expected=[],  # this collector reports a matrix, not scalar figures
        target=f"gcp-projects ({', '.join(projects)})",
        lookback=f"{len(months)} full calendar months ({months[0]}..{months[-1]})",
        args_redacted=args_redacted,
    )
    summary.environment = {
        "detected_backend": "gcp-cloud-operations",
        "version": None,
        "detection_method": "gcloud auth + project list/flag",
        "projects": projects,
    }
    summary.inventory["monthly_usage_months"] = [month_label(m) for m in months]
    summary.inventory["monthly_usage_by_sku"] = inventory_rows(rows, months)
    summary.inventory["monthly_usage_note"] = RETENTION_NOTE
    return summary.write(output_dir)


def resolve_projects(args) -> list[str]:
    """Project list for the fetch path (interactive/flag/all)."""
    if args.all_projects:
        return [p["projectId"] for p in gcp.list_gcloud_projects()]
    if args.project:
        return list(args.project)
    available = gcp.list_gcloud_projects()
    if not available:
        print("ERROR: no projects found; pass --project explicitly")
        return []
    return gcp.select_projects(available, None)


def main() -> int:
    parser = base_parser("GCP monthly usage-by-SKU export", default_lookback="180d")
    parser.add_argument(
        "--project", action="append", default=[],
        help="GCP project ID to collect (repeatable; omit for interactive selection)",
    )
    parser.add_argument(
        "--all-projects", action="store_true",
        help="Collect from all accessible projects without prompting",
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
    now = datetime.now(UTC)
    months = month_keys(now, args.months)
    start = datetime(int(months[0][:4]), int(months[0][5:7]), 1, tzinfo=UTC)
    lookback_s = int((now - start).total_seconds())

    if args.report_only:
        results = ev.load_all()
        if not results:
            print(f"ERROR: --report-only but no evidence under {ev.evidence_dir}")
            return 2
        meta = results.get("_projects")
        projects = meta.get("projects", []) if isinstance(meta, dict) else []
        if not projects:
            print("ERROR: no _projects.json in evidence; cannot determine project list")
            return 2
    else:
        try:
            token = gcloud_access_token()
        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            print(f"ERROR: gcloud auth failed: {exc}")
            print("ensure `gcloud auth login` has been run and gcloud is on PATH")
            return 2
        headers = bearer_headers(token)
        headers.update(parse_headers(args.header))
        projects = resolve_projects(args)
        if not projects:
            return 2
        print(f"GCP monthly usage: {month_label(months[0])} .. {month_label(months[-1])} "
              f"({len(months)} months) for {', '.join(projects)}")
        ev.write("_projects", {"projects": projects}, source_api="gcloud projects list / --project")
        with HttpClient(
            gcp.MONITORING_BASE, headers=headers,
            timeout_s=args.timeout, verify=not args.insecure,
        ) as client:
            for project in projects:
                for sku in GCP_SKUS:
                    key = f"monthly_ts_{sku['key']}_{gcp.safe_project(project)}"
                    print(f"collecting {sku['usage_type']} ({project})")
                    res = gcp.query_timeseries(client, project, sku["metric"], lookback_s)
                    if res.ok:
                        results[key] = res.data
                        ev.write(
                            key, res.data,
                            source_api=f"GET /v3/projects/{project}/timeSeries ({sku['metric']})",
                        )
                    else:
                        print(f"  WARN {key}: {res.error}")

    all_rows = aggregate_monthly(results, projects, months)
    rows = all_rows if args.include_empty else drop_empty_rows(all_rows)
    dropped = len(all_rows) - len(rows)
    csv_path = write_csv(args.output_dir, rows, months)
    summary_path = write_summary(
        args.output_dir, rows, months, projects,
        args_redacted={
            "projects": projects, "months": args.months,
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
    print("NOTE: Cloud Monitoring retains ~6 weeks of billing data; older months "
          "appear blank (retention limit, not zero usage).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
