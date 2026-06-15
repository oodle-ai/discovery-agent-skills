# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""GCP Cloud Operations discovery collector.

Collects metrics inventory, log ingestion, trace spans, alert policies, and
log bucket configuration from the same GCP APIs that back the Cloud Monitoring
and Cloud Logging consoles.  Writes redacted raw responses to evidence/ and
emits summary.json (see schemas/summary.schema.json).

Auth: uses `gcloud auth print-access-token` (no google-cloud SDK dependency).

Examples:
    uv run collectors/gcp/collect.py --output-dir ./discovery-output/gcp
    uv run collectors/gcp/collect.py --project my-project-1 --project my-project-2 \\
        --output-dir ./discovery-output/gcp
    uv run collectors/gcp/collect.py --all-projects --lookback 7d \\
        --output-dir ./discovery-output/gcp
    uv run collectors/gcp/collect.py --report-only \\
        --output-dir ./discovery-output/gcp

Figure <-> API mapping is documented in collectors/gcp/README.md.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.auth import bearer_headers, gcloud_access_token  # noqa: E402
from lib.cli import base_parser, parse_duration_days, parse_headers  # noqa: E402
from lib.evidence import EvidenceWriter  # noqa: E402
from lib.http import FetchResult, HttpClient  # noqa: E402
from lib.summary import ExpectedFigure, Figure, SummaryWriter  # noqa: E402

COLLECTOR = "gcp"
VERSION = "1.0.0"

MONITORING_BASE = "https://monitoring.googleapis.com"
LOGGING_BASE = "https://logging.googleapis.com"

CUSTOM_METRIC_PREFIXES = (
    "custom.googleapis.com/",
    "external.googleapis.com/",
    "workload.googleapis.com/",
    "prometheus.googleapis.com/",
)

EXPECTED = [
    ExpectedFigure("metrics.total_count", "GCP metric descriptors (total)", "metrics", "metrics"),
    ExpectedFigure(
        "metrics.custom_metrics_count", "GCP custom/external metrics", "metrics", "metrics"
    ),
    ExpectedFigure(
        "metrics.samples_per_sec", "GCP metric samples ingested", "samples/sec", "metrics"
    ),
    ExpectedFigure("logs.ingest_gb_per_day", "Cloud Logging ingestion", "GB/day", "logs"),
    ExpectedFigure("logs.stored_gb", "Cloud Logging stored (month to date)", "GB", "logs"),
    ExpectedFigure("alerts.monitor_count", "Cloud Monitoring alert policies", "policies", "alerts"),
    ExpectedFigure("traces.spans_per_sec", "Cloud Trace spans ingested", "spans/sec", "traces"),
    ExpectedFigure("cost.monthly_usd", "GCP observability cost", "USD", "cost"),
]


def safe_project(p: str) -> str:
    return p.replace("-", "_").replace(".", "_")


# ── project discovery ──────────────────────────────────────────────────


def list_gcloud_projects() -> list[dict[str, str]]:
    """List accessible projects via gcloud CLI."""
    try:
        out = subprocess.run(
            ["gcloud", "projects", "list", "--format=json(projectId,name)"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
        return json.loads(out.stdout)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        print(f"WARN: could not list projects: {exc}")
        return []


def select_projects(
    available: list[dict[str, str]], pre_selected: list[str] | None
) -> list[str]:
    """Interactive project selection with select-all option."""
    if pre_selected:
        return list(pre_selected)
    if not available:
        return []
    print("\nAvailable GCP projects:")
    print("  0) ALL projects")
    for i, p in enumerate(available, 1):
        name = p.get("name", "")
        pid = p.get("projectId", "")
        label = f"{pid} ({name})" if name and name != pid else pid
        print(f"  {i}) {label}")
    print()
    try:
        raw = input("Select projects (comma-separated numbers, or 0 for all): ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nno selection; using all projects")
        return [p["projectId"] for p in available]
    if not raw or raw == "0":
        return [p["projectId"] for p in available]
    indices = []
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part)
            if idx == 0:
                return [p["projectId"] for p in available]
            if 1 <= idx <= len(available):
                indices.append(idx - 1)
    if not indices:
        print("no valid selection; using all projects")
        return [p["projectId"] for p in available]
    return [available[i]["projectId"] for i in indices]


# ── GCP REST helpers ───────────────────────────────────────────────────


def paginated_list(
    client: HttpClient, path: str, items_key: str, params: dict[str, str] | None = None
) -> tuple[list[Any], FetchResult]:
    """Fetch all pages of a GCP list endpoint."""
    all_items: list[Any] = []
    p = dict(params or {})
    first_result: FetchResult | None = None
    while True:
        res = client.get_json(path, params=p or None)
        if first_result is None:
            first_result = res
        if not res.ok:
            return all_items, res
        all_items.extend(res.data.get(items_key, []))
        token = res.data.get("nextPageToken")
        if not token:
            break
        p["pageToken"] = token
    return all_items, FetchResult(ok=True, data=all_items)


def query_timeseries(
    client: HttpClient,
    project: str,
    metric_type: str,
    lookback_s: int,
) -> FetchResult:
    """Query Cloud Monitoring timeSeries for a billing/usage metric."""
    now = datetime.now(UTC)
    start = now - timedelta(seconds=lookback_s)
    path = f"/v3/projects/{project}/timeSeries"
    params = {
        "filter": f'metric.type="{metric_type}"',
        "interval.startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "interval.endTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "view": "FULL",
    }
    items, res = paginated_list(client, path, "timeSeries", params)
    if not res.ok:
        return res
    return FetchResult(ok=True, data={"timeSeries": items})


# ── collection ─────────────────────────────────────────────────────────


def collect_metric_descriptors(
    mon_client: HttpClient,
    project: str,
    ev: EvidenceWriter,
    results: dict[str, Any],
) -> FetchResult:
    key = f"metric_descriptors_{safe_project(project)}"
    print(f"collecting metric descriptors ({project})")
    items, res = paginated_list(
        mon_client, f"/v3/projects/{project}/metricDescriptors", "metricDescriptors"
    )
    if items or res.ok:
        results[key] = {"metricDescriptors": items}
        ev.write(
            key, results[key],
            source_api=f"GET /v3/projects/{project}/metricDescriptors (paginated)",
        )
        return FetchResult(ok=True, data=results[key])
    print(f"  WARN {key}: {res.error}")
    return res


def _collect_timeseries(
    mon_client: HttpClient,
    project: str,
    key_prefix: str,
    metric_type: str,
    label: str,
    lookback_s: int,
    ev: EvidenceWriter,
    results: dict[str, Any],
) -> tuple[str, FetchResult]:
    """Collect a single billing/usage timeSeries metric for one project."""
    key = f"{key_prefix}_{safe_project(project)}"
    print(f"collecting {label} ({project})")
    res = query_timeseries(mon_client, project, metric_type, lookback_s)
    if res.ok:
        results[key] = res.data
        ev.write(
            key, res.data,
            source_api=f"GET /v3/projects/{project}/timeSeries ({metric_type})",
        )
    else:
        print(f"  WARN {key}: {res.error}")
    return key, res


def collect_alert_policies(
    mon_client: HttpClient,
    project: str,
    ev: EvidenceWriter,
    results: dict[str, Any],
) -> FetchResult:
    key = f"alert_policies_{safe_project(project)}"
    print(f"collecting alert policies ({project})")
    items, res = paginated_list(
        mon_client, f"/v3/projects/{project}/alertPolicies", "alertPolicies"
    )
    if items or res.ok:
        results[key] = {"alertPolicies": items}
        ev.write(
            key, results[key],
            source_api=f"GET /v3/projects/{project}/alertPolicies (paginated)",
        )
        return FetchResult(ok=True, data=results[key])
    print(f"  WARN {key}: {res.error}")
    return res


def collect_log_buckets(
    log_client: HttpClient,
    project: str,
    ev: EvidenceWriter,
    results: dict[str, Any],
) -> FetchResult:
    key = f"log_buckets_{safe_project(project)}"
    print(f"collecting log buckets ({project})")
    items, res = paginated_list(
        log_client, f"/v2/projects/{project}/locations/-/buckets", "buckets"
    )
    if items or res.ok:
        results[key] = {"buckets": items}
        ev.write(
            key, results[key],
            source_api=f"GET /v2/projects/{project}/locations/-/buckets (paginated)",
        )
        return FetchResult(ok=True, data=results[key])
    print(f"  WARN {key}: {res.error}")
    return res


def collect_log_sinks(
    log_client: HttpClient,
    project: str,
    ev: EvidenceWriter,
    results: dict[str, Any],
) -> FetchResult:
    key = f"log_sinks_{safe_project(project)}"
    print(f"collecting log sinks ({project})")
    items, res = paginated_list(
        log_client, f"/v2/projects/{project}/sinks", "sinks"
    )
    if items or res.ok:
        results[key] = {"sinks": items}
        ev.write(
            key, results[key],
            source_api=f"GET /v2/projects/{project}/sinks (paginated)",
        )
        return FetchResult(ok=True, data=results[key])
    print(f"  WARN {key}: {res.error}")
    return res


# ── derivation ─────────────────────────────────────────────────────────


def all_project_items(
    results: dict[str, Any], prefix: str, list_key: str
) -> list[Any]:
    items: list[Any] = []
    for key, val in results.items():
        if key.startswith(prefix) and isinstance(val, dict):
            items.extend(val.get(list_key, []))
    return items


def project_status(
    fetches: dict[str, FetchResult], prefix: str, projects: list[str]
) -> tuple[str, list[str]]:
    """Return (status, failed_projects) based on which projects succeeded."""
    failed = []
    for p in projects:
        key = f"{prefix}{safe_project(p)}"
        res = fetches.get(key)
        if res is not None and not res.ok:
            failed.append(p)
    if len(failed) == len(projects):
        return "unavailable", failed
    if failed:
        return "partial", failed
    return "ok", []


def sum_delta_timeseries(results: dict[str, Any], prefix: str) -> float:
    """Sum all int64 DELTA points across all projects for a billing metric."""
    total = 0.0
    for key, val in results.items():
        if not key.startswith(prefix) or not isinstance(val, dict):
            continue
        for ts in val.get("timeSeries", []):
            for point in ts.get("points", []):
                v = point.get("value", {})
                total += float(v.get("int64Value", 0))
    return total


def sum_latest_cumulative(results: dict[str, Any], prefix: str) -> float | None:
    """Sum the latest cumulative value from each project's timeSeries."""
    total = 0.0
    found_any = False
    for key, val in results.items():
        if not key.startswith(prefix) or not isinstance(val, dict):
            continue
        for ts in val.get("timeSeries", []):
            latest_time = ""
            latest_val = 0.0
            for point in ts.get("points", []):
                end_time = point.get("interval", {}).get("endTime", "")
                if end_time > latest_time:
                    latest_time = end_time
                    v = point.get("value", {})
                    latest_val = float(v.get("int64Value", 0))
            if latest_time:
                total += latest_val
                found_any = True
    return total if found_any else None


def evidence_files_for(results: dict[str, Any], prefix: str) -> list[str]:
    return [
        f"evidence/{key}.json"
        for key in results
        if key.startswith(prefix)
    ]


def derive_metrics(
    results: dict[str, Any],
    fetches: dict[str, FetchResult],
    summary: SummaryWriter,
    projects: list[str],
) -> None:
    status, failed = project_status(fetches, "metric_descriptors_", projects)
    all_descriptors = all_project_items(results, "metric_descriptors_", "metricDescriptors")

    if not all_descriptors and status == "unavailable":
        res = next(
            (fetches[f"metric_descriptors_{safe_project(p)}"] for p in projects
             if fetches.get(f"metric_descriptors_{safe_project(p)}")),
            None,
        )
        reason = res.gap_reason if res and res.gap_reason else "api_error"
        detail = res.error if res and res.error else "metricDescriptors.list failed in all projects"
        summary.mark_unavailable("metrics.total_count", reason, detail)
        summary.mark_unavailable("metrics.custom_metrics_count", reason, detail)
        return

    notes = f"failed projects: {', '.join(failed)}" if failed else None
    evidence = evidence_files_for(results, "metric_descriptors_")

    total = len(all_descriptors)
    custom = sum(
        1 for d in all_descriptors
        if any(d.get("type", "").startswith(pfx) for pfx in CUSTOM_METRIC_PREFIXES)
    )

    summary.add_figure(
        Figure(
            id="metrics.total_count",
            label="GCP metric descriptors (total)",
            value=float(total),
            unit="metrics",
            status=status,
            method=f"count of metricDescriptors across {len(projects) - len(failed)} project(s)",
            source_api="GET /v3/projects/*/metricDescriptors (paginated)",
            evidence_files=evidence,
            notes=notes,
        )
    )
    summary.add_figure(
        Figure(
            id="metrics.custom_metrics_count",
            label="GCP custom/external metrics",
            value=float(custom),
            unit="metrics",
            status=status,
            method="count of descriptors with type starting with "
            "custom.|external.|workload.|prometheus.googleapis.com/",
            source_api="GET /v3/projects/*/metricDescriptors (paginated)",
            evidence_files=evidence,
            notes=notes,
        )
    )


def derive_samples(
    results: dict[str, Any],
    fetches: dict[str, FetchResult],
    summary: SummaryWriter,
    projects: list[str],
    lookback_s: int,
) -> None:
    status, failed = project_status(fetches, "billing_samples_", projects)
    total_samples = sum_delta_timeseries(results, "billing_samples_")

    if total_samples == 0 and status == "unavailable":
        res = next(
            (fetches[f"billing_samples_{safe_project(p)}"] for p in projects
             if fetches.get(f"billing_samples_{safe_project(p)}")),
            None,
        )
        reason = res.gap_reason if res and res.gap_reason else "not_configured"
        detail = (
            res.error if res and res.error
            else "billing/samples_ingested returned no data (Monitoring may not be in use)"
        )
        summary.mark_unavailable("metrics.samples_per_sec", reason, detail)
        return

    notes = f"failed projects: {', '.join(failed)}" if failed else None
    evidence = evidence_files_for(results, "billing_samples_")
    samples_per_sec = total_samples / lookback_s if lookback_s > 0 else 0.0

    summary.add_figure(
        Figure(
            id="metrics.samples_per_sec",
            label="GCP metric samples ingested",
            value=round(samples_per_sec, 2),
            unit="samples/sec",
            status=status,
            method=f"sum of billing/samples_ingested DELTA points / {lookback_s}s lookback window",
            source_api="GET /v3/projects/*/timeSeries"
            " (monitoring.googleapis.com/billing/samples_ingested)",
            evidence_files=evidence,
            notes=notes,
        )
    )


def derive_logs(
    results: dict[str, Any],
    fetches: dict[str, FetchResult],
    summary: SummaryWriter,
    projects: list[str],
    lookback_s: int,
) -> None:
    # logs.ingest_gb_per_day from bytes_ingested DELTA
    ingest_status, ingest_failed = project_status(
        fetches, "log_billing_ingest_", projects
    )
    total_bytes = sum_delta_timeseries(results, "log_billing_ingest_")
    lookback_days = lookback_s / 86400.0

    if total_bytes == 0 and ingest_status == "unavailable":
        res = next(
            (fetches[f"log_billing_ingest_{safe_project(p)}"] for p in projects
             if fetches.get(f"log_billing_ingest_{safe_project(p)}")),
            None,
        )
        reason = res.gap_reason if res and res.gap_reason else "not_configured"
        detail = (
            res.error if res and res.error
            else "billing/bytes_ingested returned no data (Logging may not be in use)"
        )
        summary.mark_unavailable("logs.ingest_gb_per_day", reason, detail)
    else:
        ingest_notes = (
            f"failed projects: {', '.join(ingest_failed)}" if ingest_failed else None
        )
        gb_per_day = total_bytes / 1e9 / lookback_days if lookback_days > 0 else 0.0
        summary.add_figure(
            Figure(
                id="logs.ingest_gb_per_day",
                label="Cloud Logging ingestion",
                value=round(gb_per_day, 2),
                unit="GB/day",
                status=ingest_status,
                method=f"sum of billing/bytes_ingested DELTA points / {lookback_days:.1f} days, "
                "bytes -> GB (1e9)",
                source_api="GET /v3/projects/*/timeSeries"
                " (logging.googleapis.com/billing/bytes_ingested)",
                evidence_files=evidence_files_for(results, "log_billing_ingest_"),
                notes=ingest_notes,
            )
        )

    # logs.stored_gb from monthly_bytes_ingested CUMULATIVE (latest value)
    monthly_status, monthly_failed = project_status(
        fetches, "log_billing_monthly_", projects
    )
    monthly_value = sum_latest_cumulative(results, "log_billing_monthly_")

    if monthly_value is None and monthly_status == "unavailable":
        res = next(
            (fetches[f"log_billing_monthly_{safe_project(p)}"] for p in projects
             if fetches.get(f"log_billing_monthly_{safe_project(p)}")),
            None,
        )
        reason = res.gap_reason if res and res.gap_reason else "not_configured"
        detail = (
            res.error if res and res.error
            else "billing/monthly_bytes_ingested returned no data"
        )
        summary.mark_unavailable("logs.stored_gb", reason, detail)
    elif monthly_value is None:
        summary.mark_unavailable(
            "logs.stored_gb", "not_configured",
            "monthly_bytes_ingested metric returned no data points",
        )
    else:
        monthly_notes = (
            f"failed projects: {', '.join(monthly_failed)}" if monthly_failed else None
        )
        summary.add_figure(
            Figure(
                id="logs.stored_gb",
                label="Cloud Logging stored (month to date)",
                value=round(monthly_value / 1e9, 2),
                unit="GB",
                status="estimated",
                method="latest value of billing/monthly_bytes_ingested "
                "(cumulative month-to-date ingestion, not retained storage; "
                "actual stored volume may differ due to retention)",
                source_api="GET /v3/projects/*/timeSeries"
                " (logging.googleapis.com/billing/monthly_bytes_ingested)",
                evidence_files=evidence_files_for(results, "log_billing_monthly_"),
                notes=monthly_notes,
            )
        )


def derive_traces(
    results: dict[str, Any],
    fetches: dict[str, FetchResult],
    summary: SummaryWriter,
    projects: list[str],
    lookback_s: int,
) -> None:
    status, failed = project_status(fetches, "trace_billing_", projects)
    total_spans = sum_delta_timeseries(results, "trace_billing_")

    if total_spans == 0 and status == "unavailable":
        res = next(
            (fetches[f"trace_billing_{safe_project(p)}"] for p in projects
             if fetches.get(f"trace_billing_{safe_project(p)}")),
            None,
        )
        reason = res.gap_reason if res and res.gap_reason else "not_configured"
        detail = (
            res.error if res and res.error
            else "billing/spans_ingested returned no data (Cloud Trace may not be in use)"
        )
        summary.mark_unavailable("traces.spans_per_sec", reason, detail)
        return

    notes = f"failed projects: {', '.join(failed)}" if failed else None
    evidence = evidence_files_for(results, "trace_billing_")
    spans_per_sec = total_spans / lookback_s if lookback_s > 0 else 0.0

    summary.add_figure(
        Figure(
            id="traces.spans_per_sec",
            label="Cloud Trace spans ingested",
            value=round(spans_per_sec, 2),
            unit="spans/sec",
            status=status,
            method=f"sum of billing/spans_ingested DELTA points / {lookback_s}s lookback window",
            source_api="GET /v3/projects/*/timeSeries"
            " (cloudtrace.googleapis.com/billing/spans_ingested)",
            evidence_files=evidence,
            notes=notes,
        )
    )


def derive_alerts(
    results: dict[str, Any],
    fetches: dict[str, FetchResult],
    summary: SummaryWriter,
    projects: list[str],
) -> None:
    status, failed = project_status(fetches, "alert_policies_", projects)
    all_policies = all_project_items(results, "alert_policies_", "alertPolicies")

    if not all_policies and status == "unavailable":
        res = next(
            (fetches[f"alert_policies_{safe_project(p)}"] for p in projects
             if fetches.get(f"alert_policies_{safe_project(p)}")),
            None,
        )
        reason = res.gap_reason if res and res.gap_reason else "api_error"
        detail = res.error if res and res.error else "alertPolicies.list failed in all projects"
        summary.mark_unavailable("alerts.monitor_count", reason, detail)
        return

    notes = f"failed projects: {', '.join(failed)}" if failed else None
    evidence = evidence_files_for(results, "alert_policies_")

    summary.add_figure(
        Figure(
            id="alerts.monitor_count",
            label="Cloud Monitoring alert policies",
            value=float(len(all_policies)),
            unit="policies",
            status=status,
            method=f"count of alertPolicies across {len(projects) - len(failed)} project(s)",
            source_api="GET /v3/projects/*/alertPolicies (paginated)",
            evidence_files=evidence,
            notes=notes,
        )
    )


def derive_cost(summary: SummaryWriter) -> None:
    summary.mark_unavailable(
        "cost.monthly_usd",
        "not_configured",
        "GCP has no cost query API; actual cost requires BigQuery billing export",
        remediation="enable BigQuery billing export and rerun with --billing-export-table "
        "(not yet implemented); see "
        "https://cloud.google.com/billing/docs/how-to/export-data-bigquery",
    )


def build_summary(
    results: dict[str, Any],
    fetches: dict[str, FetchResult],
    summary: SummaryWriter,
    projects: list[str],
    lookback_s: int,
) -> None:
    derive_metrics(results, fetches, summary, projects)
    derive_samples(results, fetches, summary, projects, lookback_s)
    derive_logs(results, fetches, summary, projects, lookback_s)
    derive_traces(results, fetches, summary, projects, lookback_s)
    derive_alerts(results, fetches, summary, projects)
    derive_cost(summary)

    inv = summary.inventory
    inv["projects_collected"] = projects

    # namespace breakdown (top 30)
    all_descriptors = all_project_items(results, "metric_descriptors_", "metricDescriptors")
    ns_counts: dict[str, int] = {}
    for d in all_descriptors:
        metric_type = d.get("type", "unknown")
        ns = metric_type.rsplit("/", 1)[0] if "/" in metric_type else metric_type
        ns_counts[ns] = ns_counts.get(ns, 0) + 1
    inv["metric_type_breakdown"] = dict(
        sorted(ns_counts.items(), key=lambda kv: -kv[1])[:30]
    )

    # custom metric prefix breakdown
    custom_prefixes: dict[str, int] = {}
    for d in all_descriptors:
        mt = d.get("type", "")
        for pfx in CUSTOM_METRIC_PREFIXES:
            if mt.startswith(pfx):
                custom_prefixes[pfx.rstrip("/")] = custom_prefixes.get(pfx.rstrip("/"), 0) + 1
                break
    if custom_prefixes:
        inv["custom_metric_prefixes"] = dict(
            sorted(custom_prefixes.items(), key=lambda kv: -kv[1])
        )

    # alert policies by condition type
    all_policies = all_project_items(results, "alert_policies_", "alertPolicies")
    cond_types: dict[str, int] = {}
    for pol in all_policies:
        for cond in pol.get("conditions", []):
            ct = "unknown"
            for field in (
                "conditionThreshold", "conditionAbsent", "conditionMatchedLog",
                "conditionMonitoringQueryLanguage", "conditionPrometheusQueryLanguage",
            ):
                if field in cond:
                    ct = field
                    break
            cond_types[ct] = cond_types.get(ct, 0) + 1
    if cond_types:
        inv["alert_policies_by_condition_type"] = dict(
            sorted(cond_types.items(), key=lambda kv: -kv[1])
        )

    # log buckets
    all_buckets = all_project_items(results, "log_buckets_", "buckets")
    if all_buckets:
        inv["log_buckets"] = [
            {
                "name": b.get("name", ""),
                "retentionDays": b.get("retentionDays"),
                "locked": b.get("locked", False),
                "lifecycleState": b.get("lifecycleState", ""),
            }
            for b in all_buckets
        ]

    # log sinks
    all_sinks = all_project_items(results, "log_sinks_", "sinks")
    if all_sinks:
        inv["log_sinks"] = [
            {
                "name": s.get("name", ""),
                "destination": s.get("destination", ""),
                "disabled": s.get("disabled", False),
            }
            for s in all_sinks
        ]


# ── main ───────────────────────────────────────────────────────────────


def main() -> int:
    parser = base_parser("GCP Cloud Operations discovery collector", default_lookback="7d")
    parser.add_argument(
        "--project",
        action="append",
        default=[],
        help="GCP project ID to collect (repeatable; omit for interactive selection)",
    )
    parser.add_argument(
        "--all-projects",
        action="store_true",
        help="Collect from all accessible projects without prompting",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ev = EvidenceWriter(args.output_dir)
    results: dict[str, Any] = {}
    fetches: dict[str, FetchResult] = {}
    lookback_days = parse_duration_days(args.lookback)
    lookback_s = int(lookback_days * 86400)

    if args.report_only:
        results = ev.load_all()
        if not results:
            print(f"ERROR: --report-only but no evidence under {ev.evidence_dir}")
            return 2
        meta = results.get("_projects")
        if meta and isinstance(meta, dict):
            projects = meta.get("projects", [])
        else:
            print("ERROR: no _projects.json in evidence; cannot determine project list")
            return 2
        if not projects:
            print("ERROR: _projects.json is empty")
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

        if args.all_projects:
            available = list_gcloud_projects()
            projects = [p["projectId"] for p in available]
        elif args.project:
            projects = list(args.project)
        else:
            available = list_gcloud_projects()
            if not available:
                print("ERROR: no projects found; pass --project explicitly")
                return 2
            projects = select_projects(available, None)

        if not projects:
            print("ERROR: no projects selected")
            return 2

        print(f"projects: {', '.join(projects)}")
        ev.write("_projects", {"projects": projects}, source_api="gcloud projects list / --project")

        with HttpClient(
            MONITORING_BASE,
            headers=headers,
            timeout_s=args.timeout,
            verify=not args.insecure,
        ) as mon_client, HttpClient(
            LOGGING_BASE,
            headers=headers,
            timeout_s=args.timeout,
            verify=not args.insecure,
        ) as log_client:
            for project in projects:
                fetches[f"metric_descriptors_{safe_project(project)}"] = (
                    collect_metric_descriptors(mon_client, project, ev, results)
                )
                ts_metrics = [
                    ("billing_samples", "monitoring.googleapis.com/billing/samples_ingested",
                     "billing samples_ingested"),
                    ("log_billing_ingest", "logging.googleapis.com/billing/bytes_ingested",
                     "log billing bytes_ingested"),
                    ("log_billing_monthly",
                     "logging.googleapis.com/billing/monthly_bytes_ingested",
                     "log billing monthly_bytes_ingested"),
                    ("trace_billing", "cloudtrace.googleapis.com/billing/spans_ingested",
                     "trace billing spans_ingested"),
                ]
                for prefix, metric_type, label in ts_metrics:
                    key, res = _collect_timeseries(
                        mon_client, project, prefix, metric_type,
                        label, lookback_s, ev, results,
                    )
                    fetches[key] = res
                fetches[f"alert_policies_{safe_project(project)}"] = (
                    collect_alert_policies(mon_client, project, ev, results)
                )
                fetches[f"log_buckets_{safe_project(project)}"] = (
                    collect_log_buckets(log_client, project, ev, results)
                )
                fetches[f"log_sinks_{safe_project(project)}"] = (
                    collect_log_sinks(log_client, project, ev, results)
                )

    summary = SummaryWriter(
        collector=COLLECTOR,
        collector_version=VERSION,
        expected=EXPECTED,
        target=f"gcp-projects ({', '.join(projects)})",
        lookback=args.lookback,
        args_redacted={
            "projects": projects,
            "lookback": args.lookback,
        },
    )
    summary.environment = {
        "detected_backend": "gcp-cloud-operations",
        "version": None,
        "detection_method": "gcloud auth + project list/flag",
        "projects": projects,
    }
    build_summary(results, fetches, summary, projects, lookback_s)
    summary.write(args.output_dir)
    ev.finalize()
    if args.tar:
        ev.tar()

    unavailable = [f for f in summary.to_dict()["figures"] if f["status"] == "unavailable"]
    print(
        f"done: {len(EXPECTED) - len(unavailable)}/{len(EXPECTED)} expected figures collected; "
        f"{len(unavailable)} gap(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
