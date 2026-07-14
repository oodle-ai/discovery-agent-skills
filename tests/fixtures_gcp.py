"""Synthetic GCP Cloud Operations API responses with known ground truth.

All values are fabricated; figures derived from them are asserted exactly in
tests (e.g. 604800 samples over 7d -> ~1.0 samples/sec).

Uses the same JSON shapes that the GCP REST APIs return.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# ── ground-truth constants ──────────────────────────────────────────────

TOTAL_DESCRIPTORS = 400
CUSTOM_DESCRIPTORS = 85  # type starts with custom.|external.|workload.|prometheus.googleapis.com/

# billing/samples_ingested: 604800 samples over 7d = 1.0 samples/sec
LOOKBACK_DAYS = 7
LOOKBACK_S = LOOKBACK_DAYS * 86400
TOTAL_SAMPLES = 604800  # -> 1.0 samples/sec

# logging/billing/bytes_ingested: 70 GB over 7d = 10 GB/day
TOTAL_LOG_BYTES = 70_000_000_000  # -> 10.0 GB/day

# logging/billing/monthly_bytes_ingested: cumulative 150 GB month-to-date
MONTHLY_LOG_BYTES = 150_000_000_000  # -> 150.0 GB stored (estimated)

# cloudtrace.googleapis.com/billing/spans_ingested: 6048000 over 7d = 10.0 spans/sec
TOTAL_TRACE_SPANS = 6_048_000  # -> 10.0 spans/sec

ALERT_POLICIES_COUNT = 23
LOG_BUCKETS_COUNT = 3
LOG_SINKS_COUNT = 2

PROJECT = "test-project-1"


# ── helpers ─────────────────────────────────────────────────────────────


def _make_metric_descriptors(total: int, custom: int) -> list[dict]:
    descriptors = []
    aws_count = total - custom
    for i in range(aws_count):
        mt = f"compute.googleapis.com/instance/cpu/utilization_{i}"
        descriptors.append({
            "name": f"projects/{PROJECT}/metricDescriptors/{mt}",
            "type": mt,
            "metricKind": "GAUGE",
            "valueType": "DOUBLE",
            "description": f"Test builtin metric {i}",
        })
    custom_types = [
        ("custom.googleapis.com/", 40),
        ("external.googleapis.com/", 15),
        ("workload.googleapis.com/", 10),
        ("prometheus.googleapis.com/", 20),
    ]
    idx = 0
    for prefix, count in custom_types:
        for _j in range(count):
            descriptors.append({
                "name": f"projects/{PROJECT}/metricDescriptors/{prefix}metric_{idx}",
                "type": f"{prefix}metric_{idx}",
                "metricKind": "GAUGE",
                "valueType": "DOUBLE",
                "description": f"Test custom metric {idx}",
            })
            idx += 1
    return descriptors


def _make_timeseries_response(
    metric_type: str, total_value: int, num_points: int = 24
) -> dict:
    """Create a timeSeries response with DELTA points summing to total_value."""
    now = datetime.now(UTC)
    per_point = total_value // num_points
    remainder = total_value - per_point * num_points
    points = []
    for i in range(num_points):
        end = now - timedelta(hours=i)
        start = end - timedelta(hours=1)
        val = per_point + (remainder if i == 0 else 0)
        points.append({
            "interval": {
                "startTime": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "endTime": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            "value": {"int64Value": str(val)},
        })
    return {
        "timeSeries": [
            {
                "metric": {"type": metric_type},
                "resource": {"type": "global"},
                "metricKind": "DELTA",
                "valueType": "INT64",
                "points": points,
            }
        ]
    }


def _make_cumulative_timeseries_response(
    metric_type: str, latest_value: int
) -> dict:
    """Create a timeSeries response with a single CUMULATIVE latest point."""
    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return {
        "timeSeries": [
            {
                "metric": {"type": metric_type},
                "resource": {"type": "global"},
                "metricKind": "CUMULATIVE",
                "valueType": "INT64",
                "points": [
                    {
                        "interval": {
                            "startTime": month_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "endTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                        },
                        "value": {"int64Value": str(latest_value)},
                    }
                ],
            }
        ]
    }


def _make_alert_policies(count: int) -> list[dict]:
    policies = []
    condition_types = [
        "conditionThreshold",
        "conditionAbsent",
        "conditionMatchedLog",
        "conditionMonitoringQueryLanguage",
        "conditionPrometheusQueryLanguage",
    ]
    for i in range(count):
        ct = condition_types[i % len(condition_types)]
        policies.append({
            "name": f"projects/{PROJECT}/alertPolicies/policy-{i}",
            "displayName": f"Alert Policy {i}",
            "enabled": True,
            "conditions": [
                {
                    "displayName": f"Condition {i}",
                    "name": f"projects/{PROJECT}/alertPolicies/policy-{i}/conditions/cond-0",
                    ct: {"filter": f'metric.type="custom.googleapis.com/test_{i}"'},
                }
            ],
            "combiner": "OR",
        })
    return policies


def _make_log_buckets(count: int) -> list[dict]:
    buckets = []
    retentions = [30, 365, 3650]
    for i in range(count):
        name = ["_Default", "_Required", "custom-audit"][i] if i < 3 else f"bucket-{i}"
        buckets.append({
            "name": f"projects/{PROJECT}/locations/global/buckets/{name}",
            "retentionDays": retentions[i % len(retentions)],
            "locked": i == 1,
            "lifecycleState": "ACTIVE",
            "createTime": "2024-01-01T00:00:00Z",
        })
    return buckets


def _make_log_sinks(count: int) -> list[dict]:
    destinations = [
        f"storage.googleapis.com/logs-archive-{PROJECT}",
        f"bigquery.googleapis.com/projects/{PROJECT}/datasets/audit_logs",
        f"pubsub.googleapis.com/projects/{PROJECT}/topics/log-export",
    ]
    return [
        {
            "name": f"sink-{i}",
            "destination": destinations[i % len(destinations)],
            "filter": 'resource.type="gce_instance"' if i == 0 else "",
            "disabled": False,
        }
        for i in range(count)
    ]


# ── pre-built responses ───────────────────────────────────────────────

METRIC_DESCRIPTORS = _make_metric_descriptors(TOTAL_DESCRIPTORS, CUSTOM_DESCRIPTORS)

BILLING_SAMPLES = _make_timeseries_response(
    "monitoring.googleapis.com/billing/samples_ingested", TOTAL_SAMPLES
)

LOG_BILLING_INGEST = _make_timeseries_response(
    "logging.googleapis.com/billing/bytes_ingested", TOTAL_LOG_BYTES
)

# monitoring metric bytes ingested (GMP): 35 GB over 7d = 5 GB/day
TOTAL_METRIC_BYTES = 35_000_000_000
METRIC_BILLING_BYTES = _make_timeseries_response(
    "monitoring.googleapis.com/billing/bytes_ingested", TOTAL_METRIC_BYTES
)

LOG_BILLING_MONTHLY = _make_cumulative_timeseries_response(
    "logging.googleapis.com/billing/monthly_bytes_ingested", MONTHLY_LOG_BYTES
)

TRACE_BILLING = _make_timeseries_response(
    "cloudtrace.googleapis.com/billing/spans_ingested", TOTAL_TRACE_SPANS
)

ALERT_POLICIES = _make_alert_policies(ALERT_POLICIES_COUNT)
LOG_BUCKETS = _make_log_buckets(LOG_BUCKETS_COUNT)
LOG_SINKS = _make_log_sinks(LOG_SINKS_COUNT)

# project list response (from gcloud projects list --format=json)
PROJECTS_LIST = [
    {"projectId": PROJECT, "name": "Test Project 1"},
]
