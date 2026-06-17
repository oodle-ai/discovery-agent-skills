# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx", "boto3"]
# ///
"""OpenSearch discovery collector.

Collects cluster topology, index catalog, ingestion rates, search
performance, ISM policies, and optionally OpenSearch Dashboards saved
objects / data views.  Writes redacted evidence to disk and emits
summary.json.

Supports three deployment modes:
  - Self-hosted OpenSearch (basic auth / API key / headers)
  - AWS Managed OpenSearch Service (SigV4)
  - AWS OpenSearch Serverless / AOSS (SigV4, restricted API surface)

Direct mode (self-hosted):
    uv run collectors/opensearch/collect.py \
        --os-url https://opensearch:9200 --os-user admin --os-password secret \
        --output-dir ./discovery-output/opensearch

AWS Managed OpenSearch (SigV4):
    uv run collectors/opensearch/collect.py \
        --os-url https://search-domain.us-east-1.es.amazonaws.com \
        --aws-sigv4 --output-dir ./discovery-output/opensearch

AWS OpenSearch Serverless (AOSS):
    uv run collectors/opensearch/collect.py \
        --os-url https://collection.us-east-1.aoss.amazonaws.com \
        --aws-sigv4 --aws-service aoss \
        --output-dir ./discovery-output/opensearch

Dashboards proxy mode:
    uv run collectors/opensearch/collect.py \
        --dashboards-url https://dashboards.example.com \
        --header "Cookie: sid=abc123" \
        --output-dir ./discovery-output/opensearch

Report-only (recompute from evidence):
    uv run collectors/opensearch/collect.py \
        --report-only --output-dir ./discovery-output/opensearch
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.auth import basic_auth, boto3_session  # noqa: E402
from lib.cli import (  # noqa: E402
    base_parser,
    credential,
    parse_duration_days,
    parse_headers,
)
from lib.evidence import EvidenceWriter  # noqa: E402
from lib.http import FetchResult, HttpClient  # noqa: E402
from lib.summary import ExpectedFigure, Figure, SummaryWriter  # noqa: E402

COLLECTOR = "opensearch"
VERSION = "1.0.0"

EXPECTED = [
    ExpectedFigure("opensearch.total_docs", "Total documents", "docs", "opensearch"),
    ExpectedFigure(
        "opensearch.total_store_size_gb",
        "Total store size (primary + replica)",
        "GB",
        "opensearch",
    ),
    ExpectedFigure(
        "opensearch.primary_store_size_gb", "Primary store size", "GB", "opensearch"
    ),
    ExpectedFigure("logs.ingest_gb_per_day", "Daily ingestion (primary)", "GB/day", "logs"),
    ExpectedFigure("hosts.count", "Data nodes", "nodes", "hosts"),
    ExpectedFigure("opensearch.total_shards", "Active shards", "shards", "opensearch"),
    ExpectedFigure(
        "traces.ingest_gb_per_day", "OTel trace ingestion (primary)", "GB/day", "traces"
    ),
    ExpectedFigure("traces.spans_per_day", "OTel spans per day", "spans/day", "traces"),
]

OS_ENDPOINTS: list[tuple[str, str]] = [
    ("cluster_health", "/_cluster/health"),
    ("cluster_stats", "/_cluster/stats"),
    (
        "nodes_stats",
        "/_nodes/stats/fs,os,process,jvm,indices",
    ),
    ("nodes_info", "/_nodes/os,jvm"),
    (
        "cat_indices",
        "/_cat/indices?format=json&bytes=b"
        "&h=index,health,status,pri,rep,"
        "docs.count,docs.deleted,"
        "store.size,pri.store.size",
    ),
    ("index_settings", "/_all/_settings?filter_path=*.settings.index.creation_date"),
    ("cluster_index_stats", "/_stats"),
    ("snapshot_repos", "/_snapshot/_all"),
    ("nodes_usage", "/_nodes/usage"),
    (
        "cat_plugins",
        "/_cat/plugins?format=json",
    ),
]

AOSS_ENDPOINTS: list[tuple[str, str]] = [
    (
        "cat_indices",
        "/_cat/indices?format=json&bytes=b"
        "&h=index,health,status,pri,rep,"
        "docs.count,docs.deleted,"
        "store.size,pri.store.size",
    ),
    ("index_settings", "/_all/_settings?filter_path=*.settings.index.creation_date"),
]

DASHBOARDS_SAVED_OBJECT_TYPES = [
    "dashboard",
    "visualization",
    "visualization-visbuilder",
    "observability-visualization",
    "search",
    "index-pattern",
    "query",
    "lens",
    "map",
]

ISM_ENDPOINT = "/_plugins/_ism/policies"
ISM_LEGACY_ENDPOINT = "/_opendistro/_ism/policies"

_DATE_PATTERNS = [
    re.compile(r"(\d{4})[.\-](\d{2})[.\-](\d{2})"),
    re.compile(r"(\d{4})(\d{2})(\d{2})"),
]

_DATE_RE_STRIP = [
    re.compile(r"[.\-]?\d{4}[.\-]\d{2}[.\-]\d{2}[.\-]?\d*$"),
    re.compile(r"[.\-]?\d{8}[.\-]?\d*$"),
    re.compile(r"[.\-]\d{6}$"),
]

DATA_ROLES = {"data", "data_content", "data_hot", "data_warm", "data_cold", "data_frozen"}

OTEL_TRACE_PREFIXES = (
    "otel-v1-apm-span-",
    ".ds-ss4o_traces-",
    "ss4o_traces-",
)


def _is_trace_index(name: str) -> bool:
    return any(name.startswith(p) for p in OTEL_TRACE_PREFIXES)


# ── helpers ─────────────────────────────────────────────────────────────


def _redact_url(url: str | None) -> str | None:
    """Strip userinfo credentials from a URL."""
    if not url:
        return url
    from urllib.parse import urlparse, urlunparse

    p = urlparse(url)
    if p.username or p.password:
        host = p.hostname or ""
        if p.port:
            host = f"{host}:{p.port}"
        p = p._replace(netloc=host)
    return urlunparse(p)


def _make_os_get(
    client: HttpClient,
    dashboards_proxy: bool,
    os_host: str | None,
):
    """Return an os_get(path) function that dispatches via Dashboards proxy or direct."""
    if dashboards_proxy:

        def os_get(path: str) -> FetchResult:
            params = {"path": path, "method": "GET"}
            if os_host:
                params["host"] = os_host
            return client.post_json(
                "/api/console/proxy", json_body={}, params=params
            )

        return os_get
    return lambda path: client.get_json(path)


def _parse_date_from_name(name: str) -> str | None:
    for pat in _DATE_PATTERNS:
        m = pat.search(name)
        if m:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 2000 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                return f"{y:04d}-{mo:02d}-{d:02d}"
    return None


def _creation_date_str(index_name: str, settings: dict) -> str | None:
    ts = (
        settings.get(index_name, {})
        .get("settings", {})
        .get("index", {})
        .get("creation_date")
    )
    if ts:
        dt = datetime.fromtimestamp(int(ts) / 1000, tz=UTC)
        return dt.strftime("%Y-%m-%d")
    return None


def _resolve_date(index_name: str, settings: dict) -> str | None:
    return _parse_date_from_name(index_name) or _creation_date_str(index_name, settings)


def _index_to_pattern(name: str) -> str:
    for pat in _DATE_RE_STRIP:
        m = pat.search(name)
        if m:
            prefix = name[: m.start()]
            if prefix.endswith(("-", ".")):
                prefix = prefix[:-1]
            return f"{prefix}-*" if prefix else name
    return name


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(val) if val is not None else default
    except (ValueError, TypeError):
        return default


# ── SigV4 transport ────────────────────────────────────────────────────


class SigV4Transport:
    """httpx transport that signs requests with AWS SigV4.

    Creates one boto3 session at init; freezes credentials per-request so
    refreshable tokens (IAM roles, SSO) stay current.
    """

    def __init__(
        self,
        service: str = "es",
        profile: str | None = None,
        region: str | None = None,
        verify: bool = True,
    ) -> None:
        import httpx as _httpx
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest

        self._SigV4Auth = SigV4Auth
        self._AWSRequest = AWSRequest

        session = boto3_session(profile=profile, region=region)
        self._credentials_provider = session.get_credentials()
        if not self._credentials_provider:
            raise RuntimeError(
                "No AWS credentials found. Configure via env vars, profile, or IAM role."
            )
        self._service = service
        self._region = region or session.region_name
        if not self._region:
            raise RuntimeError(
                "No AWS region found. Set --aws-region or AWS_DEFAULT_REGION."
            )
        self._inner = _httpx.HTTPTransport(verify=verify)

    def handle_request(self, request) -> Any:
        creds = self._credentials_provider.get_frozen_credentials()
        aws_req = self._AWSRequest(
            method=request.method,
            url=str(request.url),
            headers=dict(request.headers),
            data=request.content,
        )
        self._SigV4Auth(creds, self._service, self._region).add_auth(aws_req)
        for key, val in aws_req.headers.items():
            request.headers[key] = val
        return self._inner.handle_request(request)

    def close(self) -> None:
        self._inner.close()


# ── collection ──────────────────────────────────────────────────────────


def fetch_os_endpoints(
    os_get,
    ev: EvidenceWriter,
    results: dict[str, Any],
    fetches: dict[str, FetchResult],
    *,
    endpoints: list[tuple[str, str]],
    skip_snapshots: bool = False,
) -> None:
    for name, path in endpoints:
        if name == "snapshot_repos" and skip_snapshots:
            print(f"skipping {name} (--skip-snapshots)")
            continue
        print(f"collecting {name}")
        res = os_get(path)
        fetches[name] = res
        if res.ok:
            results[name] = res.data
            ev.write(name, res.data, source_api=f"GET {path.split('?')[0]}")
        else:
            print(f"  WARN {name}: {res.error}")


def fetch_ism_policies(
    os_get,
    ev: EvidenceWriter,
    results: dict[str, Any],
    fetches: dict[str, FetchResult],
) -> None:
    print("collecting ism_policies")
    for endpoint in (ISM_ENDPOINT, ISM_LEGACY_ENDPOINT):
        res = os_get(f"{endpoint}?size=200")
        if res.ok:
            data = res.data
            if isinstance(data, dict) and "policies" in data:
                policies = data["policies"]
            elif isinstance(data, list):
                policies = data
            else:
                policies = [data]
            results["ism_policies"] = policies
            ev.write("ism_policies", policies, source_api=f"GET {endpoint}")
            fetches["ism_policies"] = res
            return
    fetches["ism_policies"] = res
    print(f"  WARN ism_policies: {res.error}")


def fetch_snapshots_detail(
    os_get,
    ev: EvidenceWriter,
    results: dict[str, Any],
) -> None:
    repos = results.get("snapshot_repos")
    if not repos or not isinstance(repos, dict):
        return
    all_details: list[dict] = []
    for repo_name in repos:
        if repo_name.startswith("cs-automated"):
            print(f"  skipping {repo_name} (AWS automated repo)")
            continue
        print(f"  collecting snapshots for repo {repo_name}")
        res = os_get(f"/_snapshot/{repo_name}/_all")
        if res.ok:
            all_details.append({"repository": repo_name, "snapshots": res.data})
    if all_details:
        results["snapshot_details"] = all_details
        ev.write("snapshot_details", all_details, source_api="GET /_snapshot/{repo}/_all")


def fetch_dashboards_saved_objects(
    client: HttpClient,
    ev: EvidenceWriter,
    results: dict[str, Any],
) -> None:
    all_objects: dict[str, list] = {}
    for obj_type in DASHBOARDS_SAVED_OBJECT_TYPES:
        print(f"  collecting dashboards saved_objects type={obj_type}")
        objects: list[dict] = []
        page = 1
        while True:
            res = client.get_json(
                "/api/saved_objects/_find",
                params={"type": obj_type, "per_page": "1000", "page": str(page)},
            )
            if not res.ok:
                if page == 1:
                    print(f"    WARN saved_objects/{obj_type}: {res.error}")
                break
            batch = res.data.get("saved_objects", []) if isinstance(res.data, dict) else []
            objects.extend(batch)
            total = res.data.get("total", 0) if isinstance(res.data, dict) else 0
            if len(objects) >= total or len(batch) < 1000:
                break
            page += 1
        if objects:
            key = f"dashboards_saved_objects_{obj_type}"
            all_objects[obj_type] = objects
            results[key] = objects
            ev.write(key, objects, source_api=f"GET /api/saved_objects/_find?type={obj_type}")
    results["dashboards_saved_objects_summary"] = {t: len(v) for t, v in all_objects.items()}


def fetch_dashboards_data_views(
    client: HttpClient,
    ev: EvidenceWriter,
    results: dict[str, Any],
) -> None:
    print("  collecting dashboards data_views")
    res = client.get_json("/api/data_views")
    if not res.ok:
        print(f"    WARN data_views: {res.error}")
        return
    views = res.data.get("data_view", []) if isinstance(res.data, dict) else []
    if not views:
        views = res.data.get("data_views", []) if isinstance(res.data, dict) else []
    if not views:
        return
    details: list[dict] = []
    for v in views:
        vid = v.get("id", "")
        detail = client.get_json(f"/api/data_views/data_view/{vid}")
        if detail.ok:
            raw = detail.data
            dv = raw.get("data_view", raw) if isinstance(raw, dict) else raw
            details.append(dv)
    if details:
        results["dashboards_data_views"] = details
        ev.write("dashboards_data_views", details, source_api="GET /api/data_views")


# ── derivation ──────────────────────────────────────────────────────────


def derive_daily_ingestion(
    results: dict[str, Any],
    lookback_days: float,
) -> tuple[float | None, str, str, list[dict]]:
    """Derive daily primary ingestion from date-based index sizes.

    Returns (gb_per_day | None, status, method, daily_breakdown).
    """
    cat_indices = results.get("cat_indices")
    settings = results.get("index_settings") or {}
    if not cat_indices or not isinstance(cat_indices, list):
        return None, "unavailable", "", []

    now = datetime.now(UTC)
    cutoff = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")

    by_day: dict[str, int] = defaultdict(int)
    settings_used = False
    name_parse_used = False

    for idx in cat_indices:
        name = idx.get("index", "")
        if name.startswith(".") or _is_trace_index(name):
            continue
        from_name = _parse_date_from_name(name)
        date = from_name or _creation_date_str(name, settings)
        if not date or date < cutoff or date > today:
            continue
        if from_name:
            name_parse_used = True
        else:
            settings_used = True
        pri_size = _safe_int(idx.get("pri.store.size"))
        by_day[date] += pri_size

    if not by_day:
        return None, "unavailable", "", []

    total_bytes = sum(by_day.values())
    num_days = len(by_day)
    gb_per_day = total_bytes / num_days / 1e9

    if settings_used and not name_parse_used:
        method_detail = "creation_date from /_all/_settings"
    elif name_parse_used and not settings_used:
        method_detail = "date parsed from index names"
    else:
        method_detail = "date from index names + creation_date fallback"

    status = "ok" if num_days >= 3 else "estimated"
    method = (
        f"sum of pri.store.size for date-based indices in {num_days}-day window "
        f"/ {num_days} days, bytes -> GB (1e9); {method_detail}"
    )

    daily = sorted(
        [{"date": d, "primary_gb": round(b / 1e9, 3)} for d, b in by_day.items()],
        key=lambda x: x["date"],
    )
    return round(gb_per_day, 2), status, method, daily


def derive_otel_traces(
    results: dict[str, Any],
    lookback_days: float,
) -> tuple[float | None, float | None, str, str]:
    """Derive OTel trace volume from trace indices.

    Returns (gb_per_day | None, spans_per_day | None, status, method).
    """
    cat_indices = results.get("cat_indices")
    settings = results.get("index_settings") or {}
    if not cat_indices or not isinstance(cat_indices, list):
        return None, None, "unavailable", ""

    now = datetime.now(UTC)
    cutoff = (now - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")

    by_day_bytes: dict[str, int] = defaultdict(int)
    by_day_docs: dict[str, int] = defaultdict(int)

    for idx in cat_indices:
        name = idx.get("index", "")
        if not _is_trace_index(name):
            continue
        date = _parse_date_from_name(name) or _creation_date_str(name, settings)
        if not date or date < cutoff or date > today:
            continue
        by_day_bytes[date] += _safe_int(idx.get("pri.store.size"))
        by_day_docs[date] += _safe_int(idx.get("docs.count"))

    if not by_day_bytes:
        return None, None, "unavailable", ""

    num_days = len(by_day_bytes)
    total_bytes = sum(by_day_bytes.values())
    total_docs = sum(by_day_docs.values())
    gb_per_day = round(total_bytes / num_days / 1e9, 2)
    spans_per_day = round(total_docs / num_days)
    status = "ok" if num_days >= 3 else "estimated"
    method = (
        f"sum of pri.store.size / docs.count for OTel trace indices "
        f"({', '.join(OTEL_TRACE_PREFIXES)}) in {num_days}-day window"
    )
    return gb_per_day, spans_per_day, status, method


def group_index_patterns(
    cat_indices: list[dict],
    settings: dict,
    data_views: list[dict] | None = None,
) -> list[dict]:
    """Group indices by pattern (from data views or name stripping)."""
    if not cat_indices:
        return []

    groups: dict[str, list[dict]] = defaultdict(list)

    if data_views:
        import fnmatch

        matched: set[str] = set()
        for dv in data_views:
            title = dv.get("title", "")
            if not title:
                continue
            for idx in cat_indices:
                name = idx.get("index", "")
                if fnmatch.fnmatch(name, title):
                    groups[title].append(idx)
                    matched.add(name)
        for idx in cat_indices:
            if idx.get("index", "") not in matched:
                groups[_index_to_pattern(idx.get("index", ""))].append(idx)
    else:
        for idx in cat_indices:
            name = idx.get("index", "")
            groups[_index_to_pattern(name)].append(idx)

    result = []
    for pattern, idxs in sorted(groups.items(), key=lambda kv: -sum(
        _safe_int(i.get("store.size")) for i in kv[1]
    )):
        total_docs = sum(_safe_int(i.get("docs.count")) for i in idxs)
        total_size = sum(_safe_int(i.get("store.size")) for i in idxs)
        pri_size = sum(_safe_int(i.get("pri.store.size")) for i in idxs)
        latest = max(
            (_resolve_date(i.get("index", ""), settings) or "" for i in idxs),
            default="",
        )
        result.append({
            "pattern": pattern,
            "index_count": len(idxs),
            "total_docs": total_docs,
            "total_size_bytes": total_size,
            "primary_size_bytes": pri_size,
            "latest_date": latest or None,
        })
    return result


def derive_search_stats(results: dict[str, Any]) -> dict[str, Any]:
    stats = results.get("cluster_index_stats")
    if not stats:
        return {}
    total = stats.get("_all", {}).get("total", {}).get("search", {})
    query_total = total.get("query_total", 0)
    query_time = total.get("query_time_in_millis", 0)
    fetch_total = total.get("fetch_total", 0)
    fetch_time = total.get("fetch_time_in_millis", 0)
    scroll_total = total.get("scroll_total", 0)
    suggest_total = total.get("suggest_total", 0)
    return {
        "query_total": query_total,
        "query_time_ms": query_time,
        "avg_query_latency_ms": round(query_time / query_total, 2) if query_total else None,
        "fetch_total": fetch_total,
        "fetch_time_ms": fetch_time,
        "avg_fetch_latency_ms": round(fetch_time / fetch_total, 2) if fetch_total else None,
        "scroll_total": scroll_total,
        "suggest_total": suggest_total,
    }


def derive_rest_api_usage(results: dict[str, Any]) -> list[dict]:
    usage = results.get("nodes_usage")
    if not usage:
        return []
    totals: dict[str, int] = defaultdict(int)
    for node in usage.get("nodes", {}).values():
        for action, count in node.get("rest_actions", {}).items():
            totals[action] += count
    return [
        {"action": a, "count": c}
        for a, c in sorted(totals.items(), key=lambda kv: -kv[1])[:30]
    ]


# ── summary builder ─────────────────────────────────────────────────────


def build_summary(
    results: dict[str, Any],
    fetches: dict[str, FetchResult],
    summary: SummaryWriter,
    lookback_days: float,
    *,
    is_aoss: bool = False,
) -> None:
    cat_indices = results.get("cat_indices")
    settings = results.get("index_settings") or {}

    # ── opensearch.total_docs ──────────────────────────────────────
    if isinstance(cat_indices, list) and cat_indices:
        total_docs = sum(_safe_int(i.get("docs.count")) for i in cat_indices)
        total_store = sum(_safe_int(i.get("store.size")) for i in cat_indices)
        pri_store = sum(_safe_int(i.get("pri.store.size")) for i in cat_indices)

        summary.add_figure(Figure(
            id="opensearch.total_docs",
            label="Total documents",
            value=float(total_docs),
            unit="docs",
            status="ok",
            method="sum of docs.count across all indices",
            source_api="GET /_cat/indices",
            evidence_files=["evidence/cat_indices.json"],
        ))
        summary.add_figure(Figure(
            id="opensearch.total_store_size_gb",
            label="Total store size (primary + replica)",
            value=round(total_store / 1e9, 2),
            unit="GB",
            status="ok",
            method="sum of store.size across all indices, bytes -> GB (1e9)",
            source_api="GET /_cat/indices",
            evidence_files=["evidence/cat_indices.json"],
        ))
        summary.add_figure(Figure(
            id="opensearch.primary_store_size_gb",
            label="Primary store size",
            value=round(pri_store / 1e9, 2),
            unit="GB",
            status="ok",
            method="sum of pri.store.size across all indices, bytes -> GB (1e9)",
            source_api="GET /_cat/indices",
            evidence_files=["evidence/cat_indices.json"],
        ))
    else:
        res = fetches.get("cat_indices")
        reason = res.gap_reason if res and res.gap_reason else "api_error"
        detail = res.error if res and res.error else "/_cat/indices returned no data"
        for fid in (
            "opensearch.total_docs",
            "opensearch.total_store_size_gb",
            "opensearch.primary_store_size_gb",
        ):
            summary.mark_unavailable(fid, reason, detail)

    # ── hosts.count ─────────────────────────────────────────────────
    if is_aoss:
        summary.mark_unavailable(
            "hosts.count",
            "version_unsupported",
            "AOSS (serverless) does not expose node/cluster APIs",
            remediation="node count is not applicable to OpenSearch Serverless",
        )
    else:
        nodes_info = results.get("nodes_info")
        if nodes_info and isinstance(nodes_info.get("nodes"), dict):
            data_nodes = sum(
                1
                for n in nodes_info["nodes"].values()
                if DATA_ROLES & set(n.get("roles", []))
            )
            if data_nodes == 0:
                health = results.get("cluster_health") or {}
                data_nodes = health.get("number_of_data_nodes", 0)
            summary.add_figure(Figure(
                id="hosts.count",
                label="Data nodes",
                value=float(data_nodes),
                unit="nodes",
                status="ok",
                method="count of nodes with data roles from /_nodes/os,jvm",
                source_api="GET /_nodes/os,jvm",
                evidence_files=["evidence/nodes_info.json"],
            ))
        else:
            health = results.get("cluster_health") or {}
            dn = health.get("number_of_data_nodes")
            if dn is not None:
                summary.add_figure(Figure(
                    id="hosts.count",
                    label="Data nodes",
                    value=float(dn),
                    unit="nodes",
                    status="ok",
                    method="number_of_data_nodes from /_cluster/health",
                    source_api="GET /_cluster/health",
                    evidence_files=["evidence/cluster_health.json"],
                ))
            else:
                res = fetches.get("nodes_info")
                reason = res.gap_reason if res and res.gap_reason else "api_error"
                detail = res.error if res and res.error else "node info unavailable"
                summary.mark_unavailable("hosts.count", reason, detail)

    # ── opensearch.total_shards ────────────────────────────────────
    if is_aoss:
        summary.mark_unavailable(
            "opensearch.total_shards",
            "version_unsupported",
            "AOSS (serverless) does not expose /_cluster/health",
            remediation="shard count is managed by the service for serverless collections",
        )
    else:
        health = results.get("cluster_health")
        if health and health.get("active_shards") is not None:
            summary.add_figure(Figure(
                id="opensearch.total_shards",
                label="Active shards",
                value=float(health["active_shards"]),
                unit="shards",
                status="ok",
                method="active_shards from /_cluster/health",
                source_api="GET /_cluster/health",
                evidence_files=["evidence/cluster_health.json"],
            ))
        else:
            res = fetches.get("cluster_health")
            reason = res.gap_reason if res and res.gap_reason else "api_error"
            detail = res.error if res and res.error else "cluster health unavailable"
            summary.mark_unavailable("opensearch.total_shards", reason, detail)

    # ── logs.ingest_gb_per_day ──────────────────────────────────────
    gb_per_day, ing_status, ing_method, daily = derive_daily_ingestion(results, lookback_days)
    if gb_per_day is not None:
        evidence = ["evidence/cat_indices.json"]
        if results.get("index_settings"):
            evidence.append("evidence/index_settings.json")
        summary.add_figure(Figure(
            id="logs.ingest_gb_per_day",
            label="Daily ingestion (primary)",
            value=gb_per_day,
            unit="GB/day",
            status=ing_status,
            method=ing_method,
            source_api="GET /_cat/indices + GET /_all/_settings",
            evidence_files=evidence,
        ))
    else:
        summary.mark_unavailable(
            "logs.ingest_gb_per_day",
            "not_configured",
            "no date-based indices found in the lookback window",
            remediation="ensure indices follow date-based naming (e.g. logs-2026.01.15) "
            "or have creation_date in settings",
        )

    # ── traces (OTel) ──────────────────────────────────────────────
    trace_evidence = ["evidence/cat_indices.json"]
    if results.get("index_settings"):
        trace_evidence.append("evidence/index_settings.json")
    trace_gb, trace_spans, trace_status, trace_method = derive_otel_traces(
        results, lookback_days
    )
    if trace_gb is not None:
        summary.add_figure(Figure(
            id="traces.ingest_gb_per_day",
            label="OTel trace ingestion (primary)",
            value=trace_gb,
            unit="GB/day",
            status=trace_status,
            method=trace_method,
            source_api="GET /_cat/indices + GET /_all/_settings",
            evidence_files=trace_evidence,
        ))
        summary.add_figure(Figure(
            id="traces.spans_per_day",
            label="OTel spans per day",
            value=float(trace_spans),
            unit="spans/day",
            status=trace_status,
            method=trace_method,
            source_api="GET /_cat/indices + GET /_all/_settings",
            evidence_files=trace_evidence,
        ))
    else:
        summary.mark_unavailable(
            "traces.ingest_gb_per_day",
            "not_configured",
            "no OTel trace indices found "
            f"({', '.join(OTEL_TRACE_PREFIXES)})",
            remediation="if Data Prepper / OTel tracing is in use, ensure trace indices "
            "exist and are within the lookback window",
        )
        summary.mark_unavailable(
            "traces.spans_per_day",
            "not_configured",
            "no OTel trace indices found "
            f"({', '.join(OTEL_TRACE_PREFIXES)})",
            remediation="if Data Prepper / OTel tracing is in use, ensure trace indices "
            "exist and are within the lookback window",
        )

    # ── environment ─────────────────────────────────────────────────
    cluster_stats = results.get("cluster_stats") or {}
    cluster_health = results.get("cluster_health") or {}
    versions = cluster_stats.get("nodes", {}).get("versions", [])

    backend = "opensearch_serverless" if is_aoss else "opensearch"
    summary.environment = {
        "detected_backend": backend,
        "version": versions[0] if versions else None,
        "cluster_name": cluster_health.get("cluster_name"),
        "detection_method": (
            "AOSS mode (--aws-service aoss)" if is_aoss
            else "/_cluster/stats nodes.versions"
        ),
    }

    # ── inventory ───────────────────────────────────────────────────
    inv = summary.inventory

    if not is_aoss:
        inv["cluster_name"] = cluster_health.get("cluster_name")
        inv["cluster_status"] = cluster_health.get("status")
        inv["number_of_nodes"] = cluster_health.get("number_of_nodes")
        inv["number_of_data_nodes"] = cluster_health.get("number_of_data_nodes")
        inv["unassigned_shards"] = cluster_health.get("unassigned_shards", 0)

        # cluster resource totals
        nodes_os = cluster_stats.get("nodes", {}).get("os", {}).get("mem", {})
        nodes_fs = cluster_stats.get("nodes", {}).get("fs", {})
        nodes_jvm = cluster_stats.get("nodes", {}).get("jvm", {}).get("mem", {})
        inv["cluster_memory_bytes"] = nodes_os.get("total_in_bytes")
        inv["cluster_disk_total_bytes"] = nodes_fs.get("total_in_bytes")
        inv["cluster_disk_available_bytes"] = nodes_fs.get("available_in_bytes")
        inv["cluster_jvm_heap_max_bytes"] = nodes_jvm.get("heap_max_in_bytes")

    # node fleet
    ns = results.get("nodes_stats")
    ni = results.get("nodes_info")
    if ns and isinstance(ns.get("nodes"), dict):
        fleet = []
        ni_nodes = ni.get("nodes", {}) if ni else {}
        for nid, nd in ns["nodes"].items():
            info = ni_nodes.get(nid, {})
            mem = nd.get("os", {}).get("mem", {})
            jvm = nd.get("jvm", {}).get("mem", {})
            fs = nd.get("fs", {}).get("total", {})
            heap_max = jvm.get("heap_max_in_bytes", 0)
            heap_used = jvm.get("heap_used_in_bytes", 0)
            disk_total = fs.get("total_in_bytes", 0)
            disk_avail = fs.get("available_in_bytes", 0)
            fleet.append({
                "name": nd.get("name"),
                "roles": nd.get("roles", info.get("roles", [])),
                "cpu_count": info.get("os", {}).get("available_processors"),
                "ram_total_bytes": mem.get("total_in_bytes"),
                "ram_used_percent": mem.get("used_percent"),
                "heap_max_bytes": heap_max,
                "heap_used_percent": round(heap_used / heap_max * 100, 1) if heap_max else None,
                "disk_total_bytes": disk_total,
                "disk_available_bytes": disk_avail,
                "disk_used_percent": (
                    round((disk_total - disk_avail) / disk_total * 100, 1)
                    if disk_total
                    else None
                ),
            })
        inv["node_fleet"] = sorted(fleet, key=lambda n: n.get("name") or "")

    # index patterns
    data_views = results.get("dashboards_data_views")
    if isinstance(cat_indices, list):
        inv["index_patterns"] = group_index_patterns(cat_indices, settings, data_views)

    # daily ingestion breakdown
    if daily:
        inv["daily_ingestion"] = daily

    # search stats (not available on AOSS)
    if not is_aoss:
        search = derive_search_stats(results)
        if search:
            inv["search_stats"] = search

    # ISM policies
    ism = results.get("ism_policies")
    if isinstance(ism, list):
        policies = []
        for policy_item in ism:
            policy_body = policy_item.get("policy", policy_item)
            name = policy_body.get("policy_id", policy_item.get("_id", "unknown"))
            states = policy_body.get("states", [])
            state_names = [s.get("name", "") for s in states]
            policy_info: dict[str, Any] = {"name": name, "states": state_names}
            for state in states:
                actions = state.get("actions", [])
                for action in actions:
                    if "rollover" in action and "rollover" not in policy_info:
                        ro = action["rollover"]
                        policy_info["rollover"] = {
                            k: v for k, v in ro.items()
                            if k in ("min_size", "min_doc_count", "min_index_age")
                        }
                    if "delete" in action:
                        policy_info["has_delete"] = True
                transitions = state.get("transitions", [])
                for trans in transitions:
                    cond = trans.get("conditions", {})
                    if "min_index_age" in cond:
                        policy_info.setdefault("transition_ages", {})[
                            state.get("name", "")
                        ] = cond["min_index_age"]
            policies.append(policy_info)
        inv["ism_policies"] = policies

    # REST API usage
    if not is_aoss:
        rest_usage = derive_rest_api_usage(results)
        if rest_usage:
            inv["rest_api_usage"] = rest_usage

    # Dashboards saved objects summary
    so_summary = results.get("dashboards_saved_objects_summary")
    if so_summary:
        inv["saved_objects_summary"] = so_summary

    # snapshot repos
    snap_repos = results.get("snapshot_repos")
    if isinstance(snap_repos, dict):
        inv["snapshot_repositories"] = list(snap_repos.keys())

    # snapshot details
    snap_details = results.get("snapshot_details")
    if snap_details:
        snap_summary = []
        for repo in snap_details:
            snaps = repo.get("snapshots", {})
            snap_list = snaps.get("snapshots", snaps) if isinstance(snaps, dict) else snaps
            if isinstance(snap_list, list):
                for s in snap_list:
                    snap_summary.append({
                        "repository": repo.get("repository", ""),
                        "snapshot": s.get("snapshot", ""),
                        "state": s.get("state", ""),
                    })
        if snap_summary:
            inv["snapshots"] = snap_summary

    # OTel trace indices summary
    if isinstance(cat_indices, list):
        trace_indices = [i for i in cat_indices if _is_trace_index(i.get("index", ""))]
        if trace_indices:
            total_docs = sum(_safe_int(i.get("docs.count")) for i in trace_indices)
            total_pri = sum(_safe_int(i.get("pri.store.size")) for i in trace_indices)
            inv["otel_traces"] = {
                "index_count": len(trace_indices),
                "total_spans": total_docs,
                "total_primary_size_gb": round(total_pri / 1e9, 2),
            }

    # plugins
    plugins = results.get("cat_plugins")
    if isinstance(plugins, list) and plugins:
        seen: set[tuple[str, str]] = set()
        unique = []
        for p in plugins:
            key = (p.get("component", p.get("name", "")), p.get("version", ""))
            if key not in seen:
                seen.add(key)
                unique.append({"component": key[0], "version": key[1]})
        inv["plugins"] = unique


# ── main ────────────────────────────────────────────────────────────────


def main() -> int:
    parser = base_parser("OpenSearch discovery collector", default_lookback="7d")
    parser.add_argument(
        "--os-url",
        metavar="URL",
        help="OpenSearch base URL (e.g. https://opensearch:9200). Env: OS_URL",
    )
    parser.add_argument("--os-user", metavar="USER", help="Basic auth username. Env: OS_USER")
    parser.add_argument(
        "--os-password", metavar="PASS", help="Basic auth password. Env: OS_PASSWORD"
    )
    parser.add_argument(
        "--dashboards-url",
        metavar="URL",
        help="OpenSearch Dashboards base URL; when set, OS calls route through the "
        "Dashboards console proxy. Env: DASHBOARDS_URL",
    )
    parser.add_argument(
        "--os-host",
        metavar="URL",
        help="Internal OS URL for Dashboards proxy mode (passed to proxy as host param). "
        "Env: OS_HOST",
    )
    parser.add_argument(
        "--aws-sigv4",
        action="store_true",
        help="Use AWS SigV4 authentication (requires boto3 + valid AWS credentials)",
    )
    parser.add_argument(
        "--aws-service",
        default="es",
        choices=("es", "aoss"),
        help="AWS service name for SigV4: 'es' for managed OpenSearch, 'aoss' for Serverless",
    )
    parser.add_argument("--aws-profile", metavar="NAME", help="AWS CLI profile name")
    parser.add_argument(
        "--aws-region", metavar="REGION", help="AWS region. Env: AWS_DEFAULT_REGION"
    )
    parser.add_argument(
        "--skip-dashboards-objects",
        action="store_true",
        help="Skip Dashboards saved objects and data views collection",
    )
    parser.add_argument(
        "--skip-snapshots",
        action="store_true",
        help="Skip snapshot repository enumeration",
    )
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ev = EvidenceWriter(args.output_dir)
    results: dict[str, Any] = {}
    fetches: dict[str, FetchResult] = {}
    lookback_days = parse_duration_days(args.lookback)
    is_aoss = args.aws_sigv4 and args.aws_service == "aoss"

    if args.report_only:
        results = ev.load_all()
        if not results:
            print(f"ERROR: --report-only but no evidence under {ev.evidence_dir}")
            return 2
    else:
        dashboards_url = args.dashboards_url or credential(
            None, "DASHBOARDS_URL", "Dashboards URL"
        )
        os_url = args.os_url or credential(None, "OS_URL", "OpenSearch URL")
        os_host = args.os_host or credential(None, "OS_HOST", "OS host for proxy")

        if not dashboards_url and not os_url:
            print(
                "ERROR: provide --os-url or --dashboards-url "
                "(or set OS_URL / DASHBOARDS_URL)."
            )
            return 2

        use_dashboards = bool(dashboards_url)
        base_url = dashboards_url if use_dashboards else os_url
        extra_headers = parse_headers(args.header)

        # resolve auth
        auth_tuple = None
        transport = None

        if use_dashboards:
            extra_headers.setdefault("osd-xsrf", "opensearchDashboards")

        if args.aws_sigv4:
            transport = SigV4Transport(
                service=args.aws_service,
                profile=args.aws_profile,
                region=args.aws_region,
                verify=not args.insecure,
            )
        else:
            user = credential(args.os_user, "OS_USER", "OS username", interactive_ok=True)
            password = credential(
                args.os_password, "OS_PASSWORD", "OS password", interactive_ok=True
            )
            if user and password:
                auth_tuple = basic_auth(user, password)

        with HttpClient(
            base_url=base_url,
            headers=extra_headers,
            auth=auth_tuple,
            timeout_s=args.timeout,
            verify=not args.insecure,
            transport=transport,
        ) as client:
            endpoints = AOSS_ENDPOINTS if is_aoss else OS_ENDPOINTS
            os_get = _make_os_get(client, use_dashboards, os_host)
            fetch_os_endpoints(
                os_get, ev, results, fetches,
                endpoints=endpoints,
                skip_snapshots=args.skip_snapshots,
            )
            if not is_aoss:
                fetch_ism_policies(os_get, ev, results, fetches)
                if not args.skip_snapshots:
                    fetch_snapshots_detail(os_get, ev, results)
            if use_dashboards and not args.skip_dashboards_objects:
                fetch_dashboards_saved_objects(client, ev, results)
                fetch_dashboards_data_views(client, ev, results)

    target = _redact_url(args.dashboards_url or args.os_url) or "report-only"
    summary = SummaryWriter(
        collector=COLLECTOR,
        collector_version=VERSION,
        expected=EXPECTED,
        target=target,
        lookback=args.lookback,
        args_redacted={
            "os_url": _redact_url(args.os_url),
            "dashboards_url": _redact_url(args.dashboards_url),
            "aws_sigv4": args.aws_sigv4,
            "aws_service": args.aws_service if args.aws_sigv4 else None,
            "lookback": args.lookback,
            "skip_dashboards_objects": args.skip_dashboards_objects,
            "skip_snapshots": args.skip_snapshots,
        },
    )
    build_summary(results, fetches, summary, lookback_days, is_aoss=is_aoss)
    summary.write(args.output_dir)
    ev.finalize()
    if args.tar:
        ev.tar()

    doc = summary.to_dict()
    unavailable = [f for f in doc["figures"] if f["status"] == "unavailable"]
    print(
        f"done: {len(EXPECTED) - len(unavailable)}/{len(EXPECTED)} expected figures collected; "
        f"{len(unavailable)} gap(s)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
