# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""Prometheus stack collector — Prometheus, VictoriaMetrics, Thanos + optional Loki, Tempo."""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.cli import base_parser, parse_duration_s, parse_go_duration_s, parse_headers  # noqa: E402
from lib.evidence import EvidenceWriter  # noqa: E402
from lib.http import FetchResult, HttpClient  # noqa: E402
from lib.summary import ExpectedFigure, Figure, SummaryWriter  # noqa: E402

COLLECTOR = "promstack"
VERSION = "1.0.0"

EXPECTED_METRICS = [
    ExpectedFigure("metrics.active_series", "Active time series", "series", "metrics"),
    ExpectedFigure("metrics.samples_per_sec", "Ingestion rate", "samples/sec", "metrics"),
    ExpectedFigure("promstack.scrape_targets", "Scrape targets", "targets", "promstack"),
    ExpectedFigure("promstack.retention_days", "Retention period", "days", "promstack"),
]

EXPECTED_LOKI = [
    ExpectedFigure("logs.ingest_gb_per_day", "Log ingestion rate", "GB/day", "logs"),
    ExpectedFigure("logs.retention_days", "Log retention", "days", "logs"),
]

EXPECTED_TEMPO = [
    ExpectedFigure("traces.spans_per_sec", "Span ingestion rate", "spans/sec", "traces"),
    ExpectedFigure("traces.ingest_gb_per_day", "Trace ingestion rate", "GB/day", "traces"),
]


# ---------------------------------------------------------------------------
# PromQL helpers
# ---------------------------------------------------------------------------

def promql_instant(client: HttpClient, query: str) -> FetchResult:
    return client.get_json("/api/v1/query", params={"query": query})


def vector_first_value(data: dict) -> float | None:
    try:
        results = data.get("data", {}).get("result", [])
        if results:
            return float(results[0]["value"][1])
    except (KeyError, IndexError, ValueError, TypeError):
        pass
    return None


def has_results(data: dict) -> bool:
    try:
        return len(data.get("data", {}).get("result", [])) > 0
    except (KeyError, TypeError):
        return False


def sum_prom_text_counter(text: str, metric_name: str) -> float | None:
    """Sum all label-set instances of a counter from Prometheus text exposition."""
    total = 0.0
    found = False
    for line in text.split("\n"):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name_part = line.split("{")[0].split()[0] if "{" in line else line.split()[0]
        if name_part == metric_name:
            try:
                total += float(line.rstrip().rsplit(None, 1)[-1])
                found = True
            except (ValueError, IndexError):
                continue
    return total if found else None


# ---------------------------------------------------------------------------
# Backend detection
# ---------------------------------------------------------------------------

def detect_backend(client: HttpClient, ev: EvidenceWriter) -> tuple[str, str, str]:
    """Detect backend type, version, and detection method.

    Returns (backend, version, detection_method).
    backend: 'prometheus' | 'victoriametrics' | 'thanos' | 'cortex' | 'unknown'
    """
    buildinfo = client.get_json("/api/v1/status/buildinfo")
    buildinfo_version = ""
    if buildinfo.ok:
        ev.write("buildinfo", buildinfo.data, source_api="GET /api/v1/status/buildinfo")
        data = buildinfo.data.get("data", buildinfo.data)
        version_str = data.get("version", "")
        buildinfo_version = version_str
        if "victoriametrics" in version_str.lower():
            return "victoriametrics", version_str, "buildinfo version contains 'victoriametrics'"

    probes = [
        ("vm_app_version", "victoriametrics"),
        ("thanos_build_info", "thanos"),
        ("cortex_build_info", "cortex"),
        ("prometheus_build_info", "prometheus"),
    ]
    for metric, backend in probes:
        probe = promql_instant(client, metric)
        if probe.ok and has_results(probe.data):
            ev.write(f"probe_{backend}", probe.data,
                     source_api=f"GET /api/v1/query?query={metric}")
            if backend == "cortex":
                return "cortex", "", "cortex_build_info metric present"
            version = _extract_version_from_probe(probe.data, metric)
            return backend, version or buildinfo_version, f"{metric} metric present"

    tsdb = client.get_json("/api/v1/status/tsdb")
    if tsdb.ok:
        ev.write("tsdb_status", tsdb.data, source_api="GET /api/v1/status/tsdb")
        return "prometheus", buildinfo_version or "unknown", "/api/v1/status/tsdb responded"

    return "unknown", buildinfo_version, "no backend detected"


def _parse_vm_flags(text: str) -> dict:
    """Parse VictoriaMetrics /flags text format into a dict."""
    result = {}
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        key, _, val = line.partition("=")
        result[key] = val.strip('"')
    return result


def _extract_version_from_probe(data: dict, metric: str) -> str:
    try:
        result = data["data"]["result"][0]["metric"]
        return result.get("version", "")
    except (KeyError, IndexError, TypeError):
        return ""


def detect_from_evidence(cached: dict) -> tuple[str, str, str]:
    """Re-detect backend from cached evidence (for --report-only)."""
    if "buildinfo" in cached:
        data = cached["buildinfo"].get("data", cached["buildinfo"])
        version = data.get("version", "")
        if "victoriametrics" in version.lower():
            return "victoriametrics", version, "buildinfo (cached)"
    for backend in ("victoriametrics", "thanos", "prometheus"):
        if f"probe_{backend}" in cached:
            version = _extract_version_from_probe(
                cached[f"probe_{backend}"], f"{backend}_build_info"
            )
            return backend, version, f"probe_{backend} (cached)"
    if "tsdb_status" in cached:
        version = ""
        if "buildinfo" in cached:
            version = cached["buildinfo"].get("data", cached["buildinfo"]).get("version", "")
        return "prometheus", version, "tsdb_status (cached)"
    return "unknown", "", "no backend evidence"


# ---------------------------------------------------------------------------
# Collection functions (per-backend)
# ---------------------------------------------------------------------------

def collect_prometheus(client: HttpClient, ev: EvidenceWriter) -> dict:
    results: dict[str, FetchResult | None] = {}

    tsdb = client.get_json("/api/v1/status/tsdb")
    if tsdb.ok:
        ev.write("tsdb_status", tsdb.data, source_api="GET /api/v1/status/tsdb")
    results["tsdb_status"] = tsdb

    flags = client.get_json("/api/v1/status/flags")
    if flags.ok:
        ev.write("status_flags", flags.data, source_api="GET /api/v1/status/flags")
    results["status_flags"] = flags

    queries = {
        "samples_rate": "sum(rate(prometheus_tsdb_head_samples_appended_total[5m]))",
        "scrape_targets": "count(up)",
    }
    for name, query in queries.items():
        res = promql_instant(client, query)
        if res.ok:
            ev.write(f"prom_instant_{name}", res.data,
                     source_api=f"GET /api/v1/query?query={query}")
        results[name] = res

    return results


def collect_victoriametrics(client: HttpClient, ev: EvidenceWriter) -> dict:
    results: dict[str, FetchResult | None] = {}

    tsdb = client.get_json("/api/v1/status/tsdb")
    if tsdb.ok:
        ev.write("tsdb_status", tsdb.data, source_api="GET /api/v1/status/tsdb")
    results["tsdb_status"] = tsdb

    flags = client.get_text("/flags")
    if flags.ok:
        parsed_flags = _parse_vm_flags(flags.data)
        ev.write("status_flags", parsed_flags, source_api="GET /flags")
        flags = FetchResult(ok=True, data=parsed_flags, status_code=flags.status_code)
    results["status_flags"] = flags

    queries = {
        "active_series": 'vm_cache_entries{type="storage/hour_metric_ids"}',
        "samples_rate": "sum(rate(vm_rows_inserted_total[5m]))",
        "scrape_targets": "count(up)",
    }
    for name, query in queries.items():
        res = promql_instant(client, query)
        if res.ok:
            ev.write(f"prom_instant_{name}", res.data,
                     source_api=f"GET /api/v1/query?query={query}")
        results[name] = res

    return results


def collect_thanos(client: HttpClient, ev: EvidenceWriter) -> dict:
    results: dict[str, FetchResult | None] = {}

    queries = {
        "active_series": "sum(prometheus_tsdb_head_series)",
        "samples_rate": "sum(rate(prometheus_tsdb_head_samples_appended_total[5m]))",
        "scrape_targets": "count(up)",
        "replication_factor": "max(thanos_receive_replication_factor)",
    }
    for name, query in queries.items():
        res = promql_instant(client, query)
        if res.ok:
            ev.write(f"prom_instant_{name}", res.data,
                     source_api=f"GET /api/v1/query?query={query}")
        results[name] = res

    return results


def collect_loki_from_tsdb(client: HttpClient, ev: EvidenceWriter) -> dict:
    """Query Loki distributor metrics from the main TSDB."""
    results: dict[str, FetchResult | None] = {}
    query = "sum(rate(loki_distributor_bytes_received_total[5m]))"
    res = promql_instant(client, query)
    if res.ok and has_results(res.data):
        ev.write("loki_instant_ingest_rate", res.data,
                 source_api=f"GET /api/v1/query?query={query}")
    results["ingest_rate"] = res
    return results


def collect_loki_direct(
    loki_endpoint: str, ev: EvidenceWriter, verify_ssl: bool, timeout: float,
) -> dict:
    """Collect Loki config and fallback metrics from the Loki endpoint."""
    results: dict[str, FetchResult | None] = {}
    with HttpClient(loki_endpoint, verify=verify_ssl, timeout_s=timeout) as lc:
        buildinfo = lc.get_json("/loki/api/v1/status/buildinfo")
        if buildinfo.ok:
            ev.write("loki_buildinfo", buildinfo.data,
                     source_api="GET /loki/api/v1/status/buildinfo")
        results["loki_buildinfo"] = buildinfo

        config = lc.get_text("/config")
        if config.ok:
            ev.write("loki_config", {"text": config.data},
                     source_api="GET /config")
        results["loki_config"] = config

        metrics = lc.get_text("/metrics")
        if metrics.ok:
            ev.write("loki_metrics_scrape", {"text": metrics.data},
                     source_api="GET /metrics")
        results["loki_metrics"] = metrics

    return results


def collect_tempo_from_tsdb(client: HttpClient, ev: EvidenceWriter) -> dict:
    """Query Tempo distributor metrics from the main TSDB."""
    results: dict[str, FetchResult | None] = {}
    for name, query in [
        ("spans_rate", "sum(rate(tempo_distributor_spans_received_total[5m]))"),
        ("ingest_rate", "sum(rate(tempo_distributor_bytes_received_total[5m]))"),
    ]:
        res = promql_instant(client, query)
        if res.ok and has_results(res.data):
            ev.write(f"tempo_instant_{name}", res.data,
                     source_api=f"GET /api/v1/query?query={query}")
        results[name] = res
    return results


def collect_tempo_direct(
    tempo_endpoint: str, ev: EvidenceWriter, verify_ssl: bool, timeout: float,
) -> dict:
    """Collect Tempo build info, config, and fallback metrics."""
    results: dict[str, FetchResult | None] = {}
    with HttpClient(tempo_endpoint, verify=verify_ssl, timeout_s=timeout) as tc:
        buildinfo = tc.get_json("/api/status/buildinfo")
        if buildinfo.ok:
            ev.write("tempo_buildinfo", buildinfo.data,
                     source_api="GET /api/status/buildinfo")
        results["tempo_buildinfo"] = buildinfo

        metrics = tc.get_text("/metrics")
        if metrics.ok:
            ev.write("tempo_metrics_scrape", {"text": metrics.data},
                     source_api="GET /metrics")
        results["tempo_metrics"] = metrics

    return results


# ---------------------------------------------------------------------------
# Figure derivation (per-backend)
# ---------------------------------------------------------------------------

def derive_prometheus(results: dict, summary: SummaryWriter) -> None:
    tsdb = results.get("tsdb_status")
    if tsdb and tsdb.ok:
        head = tsdb.data.get("data", {}).get("headStats", {})
        num_series = head.get("numSeries")
        if num_series is not None:
            summary.add_figure(Figure(
                id="metrics.active_series", label="Active time series",
                value=int(num_series), unit="series", status="ok",
                method="TSDB head stats",
                source_api="GET /api/v1/status/tsdb",
                evidence_files=["evidence/tsdb_status.json"],
            ))
        else:
            summary.mark_unavailable("metrics.active_series", "api_error",
                                     "headStats.numSeries missing from /status/tsdb")
    else:
        gap_reason = tsdb.gap_reason if tsdb else "endpoint_404"
        summary.mark_unavailable("metrics.active_series", gap_reason,
                                 "/api/v1/status/tsdb unavailable")

    _derive_instant_figure(
        results, "samples_rate", summary,
        "metrics.samples_per_sec", "Ingestion rate", "samples/sec",
        "sum(rate(prometheus_tsdb_head_samples_appended_total[5m]))",
        "5m rate of head samples appended",
    )

    _derive_instant_figure(
        results, "scrape_targets", summary,
        "promstack.scrape_targets", "Scrape targets", "targets",
        "count(up)", "count of up metric",
    )

    flags = results.get("status_flags")
    if flags and flags.ok:
        flags_data = flags.data.get("data", flags.data)
        retention_str = flags_data.get("storage.tsdb.retention.time", "")
        retention_s = parse_go_duration_s(retention_str) if retention_str else None
        if retention_s:
            summary.add_figure(Figure(
                id="promstack.retention_days", label="Retention period",
                value=round(retention_s / 86400, 1), unit="days", status="ok",
                method=f"storage.tsdb.retention.time={retention_str}",
                source_api="GET /api/v1/status/flags",
                evidence_files=["evidence/status_flags.json"],
            ))
        else:
            summary.mark_unavailable("promstack.retention_days", "api_error",
                                     "retention flag not found in /status/flags")
    else:
        summary.mark_unavailable("promstack.retention_days",
                                 flags.gap_reason if flags else "endpoint_404",
                                 "/api/v1/status/flags unavailable")


def derive_victoriametrics(results: dict, summary: SummaryWriter) -> None:
    _derive_instant_figure(
        results, "active_series", summary,
        "metrics.active_series", "Active time series", "series",
        'vm_cache_entries{type="storage/hour_metric_ids"}',
        "VM hour_metric_ids cache entry count",
    )

    _derive_instant_figure(
        results, "samples_rate", summary,
        "metrics.samples_per_sec", "Ingestion rate", "samples/sec",
        "sum(rate(vm_rows_inserted_total[5m]))",
        "5m rate of vm_rows_inserted_total",
    )

    _derive_instant_figure(
        results, "scrape_targets", summary,
        "promstack.scrape_targets", "Scrape targets", "targets",
        "count(up)", "count of up metric",
    )

    flags = results.get("status_flags")
    if flags and flags.ok:
        flags_data = flags.data if isinstance(flags.data, dict) else {}
        retention_str = flags_data.get("-retentionPeriod", "")
        retention_s = parse_go_duration_s(retention_str) if retention_str else None
        if retention_s:
            summary.add_figure(Figure(
                id="promstack.retention_days", label="Retention period",
                value=round(retention_s / 86400, 1), unit="days", status="ok",
                method=f"-retentionPeriod={retention_str}",
                source_api="GET /flags",
                evidence_files=["evidence/status_flags.json"],
            ))
        else:
            summary.mark_unavailable("promstack.retention_days", "api_error",
                                     "-retentionPeriod not found in /flags")
    else:
        summary.mark_unavailable("promstack.retention_days",
                                 flags.gap_reason if flags else "endpoint_404",
                                 "/flags unavailable")


def derive_thanos(results: dict, summary: SummaryWriter, cli_rf: int = 1) -> None:
    raw = results.get("active_series")
    rf_res = results.get("replication_factor")
    rf = cli_rf
    if rf_res and rf_res.ok:
        detected_rf = vector_first_value(rf_res.data)
        if detected_rf and detected_rf >= 1:
            rf = int(detected_rf)

    if raw and raw.ok:
        val = vector_first_value(raw.data)
        if val is not None:
            adjusted = int(val / rf)
            status = "partial" if rf > 1 else "ok"
            method = "sum(prometheus_tsdb_head_series)"
            if rf > 1:
                method += f" / RF={rf}"
            summary.add_figure(Figure(
                id="metrics.active_series", label="Active time series",
                value=adjusted, unit="series", status=status,
                method=method,
                source_api="GET /api/v1/query",
                query="sum(prometheus_tsdb_head_series)",
                evidence_files=["evidence/prom_instant_active_series.json"],
                notes=(f"Head-block only; divided by replication factor {rf}"
                       if rf > 1 else "Head-block series count"),
            ))
        else:
            summary.mark_unavailable("metrics.active_series", "api_error",
                                     "no result from sum(prometheus_tsdb_head_series)")
    else:
        summary.mark_unavailable("metrics.active_series",
                                 raw.gap_reason if raw else "api_error",
                                 "prometheus_tsdb_head_series query failed")

    _derive_instant_figure(
        results, "samples_rate", summary,
        "metrics.samples_per_sec", "Ingestion rate", "samples/sec",
        "sum(rate(prometheus_tsdb_head_samples_appended_total[5m]))",
        "5m rate of head samples appended",
    )

    _derive_instant_figure(
        results, "scrape_targets", summary,
        "promstack.scrape_targets", "Scrape targets", "targets",
        "count(up)", "count of up metric",
    )

    summary.mark_unavailable(
        "promstack.retention_days", "not_configured",
        "Thanos object-store retention is not queryable via API",
        remediation="Provide retention via --retention flag or check compactor config",
    )


def derive_loki(results: dict, summary: SummaryWriter) -> None:
    ingest_res = results.get("ingest_rate")
    if ingest_res and ingest_res.ok and has_results(ingest_res.data):
        bytes_per_sec = vector_first_value(ingest_res.data)
        if bytes_per_sec is not None and bytes_per_sec > 0:
            gb_per_day = bytes_per_sec * 86400 / (1024 ** 3)
            summary.add_figure(Figure(
                id="logs.ingest_gb_per_day", label="Log ingestion rate",
                value=round(gb_per_day, 2), unit="GB/day", status="ok",
                method="sum(rate(loki_distributor_bytes_received_total[5m])) * 86400 / GiB",
                source_api="GET /api/v1/query",
                query="sum(rate(loki_distributor_bytes_received_total[5m]))",
                evidence_files=["evidence/loki_instant_ingest_rate.json"],
            ))
        else:
            summary.mark_unavailable("logs.ingest_gb_per_day", "api_error",
                                     "loki_distributor_bytes_received_total returned 0 or null")
    else:
        metrics_res = results.get("loki_metrics")
        if metrics_res and metrics_res.ok:
            text = metrics_res.data if isinstance(metrics_res.data, str) else ""
            total_bytes = sum_prom_text_counter(text, "loki_distributor_bytes_received_total")
            uptime = _parse_uptime_from_text(text)
            if total_bytes is not None and uptime and uptime > 60:
                bytes_per_sec = total_bytes / uptime
                gb_per_day = bytes_per_sec * 86400 / (1024 ** 3)
                summary.add_figure(Figure(
                    id="logs.ingest_gb_per_day", label="Log ingestion rate",
                    value=round(gb_per_day, 2), unit="GB/day", status="estimated",
                    method=f"counter_total / uptime ({int(uptime)}s) — average since start",
                    source_api="GET /metrics (direct Loki scrape)",
                    evidence_files=["evidence/loki_metrics_scrape.json"],
                ))
            else:
                summary.mark_unavailable(
                    "logs.ingest_gb_per_day", "api_error",
                    "loki_distributor_bytes_received_total not found in /metrics")
        else:
            summary.mark_unavailable(
                "logs.ingest_gb_per_day", "not_configured",
                "Loki distributor metrics not found in TSDB",
                remediation="Ensure Loki metrics are scraped into your TSDB, "
                "or provide --loki-endpoint",
            )

    config_res = results.get("loki_config")
    if config_res and config_res.ok:
        text = (config_res.data if isinstance(config_res.data, str)
                else config_res.data.get("text", ""))
        retention_s = _parse_loki_retention(text)
        if retention_s:
            summary.add_figure(Figure(
                id="logs.retention_days", label="Log retention",
                value=round(retention_s / 86400, 1), unit="days", status="ok",
                method="limits_config.retention_period from /config",
                source_api="GET /config",
                evidence_files=["evidence/loki_config.json"],
            ))
        else:
            summary.mark_unavailable("logs.retention_days", "api_error",
                                     "retention_period not found in Loki /config")
    else:
        summary.mark_unavailable(
            "logs.retention_days", "not_configured",
            "Loki /config not available",
            remediation="Provide --loki-endpoint to read Loki config",
        )


def derive_tempo(results: dict, summary: SummaryWriter) -> None:
    spans_res = results.get("spans_rate")
    if spans_res and spans_res.ok and has_results(spans_res.data):
        val = vector_first_value(spans_res.data)
        if val is not None and val > 0:
            summary.add_figure(Figure(
                id="traces.spans_per_sec", label="Span ingestion rate",
                value=round(val, 1), unit="spans/sec", status="ok",
                method="sum(rate(tempo_distributor_spans_received_total[5m]))",
                source_api="GET /api/v1/query",
                query="sum(rate(tempo_distributor_spans_received_total[5m]))",
                evidence_files=["evidence/tempo_instant_spans_rate.json"],
            ))
        else:
            summary.mark_unavailable("traces.spans_per_sec", "api_error",
                                     "tempo_distributor_spans_received_total returned 0 or null")
    else:
        _try_tempo_metrics_fallback(results, summary, "spans")

    ingest_res = results.get("ingest_rate")
    if ingest_res and ingest_res.ok and has_results(ingest_res.data):
        bytes_per_sec = vector_first_value(ingest_res.data)
        if bytes_per_sec is not None and bytes_per_sec > 0:
            gb_per_day = bytes_per_sec * 86400 / (1024 ** 3)
            summary.add_figure(Figure(
                id="traces.ingest_gb_per_day", label="Trace ingestion rate",
                value=round(gb_per_day, 2), unit="GB/day", status="ok",
                method="sum(rate(tempo_distributor_bytes_received_total[5m])) * 86400 / GiB",
                source_api="GET /api/v1/query",
                query="sum(rate(tempo_distributor_bytes_received_total[5m]))",
                evidence_files=["evidence/tempo_instant_ingest_rate.json"],
            ))
        else:
            summary.mark_unavailable("traces.ingest_gb_per_day", "api_error",
                                     "tempo_distributor_bytes_received_total returned 0 or null")
    else:
        _try_tempo_metrics_fallback(results, summary, "bytes")


def _try_tempo_metrics_fallback(results: dict, summary: SummaryWriter, kind: str) -> None:
    metrics_res = results.get("tempo_metrics")
    if metrics_res and metrics_res.ok:
        text = metrics_res.data if isinstance(metrics_res.data, str) else ""
        if kind == "spans":
            metric_name = "tempo_distributor_spans_received_total"
            fig_id = "traces.spans_per_sec"
            label = "Span ingestion rate"
            unit = "spans/sec"
        else:
            metric_name = "tempo_distributor_bytes_received_total"
            fig_id = "traces.ingest_gb_per_day"
            label = "Trace ingestion rate"
            unit = "GB/day"
        total = sum_prom_text_counter(text, metric_name)
        uptime = _parse_uptime_from_text(text)
        if total is not None and uptime and uptime > 60:
            rate = total / uptime
            value = rate if kind == "spans" else rate * 86400 / (1024 ** 3)
            summary.add_figure(Figure(
                id=fig_id, label=label,
                value=round(value, 2), unit=unit, status="estimated",
                method=f"counter_total / uptime ({int(uptime)}s) — average since start",
                source_api="GET /metrics (direct Tempo scrape)",
                evidence_files=["evidence/tempo_metrics_scrape.json"],
            ))
        else:
            summary.mark_unavailable(fig_id, "api_error",
                                     f"{metric_name} not found in Tempo /metrics")
    else:
        fig_id = "traces.spans_per_sec" if kind == "spans" else "traces.ingest_gb_per_day"
        summary.mark_unavailable(
            fig_id, "not_configured",
            "Tempo distributor metrics not found in TSDB",
            remediation="Ensure Tempo metrics are scraped into your TSDB, "
            "or provide --tempo-endpoint",
        )


def _derive_instant_figure(
    results: dict, key: str, summary: SummaryWriter,
    fig_id: str, label: str, unit: str,
    query: str, method: str,
) -> None:
    res = results.get(key)
    if res and res.ok:
        val = vector_first_value(res.data)
        if val is not None:
            summary.add_figure(Figure(
                id=fig_id, label=label,
                value=round(val, 2) if isinstance(val, float) else int(val),
                unit=unit, status="ok",
                method=method, source_api="GET /api/v1/query", query=query,
                evidence_files=[f"evidence/prom_instant_{key}.json"],
            ))
        else:
            summary.mark_unavailable(fig_id, "api_error", f"no value from {query}")
    else:
        gap_reason = res.gap_reason if res else "api_error"
        summary.mark_unavailable(fig_id, gap_reason, f"{query} failed")


def _parse_loki_retention(config_text: str) -> int | None:
    """Extract retention_period from Loki YAML config text."""
    m = re.search(r"retention_period:\s*(\d+[dhms]+)", config_text)
    if m:
        return parse_go_duration_s(m.group(1))
    return None


def _parse_uptime_from_text(text: str) -> float | None:
    """Parse process_start_time_seconds from Prometheus text format, return uptime."""
    import time as _time
    for line in text.split("\n"):
        if line.startswith("process_start_time_seconds"):
            try:
                start = float(line.split()[-1])
                return _time.time() - start
            except (ValueError, IndexError):
                pass
    return None


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

def build_inventory(
    results: dict, loki_results: dict, tempo_results: dict,
    summary: SummaryWriter, backend: str,
) -> None:
    inv: dict = {"backend": backend}

    tsdb = results.get("tsdb_status")
    if tsdb and tsdb.ok:
        data = tsdb.data.get("data", {})
        head = data.get("headStats", data)
        series_by_metric = (
            head.get("seriesCountByMetricName")
            or data.get("seriesCountByMetricName")
            or []
        )
        if series_by_metric:
            inv["top_metrics_by_series"] = series_by_metric[:20]

    summary.inventory = inv


# ---------------------------------------------------------------------------
# Report-only evidence reconstruction
# ---------------------------------------------------------------------------

def reconstruct_results(cached: dict, backend: str) -> dict:
    results: dict[str, FetchResult] = {}
    mapping = {
        "tsdb_status": "tsdb_status",
        "status_flags": "status_flags",
        "prom_instant_samples_rate": "samples_rate",
        "prom_instant_scrape_targets": "scrape_targets",
        "prom_instant_active_series": "active_series",
        "prom_instant_replication_factor": "replication_factor",
    }
    for evidence_key, result_key in mapping.items():
        if evidence_key in cached:
            results[result_key] = FetchResult(ok=True, data=cached[evidence_key],
                                              status_code=200)
    return results


def reconstruct_loki_results(cached: dict) -> dict:
    results: dict[str, FetchResult] = {}
    if "loki_instant_ingest_rate" in cached:
        results["ingest_rate"] = FetchResult(ok=True, data=cached["loki_instant_ingest_rate"],
                                             status_code=200)
    if "loki_config" in cached:
        results["loki_config"] = FetchResult(ok=True, data=cached["loki_config"],
                                             status_code=200)
    if "loki_metrics_scrape" in cached:
        results["loki_metrics"] = FetchResult(ok=True, data=cached["loki_metrics_scrape"],
                                              status_code=200)
    return results


def reconstruct_tempo_results(cached: dict) -> dict:
    results: dict[str, FetchResult] = {}
    if "tempo_instant_spans_rate" in cached:
        results["spans_rate"] = FetchResult(ok=True, data=cached["tempo_instant_spans_rate"],
                                            status_code=200)
    if "tempo_instant_ingest_rate" in cached:
        results["ingest_rate"] = FetchResult(ok=True, data=cached["tempo_instant_ingest_rate"],
                                             status_code=200)
    if "tempo_metrics_scrape" in cached:
        results["tempo_metrics"] = FetchResult(ok=True, data=cached["tempo_metrics_scrape"],
                                               status_code=200)
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = base_parser("Prometheus stack collector", default_lookback="7d")
    parser.add_argument("--endpoint", help="Metrics endpoint URL (e.g. http://localhost:9090)")
    parser.add_argument("--kube", action="store_true", help="Use kubectl port-forward")
    parser.add_argument("--context", help="Kubernetes context (with --kube)")
    parser.add_argument("--namespace", "-n", default="monitoring",
                        help="Kubernetes namespace (with --kube)")
    parser.add_argument("--service", default="prometheus",
                        help="Service to port-forward (with --kube)")
    parser.add_argument("--service-port", type=int, default=9090,
                        help="Service port (with --kube)")
    parser.add_argument("--loki-endpoint", help="Loki HTTP endpoint (e.g. http://localhost:3100)")
    parser.add_argument("--tempo-endpoint", help="Tempo HTTP endpoint (e.g. http://localhost:3200)")
    parser.add_argument("--tenant", help="Tenant header (X-Scope-OrgID)")
    parser.add_argument("--replication-factor", type=int, default=1,
                        help="Thanos replication factor override (default: auto-detect)")
    args = parser.parse_args()

    parse_duration_s(args.lookback)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    ev = EvidenceWriter(output_dir)

    expected = list(EXPECTED_METRICS) + list(EXPECTED_LOKI) + list(EXPECTED_TEMPO)

    if args.report_only:
        cached = ev.load_all()
        if not cached:
            print("ERROR: no evidence found in", ev.evidence_dir, file=sys.stderr)
            return 2
        backend, version, method = detect_from_evidence(cached)
        results = reconstruct_results(cached, backend)
        loki_results = reconstruct_loki_results(cached)
        tempo_results = reconstruct_tempo_results(cached)
    else:
        if not args.endpoint and not args.kube:
            print("ERROR: --endpoint or --kube required", file=sys.stderr)
            return 1

        pf_ctx = None
        if args.kube:
            from lib.portforward import PortForward
            pf_ctx = PortForward(
                f"svc/{args.service}", args.service_port,
                namespace=args.namespace, context=args.context,
            )
            endpoint = pf_ctx.__enter__()
        else:
            endpoint = args.endpoint.rstrip("/")

        headers = parse_headers(args.header)
        if args.tenant:
            headers["X-Scope-OrgID"] = args.tenant

        try:
            with HttpClient(endpoint, verify=not args.insecure,
                            timeout_s=args.timeout, headers=headers) as client:
                backend, version, method = detect_backend(client, ev)

                if backend == "cortex":
                    print("Cortex detected — use the mimir collector instead.",
                          file=sys.stderr)
                    return 1

                if backend == "victoriametrics":
                    results = collect_victoriametrics(client, ev)
                elif backend == "thanos":
                    results = collect_thanos(client, ev)
                else:
                    results = collect_prometheus(client, ev)

                loki_results = collect_loki_from_tsdb(client, ev)
                tempo_results = collect_tempo_from_tsdb(client, ev)

            if args.loki_endpoint:
                loki_ep = collect_loki_direct(
                    args.loki_endpoint, ev, not args.insecure, args.timeout)
                loki_results.update(loki_ep)

            if args.tempo_endpoint:
                tempo_ep = collect_tempo_direct(
                    args.tempo_endpoint, ev, not args.insecure, args.timeout)
                tempo_results.update(tempo_ep)
        finally:
            if pf_ctx:
                pf_ctx.__exit__(None, None, None)

    target = args.endpoint or (f"kube:{args.namespace}/{args.service}" if args.kube else "unknown")
    summary = SummaryWriter(COLLECTOR, VERSION, expected, target, args.lookback)
    summary.environment = {
        "detected_backend": backend,
        "version": version,
        "detection_method": method,
    }

    if backend == "victoriametrics":
        derive_victoriametrics(results, summary)
    elif backend == "thanos":
        derive_thanos(results, summary, cli_rf=args.replication_factor)
    else:
        derive_prometheus(results, summary)

    derive_loki(loki_results, summary)
    derive_tempo(tempo_results, summary)
    build_inventory(results, loki_results, tempo_results, summary, backend)

    summary.write(output_dir)
    ev.finalize()
    if args.tar:
        ev.tar()

    all_figs = summary.to_dict()["figures"]
    ok = sum(1 for f in all_figs if f["status"] != "unavailable")
    gaps = len(all_figs) - ok
    print(f"\n{COLLECTOR} v{VERSION}: {ok}/{len(all_figs)} figures, {gaps} gap(s)")
    print(f"Backend: {backend} {version}")
    print(f"Output:  {output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
