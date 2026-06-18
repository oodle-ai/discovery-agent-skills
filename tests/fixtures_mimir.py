"""Synthetic Mimir API responses with known ground truth.

All values are fabricated; figures derived from them are asserted exactly in
tests. The fixture shapes match real Mimir Prometheus API responses.
"""
from __future__ import annotations

# ── ground-truth constants ──────────────────────────────────────────────

ACTIVE_SERIES = 4_823_511.0
INGESTION_RATE_AVG = 125_000.0
INGESTION_RATE_PEAK = 180_000.0
QUERY_RATE_AVG = 42.5
QUERY_RATE_PEAK = 85.0
OBJSTORE_OPS_PER_DAY_APPROX = 8_640_000.0  # 100 ops/s * 86400

RETENTION_DAYS = 30.0
REPLICATION_FACTOR = 3
VERSION = "2.14.0"

# Per-tenant breakdown
TENANT_A_SERIES = 3_000_000.0
TENANT_B_SERIES = 1_823_511.0
TENANT_A_RATE = 80_000.0
TENANT_B_RATE = 45_000.0

# Cost estimation (storage + API only, no compute)
# storage_gb = ingest_avg * bytes_per_sample * retention_s / 1024^3
# = 125000 * 1.5 * 30*86400 / 1073741824 ≈ 452.6 GB
BYTES_PER_SAMPLE = 1.5
ESTIMATED_STORAGE_GB = INGESTION_RATE_AVG * BYTES_PER_SAMPLE * RETENTION_DAYS * 86400 / (1024**3)
API_COST_MO = (OBJSTORE_OPS_PER_DAY_APPROX / 1000) * 30 * 0.0004
OBJSTORE_COST_MO = ESTIMATED_STORAGE_GB * 0.023
TOTAL_COST_MO = API_COST_MO + OBJSTORE_COST_MO


# ── Prometheus instant query responses ──────────────────────────────────


def _instant_vector(value: float, metric: dict | None = None) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": metric or {}, "value": [1718700000, str(value)]}
            ],
        },
    }


def _instant_vector_multi(entries: list[tuple[dict, float]]) -> dict:
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": m, "value": [1718700000, str(v)]} for m, v in entries
            ],
        },
    }


ACTIVE_SERIES_SUM = _instant_vector(ACTIVE_SERIES)

ACTIVE_SERIES_BY_USER = _instant_vector_multi([
    ({"user": "tenant-a"}, TENANT_A_SERIES),
    ({"user": "tenant-b"}, TENANT_B_SERIES),
])

SAMPLES_RATE_BY_USER = _instant_vector_multi([
    ({"user": "tenant-a"}, TENANT_A_RATE),
    ({"user": "tenant-b"}, TENANT_B_RATE),
])


# ── Prometheus range query responses ────────────────────────────────────

# For range queries, we generate a matrix with values that average to our
# ground truth and peak at the expected peak.


def _range_matrix_single(avg: float, peak: float, num_points: int = 10) -> dict:
    """Build a range query response with one series whose values average
    to `avg` and max to `peak`."""
    # Generate values: 9 at avg, 1 at peak (adjusting the 9 so the overall avg is correct)
    non_peak_count = num_points - 1
    non_peak_val = (avg * num_points - peak) / non_peak_count
    base_ts = 1718700000
    step = 300
    values = []
    for i in range(non_peak_count):
        values.append([base_ts + i * step, str(non_peak_val)])
    values.append([base_ts + non_peak_count * step, str(peak)])
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": [{"metric": {}, "values": values}],
        },
    }


def _range_matrix_by_label(entries: list[tuple[str, str, float]], num_points: int = 5) -> dict:
    """Build a range matrix with multiple series by label. Each series has
    constant values = rate_per_sec."""
    base_ts = 1718700000
    step = 300
    result = []
    for label_key, label_val, rate in entries:
        values = [[base_ts + i * step, str(rate)] for i in range(num_points)]
        result.append({"metric": {label_key: label_val}, "values": values})
    return {
        "status": "success",
        "data": {
            "resultType": "matrix",
            "result": result,
        },
    }


INGESTION_RATE_RANGE = _range_matrix_single(INGESTION_RATE_AVG, INGESTION_RATE_PEAK)

QUERY_REQUESTS_RANGE = _range_matrix_single(QUERY_RATE_AVG, QUERY_RATE_PEAK)

# 100 ops/sec total: 60 get + 30 upload + 10 delete
OBJSTORE_OPS_RANGE = _range_matrix_by_label([
    ("operation", "get", 60.0),
    ("operation", "upload", 30.0),
    ("operation", "delete", 10.0),
])


# ── Mimir admin API responses ──────────────────────────────────────────


BUILDINFO = {
    "status": "success",
    "data": {
        "version": VERSION,
        "revision": "abc123def",
        "branch": "main",
        "goVersion": "go1.22.0",
    },
}

CONFIG_TEXT = f"""
server:
  http_listen_port: 8080
multitenancy_enabled: true
ingester:
  ring:
    replication_factor: {REPLICATION_FACTOR}
compactor:
  compactor_blocks_retention_period: {int(RETENTION_DAYS)}d
limits:
  max_global_series_per_user: 5000000
blocks_storage:
  backend: s3
  s3:
    bucket_name: mimir-blocks
    endpoint: s3.us-east-1.amazonaws.com
"""

CONFIG_RESPONSE = {"_raw_text": CONFIG_TEXT, "_content_type": "text/yaml"}

# For the "no config" scenario
BUILDINFO_ONLY = {
    "status": "success",
    "data": {"version": "2.14.0"},
}

# For auth failure scenarios
AUTH_FAILED_RESPONSE = {
    "status_code": 401,
    "error": "Unauthorized",
}

EMPTY_INSTANT = {
    "status": "success",
    "data": {"resultType": "vector", "result": []},
}

EMPTY_RANGE = {
    "status": "success",
    "data": {"resultType": "matrix", "result": []},
}
