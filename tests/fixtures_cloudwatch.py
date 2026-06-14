"""Synthetic AWS CloudWatch API responses with known ground truth.

All values are fabricated; figures derived from them are asserted exactly in
tests (e.g. 500 GB stored across all groups, 10 GB/day log ingestion).

Uses botocore.stub.Stubber response shapes — these are the raw dicts that
boto3 would return (no envelope, no pagination tokens unless we add them).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# ── ground-truth constants ──────────────────────────────────────────────

TOTAL_METRICS = 500
CUSTOM_METRICS = 120  # namespace not AWS/*
STORED_BYTES_TOTAL = 500_000_000_000  # 500 GB
LOG_GROUPS_COUNT = 80
ALARMS_COUNT = 45
CE_MONTHLY_COST = 3240.50
CE_LOG_INGEST_BYTES_PER_DAY = 10_000_000_000  # 10 GB
LOOKBACK_DAYS = 30
DASHBOARDS_COUNT = 12
METRIC_STREAMS_COUNT = 3
BREAKDOWN_TOP_GROUPS = 5  # for testing --log-group-breakdown
XRAY_GROUPS_COUNT = 4
XRAY_TRACES_PER_DAY = 1500


# ── helpers ─────────────────────────────────────────────────────────────


def _make_metrics(total: int, custom: int) -> list[dict]:
    metrics = []
    for i in range(total - custom):
        metrics.append({
            "Namespace": "AWS/EC2",
            "MetricName": f"aws_metric_{i}",
            "Dimensions": [],
        })
    for i in range(custom):
        metrics.append({
            "Namespace": "Custom/App",
            "MetricName": f"custom_metric_{i}",
            "Dimensions": [],
        })
    return metrics


def _make_log_groups(count: int, stored_bytes_total: int) -> list[dict]:
    per_group = stored_bytes_total // count
    groups = []
    for i in range(count):
        groups.append({
            "logGroupName": f"/aws/lambda/func-{i}",
            "storedBytes": per_group,
            "retentionInDays": 7 if i % 3 == 0 else (30 if i % 3 == 1 else None),
            "arn": f"arn:aws:logs:us-east-1:123456789012:log-group:/aws/lambda/func-{i}",
        })
    return groups


def _make_alarms(count: int) -> tuple[list[dict], list[dict]]:
    metric_count = count - count // 5
    composite_count = count // 5
    metric_alarms = [
        {
            "AlarmName": f"metric-alarm-{i}",
            "AlarmType": "MetricAlarm",
            "StateValue": "OK" if i % 2 == 0 else "ALARM",
            "Namespace": "AWS/EC2",
            "MetricName": "CPUUtilization",
        }
        for i in range(metric_count)
    ]
    composite_alarms = [
        {
            "AlarmName": f"composite-alarm-{i}",
            "AlarmType": "CompositeAlarm",
            "StateValue": "OK",
            "AlarmRule": "ALARM(metric-alarm-0)",
        }
        for i in range(composite_count)
    ]
    return metric_alarms, composite_alarms


def _make_dashboards(count: int) -> list[dict]:
    return [
        {
            "DashboardName": f"dash-{i}",
            "DashboardArn": f"arn:aws:cloudwatch::123456789012:dashboard/dash-{i}",
        }
        for i in range(count)
    ]


def _make_metric_streams(count: int) -> list[dict]:
    return [
        {
            "Name": f"stream-{i}",
            "Arn": f"arn:aws:cloudwatch:us-east-1:123456789012:metric-stream/stream-{i}",
        }
        for i in range(count)
    ]


def _make_ce_cost_response(monthly_cost: float) -> dict:
    """Monthly CE response with usage-type breakdown for AmazonCloudWatch."""
    now = datetime.now(UTC)
    month_end = now.replace(day=1).strftime("%Y-%m-%d")
    month_start = (now.replace(day=1) - timedelta(days=1)).replace(day=1).strftime("%Y-%m-%d")
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": month_start, "End": month_end},
                "Groups": [
                    {
                        "Keys": ["USE1-CW:MetricMonitorUsage"],
                        "Metrics": {
                            "BlendedCost": {
                                "Amount": str(round(monthly_cost * 0.4, 2)),
                                "Unit": "USD",
                            }
                        },
                    },
                    {
                        "Keys": ["USE1-CW:Requests"],
                        "Metrics": {
                            "BlendedCost": {
                                "Amount": str(round(monthly_cost * 0.3, 2)),
                                "Unit": "USD",
                            }
                        },
                    },
                    {
                        "Keys": ["USE1-DataProcessing-Bytes"],
                        "Metrics": {
                            "BlendedCost": {
                                "Amount": str(round(monthly_cost * 0.3, 2)),
                                "Unit": "USD",
                            }
                        },
                    },
                ],
                "Estimated": False,
            }
        ],
        "DimensionValueAttributes": [],
        "ResponseMetadata": {"RequestId": "test", "HTTPStatusCode": 200, "HTTPHeaders": {}},
    }


def _make_ce_log_ingest_response(bytes_per_day: float, days: int) -> dict:
    """Daily CE response for log ingestion UsageQuantity."""
    now = datetime.now(UTC)
    results_by_time = []
    for d in range(days):
        day = now - timedelta(days=days - d)
        start = day.strftime("%Y-%m-%d")
        end = (day + timedelta(days=1)).strftime("%Y-%m-%d")
        results_by_time.append({
            "TimePeriod": {"Start": start, "End": end},
            "Groups": [
                {
                    "Keys": ["USE1-DataProcessing-Bytes"],
                    "Metrics": {
                        "UsageQuantity": {"Amount": str(bytes_per_day), "Unit": "Bytes"}
                    },
                },
            ],
            "Estimated": d == days - 1,
        })
    return {
        "ResultsByTime": results_by_time,
        "DimensionValueAttributes": [],
        "ResponseMetadata": {"RequestId": "test", "HTTPStatusCode": 200, "HTTPHeaders": {}},
    }


def _make_breakdown_stats(bytes_total: float) -> dict:
    """GetMetricStatistics response for one log group's IncomingBytes."""
    now = datetime.now(UTC)
    return {
        "Label": "IncomingBytes",
        "Datapoints": [
            {
                "Timestamp": (now - timedelta(days=d)),
                "Sum": bytes_total / LOOKBACK_DAYS,
                "Unit": "Bytes",
            }
            for d in range(LOOKBACK_DAYS)
        ],
        "ResponseMetadata": {"RequestId": "test", "HTTPStatusCode": 200, "HTTPHeaders": {}},
    }


def _make_xray_groups(count: int) -> list[dict]:
    return [
        {
            "GroupName": f"service-{i}" if i > 0 else "Default",
            "GroupARN": f"arn:aws:xray:us-east-1:123456789012:group/service-{i}",
            "FilterExpression": "" if i == 0 else f'service("service-{i}")',
        }
        for i in range(count)
    ]


def _make_xray_trace_summaries(count: int) -> list[dict]:
    return [
        {
            "Id": f"1-{i:08x}-abcdef012345678901234567",
            "Duration": 0.5 + (i % 10) * 0.1,
            "ResponseTime": 0.3 + (i % 10) * 0.05,
            "HasFault": i % 20 == 0,
            "HasError": i % 10 == 0,
            "Http": {"HttpStatus": 200},
            "Annotations": {},
            "Users": [],
            "ServiceIds": [],
        }
        for i in range(count)
    ]


# ── pre-built responses ────────────────────────────────────────────────

METRICS = _make_metrics(TOTAL_METRICS, CUSTOM_METRICS)
LOG_GROUPS = _make_log_groups(LOG_GROUPS_COUNT, STORED_BYTES_TOTAL)
METRIC_ALARMS, COMPOSITE_ALARMS = _make_alarms(ALARMS_COUNT)
DASHBOARDS = _make_dashboards(DASHBOARDS_COUNT)
METRIC_STREAMS = _make_metric_streams(METRIC_STREAMS_COUNT)
CE_COST = _make_ce_cost_response(CE_MONTHLY_COST)
CE_LOG_INGEST = _make_ce_log_ingest_response(CE_LOG_INGEST_BYTES_PER_DAY, LOOKBACK_DAYS)
XRAY_GROUPS = _make_xray_groups(XRAY_GROUPS_COUNT)
XRAY_TRACE_SUMMARIES = _make_xray_trace_summaries(XRAY_TRACES_PER_DAY)
