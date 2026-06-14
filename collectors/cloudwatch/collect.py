# /// script
# requires-python = ">=3.11"
# dependencies = ["boto3"]
# ///
"""AWS CloudWatch discovery collector.

Collects metrics inventory, log group storage, alarms, dashboards, and
observability spend from the same AWS APIs that back the CloudWatch console
and Cost Explorer billing pages.  Writes redacted raw responses to evidence/
and emits summary.json (see schemas/summary.schema.json).

Examples:
    uv run collectors/cloudwatch/collect.py --output-dir ./discovery-output/cloudwatch
    uv run collectors/cloudwatch/collect.py --region us-east-1 --region us-west-2 \\
        --output-dir ./discovery-output/cloudwatch
    uv run collectors/cloudwatch/collect.py --all-regions --profile prod \\
        --log-group-breakdown --output-dir ./discovery-output/cloudwatch
    uv run collectors/cloudwatch/collect.py --report-only \\
        --output-dir ./discovery-output/cloudwatch

Figure <-> API mapping is documented in collectors/cloudwatch/README.md.
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.auth import boto3_session  # noqa: E402
from lib.cli import base_parser, parse_duration_days  # noqa: E402
from lib.evidence import EvidenceWriter  # noqa: E402
from lib.summary import ExpectedFigure, Figure, SummaryWriter  # noqa: E402

COLLECTOR = "cloudwatch"
VERSION = "1.0.0"

EXPECTED = [
    ExpectedFigure("metrics.total_count", "CloudWatch metrics (total)", "metrics", "metrics"),
    ExpectedFigure(
        "metrics.custom_metrics_count", "CloudWatch custom metrics", "metrics", "metrics"
    ),
    ExpectedFigure("logs.stored_gb", "CloudWatch Logs stored", "GB", "logs"),
    ExpectedFigure("logs.ingest_gb_per_day", "CloudWatch Logs ingestion", "GB/day", "logs"),
    ExpectedFigure("cloudwatch.log_groups_count", "Log Groups", "log groups", "cloudwatch"),
    ExpectedFigure("alerts.monitor_count", "CloudWatch Alarms", "alarms", "alerts"),
    ExpectedFigure("cost.monthly_usd", "CloudWatch monthly cost", "USD", "cost"),
]

CE_SERVICE = "AmazonCloudWatch"
LOG_INGEST_USAGE_SUBSTR = "DataProcessing-Bytes"


def safe_region(r: str) -> str:
    return r.replace("-", "_")


# ── boto3 wrapper ───────────────────────────────────────────────────────


@dataclass
class BotoResult:
    ok: bool
    data: Any = None
    error: str | None = None
    gap_reason: str | None = None


def boto_call(fn) -> BotoResult:
    try:
        return BotoResult(ok=True, data=fn())
    except Exception as exc:
        code = ""
        if hasattr(exc, "response"):
            code = exc.response.get("Error", {}).get("Code", "")
        reason = (
            "permission_denied"
            if code in ("AccessDeniedException", "AccessDenied", "OptInRequired")
            else "api_error"
        )
        msg = f"{code}: {exc}" if code else str(exc)
        return BotoResult(ok=False, error=msg[:500], gap_reason=reason)


# ── collection ──────────────────────────────────────────────────────────


def resolve_regions(profile: str | None, region_args: list[str], all_regions: bool) -> list[str]:
    if all_regions:
        session = boto3_session(profile, "us-east-1")
        ec2 = session.client("ec2")
        res = boto_call(
            lambda: ec2.describe_regions(
                Filters=[
                    {"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}
                ]
            )
        )
        if res.ok:
            return sorted(r["RegionName"] for r in res.data["Regions"])
        print(f"WARN: could not list regions ({res.error}); using default")
    if region_args:
        return list(region_args)
    session = boto3_session(profile)
    return [session.region_name or "us-east-1"]


def collect_metrics(
    profile: str | None,
    regions: list[str],
    ev: EvidenceWriter,
    results: dict[str, Any],
) -> dict[str, BotoResult]:
    fetches: dict[str, BotoResult] = {}
    for region in regions:
        key = f"metrics_{safe_region(region)}"
        print(f"collecting metrics ({region})")
        session = boto3_session(profile, region)
        cw = session.client("cloudwatch")
        res = boto_call(
            lambda _c=cw: _c.get_paginator("list_metrics").paginate().build_full_result()
        )
        fetches[key] = res
        if res.ok:
            results[key] = {"metrics": res.data.get("Metrics", [])}
            ev.write(key, results[key], source_api=f"GET cloudwatch:ListMetrics ({region})")
        else:
            print(f"  WARN {key}: {res.error}")
    return fetches


def collect_log_groups(
    profile: str | None,
    regions: list[str],
    ev: EvidenceWriter,
    results: dict[str, Any],
) -> dict[str, BotoResult]:
    fetches: dict[str, BotoResult] = {}
    for region in regions:
        key = f"log_groups_{safe_region(region)}"
        print(f"collecting log groups ({region})")
        session = boto3_session(profile, region)
        logs = session.client("logs")
        res = boto_call(
            lambda _l=logs: _l.get_paginator("describe_log_groups").paginate().build_full_result()
        )
        fetches[key] = res
        if res.ok:
            results[key] = {"log_groups": res.data.get("logGroups", [])}
            ev.write(key, results[key], source_api=f"GET logs:DescribeLogGroups ({region})")
        else:
            print(f"  WARN {key}: {res.error}")
    return fetches


def collect_alarms(
    profile: str | None,
    regions: list[str],
    ev: EvidenceWriter,
    results: dict[str, Any],
) -> dict[str, BotoResult]:
    fetches: dict[str, BotoResult] = {}
    for region in regions:
        key = f"alarms_{safe_region(region)}"
        print(f"collecting alarms ({region})")
        session = boto3_session(profile, region)
        cw = session.client("cloudwatch")
        res = boto_call(
            lambda _c=cw: _c.get_paginator("describe_alarms")
            .paginate(AlarmTypes=["MetricAlarm", "CompositeAlarm"])
            .build_full_result()
        )
        fetches[key] = res
        if res.ok:
            metric_alarms = res.data.get("MetricAlarms", [])
            composite_alarms = res.data.get("CompositeAlarms", [])
            results[key] = {"alarms": metric_alarms + composite_alarms}
            ev.write(key, results[key], source_api=f"GET cloudwatch:DescribeAlarms ({region})")
        else:
            print(f"  WARN {key}: {res.error}")
    return fetches


def collect_dashboards(
    profile: str | None,
    region: str,
    ev: EvidenceWriter,
    results: dict[str, Any],
) -> BotoResult:
    print("collecting dashboards")
    session = boto3_session(profile, region)
    cw = session.client("cloudwatch")
    res = boto_call(
        lambda: cw.get_paginator("list_dashboards").paginate().build_full_result()
    )
    if res.ok:
        results["dashboards"] = {"entries": res.data.get("DashboardEntries", [])}
        ev.write("dashboards", results["dashboards"], source_api="GET cloudwatch:ListDashboards")
    else:
        print(f"  WARN dashboards: {res.error}")
    return res


def collect_metric_streams(
    profile: str | None,
    region: str,
    ev: EvidenceWriter,
    results: dict[str, Any],
) -> BotoResult:
    print("collecting metric streams")
    session = boto3_session(profile, region)
    cw = session.client("cloudwatch")
    entries: list[Any] = []
    res = boto_call(lambda: cw.list_metric_streams())
    if not res.ok:
        print(f"  WARN metric_streams: {res.error}")
        return res
    entries.extend(res.data.get("Entries", []))
    while res.ok and res.data.get("NextToken"):
        token = res.data["NextToken"]
        res = boto_call(lambda _t=token: cw.list_metric_streams(NextToken=_t))
        if res.ok:
            entries.extend(res.data.get("Entries", []))
    results["metric_streams"] = {"entries": entries}
    ev.write(
        "metric_streams",
        results["metric_streams"],
        source_api="GET cloudwatch:ListMetricStreams",
    )
    return BotoResult(ok=True, data={"Entries": entries})


def collect_cost_ce(
    profile: str | None,
    lookback_days: float,
    ev: EvidenceWriter,
    results: dict[str, Any],
) -> BotoResult:
    print("collecting cost (Cost Explorer)")
    session = boto3_session(profile, "us-east-1")
    ce = session.client("ce")
    now = datetime.now(UTC)
    # go back 3 months to get at least one full billed month
    start = (now - timedelta(days=max(lookback_days, 90))).strftime("%Y-%m-01")
    end = now.strftime("%Y-%m-%d")
    res = boto_call(
        lambda: ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="MONTHLY",
            Metrics=["BlendedCost"],
            Filter={
                "Dimensions": {"Key": "SERVICE", "Values": [CE_SERVICE]},
            },
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        )
    )
    if res.ok:
        results["ce_cost"] = res.data
        ev.write(
            "ce_cost",
            res.data,
            source_api="GET ce:GetCostAndUsage (MONTHLY, BlendedCost, SERVICE=AmazonCloudWatch)",
        )
    else:
        print(f"  WARN ce_cost: {res.error}")
    return res


def collect_log_ingest_ce(
    profile: str | None,
    lookback_days: float,
    ev: EvidenceWriter,
    results: dict[str, Any],
) -> BotoResult:
    print("collecting log ingestion (Cost Explorer)")
    session = boto3_session(profile, "us-east-1")
    ce = session.client("ce")
    now = datetime.now(UTC)
    start = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    end = now.strftime("%Y-%m-%d")
    res = boto_call(
        lambda: ce.get_cost_and_usage(
            TimePeriod={"Start": start, "End": end},
            Granularity="DAILY",
            Metrics=["UsageQuantity"],
            Filter={
                "Dimensions": {"Key": "SERVICE", "Values": [CE_SERVICE]},
            },
            GroupBy=[{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        )
    )
    if res.ok:
        results["ce_log_ingest"] = res.data
        ev.write(
            "ce_log_ingest",
            res.data,
            source_api="GET ce:GetCostAndUsage (DAILY, UsageQuantity, SERVICE=AmazonCloudWatch)",
        )
    else:
        print(f"  WARN ce_log_ingest: {res.error}")
    return res


def collect_log_group_breakdown(
    profile: str | None,
    candidates: list[dict[str, Any]],
    lookback_days: float,
    ev: EvidenceWriter,
    results: dict[str, Any],
) -> None:
    """Fetch per-group ingestion for the top candidates by storedBytes."""
    print(f"collecting log group breakdown ({len(candidates)} candidates)")
    now = datetime.now(UTC)
    start = now - timedelta(days=lookback_days)

    def fetch_one(candidate: dict[str, Any]) -> dict[str, Any]:
        name = candidate["name"]
        region = candidate["region"]
        session = boto3_session(profile, region)
        cw = session.client("cloudwatch")
        res = boto_call(
            lambda: cw.get_metric_statistics(
                Namespace="AWS/Logs",
                MetricName="IncomingBytes",
                Dimensions=[{"Name": "LogGroupName", "Value": name}],
                StartTime=start,
                EndTime=now,
                Period=86400,
                Statistics=["Sum"],
            )
        )
        total_bytes = 0.0
        if res.ok:
            for dp in res.data.get("Datapoints", []):
                total_bytes += dp.get("Sum", 0.0)
        return {
            "name": name,
            "region": region,
            "total_bytes": total_bytes,
            "daily_avg_bytes": total_bytes / max(1, lookback_days),
        }

    with ThreadPoolExecutor(max_workers=5) as pool:
        group_results = list(pool.map(fetch_one, candidates))

    group_results.sort(key=lambda g: -g["total_bytes"])
    top = group_results[:20]
    results["log_group_breakdown"] = {"groups": top}
    ev.write(
        "log_group_breakdown",
        results["log_group_breakdown"],
        source_api="GET cloudwatch:GetMetricStatistics (AWS/Logs IncomingBytes per group)",
    )


# ── derivation ──────────────────────────────────────────────────────────


def all_region_items(results: dict[str, Any], prefix: str, list_key: str) -> list[Any]:
    items: list[Any] = []
    for key, val in results.items():
        if key.startswith(prefix) and isinstance(val, dict):
            items.extend(val.get(list_key, []))
    return items


def region_status(
    fetches: dict[str, BotoResult], prefix: str, regions: list[str]
) -> tuple[str, list[str]]:
    """Return (status, failed_regions) based on which regions succeeded."""
    failed = []
    for r in regions:
        key = f"{prefix}{safe_region(r)}"
        res = fetches.get(key)
        if res is not None and not res.ok:
            failed.append(r)
    if len(failed) == len(regions):
        return "unavailable", failed
    if failed:
        return "partial", failed
    return "ok", []


def derive_metrics(
    results: dict[str, Any],
    fetches: dict[str, BotoResult],
    summary: SummaryWriter,
    regions: list[str],
) -> None:
    status, failed = region_status(fetches, "metrics_", regions)
    all_metrics = all_region_items(results, "metrics_", "metrics")

    if not all_metrics and status == "unavailable":
        res = next(
            (fetches[f"metrics_{safe_region(r)}"] for r in regions
             if fetches.get(f"metrics_{safe_region(r)}")),
            None,
        )
        reason = res.gap_reason if res and res.gap_reason else "api_error"
        detail = res.error if res and res.error else "ListMetrics failed in all regions"
        summary.mark_unavailable("metrics.total_count", reason, detail)
        summary.mark_unavailable("metrics.custom_metrics_count", reason, detail)
        return

    notes = f"failed regions: {', '.join(failed)}" if failed else None
    evidence = [
        f"evidence/metrics_{safe_region(r)}.json"
        for r in regions
        if results.get(f"metrics_{safe_region(r)}")
    ]
    regions_collected = [r for r in regions if results.get(f"metrics_{safe_region(r)}")]

    total = len(all_metrics)
    custom = sum(1 for m in all_metrics if not m.get("Namespace", "").startswith("AWS/"))

    summary.add_figure(
        Figure(
            id="metrics.total_count",
            label="CloudWatch metrics (total)",
            value=float(total),
            unit="metrics",
            status=status,
            method=f"count of ListMetrics results across {len(regions_collected)} region(s)",
            source_api="cloudwatch:ListMetrics (paginated)",
            evidence_files=evidence,
            notes=notes,
        )
    )
    summary.add_figure(
        Figure(
            id="metrics.custom_metrics_count",
            label="CloudWatch custom metrics",
            value=float(custom),
            unit="metrics",
            status=status,
            method="count of metrics with namespace not starting with AWS/",
            source_api="cloudwatch:ListMetrics (paginated)",
            evidence_files=evidence,
            notes=notes,
        )
    )


def derive_logs(
    results: dict[str, Any],
    fetches: dict[str, BotoResult],
    summary: SummaryWriter,
    regions: list[str],
) -> None:
    status, failed = region_status(fetches, "log_groups_", regions)
    all_groups = all_region_items(results, "log_groups_", "log_groups")

    if not all_groups and status == "unavailable":
        res = next(
            (fetches[f"log_groups_{safe_region(r)}"] for r in regions
             if fetches.get(f"log_groups_{safe_region(r)}")),
            None,
        )
        reason = res.gap_reason if res and res.gap_reason else "api_error"
        detail = res.error if res and res.error else "DescribeLogGroups failed in all regions"
        summary.mark_unavailable("logs.stored_gb", reason, detail)
        summary.mark_unavailable("cloudwatch.log_groups_count", reason, detail)
        return

    notes = f"failed regions: {', '.join(failed)}" if failed else None
    evidence = [
        f"evidence/log_groups_{safe_region(r)}.json"
        for r in regions
        if results.get(f"log_groups_{safe_region(r)}")
    ]

    stored_bytes = sum(g.get("storedBytes", 0) for g in all_groups)
    summary.add_figure(
        Figure(
            id="logs.stored_gb",
            label="CloudWatch Logs stored",
            value=round(stored_bytes / 1e9, 2),
            unit="GB",
            status=status,
            method="sum of storedBytes across all log groups",
            source_api="logs:DescribeLogGroups (paginated)",
            evidence_files=evidence,
            notes=notes,
        )
    )
    summary.add_figure(
        Figure(
            id="cloudwatch.log_groups_count",
            label="Log Groups",
            value=float(len(all_groups)),
            unit="log groups",
            status=status,
            method="count of log groups across all regions",
            source_api="logs:DescribeLogGroups (paginated)",
            evidence_files=evidence,
            notes=notes,
        )
    )

    # retention distribution for inventory
    retention_dist: dict[str, int] = {}
    for g in all_groups:
        ret = g.get("retentionInDays")
        label = f"{ret}d" if ret else "never expire"
        retention_dist[label] = retention_dist.get(label, 0) + 1
    summary.inventory["retention_distribution"] = dict(
        sorted(retention_dist.items(), key=lambda kv: -kv[1])
    )


def derive_log_ingest(
    results: dict[str, Any],
    fetches: dict[str, BotoResult],
    summary: SummaryWriter,
    lookback_days: float,
) -> None:
    data = results.get("ce_log_ingest")
    if not data:
        res = fetches.get("ce_log_ingest")
        reason = res.gap_reason if res and res.gap_reason else "api_error"
        detail = res.error if res and res.error else "Cost Explorer log ingest query failed"
        summary.mark_unavailable(
            "logs.ingest_gb_per_day",
            reason,
            detail,
            remediation="attach ce:GetCostAndUsage IAM permission",
        )
        return

    total_bytes = 0.0
    all_days = {
        period.get("TimePeriod", {}).get("Start", "")
        for period in data.get("ResultsByTime", [])
        if period.get("TimePeriod", {}).get("Start")
    }
    for period in data.get("ResultsByTime", []):
        for group in period.get("Groups", []):
            usage_type = group.get("Keys", [""])[0]
            if LOG_INGEST_USAGE_SUBSTR in usage_type:
                qty = float(group.get("Metrics", {}).get("UsageQuantity", {}).get("Amount", "0"))
                total_bytes += qty
    num_days = len(all_days) or lookback_days
    if total_bytes == 0:
        summary.add_figure(
            Figure(
                id="logs.ingest_gb_per_day",
                label="CloudWatch Logs ingestion",
                value=0.0,
                unit="GB/day",
                status="ok",
                method="CE UsageQuantity for DataProcessing-Bytes = 0 over lookback window",
                source_api="ce:GetCostAndUsage (DAILY, UsageQuantity, SERVICE=AmazonCloudWatch)",
                evidence_files=["evidence/ce_log_ingest.json"],
            )
        )
        return

    gb_per_day = total_bytes / 1e9 / num_days
    summary.add_figure(
        Figure(
            id="logs.ingest_gb_per_day",
            label="CloudWatch Logs ingestion",
            value=round(gb_per_day, 2),
            unit="GB/day",
            status="ok",
            method=f"CE DataProcessing-Bytes UsageQuantity / {num_days} days, bytes -> GB (1e9)",
            source_api="ce:GetCostAndUsage (DAILY, UsageQuantity, SERVICE=AmazonCloudWatch)",
            query=f"SERVICE={CE_SERVICE}, USAGE_TYPE contains {LOG_INGEST_USAGE_SUBSTR}",
            time_window={
                "start": (datetime.now(UTC) - timedelta(days=lookback_days)).strftime(
                    "%Y-%m-%dT00:00:00Z"
                ),
                "end": datetime.now(UTC).strftime("%Y-%m-%dT00:00:00Z"),
            },
            evidence_files=["evidence/ce_log_ingest.json"],
        )
    )


def derive_alarms(
    results: dict[str, Any],
    fetches: dict[str, BotoResult],
    summary: SummaryWriter,
    regions: list[str],
) -> None:
    status, failed = region_status(fetches, "alarms_", regions)
    all_alarms = all_region_items(results, "alarms_", "alarms")

    if not all_alarms and status == "unavailable":
        res = next(
            (fetches[f"alarms_{safe_region(r)}"] for r in regions
             if fetches.get(f"alarms_{safe_region(r)}")),
            None,
        )
        reason = res.gap_reason if res and res.gap_reason else "api_error"
        detail = res.error if res and res.error else "DescribeAlarms failed in all regions"
        summary.mark_unavailable("alerts.monitor_count", reason, detail)
        return

    notes = f"failed regions: {', '.join(failed)}" if failed else None
    evidence = [
        f"evidence/alarms_{safe_region(r)}.json"
        for r in regions
        if results.get(f"alarms_{safe_region(r)}")
    ]

    summary.add_figure(
        Figure(
            id="alerts.monitor_count",
            label="CloudWatch Alarms",
            value=float(len(all_alarms)),
            unit="alarms",
            status=status,
            method="count of MetricAlarm + CompositeAlarm across all regions",
            source_api="cloudwatch:DescribeAlarms (paginated)",
            evidence_files=evidence,
            notes=notes,
        )
    )


def derive_cost(
    results: dict[str, Any],
    fetches: dict[str, BotoResult],
    summary: SummaryWriter,
) -> None:
    data = results.get("ce_cost")
    if not data:
        res = fetches.get("ce_cost")
        reason = res.gap_reason if res and res.gap_reason else "api_error"
        detail = res.error if res and res.error else "Cost Explorer cost query failed"
        summary.mark_unavailable(
            "cost.monthly_usd",
            reason,
            detail,
            remediation="attach ce:GetCostAndUsage IAM permission",
        )
        return

    now = datetime.now(UTC)
    cur_month = now.strftime("%Y-%m")

    # parse per-month totals and per-usage-type breakdown
    months: dict[str, dict[str, Any]] = {}
    for period in data.get("ResultsByTime", []):
        month = period.get("TimePeriod", {}).get("Start", "")[:7]
        if not month:
            continue
        entry = months.setdefault(month, {"total": 0.0, "usage_types": []})
        for group in period.get("Groups", []):
            usage_type = group.get("Keys", [""])[0]
            amount = float(
                group.get("Metrics", {}).get("BlendedCost", {}).get("Amount", "0")
            )
            entry["total"] += amount
            if amount > 0:
                entry["usage_types"].append(
                    {"usage_type": usage_type, "amount_usd": round(amount, 2)}
                )

    # prefer last full month; fall back to current month (estimated)
    sorted_months = sorted(months.keys())
    full_months = [m for m in sorted_months if m < cur_month and months[m]["total"] > 0]

    if full_months:
        pick = full_months[-1]
        m = months[pick]
        summary.add_figure(
            Figure(
                id="cost.monthly_usd",
                label=f"CloudWatch cost ({pick})",
                value=round(m["total"], 2),
                unit="USD",
                status="ok",
                method=f"Cost Explorer BlendedCost for SERVICE={CE_SERVICE}, month {pick} "
                "(same data as the AWS billing console)",
                source_api="ce:GetCostAndUsage (MONTHLY, BlendedCost, SERVICE=AmazonCloudWatch)",
                evidence_files=["evidence/ce_cost.json"],
            )
        )
        summary.inventory["cost_by_usage_type"] = sorted(
            m["usage_types"], key=lambda x: -x["amount_usd"]
        )
        summary.inventory["cost_breakdown_month"] = pick
    elif cur_month in months and months[cur_month]["total"] > 0:
        m = months[cur_month]
        summary.add_figure(
            Figure(
                id="cost.monthly_usd",
                label=f"CloudWatch cost ({cur_month}, month to date)",
                value=round(m["total"], 2),
                unit="USD",
                status="estimated",
                method=f"Cost Explorer month-to-date for {cur_month}; no full prior month "
                "available yet",
                source_api="ce:GetCostAndUsage (MONTHLY, BlendedCost, SERVICE=AmazonCloudWatch)",
                evidence_files=["evidence/ce_cost.json"],
            )
        )
        summary.inventory["cost_by_usage_type"] = sorted(
            m["usage_types"], key=lambda x: -x["amount_usd"]
        )
        summary.inventory["cost_breakdown_month"] = cur_month
    else:
        summary.mark_unavailable(
            "cost.monthly_usd",
            "not_configured",
            "Cost Explorer returned no CloudWatch cost data for the queried window",
            remediation="verify the account has CloudWatch usage and ce:GetCostAndUsage permission",
        )


def build_summary(
    results: dict[str, Any],
    fetches: dict[str, BotoResult],
    summary: SummaryWriter,
    regions: list[str],
    lookback_days: float,
) -> None:
    derive_metrics(results, fetches, summary, regions)
    derive_logs(results, fetches, summary, regions)
    derive_log_ingest(results, fetches, summary, lookback_days)
    derive_alarms(results, fetches, summary, regions)
    derive_cost(results, fetches, summary)

    inv = summary.inventory
    inv["regions_collected"] = regions

    # dashboards
    dash = results.get("dashboards") or {}
    inv["dashboards_count"] = len(dash.get("entries", []))

    # metric streams
    streams = results.get("metric_streams") or {}
    inv["metric_streams_count"] = len(streams.get("entries", []))

    # namespace breakdown (top 30)
    all_metrics = all_region_items(results, "metrics_", "metrics")
    ns_counts: dict[str, int] = {}
    for m in all_metrics:
        ns = m.get("Namespace", "unknown")
        ns_counts[ns] = ns_counts.get(ns, 0) + 1
    inv["namespace_breakdown"] = dict(
        sorted(ns_counts.items(), key=lambda kv: -kv[1])[:30]
    )

    # log group breakdown (opt-in)
    breakdown = results.get("log_group_breakdown")
    if breakdown:
        inv["log_group_top20"] = [
            {
                "name": g["name"],
                "region": g["region"],
                "ingest_gb_per_day": round(g["daily_avg_bytes"] / 1e9, 3),
            }
            for g in breakdown.get("groups", [])[:20]
        ]

    # cross-check: CE DataProcessing-Bytes vs summed top-group IncomingBytes
    if breakdown and results.get("ce_log_ingest"):
        ce_total = 0.0
        for period in results["ce_log_ingest"].get("ResultsByTime", []):
            for group in period.get("Groups", []):
                usage_type = group.get("Keys", [""])[0]
                if LOG_INGEST_USAGE_SUBSTR in usage_type:
                    ce_total += float(
                        group.get("Metrics", {}).get("UsageQuantity", {}).get("Amount", "0")
                    )
        top_sum = sum(g["total_bytes"] for g in breakdown.get("groups", []))
        if top_sum > ce_total * 1.05 and ce_total > 0:
            inv["log_group_breakdown_note"] = (
                f"WARNING: top-group IncomingBytes sum ({top_sum / 1e9:.1f} GB) exceeds "
                f"CE DataProcessing-Bytes ({ce_total / 1e9:.1f} GB) by "
                f"{(top_sum / ce_total - 1) * 100:.0f}% — possible derivation mismatch"
            )


# ── main ────────────────────────────────────────────────────────────────


def main() -> int:
    parser = base_parser("AWS CloudWatch discovery collector", default_lookback="30d")
    parser.add_argument("--profile", help="AWS CLI / boto3 profile name")
    parser.add_argument(
        "--region",
        action="append",
        default=[],
        help="AWS region to collect (repeatable; default: configured region or us-east-1)",
    )
    parser.add_argument(
        "--all-regions",
        action="store_true",
        help="Collect from all opted-in regions",
    )
    parser.add_argument(
        "--log-group-breakdown",
        action="store_true",
        help="Fetch per-group ingestion for top log groups (uses GetMetricStatistics)",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ev = EvidenceWriter(args.output_dir)
    results: dict[str, Any] = {}
    fetches: dict[str, BotoResult] = {}
    lookback_days = parse_duration_days(args.lookback)

    if args.report_only:
        results = ev.load_all()
        if not results:
            print(f"ERROR: --report-only but no evidence under {ev.evidence_dir}")
            return 2
        # recover regions from evidence keys
        prefix = "metrics_"
        regions = sorted({
            k[len(prefix):].replace("_", "-")
            for k in results
            if k.startswith(prefix)
        }) or ["us-east-1"]
    else:
        regions = resolve_regions(args.profile, args.region, args.all_regions)
        print(f"regions: {', '.join(regions)}")

        fetches.update(collect_metrics(args.profile, regions, ev, results))
        fetches.update(collect_log_groups(args.profile, regions, ev, results))
        fetches.update(collect_alarms(args.profile, regions, ev, results))
        collect_dashboards(args.profile, regions[0], ev, results)
        collect_metric_streams(args.profile, regions[0], ev, results)
        fetches["ce_cost"] = collect_cost_ce(args.profile, lookback_days, ev, results)
        fetches["ce_log_ingest"] = collect_log_ingest_ce(
            args.profile, lookback_days, ev, results
        )

        if args.log_group_breakdown:
            candidates = sorted(
                [
                    {
                        "name": g.get("logGroupName", ""),
                        "region": r,
                        "storedBytes": g.get("storedBytes", 0),
                    }
                    for r in regions
                    for g in (
                        results.get(f"log_groups_{safe_region(r)}") or {}
                    ).get("log_groups", [])
                ],
                key=lambda g: -g["storedBytes"],
            )[:100]
            if candidates:
                collect_log_group_breakdown(
                    args.profile, candidates, lookback_days, ev, results
                )

    summary = SummaryWriter(
        collector=COLLECTOR,
        collector_version=VERSION,
        expected=EXPECTED,
        target=f"aws-account (regions: {', '.join(regions)})",
        lookback=args.lookback,
        args_redacted={
            "profile": args.profile or "default",
            "regions": regions,
            "lookback": args.lookback,
            "log_group_breakdown": args.log_group_breakdown,
        },
    )
    summary.environment = {
        "detected_backend": "aws-cloudwatch",
        "version": None,
        "detection_method": "boto3 session / region discovery",
        "regions": regions,
    }
    build_summary(results, fetches, summary, regions, lookback_days)
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
