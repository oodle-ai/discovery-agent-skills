"""Tests for the GCP monthly usage-by-SKU export (collectors/gcp/monthly_usage.py).

The monthly bucketing is a pure function over synthetic Cloud Monitoring
timeSeries with known ground truth, so every monthly number is asserted exactly.
"""

from __future__ import annotations

import json

MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]


def _point(end: str, int64: int) -> dict:
    return {"interval": {"endTime": end}, "value": {"int64Value": str(int64)}}


def _ts(points: list[dict]) -> dict:
    return {"timeSeries": [{"points": points}]}


def test_month_keys_and_labels(gcp_monthly):
    from datetime import UTC, datetime

    m = gcp_monthly
    assert m.month_keys(datetime(2026, 7, 10, tzinfo=UTC), 6) == MONTHS
    assert m.month_label("2026-06") == "Jun 2026"


def test_bucket_by_month_sums_delta_points_and_blanks_missing(gcp_monthly):
    m = gcp_monthly
    points = [
        _point("2026-05-10T00:00:00Z", 100),
        _point("2026-05-20T00:00:00Z", 50),
        _point("2026-06-01T00:00:00Z", 200),
        _point("2025-12-31T00:00:00Z", 999),  # out of window -> ignored
    ]
    vals = m.bucket_by_month([{"points": points}], MONTHS, divisor=1.0)
    assert vals["2026-05"] == 150.0  # 100 + 50 summed
    assert vals["2026-06"] == 200.0
    assert vals["2026-01"] is None  # no points -> blank, not 0


def test_bucket_by_month_divides_bytes_to_gb(gcp_monthly):
    m = gcp_monthly
    vals = m.bucket_by_month(
        [{"points": [_point("2026-06-05T00:00:00Z", 3_000_000_000)]}], MONTHS, divisor=1e9
    )
    assert vals["2026-06"] == 3.0  # 3 GB


def test_aggregate_monthly_produces_one_row_per_sku_summed_across_projects(gcp_monthly):
    m = gcp_monthly
    results = {
        "monthly_ts_log_bytes_ingested_proj_a": _ts(
            [_point("2026-06-01T00:00:00Z", 2_000_000_000)]),
        "monthly_ts_log_bytes_ingested_proj_b": _ts(
            [_point("2026-06-02T00:00:00Z", 1_000_000_000)]),
        "monthly_ts_samples_ingested_proj_a": _ts(
            [_point("2026-06-01T00:00:00Z", 500)]),
    }
    rows = m.aggregate_monthly(results, ["proj-a", "proj-b"], MONTHS)
    by = {r["usage_type"]: r for r in rows}

    logs = by["bytes_ingested"]
    assert logs["product_family"] == "logs" and logs["unit"] == "GB"
    assert logs["values"]["2026-06"] == 3.0  # 2 GB + 1 GB across projects

    samples = by["samples_ingested"]
    assert samples["values"]["2026-06"] == 500.0
    # traces SKU had no data at all -> all months blank
    assert all(v is None for v in by["spans_ingested"]["values"].values())


def test_drop_empty_rows_removes_no_data_skus(gcp_monthly):
    m = gcp_monthly
    results = {
        "monthly_ts_log_bytes_ingested_p": _ts([_point("2026-06-01T00:00:00Z", 2_000_000_000)]),
    }
    all_rows = m.aggregate_monthly(results, ["p"], MONTHS)
    kept = m.drop_empty_rows(all_rows)
    kept_uts = {r["usage_type"] for r in kept}
    assert "bytes_ingested" in kept_uts
    assert "spans_ingested" not in kept_uts  # no data -> dropped
    assert "samples_ingested" not in kept_uts


def test_write_summary_valid_with_matrix_and_retention_note(tmp_path, gcp_monthly):
    m = gcp_monthly
    results = {
        "monthly_ts_log_bytes_ingested_p": _ts([_point("2026-06-01T00:00:00Z", 3_000_000_000)]),
    }
    rows = m.drop_empty_rows(m.aggregate_monthly(results, ["p"], MONTHS))
    path = m.write_summary(tmp_path, rows, MONTHS, ["p"], {"projects": ["p"], "months": 6})
    doc = json.loads(path.read_text())

    assert doc["collector"] == "gcp_monthly"
    assert doc["figures"] == []
    sku = doc["inventory"]["monthly_usage_by_sku"]
    assert sku[0]["usage_type"] == "bytes_ingested"
    assert sku[0]["Jun 2026"] == "3.000"
    assert "6 weeks" in doc["inventory"]["monthly_usage_note"]  # retention caveat present
    from lib.summary import validate_summary
    validate_summary(doc)
