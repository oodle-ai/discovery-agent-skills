"""Synthetic Datadog API responses with known ground truth.

All values are fabricated; figures derived from them are asserted exactly in
tests (e.g. 1 GB/hour of log ingestion -> 24 GB/day).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

LOG_BYTES_PER_HOUR = 1_000_000_000  # 1 GB -> 24 GB/day
SPAN_BYTES_PER_HOUR = 500_000_000  # 0.5 GB -> 12 GB/day
CUSTOM_TS_PER_HOUR = 1_000  # gauge -> avg 1000
RUM_SESSIONS_PER_HOUR = 100  # -> 2400/day
HOST_COUNT_PER_HOUR = 50
HOURS = 48


def hourly_timestamps(hours: int = HOURS) -> list[str]:
    end = datetime(2026, 6, 12, 0, 0, tzinfo=UTC)
    return [
        (end - timedelta(hours=h)).strftime("%Y-%m-%dT%H:00:00+00:00")
        for h in range(hours, 0, -1)
    ]


def hourly_usage(family: str, measurements: dict[str, float], hours: int = HOURS) -> dict:
    return {
        "data": [
            {
                "id": f"{family}-{i}",
                "type": "usage_timeseries",
                "attributes": {
                    "product_family": family,
                    "timestamp": ts,
                    "measurements": [
                        {"usage_type": ut, "value": val} for ut, val in measurements.items()
                    ],
                },
            }
            for i, ts in enumerate(hourly_timestamps(hours))
        ]
    }


HOURLY_RESPONSES: dict[str, dict] = {
    "infra_hosts": hourly_usage(
        "infra_hosts", {"host_count": HOST_COUNT_PER_HOUR, "container_count": 200}
    ),
    "logs": hourly_usage(
        "logs",
        {"ingested_events_bytes": LOG_BYTES_PER_HOUR, "indexed_events_count": 50_000},
    ),
    "ingested_spans": hourly_usage(
        "ingested_spans", {"ingested_events_bytes": SPAN_BYTES_PER_HOUR}
    ),
    "indexed_spans": hourly_usage("indexed_spans", {"indexed_events_count": 10_000}),
    "rum": hourly_usage("rum", {"rum_total_session_count": RUM_SESSIONS_PER_HOUR}),
    "timeseries": hourly_usage("timeseries", {"num_custom_timeseries": CUSTOM_TS_PER_HOUR}),
}

HOSTS_TOTALS = {"total_active": 142, "total_up": 140}

DASHBOARDS = {"dashboards": [{"id": f"d{i}", "title": f"Dash {i}"} for i in range(5)]}

SYNTHETICS = {"tests": [{"type": "api"}, {"type": "api"}, {"type": "browser"}]}

NOTEBOOKS = {"data": [{"id": 1}]}

LOGS_PIPELINES = [{"id": "p1"}, {"id": "p2"}]

LOGS_INDEXES = {
    "indexes": [
        {"name": "main", "num_retention_days": 15, "daily_limit": None},
        {"name": "audit", "num_retention_days": 30, "daily_limit": 10_000_000},
    ]
}

MONITORS = [
    {"id": i, "type": "metric alert" if i % 2 else "log alert", "name": f"mon {i}"}
    for i in range(7)
]

SLOS = {"data": [{"id": "slo1"}], "metadata": {"total_count": 1}}

USAGE_SUMMARY = {"usage": []}

ESTIMATED_COST = {
    "data": [
        {
            "type": "cost_by_org",
            "attributes": {"total_cost": 41230.5, "date": "2026-06-01"},
        }
    ]
}

METRICS_LIST = {"metrics": ["system.cpu.user", "app.requests", "custom.thing"], "from": "ts"}
