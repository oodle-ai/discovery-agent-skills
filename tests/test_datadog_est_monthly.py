"""Tests for the Datadog estimated-usage monthly summary
(collectors/datadog/estimated_usage_monthly.py).

Each metric is reduced per month by its own aggregation over synthetic
/api/v1/query responses with known ground truth.
"""

from __future__ import annotations

import json

import pytest

MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]


def _series(points: list[tuple[int, float]]) -> dict:
    return {"series": [{"pointlist": [[t, v] for t, v in points]}]}


def test_month_keys_bounds_labels(datadog_est_monthly):
    from datetime import UTC, datetime

    m = datadog_est_monthly
    assert m.month_keys(datetime(2026, 7, 10, tzinfo=UTC), 6) == MONTHS
    assert m.month_label("2026-06") == "Jun 2026"
    frm, to = m.month_bounds("2026-02")
    # Feb 2026: 2026-02-01 .. 2026-03-01 UTC
    assert datetime.fromtimestamp(frm, UTC) == datetime(2026, 2, 1, tzinfo=UTC)
    assert datetime.fromtimestamp(to, UTC) == datetime(2026, 3, 1, tzinfo=UTC)
    # December rolls the year
    frm_d, to_d = m.month_bounds("2025-12")
    assert datetime.fromtimestamp(to_d, UTC) == datetime(2026, 1, 1, tzinfo=UTC)


def test_reduce_points_per_aggregation(datadog_est_monthly):
    r = datadog_est_monthly.reduce_points
    pts = [(1, 10.0), (2, 40.0), (3, 20.0)]
    assert r(pts, "sum") == 70.0
    assert r(pts, "max") == 40.0
    assert r(pts, "avg") == pytest.approx(70.0 / 3)
    assert r([], "sum") is None


def test_aggregate_monthly_applies_correct_agg_and_gb(datadog_est_monthly):
    m = datadog_est_monthly
    results = {
        # hosts gauge -> max: peak 61 in May (not the sum of hourly samples)
        "est_infra_hosts_2026-05": _series([(1, 58.0), (2, 61.0), (3, 60.0)]),
        # ingested logs count -> sum over the month
        "est_logs_ingested_2026-05": _series([(1, 1000.0), (2, 2000.0)]),
        # ingested spans bytes -> sum, shown in GB (3e9 bytes -> 3.0 GB)
        "est_apm_ingested_bytes_2026-06": _series([(1, 1e9), (2, 2e9)]),
        # custom metrics gauge -> avg
        "est_custom_metrics_2026-05": _series([(1, 1000.0), (2, 3000.0)]),
    }
    rows = {r["key"]: r for r in m.aggregate_monthly(results, MONTHS)}

    assert rows["infra_hosts"]["aggregation"] == "max"
    assert rows["infra_hosts"]["values"]["2026-05"] == 61.0  # peak, not summed
    assert rows["infra_hosts"]["values"]["2026-01"] is None  # no data -> blank

    assert rows["logs_ingested"]["values"]["2026-05"] == 3000.0  # summed

    apm = rows["apm_ingested_bytes"]
    assert apm["unit"] == "GB"
    assert apm["values"]["2026-06"] == 3.0  # 3e9 bytes summed -> 3 GB

    assert rows["custom_metrics"]["values"]["2026-05"] == 2000.0  # (1000+3000)/2


def test_write_summary_matrix_and_valid(tmp_path, datadog_est_monthly):
    m = datadog_est_monthly
    results = {"est_infra_hosts_2026-06": _series([(1, 40.0), (2, 55.0)])}
    rows = m.aggregate_monthly(results, MONTHS)
    path = m.write_summary(tmp_path, rows, MONTHS, "datadoghq.com",
                           {"site": "us5", "months": 6})
    doc = json.loads(path.read_text())
    assert doc["collector"] == "datadog_estimated_usage_monthly"
    assert doc["figures"] == []
    sku = {r["usage_type"]: r for r in doc["inventory"]["monthly_usage_by_sku"]}
    assert sku["Infra hosts"]["Jun 2026"] == "55"  # peak
    from lib.summary import validate_summary
    validate_summary(doc)
