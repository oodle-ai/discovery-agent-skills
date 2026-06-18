# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Grafana Mimir discovery collector.

Collects active series, ingestion rate, query rate, object-store operations,
retention, per-tenant breakdown, and infrastructure cost estimates from the
same Mimir internal metrics and admin APIs that back the Grafana Mimir
dashboard and operational tooling.

Examples:
    uv run collectors/mimir/collect.py \\
        --endpoint https://mimir-gateway:8080 \\
        --output-dir ./discovery-output/mimir

    uv run collectors/mimir/collect.py \\
        --endpoint https://mimir-gateway:8080 \\
        --username admin --password secret \\
        --tenant myorg --lookback 7d \\
        --output-dir ./discovery-output/mimir

    uv run collectors/mimir/collect.py \\
        --report-only --output-dir ./discovery-output/mimir

Figure <-> API mapping is documented in collectors/mimir/README.md.
"""
from __future__ import annotations

import math
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.auth import basic_auth, bearer_headers  # noqa: E402
from lib.cli import base_parser, credential, parse_duration_s, parse_headers  # noqa: E402
from lib.evidence import EvidenceWriter  # noqa: E402
from lib.http import FetchResult, HttpClient  # noqa: E402
from lib.summary import ExpectedFigure, Figure, SummaryWriter  # noqa: E402

COLLECTOR = "mimir"
VERSION = "1.0.0"

EXPECTED = [
    ExpectedFigure("metrics.active_series", "Active time series", "series", "metrics"),
    ExpectedFigure(
        "metrics.samples_per_sec", "Ingestion rate (avg)", "samples/sec", "metrics"
    ),
    ExpectedFigure(
        "mimir.ingestion_rate_peak", "Ingestion rate (peak)", "samples/sec", "mimir"
    ),
    ExpectedFigure("mimir.query_rate_avg", "Query rate (avg)", "req/sec", "mimir"),
    ExpectedFigure("mimir.query_rate_peak", "Query rate (peak)", "req/sec", "mimir"),
    ExpectedFigure(
        "mimir.objstore_ops_per_day",
        "Object store operations",
        "ops/day",
        "mimir",
    ),
    ExpectedFigure("mimir.retention_days", "Retention period", "days", "mimir"),
    ExpectedFigure("cost.monthly_usd", "Estimated infra cost", "USD", "cost"),
]


# ── helpers ──────────────────────────────────────────────────────────────


def _safe_float(raw: Any) -> float | None:
    try:
        v = float(raw)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def _query_range_step(lookback_s: int) -> int:
    if lookback_s <= 3600:
        return 15
    if lookback_s <= 86400:
        return 60
    if lookback_s <= 7 * 86400:
        return 300
    return 900


def _parse_go_duration(s: str) -> int | None:
    s = s.strip()
    if not s or s in ("0s", "0"):
        return None
    total = 0
    for val, unit in re.findall(r"(\d+)([dhms])", s):
        n = int(val)
        total += n * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
    return total if total > 0 else None


# ── PromQL helpers ───────────────────────────────────────────────────────


def detect_prom_prefix(client: HttpClient) -> str:
    """Auto-detect whether the Prometheus API lives at /prometheus/api/v1 or /api/v1."""
    res = client.get_json("/prometheus/api/v1/query", params={"query": "up"})
    if res.ok:
        return "/prometheus"
    res2 = client.get_json("/api/v1/query", params={"query": "up"})
    if res2.ok:
        return ""
    return "/prometheus"


def promql_instant(client: HttpClient, query: str, prefix: str = "") -> FetchResult:
    return client.get_json(f"{prefix}/api/v1/query", params={"query": query})


def promql_range(
    client: HttpClient,
    query: str,
    start: datetime,
    end: datetime,
    step_s: int,
    prefix: str = "",
) -> FetchResult:
    params = {
        "query": query,
        "start": str(start.timestamp()),
        "end": str(end.timestamp()),
        "step": str(step_s),
    }
    return client.get_json(f"{prefix}/api/v1/query_range", params=params)


def vector_first_value(result: FetchResult) -> float | None:
    if result.failed or not isinstance(result.data, dict):
        return None
    data = result.data
    if data.get("status") != "success":
        return None
    inner = data.get("data") or {}
    if inner.get("resultType") != "vector":
        return None
    res = inner.get("result") or []
    if not res:
        return None
    val = res[0].get("value")
    if not val or len(val) < 2:
        return None
    return _safe_float(val[1])


def instant_vector_by_label(
    result: FetchResult, label_key: str
) -> dict[str, float]:
    out: dict[str, float] = {}
    if result.failed or not isinstance(result.data, dict):
        return out
    if result.data.get("status") != "success":
        return out
    for s in (result.data.get("data") or {}).get("result") or []:
        lbl = (s.get("metric") or {}).get(label_key, "_total")
        val = s.get("value")
        if not val or len(val) < 2:
            continue
        v = _safe_float(val[1])
        if v is not None:
            out[str(lbl)] = v
    return out


def matrix_avg_peak(result: FetchResult) -> tuple[float | None, float | None]:
    if result.failed or not isinstance(result.data, dict):
        return None, None
    if result.data.get("status") != "success":
        return None, None
    series_list = (result.data.get("data") or {}).get("result") or []
    if not series_list:
        return None, None
    all_values: list[float] = []
    for s in series_list:
        for raw in s.get("values") or []:
            if len(raw) >= 2:
                v = _safe_float(raw[1])
                if v is not None:
                    all_values.append(v)
    if not all_values:
        return None, None
    avg = sum(all_values) / len(all_values)
    peak = max(all_values)
    return avg, peak


def matrix_last_by_label(
    result: FetchResult, label_key: str
) -> dict[str, float]:
    out: dict[str, float] = {}
    if result.failed or not isinstance(result.data, dict):
        return out
    if result.data.get("status") != "success":
        return out
    for s in (result.data.get("data") or {}).get("result") or []:
        lbl = (s.get("metric") or {}).get(label_key, "_total")
        vals = s.get("values") or []
        if not vals or len(vals[-1]) < 2:
            continue
        v = _safe_float(vals[-1][1])
        if v is not None:
            out[str(lbl)] = v
    return out


# ── collection ───────────────────────────────────────────────────────────


INSTANT_QUERIES = [
    ("active_series_sum", "sum(cortex_ingester_active_series)"),
    (
        "active_series_by_user",
        "sum by (user) (cortex_ingester_active_series)",
    ),
    (
        "samples_rate_by_user",
        "sum by (user) ("
        "rate(cortex_distributor_received_samples_total[5m])"
        ") or "
        "sum by (user) ("
        "rate(cortex_ingest_storage_reader_fetch_records_total[5m])"
        ")",
    ),
]

RANGE_QUERIES = [
    (
        "ingestion_rate",
        "sum(rate(cortex_distributor_received_samples_total[5m])) or "
        "sum(rate(cortex_ingest_storage_reader_fetch_records_total[5m]))",
    ),
    (
        "query_requests",
        'sum(rate(cortex_request_duration_seconds_count{route=~".*query.*"}[5m]))',
    ),
    (
        "objstore_ops_by_op",
        "sum(rate(thanos_objstore_bucket_operations_total[5m])) by (operation)",
    ),
]


def collect_buildinfo(
    client: HttpClient, ev: EvidenceWriter
) -> FetchResult:
    print("collecting buildinfo")
    res = client.get_json("/api/v1/status/buildinfo")
    if res.ok and res.data:
        ev.write("buildinfo", res.data, source_api="GET /api/v1/status/buildinfo")
    else:
        print(f"  WARN buildinfo: {res.error}")
    return res


def collect_config(client: HttpClient, ev: EvidenceWriter) -> FetchResult:
    """Mimir /config returns YAML text — fetch as text and wrap for _parse_config_text."""
    print("collecting config")
    res = client.get_text("/config")
    if res.ok and res.data:
        data = {"_raw_text": res.data}
        ev.write("config", data, source_api="GET /config")
        return FetchResult(ok=True, data=data, status_code=res.status_code)
    print(f"  WARN config: {res.error}")
    return res


def collect_instant_queries(
    client: HttpClient,
    ev: EvidenceWriter,
    prefix: str = "",
) -> dict[str, FetchResult]:
    results: dict[str, FetchResult] = {}
    for name, query in INSTANT_QUERIES:
        print(f"  instant query: {name}")
        res = promql_instant(client, query, prefix)
        results[name] = res
        if res.ok and res.data:
            ev.write(
                f"prom_instant_{name}",
                res.data,
                source_api=f"GET {prefix}/api/v1/query?query={query[:80]}",
            )
        elif res.failed:
            print(f"    WARN {name}: {res.error}")
    return results


def collect_range_queries(
    client: HttpClient,
    ev: EvidenceWriter,
    lookback_s: int,
    prefix: str = "",
) -> dict[str, FetchResult]:
    results: dict[str, FetchResult] = {}
    end = datetime.now(UTC)
    start = end - timedelta(seconds=lookback_s)
    step = _query_range_step(lookback_s)

    for name, query in RANGE_QUERIES:
        print(f"  range query: {name}")
        res = promql_range(client, query, start, end, step, prefix)
        results[name] = res
        if res.ok and res.data:
            ev.write(
                f"prom_range_{name}",
                res.data,
                source_api=f"GET {prefix}/api/v1/query_range?query={query[:80]}",
            )
        elif res.failed:
            print(f"    WARN {name}: {res.error}")
    return results


# ── derivation ───────────────────────────────────────────────────────────


def _extract_version(buildinfo: FetchResult) -> str:
    if buildinfo.failed or not isinstance(buildinfo.data, dict):
        return ""
    d = buildinfo.data
    nested = d.get("data") if isinstance(d.get("data"), dict) else {}
    return str(d.get("version") or nested.get("version") or "")


def _parse_config_text(config_res: FetchResult) -> str:
    if config_res.failed or not config_res.data:
        return ""
    if isinstance(config_res.data, dict):
        return str(config_res.data.get("_raw_text", ""))
    return str(config_res.data)


def _parse_retention_seconds(config_text: str) -> int | None:
    if not config_text:
        return None
    m = re.search(
        r"compactor_blocks_retention_period:\s*([\d]+[dhms][\d dhms]*)",
        config_text,
        re.I,
    )
    if m:
        return _parse_go_duration(m.group(1))
    return None


def _parse_replication_factor(config_text: str) -> int | None:
    if not config_text:
        return None
    m = re.search(r"replication_factor:\s*(\d+)", config_text)
    return int(m.group(1)) if m else None


def derive_active_series(
    instant: dict[str, FetchResult],
    summary: SummaryWriter,
) -> None:
    res = instant.get("active_series_sum")
    if not res or res.failed:
        reason = res.gap_reason if res else "api_error"
        detail = res.error if res and res.error else "instant query failed"
        summary.mark_unavailable("metrics.active_series", reason or "api_error", detail)
        return
    val = vector_first_value(res)
    if val is None:
        summary.mark_unavailable(
            "metrics.active_series",
            "api_error",
            "query returned no data or non-success status",
        )
        return
    summary.add_figure(
        Figure(
            id="metrics.active_series",
            label="Active time series",
            value=val,
            unit="series",
            status="ok",
            method="sum(cortex_ingester_active_series) instant query",
            source_api="GET /api/v1/query",
            query="sum(cortex_ingester_active_series)",
            evidence_files=["evidence/prom_instant_active_series_sum.json"],
        )
    )


def derive_ingestion_rate(
    range_results: dict[str, FetchResult],
    summary: SummaryWriter,
    lookback_s: int,
) -> tuple[float | None, float | None]:
    res = range_results.get("ingestion_rate")
    if not res or res.failed:
        reason = res.gap_reason if res else "api_error"
        detail = res.error if res and res.error else "range query failed"
        summary.mark_unavailable(
            "metrics.samples_per_sec", reason or "api_error", detail
        )
        summary.mark_unavailable(
            "mimir.ingestion_rate_peak", reason or "api_error", detail
        )
        return None, None

    avg, peak = matrix_avg_peak(res)
    if avg is None:
        summary.mark_unavailable(
            "metrics.samples_per_sec",
            "api_error",
            "ingestion_rate query returned no data",
        )
        summary.mark_unavailable(
            "mimir.ingestion_rate_peak",
            "api_error",
            "ingestion_rate query returned no data",
        )
        return None, None

    query = RANGE_QUERIES[0][1]
    summary.add_figure(
        Figure(
            id="metrics.samples_per_sec",
            label="Ingestion rate (avg)",
            value=round(avg, 2),
            unit="samples/sec",
            status="ok",
            method=f"avg of range query over {lookback_s // 86400}d lookback",
            source_api="GET /api/v1/query_range",
            query=query,
            evidence_files=["evidence/prom_range_ingestion_rate.json"],
        )
    )
    summary.add_figure(
        Figure(
            id="mimir.ingestion_rate_peak",
            label="Ingestion rate (peak)",
            value=round(peak, 2),
            unit="samples/sec",
            status="ok",
            method=f"max of range query over {lookback_s // 86400}d lookback",
            source_api="GET /api/v1/query_range",
            query=query,
            evidence_files=["evidence/prom_range_ingestion_rate.json"],
        )
    )
    return avg, peak


def derive_query_rate(
    range_results: dict[str, FetchResult],
    summary: SummaryWriter,
    lookback_s: int,
) -> None:
    res = range_results.get("query_requests")
    if not res or res.failed:
        reason = res.gap_reason if res else "api_error"
        detail = res.error if res and res.error else "range query failed"
        summary.mark_unavailable("mimir.query_rate_avg", reason or "api_error", detail)
        summary.mark_unavailable("mimir.query_rate_peak", reason or "api_error", detail)
        return

    avg, peak = matrix_avg_peak(res)
    if avg is None:
        summary.mark_unavailable(
            "mimir.query_rate_avg", "api_error", "query_requests returned no data"
        )
        summary.mark_unavailable(
            "mimir.query_rate_peak", "api_error", "query_requests returned no data"
        )
        return

    query = RANGE_QUERIES[1][1]
    summary.add_figure(
        Figure(
            id="mimir.query_rate_avg",
            label="Query rate (avg)",
            value=round(avg, 4),
            unit="req/sec",
            status="ok",
            method=f"avg of range query over {lookback_s // 86400}d lookback",
            source_api="GET /api/v1/query_range",
            query=query,
            evidence_files=["evidence/prom_range_query_requests.json"],
        )
    )
    summary.add_figure(
        Figure(
            id="mimir.query_rate_peak",
            label="Query rate (peak)",
            value=round(peak, 4),
            unit="req/sec",
            status="ok",
            method=f"max of range query over {lookback_s // 86400}d lookback",
            source_api="GET /api/v1/query_range",
            query=query,
            evidence_files=["evidence/prom_range_query_requests.json"],
        )
    )


def derive_objstore_ops(
    range_results: dict[str, FetchResult],
    summary: SummaryWriter,
) -> dict[str, float]:
    res = range_results.get("objstore_ops_by_op")
    if not res or res.failed:
        reason = res.gap_reason if res else "api_error"
        detail = res.error if res and res.error else "range query failed"
        summary.mark_unavailable(
            "mimir.objstore_ops_per_day", reason or "api_error", detail
        )
        return {}

    ops_by_op = matrix_last_by_label(res, "operation")
    if not ops_by_op:
        summary.mark_unavailable(
            "mimir.objstore_ops_per_day",
            "api_error",
            "objstore_ops query returned no data",
        )
        return {}

    day_s = 86400.0
    ops_per_day = sum(v * day_s for v in ops_by_op.values())
    summary.add_figure(
        Figure(
            id="mimir.objstore_ops_per_day",
            label="Object store operations",
            value=round(ops_per_day, 0),
            unit="ops/day",
            status="ok",
            method="sum of per-operation rates × 86400",
            source_api="GET /api/v1/query_range",
            query=RANGE_QUERIES[2][1],
            evidence_files=["evidence/prom_range_objstore_ops_by_op.json"],
        )
    )
    return ops_by_op


def derive_retention(
    config_text: str,
    summary: SummaryWriter,
) -> int | None:
    retention_s = _parse_retention_seconds(config_text)
    if retention_s is None:
        summary.mark_unavailable(
            "mimir.retention_days",
            "not_configured",
            "compactor_blocks_retention_period not found in /config response",
            remediation="ensure Mimir /config endpoint is accessible and returns "
            "compactor_blocks_retention_period",
        )
        return None
    days = retention_s / 86400.0
    summary.add_figure(
        Figure(
            id="mimir.retention_days",
            label="Retention period",
            value=round(days, 1),
            unit="days",
            status="ok",
            method="parsed compactor_blocks_retention_period from /config",
            source_api="GET /config",
            evidence_files=["evidence/config.json"],
        )
    )
    return retention_s


def derive_cost(
    ingest_avg: float | None,
    ops_by_op: dict[str, float],
    summary: SummaryWriter,
    cost_per_vcpu_hour: float = 0.048,
    cost_per_gb_ram_hour: float = 0.006,
    cost_per_gb_month: float = 0.023,
    cost_per_1k_api: float = 0.0004,
    bytes_per_sample: float = 1.5,
    retention_s: int | None = None,
    bucket_size_gb: float | None = None,
) -> dict[str, Any] | None:
    day_s = 86400.0

    ops_per_day = sum(v * day_s for v in ops_by_op.values())
    api_cost_mo = (ops_per_day / 1000.0) * 30.0 * cost_per_1k_api

    storage_gb = bucket_size_gb
    if storage_gb is None and ingest_avg is not None and retention_s:
        bps = ingest_avg * bytes_per_sample
        storage_gb = (bps * retention_s) / (1024**3)
    objstore_cost_mo = (storage_gb or 0) * cost_per_gb_month

    total_mo = api_cost_mo + objstore_cost_mo

    if total_mo <= 0 and ingest_avg is None:
        summary.mark_unavailable(
            "cost.monthly_usd",
            "not_configured",
            "insufficient data to estimate cost (no ingestion rate or storage size)",
            remediation="ensure Mimir internal metrics are queryable",
        )
        return None

    cost_breakdown: list[dict[str, Any]] = []
    if api_cost_mo > 0:
        cost_breakdown.append(
            {"product": "Object store API ops", "monthly_usd": round(api_cost_mo, 2)}
        )
    if objstore_cost_mo > 0:
        cost_breakdown.append(
            {"product": "Object storage", "monthly_usd": round(objstore_cost_mo, 2)}
        )

    parts = []
    if storage_gb is not None and bucket_size_gb is None:
        parts.append(
            f"storage estimated from ingestion rate × {bytes_per_sample} bytes/sample "
            f"× retention"
        )
    elif bucket_size_gb is not None:
        parts.append(f"storage from --bucket-size-gb={bucket_size_gb}")
    else:
        parts.append("storage cost excluded (missing retention or ingestion data)")
    parts.append("does not include compute or disk costs (no kubectl data)")

    summary.add_figure(
        Figure(
            id="cost.monthly_usd",
            label="Estimated infra cost (storage + API)",
            value=round(total_mo, 2),
            unit="USD",
            status="estimated",
            method="; ".join(parts),
            source_api="derived from /api/v1/query_range + /config",
            evidence_files=[
                "evidence/prom_range_objstore_ops_by_op.json",
                "evidence/config.json",
            ],
        )
    )
    return {
        "api_cost_mo": round(api_cost_mo, 2),
        "objstore_cost_mo": round(objstore_cost_mo, 2),
        "storage_gb": round(storage_gb, 2) if storage_gb else None,
        "total_mo": round(total_mo, 2),
        "cost_breakdown": cost_breakdown,
    }


def build_summary(
    buildinfo: FetchResult,
    config_res: FetchResult,
    instant: dict[str, FetchResult],
    range_results: dict[str, FetchResult],
    summary: SummaryWriter,
    lookback_s: int,
    bucket_size_gb: float | None = None,
    bytes_per_sample: float = 1.5,
) -> None:
    version = _extract_version(buildinfo)
    config_text = _parse_config_text(config_res)

    summary.environment = {
        "detected_backend": "mimir",
        "version": version or None,
        "detection_method": "/api/v1/status/buildinfo",
        "tenancy": "multi" if "multitenancy_enabled: true" in config_text else "single",
    }

    derive_active_series(instant, summary)
    ingest_avg, _ = derive_ingestion_rate(range_results, summary, lookback_s)
    derive_query_rate(range_results, summary, lookback_s)
    ops_by_op = derive_objstore_ops(range_results, summary)
    retention_s = derive_retention(config_text, summary)

    cost_data = derive_cost(
        ingest_avg,
        ops_by_op,
        summary,
        retention_s=retention_s,
        bucket_size_gb=bucket_size_gb,
        bytes_per_sample=bytes_per_sample,
    )

    # inventory: per-tenant breakdown
    users_active = instant_vector_by_label(
        instant.get("active_series_by_user", FetchResult(ok=False)), "user"
    )
    users_rate = instant_vector_by_label(
        instant.get("samples_rate_by_user", FetchResult(ok=False)), "user"
    )
    if users_active:
        total_active = sum(users_active.values()) or 1.0
        tenant_breakdown = sorted(
            [
                {
                    "tenant": u,
                    "active_series": int(v),
                    "pct_of_total": round(100 * v / total_active, 1),
                    "samples_per_sec": round(users_rate.get(u, 0), 4),
                }
                for u, v in users_active.items()
            ],
            key=lambda t: -t["active_series"],
        )[:50]
        summary.inventory["tenant_breakdown"] = tenant_breakdown

    repl_factor = _parse_replication_factor(config_text)
    if repl_factor:
        summary.inventory["replication_factor"] = repl_factor

    if cost_data:
        summary.inventory["cost_breakdown"] = cost_data.get("cost_breakdown", [])
        if cost_data.get("storage_gb"):
            summary.inventory["estimated_storage_gb"] = cost_data["storage_gb"]


# ── main ─────────────────────────────────────────────────────────────────


def main() -> int:
    parser = base_parser("Grafana Mimir discovery collector", default_lookback="7d")
    parser.add_argument(
        "--endpoint",
        help="Mimir base URL (serves both /api/v1/* PromQL and /config admin API)",
    )
    parser.add_argument("--username", help="HTTP basic auth username")
    parser.add_argument("--password", help="HTTP basic auth password")
    parser.add_argument(
        "--bearer-token",
        help="Bearer token for auth (or set MIMIR_BEARER_TOKEN)",
    )
    parser.add_argument(
        "--tenant",
        help="X-Scope-OrgID tenant header value (or set MIMIR_TENANT)",
    )
    parser.add_argument(
        "--bucket-size-gb",
        type=float,
        default=None,
        help="Override object storage size (GB) for cost estimation",
    )
    parser.add_argument(
        "--bytes-per-sample",
        type=float,
        default=1.5,
        help="Bytes/sample heuristic for storage estimation",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ev = EvidenceWriter(args.output_dir)
    lookback_s = parse_duration_s(args.lookback)

    if args.report_only:
        evidence = ev.load_all()
        if not evidence:
            print(f"ERROR: --report-only but no evidence under {ev.evidence_dir}")
            return 2

        instant_results: dict[str, FetchResult] = {}
        range_results: dict[str, FetchResult] = {}
        bi = evidence.get("buildinfo")
        buildinfo_res = FetchResult(ok=bool(bi), data=bi)
        cfg = evidence.get("config")
        config_result = FetchResult(ok=bool(cfg), data=cfg)

        for name, _ in INSTANT_QUERIES:
            key = f"prom_instant_{name}"
            data = evidence.get(key)
            instant_results[name] = FetchResult(ok=bool(data), data=data)

        for name, _ in RANGE_QUERIES:
            key = f"prom_range_{name}"
            data = evidence.get(key)
            range_results[name] = FetchResult(ok=bool(data), data=data)
    else:
        if not args.endpoint:
            print("ERROR: --endpoint is required unless --report-only")
            return 1

        headers = parse_headers(args.header)
        auth_tuple = None
        token = credential(
            args.bearer_token, "MIMIR_BEARER_TOKEN",
            "Mimir bearer token", interactive_ok=False,
        )
        tenant = credential(
            args.tenant, "MIMIR_TENANT",
            "Mimir tenant", interactive_ok=False,
        )

        if args.username and args.password:
            auth_tuple = basic_auth(args.username, args.password)
        if token:
            headers.update(bearer_headers(token))
        if tenant:
            headers["X-Scope-OrgID"] = tenant

        with HttpClient(
            base_url=args.endpoint,
            headers=headers,
            auth=auth_tuple,
            timeout_s=args.timeout,
            verify=not args.insecure,
        ) as client:
            print(f"collecting from {args.endpoint}")
            buildinfo_res = collect_buildinfo(client, ev)
            config_result = collect_config(client, ev)

            prom_prefix = detect_prom_prefix(client)
            if prom_prefix:
                print(f"  detected Prometheus API at {prom_prefix}/api/v1/")

            print("running instant queries")
            instant_results = collect_instant_queries(client, ev, prom_prefix)

            print(f"running range queries (lookback {args.lookback})")
            range_results = collect_range_queries(client, ev, lookback_s, prom_prefix)

    target = args.endpoint or "report-only"
    summary = SummaryWriter(
        collector=COLLECTOR,
        collector_version=VERSION,
        expected=EXPECTED,
        target=target,
        lookback=args.lookback,
        args_redacted={
            "endpoint": target,
            "lookback": args.lookback,
            "tenant": "***" if (
                args.tenant
                or credential(None, "MIMIR_TENANT", "", interactive_ok=False)
            ) else None,
        },
    )

    build_summary(
        buildinfo_res,
        config_result,
        instant_results,
        range_results,
        summary,
        lookback_s,
        bucket_size_gb=args.bucket_size_gb,
        bytes_per_sample=args.bytes_per_sample,
    )
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
