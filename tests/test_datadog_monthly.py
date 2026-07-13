"""Tests for the Datadog monthly usage-by-SKU export (collectors/datadog/monthly_usage.py).

The aggregation is a pure function over synthetic hourly-usage data with known
ground truth, so every monthly number is asserted exactly.
"""

from __future__ import annotations

from datetime import UTC, datetime


def _entry(ts: str, measurements: dict[str, float]) -> dict:
    return {
        "attributes": {
            "timestamp": ts,
            "measurements": [
                {"usage_type": ut, "value": v} for ut, v in measurements.items()
            ],
        }
    }


def _hourly(hours: list[tuple[str, dict[str, float]]]) -> dict:
    return {"data": [_entry(ts, m) for ts, m in hours]}


def _log(gb_bytes: float, indexed: int) -> dict[str, float]:
    return {"ingested_events_bytes": gb_bytes, "indexed_events_count": indexed}


MONTHS = ["2026-01", "2026-02", "2026-03", "2026-04", "2026-05", "2026-06"]


def test_month_keys_and_labels(datadog_monthly):
    m = datadog_monthly
    assert m.month_keys(datetime(2026, 7, 10, tzinfo=UTC), 6) == MONTHS
    # year rollover: Feb 2026 -> back six months = Aug..Jan
    assert m.month_keys(datetime(2026, 2, 5, tzinfo=UTC), 6) == [
        "2025-08", "2025-09", "2025-10", "2025-11", "2025-12", "2026-01"
    ]
    assert m.month_label("2026-01") == "Jan 2026"


def test_classify(datadog_monthly):
    c = datadog_monthly.classify
    assert c("ingested_events_bytes") == ("GB", "sum")
    assert c("indexed_events_count") == ("count", "sum")
    assert c("num_custom_timeseries") == ("custom metrics (avg)", "avg")
    assert c("host_count") == ("hosts (avg)", "avg")


def test_aggregate_monthly_sums_bytes_to_gb_and_averages_gauges(datadog_monthly):
    m = datadog_monthly
    results = {
        # logs: 3h in May @1GB/h, 2h in Jun @1GB/h; indexed events summed
        "usage_hourly_logs": _hourly([
            ("2026-05-10T01:00:00+00:00", _log(1e9, 100)),
            ("2026-05-10T02:00:00+00:00", _log(1e9, 100)),
            ("2026-05-10T03:00:00+00:00", _log(1e9, 100)),
            ("2026-06-01T00:00:00+00:00", _log(1e9, 100)),
            ("2026-06-02T00:00:00+00:00", _log(1e9, 100)),
            # out of window -> must be ignored
            ("2025-12-31T23:00:00+00:00", _log(9e9, 999)),
        ]),
        # custom metrics gauge: averaged, not summed
        "usage_hourly_timeseries": _hourly([
            ("2026-06-01T00:00:00+00:00", {"num_custom_timeseries": 1000}),
            ("2026-06-01T01:00:00+00:00", {"num_custom_timeseries": 3000}),
        ]),
    }
    rows = m.aggregate_monthly(results, ["logs", "ingested_spans", "timeseries"], MONTHS)
    by_ut = {(r["product_family"], r["usage_type"]): r for r in rows}

    logs_bytes = by_ut[("logs", "ingested_events_bytes")]
    assert logs_bytes["unit"] == "GB" and logs_bytes["aggregation"] == "sum"
    assert logs_bytes["values"]["2026-05"] == 3.0  # 3 GB summed
    assert logs_bytes["values"]["2026-06"] == 2.0
    assert logs_bytes["values"]["2026-01"] is None  # no data that month

    logs_idx = by_ut[("logs", "indexed_events_count")]
    assert logs_idx["values"]["2026-05"] == 300
    assert logs_idx["values"]["2026-06"] == 200

    ts = by_ut[("logs", "ingested_events_bytes")]["values"]
    assert "2025-12" not in ts  # window months only

    custom = by_ut[("timeseries", "num_custom_timeseries")]
    assert custom["aggregation"] == "avg"
    assert custom["values"]["2026-06"] == 2000.0  # (1000+3000)/2, not summed


def test_aggregate_orders_by_family_then_usage_type(datadog_monthly):
    m = datadog_monthly
    results = {
        "usage_hourly_rum": _hourly([
            ("2026-06-01T00:00:00+00:00", {"rum_total_session_count": 10})
        ]),
        "usage_hourly_logs": _hourly([
            ("2026-06-01T00:00:00+00:00", {"ingested_events_bytes": 1e9})
        ]),
    }
    rows = m.aggregate_monthly(results, m.__dict__["dd"].HOURLY_FAMILIES, MONTHS)
    fams = [r["product_family"] for r in rows]
    # logs appears before rum in HOURLY_FAMILIES order
    assert fams.index("logs") < fams.index("rum")


def test_write_csv_roundtrip(tmp_path, datadog_monthly):
    import csv

    m = datadog_monthly
    results = {
        "usage_hourly_logs": _hourly([
            ("2026-06-01T00:00:00+00:00", {"ingested_events_bytes": 2e9})
        ]),
    }
    rows = m.aggregate_monthly(results, ["logs"], MONTHS)
    path = m.write_csv(tmp_path, rows, MONTHS)
    with path.open() as f:
        parsed = list(csv.reader(f))
    assert parsed[0] == ["product_family", "usage_type", "unit", "aggregation",
                         "Jan 2026", "Feb 2026", "Mar 2026", "Apr 2026", "May 2026", "Jun 2026"]
    row = parsed[1]
    assert row[:4] == ["logs", "ingested_events_bytes", "GB", "sum"]
    assert row[-1] == "2.000"  # Jun 2026 = 2 GB
    assert row[4] == ""        # Jan 2026 empty


def test_drop_empty_rows_removes_all_zero_and_no_data_skus(datadog_monthly):
    m = datadog_monthly
    results = {
        # real usage in some months
        "usage_hourly_logs": _hourly([
            ("2026-06-01T00:00:00+00:00", {"ingested_events_bytes": 2e9}),
        ]),
        # every measurement is exactly 0 -> pure noise, must be dropped
        "usage_hourly_infra_hosts": _hourly([
            ("2026-05-10T00:00:00+00:00", {"aws_host_count": 0, "agent_host_count": 5}),
            ("2026-06-01T00:00:00+00:00", {"aws_host_count": 0, "agent_host_count": 5}),
        ]),
    }
    fams = ["logs", "infra_hosts"]
    all_rows = m.aggregate_monthly(results, fams, MONTHS)
    kept = m.drop_empty_rows(all_rows)
    kept_uts = {(r["product_family"], r["usage_type"]) for r in kept}

    assert ("infra_hosts", "agent_host_count") in kept_uts  # 5/5 -> real usage
    assert ("logs", "ingested_events_bytes") in kept_uts
    assert ("infra_hosts", "aws_host_count") not in kept_uts  # all zero -> dropped
    # has_usage: None-only and 0-only rows are empty; any nonzero keeps the row
    assert m.has_usage({"values": {"2026-06": None, "2026-05": 0}}) is False
    assert m.has_usage({"values": {"2026-06": 0.0001}}) is True


def test_inventory_rows_carry_month_labels_and_match_csv_formatting(datadog_monthly):
    m = datadog_monthly
    results = {
        "usage_hourly_logs": _hourly([
            ("2026-05-10T01:00:00+00:00", _log(2e9, 100)),
            ("2026-06-01T00:00:00+00:00", _log(1e9, 100)),
        ]),
    }
    rows = m.aggregate_monthly(results, ["logs"], MONTHS)
    inv = m.inventory_rows(rows, MONTHS)
    bytes_row = next(r for r in inv if r["usage_type"] == "ingested_events_bytes")
    assert bytes_row["product_family"] == "logs"
    assert bytes_row["unit"] == "GB" and bytes_row["aggregation"] == "sum"
    # month labels are the keys; values are the same display strings as the CSV
    assert bytes_row["May 2026"] == "2.000"
    assert bytes_row["Jun 2026"] == "1.000"
    assert bytes_row["Jan 2026"] == ""  # no data -> empty, never a fabricated 0


def test_write_summary_emits_valid_summary_with_matrix_in_inventory(tmp_path, datadog_monthly):
    import json

    m = datadog_monthly
    results = {
        "usage_hourly_logs": _hourly([
            ("2026-06-01T00:00:00+00:00", {"ingested_events_bytes": 3e9})
        ]),
    }
    rows = m.aggregate_monthly(results, ["logs"], MONTHS)
    path = m.write_summary(tmp_path, rows, MONTHS, "datadoghq.com", {"site": "us1", "months": 6})
    doc = json.loads(path.read_text())

    assert doc["collector"] == "datadog_monthly"
    assert doc["figures"] == []  # a matrix collector: no scalar figures
    assert doc["inventory"]["monthly_usage_months"][0] == "Jan 2026"
    sku = doc["inventory"]["monthly_usage_by_sku"]
    assert sku[0]["usage_type"] == "ingested_events_bytes"
    assert sku[0]["Jun 2026"] == "3.000"
    # structurally valid per the dependency-free validator the report also runs
    from lib.summary import validate_summary
    validate_summary(doc)
