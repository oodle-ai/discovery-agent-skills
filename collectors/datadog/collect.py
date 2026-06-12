# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Datadog discovery collector.

Collects inventory and usage figures from the same Datadog APIs that back the
account's own Usage & Cost pages, writes redacted raw responses to
evidence/, and emits summary.json (see schemas/summary.schema.json).

Examples:
    uv run collectors/datadog/collect.py --output-dir ./discovery-output/datadog
    DD_API_KEY=... DD_APP_KEY=... uv run collectors/datadog/collect.py \\
        --site us5 --lookback 30d --output-dir ./discovery-output/datadog
    uv run collectors/datadog/collect.py --report-only --output-dir ./discovery-output/datadog

Figure <-> API mapping is documented in collectors/datadog/README.md.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.auth import datadog_headers  # noqa: E402
from lib.cli import base_parser, credential, parse_duration_days, parse_headers  # noqa: E402
from lib.evidence import EvidenceWriter  # noqa: E402
from lib.http import FetchResult, HttpClient  # noqa: E402
from lib.summary import ExpectedFigure, Figure, SummaryWriter  # noqa: E402

COLLECTOR = "datadog"
VERSION = "2.0.0"

DD_SITES: dict[str, str] = {
    "us1": "datadoghq.com",
    "us3": "us3.datadoghq.com",
    "us5": "us5.datadoghq.com",
    "eu1": "datadoghq.eu",
    "ap1": "ap1.datadoghq.com",
    "gov": "ddog-gov.com",
}

# Hourly-usage product families and the usage_type measurements we read.
HOURLY_FAMILIES = ["infra_hosts", "logs", "ingested_spans", "indexed_spans", "rum", "timeseries"]

# Datadog public list prices (USD, on-demand) used ONLY for the fallback
# estimate when the estimated_cost API is not accessible (needs billing
# read). Versioned so the report can state which price sheet was assumed;
# actual contract pricing usually differs.
LIST_PRICES_VERSION = "2026-06"
LIST_PRICES = {
    "infra_host_month": 18.0,
    "custom_metric_month": 0.05,  # per custom metric beyond the per-host allocation
    "custom_metrics_per_host_allocation": 100,
    "logs_ingest_gb": 0.10,
    "logs_indexed_million_events": 1.70,  # 7-day retention tier
    "apm_ingest_gb": 0.10,
    "rum_1k_sessions": 1.50,
}

EXPECTED = [
    ExpectedFigure("hosts.count", "Active hosts", "hosts", "hosts"),
    ExpectedFigure("metrics.total_count", "Active metric names", "metrics", "metrics"),
    ExpectedFigure("metrics.custom_metrics_count", "Custom metrics (avg)", "metrics", "metrics"),
    ExpectedFigure("logs.ingest_gb_per_day", "Log ingestion", "GB/day", "logs"),
    ExpectedFigure(
        "datadog.logs_indexed_events_per_day", "Indexed log events", "events/day", "logs"
    ),
    ExpectedFigure("traces.ingest_gb_per_day", "Trace ingestion", "GB/day", "traces"),
    ExpectedFigure("datadog.rum_sessions_per_day", "RUM sessions", "sessions/day", "datadog"),
    ExpectedFigure("alerts.monitor_count", "Monitors", "monitors", "alerts"),
    ExpectedFigure("cost.monthly_usd", "Datadog estimated cost (month to date)", "USD", "cost"),
]


def resolve_site(site: str) -> str:
    return DD_SITES.get(site, site)


# ── collection ───────────────────────────────────────────────────────────


def fetch_simple(
    client: HttpClient, ev: EvidenceWriter, results: dict[str, Any]
) -> dict[str, FetchResult]:
    endpoints = {
        "hosts_totals": "/api/v1/hosts/totals",
        "dashboards": "/api/v1/dashboard",
        "synthetics_tests": "/api/v1/synthetics/tests",
        "notebooks": "/api/v1/notebooks",
        "logs_pipelines": "/api/v1/logs/config/pipelines",
        "logs_indexes": "/api/v1/logs/config/indexes",
    }
    fetches: dict[str, FetchResult] = {}
    for name, path in endpoints.items():
        print(f"collecting {name}")
        res = client.get_json(path)
        fetches[name] = res
        if res.ok:
            results[name] = res.data
            ev.write(name, res.data, source_api=f"GET {path}")
        else:
            print(f"  WARN {name}: {res.error}")
    return fetches


def fetch_monitors(client: HttpClient, ev: EvidenceWriter, results: dict[str, Any]) -> FetchResult:
    print("collecting monitors")
    all_monitors: list[Any] = []
    page, page_size = 0, 100
    last: FetchResult | None = None
    while True:
        res = client.get_json(
            "/api/v1/monitor", params={"page": str(page), "page_size": str(page_size)}
        )
        last = res
        if not res.ok:
            if page == 0:
                return res
            break
        if not isinstance(res.data, list):
            break
        all_monitors.extend(res.data)
        if len(res.data) < page_size:
            break
        page += 1
    results["monitors"] = all_monitors
    ev.write("monitors", all_monitors, source_api="GET /api/v1/monitor (paginated)")
    return FetchResult(ok=True, data=all_monitors, status_code=last.status_code if last else None)


def fetch_slos(client: HttpClient, ev: EvidenceWriter, results: dict[str, Any]) -> None:
    print("collecting slos")
    all_slos: list[Any] = []
    offset, limit = 0, 1000
    while True:
        res = client.get_json("/api/v1/slo", params={"limit": str(limit), "offset": str(offset)})
        if not res.ok:
            if offset == 0:
                print(f"  WARN slos: {res.error}")
                return
            break
        slos = res.data.get("data", [])
        all_slos.extend(slos)
        total = res.data.get("metadata", {}).get("total_count")
        if (total is not None and len(all_slos) >= total) or len(slos) < limit:
            break
        offset += limit
    results["slos"] = {"data": all_slos}
    ev.write("slos", results["slos"], source_api="GET /api/v1/slo (paginated)")


def fetch_hourly_usage(
    client: HttpClient,
    families: list[str],
    start_hr: str,
    end_hr: str,
    ev: EvidenceWriter,
    results: dict[str, Any],
) -> dict[str, FetchResult]:
    """Fetch v2 hourly usage for all product families in ONE call, paginated.

    Two fixes over the oodlectl version:
    - pagination via meta.pagination.next_record_id (responses cap at 500
      records; a 30d window is 720+ hours per family)
    - a single comma-separated product_families filter instead of one call
      per family: this endpoint costs ~3s server-side per call regardless of
      payload, so 6 sequential calls dominated the collector's runtime
    """
    families_param = ",".join(families)
    print(f"collecting hourly usage ({families_param})")
    records: list[Any] = []
    next_id: str | None = None
    first_error: FetchResult | None = None
    for _ in range(100):  # hard page cap
        params = {
            "filter[product_families]": families_param,
            "filter[timestamp][start]": start_hr,
            "filter[timestamp][end]": end_hr,
        }
        if next_id:
            params["page[next_record_id]"] = next_id
        res = client.get_json("/api/v2/usage/hourly_usage", params=params)
        if not res.ok:
            first_error = res
            break
        records.extend(res.data.get("data", []))
        next_id = (
            res.data.get("meta", {}).get("pagination", {}).get("next_record_id")
        )
        if not next_id:
            break
    if not records and first_error is not None:
        print(f"  WARN hourly usage: {first_error.error}")
        return {f"usage_hourly_{f}": first_error for f in families}
    by_family: dict[str, list[Any]] = {f: [] for f in families}
    for rec in records:
        fam = rec.get("attributes", {}).get("product_family")
        if fam in by_family:
            by_family[fam].append(rec)
    fetches: dict[str, FetchResult] = {}
    for fam in families:
        key = f"usage_hourly_{fam}"
        payload = {"data": by_family[fam]}
        results[key] = payload
        ev.write(
            key,
            payload,
            source_api="GET /api/v2/usage/hourly_usage"
            f"?filter[product_families]={families_param} (paginated, split by family)",
        )
        fetches[key] = FetchResult(ok=True, data=payload)
    return fetches


def fetch_usage_summary(
    client: HttpClient,
    now: datetime,
    lookback_days: float,
    ev: EvidenceWriter,
    results: dict[str, Any],
) -> None:
    start = now - timedelta(days=max(lookback_days, 28))
    params = {"start_month": start.strftime("%Y-%m"), "end_month": now.strftime("%Y-%m")}
    print("collecting usage_summary")
    res = client.get_json("/api/v1/usage/summary", params=params)
    if res.ok:
        results["usage_summary"] = res.data
        ev.write("usage_summary", res.data, source_api="GET /api/v1/usage/summary")
    else:
        print(f"  WARN usage_summary: {res.error}")


def fetch_estimated_cost(
    client: HttpClient, now: datetime, ev: EvidenceWriter, results: dict[str, Any]
) -> FetchResult:
    """Current month-to-date cost. This endpoint only serves the current
    month (verified empirically: past start_month returns empty data)."""
    print("collecting estimated_cost")
    res = client.get_json(
        "/api/v2/usage/estimated_cost",
        params={"view": "summary", "start_month": now.strftime("%Y-%m")},
    )
    if res.ok:
        results["estimated_cost"] = res.data
        ev.write("estimated_cost", res.data, source_api="GET /api/v2/usage/estimated_cost")
    else:
        print(f"  WARN estimated_cost: {res.error}")
    return res


def fetch_historical_cost(
    client: HttpClient, now: datetime, ev: EvidenceWriter, results: dict[str, Any]
) -> FetchResult:
    """Past billed months come from historical_cost, not estimated_cost.
    May legitimately be empty early in a month (previous month not yet
    finalized) or for young orgs."""
    print("collecting historical_cost")
    prev_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
    res = client.get_json(
        "/api/v2/usage/historical_cost",
        params={"view": "summary", "start_month": prev_month},
    )
    if res.ok:
        results["historical_cost"] = res.data
        ev.write("historical_cost", res.data, source_api="GET /api/v2/usage/historical_cost")
    else:
        print(f"  WARN historical_cost: {res.error}")
    return res


def fetch_metrics_list(
    client: HttpClient, now: datetime, ev: EvidenceWriter, results: dict[str, Any]
) -> FetchResult:
    print("collecting metrics_list")
    from_ts = int((now - timedelta(hours=2)).timestamp())
    res = client.get_json("/api/v1/metrics", params={"from": str(from_ts)})
    if res.ok:
        results["metrics_list"] = res.data
        ev.write("metrics_list", res.data, source_api="GET /api/v1/metrics?from=<2h ago>")
    return res


# ── derivation (deterministic; every output figure is computed here) ────


def hourly_values(data: dict | None, usage_type: str) -> list[tuple[str, float]]:
    """(timestamp, value) pairs for one usage_type from a v2 hourly response."""
    if not data:
        return []
    out: list[tuple[str, float]] = []
    for entry in data.get("data", []):
        attrs = entry.get("attributes", {})
        ts = attrs.get("timestamp", "")
        for m in attrs.get("measurements", []):
            if m.get("usage_type") == usage_type and m.get("value") is not None:
                out.append((ts, float(m["value"])))
    return sorted(out)


def window_of(values: list[tuple[str, float]]) -> dict[str, str] | None:
    if not values:
        return None
    return {"start": values[0][0], "end": values[-1][0]}


def covered_days(values: list[tuple[str, float]]) -> float:
    """Days covered, counted from distinct hourly samples (robust to gaps)."""
    return len({ts for ts, _ in values}) / 24.0


def per_day(values: list[tuple[str, float]]) -> float | None:
    days = covered_days(values)
    if days <= 0:
        return None
    return sum(v for _, v in values) / days


def avg(values: list[tuple[str, float]]) -> float | None:
    if not values:
        return None
    return sum(v for _, v in values) / len(values)


def add_hourly_figure(
    summary: SummaryWriter,
    results: dict[str, Any],
    fetches: dict[str, FetchResult],
    figure_id: str,
    family: str,
    usage_type: str,
    transform: str,  # "per_day_gb" | "per_day" | "avg"
    remediation: str | None = None,
) -> None:
    """Derive one figure from an hourly-usage family, or record the gap."""
    exp = summary.expected[figure_id]
    key = f"usage_hourly_{family}"
    data = results.get(key)
    values = hourly_values(data, usage_type)
    if not values:
        res = fetches.get(key)
        reason = res.gap_reason if res is not None and res.gap_reason else "not_configured"
        detail = (
            res.error
            if res is not None and res.error
            else f"no '{usage_type}' measurements in {family} hourly usage "
            f"(product likely not in use)"
        )
        summary.mark_unavailable(figure_id, reason, detail, remediation)
        return
    if transform == "per_day_gb":
        value = per_day(values)
        value = value / 1e9 if value is not None else None
        method = f"sum of hourly '{usage_type}' / days covered, bytes -> GB (1e9)"
    elif transform == "per_day":
        value = per_day(values)
        method = f"sum of hourly '{usage_type}' / days covered"
    else:
        value = avg(values)
        method = f"average of hourly '{usage_type}' samples"
    if value is None:
        summary.mark_unavailable(figure_id, "not_configured", "empty hourly usage window")
        return
    summary.add_figure(
        Figure(
            id=figure_id,
            label=exp.label,
            value=round(value, 2),
            unit=exp.unit,
            status="ok",
            method=method,
            source_api="GET /api/v2/usage/hourly_usage"
            f"?filter[product_families]={','.join(HOURLY_FAMILIES)}",
            query=f"product_family={family}, usage_type={usage_type}",
            time_window=window_of(values),
            evidence_files=[f"evidence/{key}.json"],
        )
    )


def estimate_cost_from_usage(results: dict[str, Any]) -> tuple[float, list[dict[str, Any]]]:
    """Fallback: estimate monthly cost from collected usage x public list prices.

    Used only when the estimated_cost API is inaccessible. Every component
    states its basis; the figure is emitted as status=estimated.
    """
    p = LIST_PRICES
    comps: list[dict[str, Any]] = []

    def comp(product: str, basis: str, monthly: float) -> None:
        if monthly > 0:
            comps.append({"product": product, "basis": basis, "monthly_usd": round(monthly, 2)})

    hosts = avg(hourly_values(results.get("usage_hourly_infra_hosts"), "host_count"))
    if hosts:
        comp("infra_host", f"{hosts:.0f} avg hosts x ${p['infra_host_month']}/host/mo",
             hosts * p["infra_host_month"])
    custom = avg(hourly_values(results.get("usage_hourly_timeseries"), "num_custom_timeseries"))
    if custom:
        allocation = (hosts or 0) * p["custom_metrics_per_host_allocation"]
        billable = max(0.0, custom - allocation)
        comp(
            "custom_metrics",
            f"{custom:.0f} avg custom metrics - {allocation:.0f} host allocation, "
            f"x ${p['custom_metric_month']}/metric/mo",
            billable * p["custom_metric_month"],
        )
    logs_bytes_day = per_day(hourly_values(results.get("usage_hourly_logs"),
                                           "ingested_events_bytes"))
    if logs_bytes_day:
        gb_day = logs_bytes_day / 1e9
        comp("logs_ingest", f"{gb_day:.1f} GB/day x 30 x ${p['logs_ingest_gb']}/GB",
             gb_day * 30 * p["logs_ingest_gb"])
    indexed_day = per_day(hourly_values(results.get("usage_hourly_logs"),
                                        "indexed_events_count"))
    if indexed_day:
        m_events = indexed_day * 30 / 1e6
        comp(
            "logs_indexed",
            f"{m_events:.1f}M events/mo x ${p['logs_indexed_million_events']}/M (7d tier)",
            m_events * p["logs_indexed_million_events"],
        )
    spans_bytes_day = per_day(hourly_values(results.get("usage_hourly_ingested_spans"),
                                            "ingested_events_bytes"))
    if spans_bytes_day:
        gb_day = spans_bytes_day / 1e9
        comp("apm_ingest", f"{gb_day:.1f} GB/day x 30 x ${p['apm_ingest_gb']}/GB",
             gb_day * 30 * p["apm_ingest_gb"])
    rum_day = per_day(hourly_values(results.get("usage_hourly_rum"), "rum_total_session_count"))
    if rum_day:
        comp("rum", f"{rum_day:.0f} sessions/day x 30 / 1000 x ${p['rum_1k_sessions']}",
             rum_day * 30 / 1000 * p["rum_1k_sessions"])
    return sum(c["monthly_usd"] for c in comps), comps


def build_cost_figures(
    results: dict[str, Any],
    fetches: dict[str, FetchResult],
    summary: SummaryWriter,
) -> None:
    """cost.monthly_usd (headline) + month-to-date / projection sub-figures.

    Headline preference: last full month (stable, fully accrued) > linear
    projection of the current month (estimated) > usage x list prices
    (estimated fallback when the billing API is inaccessible).
    """
    now = datetime.now(UTC)
    cur_month = now.strftime("%Y-%m")
    prev_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    months: dict[str, dict[str, Any]] = {}

    def ingest(payload: Any, evidence_file: str, source: str) -> None:
        for entry in (payload or {}).get("data", []):
            attrs = entry.get("attributes", {})
            if attrs.get("total_cost") is None:
                continue
            month = str(attrs.get("date", ""))[:7]
            # historical (finalized) is ingested first and wins over estimated
            if month in months:
                continue
            m = months.setdefault(
                month, {"total": 0.0, "charges": [], "evidence": evidence_file,
                        "source": source}
            )
            m["total"] += float(attrs["total_cost"])
            for ch in attrs.get("charges", []):
                if ch.get("cost"):
                    m["charges"].append(
                        {
                            "product": ch.get("product_name", "unknown"),
                            "charge_type": ch.get("charge_type", ""),
                            "monthly_usd": round(float(ch["cost"]), 2),
                        }
                    )

    ingest(results.get("historical_cost"), "evidence/historical_cost.json",
           "GET /api/v2/usage/historical_cost?view=summary")
    ingest(results.get("estimated_cost"), "evidence/estimated_cost.json",
           "GET /api/v2/usage/estimated_cost?view=summary")

    evidence = ["evidence/estimated_cost.json"]
    source_api = "GET /api/v2/usage/estimated_cost?view=summary"
    mtd = months.get(cur_month, {}).get("total")
    last_full = months.get(prev_month, {}).get("total")

    if mtd is not None:
        summary.add_figure(Figure(
            id="datadog.cost_month_to_date_usd",
            label=f"Datadog cost, month to date ({cur_month})",
            value=round(mtd, 2), unit="USD", status="ok",
            method="Datadog estimated_cost API, current month "
            "(same source as the Usage & Cost page)",
            source_api=source_api, evidence_files=evidence,
        ))
        days_in_month = (
            (now.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        ).day
        projected = mtd / max(1, now.day) * days_in_month
        summary.add_figure(Figure(
            id="datadog.cost_projected_month_usd",
            label=f"Datadog cost, projected full month ({cur_month})",
            value=round(projected, 2), unit="USD", status="estimated",
            method=f"linear extrapolation: month-to-date / {now.day} days elapsed "
            f"x {days_in_month} days",
            source_api=source_api, evidence_files=evidence,
        ))

    def pick_breakdown(month: str) -> None:
        charges = months.get(month, {}).get("charges", [])
        totals = [c for c in charges if c["charge_type"] == "total"]
        breakdown = totals if totals else charges
        if breakdown:
            summary.inventory["cost_breakdown"] = sorted(
                breakdown, key=lambda c: -c["monthly_usd"]
            )
            summary.inventory["cost_breakdown_month"] = month

    if last_full is not None:
        prev_info = months[prev_month]
        summary.add_figure(Figure(
            id="cost.monthly_usd",
            label=f"Datadog cost, last full month ({prev_month})",
            value=round(last_full, 2), unit="USD", status="ok",
            method=f"Datadog billed cost for the last full month ({prev_month}) "
            "(same source as the Usage & Cost page)",
            source_api=prev_info["source"], evidence_files=[prev_info["evidence"]],
        ))
        pick_breakdown(prev_month)
        return
    if mtd is not None:
        days_in_month = (
            (now.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        ).day
        summary.add_figure(Figure(
            id="cost.monthly_usd",
            label=f"Datadog cost, projected full month ({cur_month})",
            value=round(mtd / max(1, now.day) * days_in_month, 2), unit="USD",
            status="estimated",
            method=f"no billed month available yet; linear extrapolation of "
            f"month-to-date over {now.day}/{days_in_month} days",
            source_api=source_api, evidence_files=evidence,
            notes="first month of usage or org created this month",
        ))
        pick_breakdown(cur_month)
        return

    # Billing API inaccessible: estimate from collected usage x list prices
    res = fetches.get("estimated_cost")
    reason = res.gap_reason if res and res.gap_reason else "api_error"
    api_err = res.error if res and res.error else "estimated cost not present in response"
    est_total, comps = estimate_cost_from_usage(results)
    if comps:
        summary.add_figure(Figure(
            id="cost.monthly_usd",
            label="Datadog cost (estimated from usage)",
            value=round(est_total, 2), unit="USD", status="estimated",
            method=f"usage x Datadog public list prices ({LIST_PRICES_VERSION}); "
            "actual contract pricing usually differs; see cost_estimate_components "
            "for the per-product basis",
            source_api="GET /api/v2/usage/hourly_usage (usage) x list prices",
            evidence_files=[
                f"evidence/usage_hourly_{f}.json" for f in HOURLY_FAMILIES
            ],
            notes=f"estimated_cost API unavailable ({reason}): {api_err}",
        ))
        summary.inventory["cost_estimate_components"] = comps
        summary.add_gap(
            "cost", ["cost.monthly_usd"], reason,
            f"estimated_cost API unavailable ({api_err}); cost.monthly_usd is "
            "estimated from usage x list prices instead of measured billing data",
            remediation="grant billing_read / usage_read on the application key "
            "for the measured number",
        )
    else:
        summary.mark_unavailable(
            "cost.monthly_usd", reason,
            f"{api_err}; no usage data available for a fallback estimate either",
            remediation="estimated_cost requires billing_read / usage_read permission "
            "on the application key",
        )


def build_summary(
    results: dict[str, Any],
    fetches: dict[str, FetchResult],
    summary: SummaryWriter,
) -> None:
    # hosts.count — real-time active hosts
    hosts = results.get("hosts_totals")
    if hosts and hosts.get("total_active") is not None:
        summary.add_figure(
            Figure(
                id="hosts.count",
                label="Active hosts",
                value=float(hosts["total_active"]),
                unit="hosts",
                status="ok",
                method="hosts/totals total_active (hosts seen in the last ~2h)",
                source_api="GET /api/v1/hosts/totals",
                evidence_files=["evidence/hosts_totals.json"],
                notes=f"total_up={hosts.get('total_up')}",
            )
        )
    else:
        res = fetches.get("hosts_totals")
        summary.mark_unavailable(
            "hosts.count",
            res.gap_reason if res and res.gap_reason else "api_error",
            res.error if res and res.error else "hosts/totals returned no data",
        )

    # metrics.total_count — active metric names over the trailing 2h
    metrics_list = results.get("metrics_list")
    if metrics_list is not None and isinstance(metrics_list.get("metrics"), list):
        summary.add_figure(
            Figure(
                id="metrics.total_count",
                label="Active metric names",
                value=float(len(metrics_list["metrics"])),
                unit="metrics",
                status="ok",
                method="count of metric names actively reporting in the last 2h",
                source_api="GET /api/v1/metrics?from=<2h ago>",
                evidence_files=["evidence/metrics_list.json"],
            )
        )
    else:
        res = fetches.get("metrics_list")
        summary.mark_unavailable(
            "metrics.total_count",
            res.gap_reason if res and res.gap_reason else "api_error",
            res.error if res and res.error else "metrics list returned no data",
        )

    # Usage-derived figures (each records its own gap when missing)
    add_hourly_figure(
        summary, results, fetches,
        "metrics.custom_metrics_count", "timeseries", "num_custom_timeseries", "avg",
        remediation="custom metrics usage requires the timeseries product family; "
        "verify the app key has usage_read",
    )
    add_hourly_figure(
        summary, results, fetches,
        "logs.ingest_gb_per_day", "logs", "ingested_events_bytes", "per_day_gb",
    )
    add_hourly_figure(
        summary, results, fetches,
        "datadog.logs_indexed_events_per_day", "logs", "indexed_events_count", "per_day",
    )
    add_hourly_figure(
        summary, results, fetches,
        "traces.ingest_gb_per_day", "ingested_spans", "ingested_events_bytes", "per_day_gb",
    )
    add_hourly_figure(
        summary, results, fetches,
        "datadog.rum_sessions_per_day", "rum", "rum_total_session_count", "per_day",
    )

    # alerts.monitor_count
    monitors = results.get("monitors")
    if isinstance(monitors, list):
        summary.add_figure(
            Figure(
                id="alerts.monitor_count",
                label="Monitors",
                value=float(len(monitors)),
                unit="monitors",
                status="ok",
                method="count of monitors across all pages",
                source_api="GET /api/v1/monitor (paginated)",
                evidence_files=["evidence/monitors.json"],
            )
        )
        by_type: dict[str, int] = {}
        for m in monitors:
            by_type[m.get("type", "unknown")] = by_type.get(m.get("type", "unknown"), 0) + 1
        summary.inventory["monitors_by_type"] = dict(
            sorted(by_type.items(), key=lambda kv: -kv[1])
        )
    else:
        res = fetches.get("monitors")
        summary.mark_unavailable(
            "alerts.monitor_count",
            res.gap_reason if res and res.gap_reason else "api_error",
            res.error if res and res.error else "monitor list unavailable",
        )

    build_cost_figures(results, fetches, summary)

    # Inventory (non-numeric facts for the deep-dive section)
    inv = summary.inventory
    dash = results.get("dashboards") or {}
    inv["dashboards_count"] = len(dash.get("dashboards", []))
    notebooks = results.get("notebooks") or {}
    inv["notebooks_count"] = len(notebooks.get("data", []))
    slos = results.get("slos") or {}
    inv["slo_count"] = len(slos.get("data", []))
    synth = results.get("synthetics_tests") or {}
    tests = synth.get("tests", [])
    inv["synthetics"] = {
        "total": len(tests),
        "api": sum(1 for t in tests if t.get("type") == "api"),
        "browser": sum(1 for t in tests if t.get("type") == "browser"),
    }
    pipelines = results.get("logs_pipelines")
    if isinstance(pipelines, list):
        inv["log_pipelines_count"] = len(pipelines)
    indexes = results.get("logs_indexes") or {}
    inv["log_indexes"] = [
        {
            "name": ix.get("name"),
            "retention_days": ix.get("num_retention_days"),
            "daily_limit": ix.get("daily_limit"),
        }
        for ix in indexes.get("indexes", [])
    ]
    infra_values = hourly_values(results.get("usage_hourly_infra_hosts"), "host_count")
    if infra_values:
        inv["billable_infra_hosts_avg"] = round(avg(infra_values) or 0, 1)
        inv["billable_infra_hosts_max"] = max(v for _, v in infra_values)


# ── main ─────────────────────────────────────────────────────────────────


def main() -> int:
    parser = base_parser("Datadog discovery collector", default_lookback="30d")
    parser.add_argument("--api-key", help="Datadog API key (or env DD_API_KEY)")
    parser.add_argument("--app-key", help="Datadog application key (or env DD_APP_KEY)")
    parser.add_argument(
        "--site",
        default="us1",
        help=f"Datadog site: {', '.join(DD_SITES)} or a full domain",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ev = EvidenceWriter(args.output_dir)
    results: dict[str, Any] = {}
    fetches: dict[str, FetchResult] = {}
    domain = resolve_site(args.site)

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
        now = datetime.now(UTC)
        lookback_days = parse_duration_days(args.lookback)
        start_hr = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%dT%H:00:00+00:00")
        end_hr = now.strftime("%Y-%m-%dT%H:00:00+00:00")
        with HttpClient(
            f"https://api.{domain}",
            headers=headers,
            timeout_s=args.timeout,
            verify=not args.insecure,
        ) as client:
            fetches.update(fetch_simple(client, ev, results))
            fetches["monitors"] = fetch_monitors(client, ev, results)
            fetch_slos(client, ev, results)
            fetch_usage_summary(client, now, lookback_days, ev, results)
            fetches.update(
                fetch_hourly_usage(client, HOURLY_FAMILIES, start_hr, end_hr, ev, results)
            )
            fetches["estimated_cost"] = fetch_estimated_cost(client, now, ev, results)
            fetches["historical_cost"] = fetch_historical_cost(client, now, ev, results)
            fetches["metrics_list"] = fetch_metrics_list(client, now, ev, results)

    summary = SummaryWriter(
        collector=COLLECTOR,
        collector_version=VERSION,
        expected=EXPECTED,
        target=f"https://api.{domain}",
        lookback=args.lookback,
        args_redacted={"site": args.site, "lookback": args.lookback},
    )
    summary.environment = {
        "detected_backend": "datadog",
        "version": None,
        "detection_method": "site flag / API reachability",
        "site": domain,
    }
    build_summary(results, fetches, summary)
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
