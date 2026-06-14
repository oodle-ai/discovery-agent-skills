from __future__ import annotations

import json
from unittest.mock import MagicMock

import fixtures_cloudwatch as fx
import pytest

REGION = "us-east-1"


def _make_paginator(data: dict):
    """Create a mock paginator whose .paginate().build_full_result() returns data."""
    paginator = MagicMock()
    paginator.paginate.return_value.build_full_result.return_value = data
    return paginator


def _make_cw_client():
    cw = MagicMock()
    cw.get_paginator.side_effect = lambda op: {
        "list_metrics": _make_paginator({"Metrics": list(fx.METRICS)}),
        "describe_alarms": _make_paginator({
            "MetricAlarms": list(fx.METRIC_ALARMS),
            "CompositeAlarms": list(fx.COMPOSITE_ALARMS),
        }),
        "list_dashboards": _make_paginator({
            "DashboardEntries": list(fx.DASHBOARDS),
        }),
    }[op]
    cw.list_metric_streams.return_value = {
        "Entries": list(fx.METRIC_STREAMS),
    }
    cw.get_metric_statistics.return_value = fx._make_breakdown_stats(
        50_000_000_000  # 50 GB total per group for breakdown testing
    )
    return cw


def _make_logs_client():
    logs = MagicMock()
    logs.get_paginator.side_effect = lambda op: {
        "describe_log_groups": _make_paginator({"logGroups": list(fx.LOG_GROUPS)}),
    }[op]
    return logs


def _make_ce_client(deny=False):
    from botocore.exceptions import ClientError

    ce = MagicMock()
    if deny:
        ce.get_cost_and_usage.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "GetCostAndUsage",
        )
    else:
        def mock_get_cost(*, TimePeriod, Granularity, Metrics, Filter, GroupBy):
            if Granularity == "MONTHLY":
                return fx.CE_COST
            else:
                return fx.CE_LOG_INGEST

        ce.get_cost_and_usage.side_effect = mock_get_cost
    return ce


def _mock_session_factory(ce_deny=False):
    """Return a function that mimics boto3_session(profile, region)."""

    def factory(profile=None, region=None):
        class FakeSession:
            region_name = region or REGION

            def client(self, service, **kwargs):
                if service == "cloudwatch":
                    return _make_cw_client()
                elif service == "logs":
                    return _make_logs_client()
                elif service == "ce":
                    return _make_ce_client(deny=ce_deny)
                elif service == "ec2":
                    ec2 = MagicMock()
                    ec2.describe_regions.return_value = {
                        "Regions": [{"RegionName": REGION}]
                    }
                    return ec2
                return MagicMock()

        return FakeSession()

    return factory


def run_collector(cloudwatch_collect, tmp_path, monkeypatch, extra_args=None, ce_deny=False):
    argv = [
        "collect.py",
        "--output-dir",
        str(tmp_path / "out"),
        "--lookback",
        "30d",
        "--region",
        REGION,
    ]
    argv += extra_args or []
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(cloudwatch_collect, "boto3_session", _mock_session_factory(ce_deny))
    rc = cloudwatch_collect.main()
    assert rc == 0
    return json.loads((tmp_path / "out" / "summary.json").read_text())


def figures_by_id(summary):
    return {f["id"]: f for f in summary["figures"]}


class TestCloudwatchCollector:
    def test_full_run_known_ground_truth(
        self, cloudwatch_collect, tmp_path, monkeypatch, summary_schema
    ):
        summary = run_collector(cloudwatch_collect, tmp_path, monkeypatch)
        jsonschema = pytest.importorskip("jsonschema")
        jsonschema.validate(
            {k: v for k, v in summary.items() if not k.startswith("_")}, summary_schema
        )

        figs = figures_by_id(summary)
        assert figs["metrics.total_count"]["value"] == fx.TOTAL_METRICS
        assert figs["metrics.custom_metrics_count"]["value"] == fx.CUSTOM_METRICS
        assert figs["logs.stored_gb"]["value"] == pytest.approx(
            fx.STORED_BYTES_TOTAL / 1e9, rel=0.01
        )
        assert figs["cloudwatch.log_groups_count"]["value"] == fx.LOG_GROUPS_COUNT
        assert figs["alerts.monitor_count"]["value"] == fx.ALARMS_COUNT
        assert figs["logs.ingest_gb_per_day"]["value"] == pytest.approx(
            fx.CE_LOG_INGEST_BYTES_PER_DAY / 1e9, rel=0.01
        )
        assert figs["cost.monthly_usd"]["value"] == pytest.approx(
            fx.CE_MONTHLY_COST, rel=0.01
        )
        assert figs["cost.monthly_usd"]["status"] == "ok"
        assert summary["gaps"] == []

        # provenance present on every collected figure
        for fig in summary["figures"]:
            if fig["status"] != "unavailable":
                assert fig["method"], fig["id"]
                assert fig["source_api"], fig["id"]
                assert fig["evidence_files"], fig["id"]

    def test_inventory_populated(self, cloudwatch_collect, tmp_path, monkeypatch):
        summary = run_collector(cloudwatch_collect, tmp_path, monkeypatch)
        inv = summary["inventory"]
        assert inv["dashboards_count"] == fx.DASHBOARDS_COUNT
        assert inv["metric_streams_count"] == fx.METRIC_STREAMS_COUNT
        assert "retention_distribution" in inv
        assert "namespace_breakdown" in inv
        assert "cost_by_usage_type" in inv
        assert "regions_collected" in inv

    def test_ce_permission_denied_gaps_cost_and_ingest(
        self, cloudwatch_collect, tmp_path, monkeypatch
    ):
        summary = run_collector(
            cloudwatch_collect, tmp_path, monkeypatch, ce_deny=True
        )
        figs = figures_by_id(summary)
        assert figs["cost.monthly_usd"]["status"] == "unavailable"
        assert figs["logs.ingest_gb_per_day"]["status"] == "unavailable"
        cost_gap = next(
            g for g in summary["gaps"] if "cost.monthly_usd" in g["figure_ids"]
        )
        assert cost_gap["reason"] == "permission_denied"
        assert cost_gap["remediation"]
        ingest_gap = next(
            g for g in summary["gaps"] if "logs.ingest_gb_per_day" in g["figure_ids"]
        )
        assert ingest_gap["reason"] == "permission_denied"

    def test_log_group_breakdown(self, cloudwatch_collect, tmp_path, monkeypatch):
        summary = run_collector(
            cloudwatch_collect, tmp_path, monkeypatch, extra_args=["--log-group-breakdown"]
        )
        inv = summary["inventory"]
        assert "log_group_top20" in inv
        assert len(inv["log_group_top20"]) <= 20
        for entry in inv["log_group_top20"]:
            assert "name" in entry
            assert "region" in entry
            assert "ingest_gb_per_day" in entry
            assert entry["ingest_gb_per_day"] > 0

    def test_report_only_recomputes_from_evidence(
        self, cloudwatch_collect, tmp_path, monkeypatch
    ):
        first = run_collector(cloudwatch_collect, tmp_path, monkeypatch)
        monkeypatch.setattr(
            "sys.argv",
            ["collect.py", "--output-dir", str(tmp_path / "out"), "--report-only"],
        )
        rc = cloudwatch_collect.main()
        assert rc == 0
        second = json.loads((tmp_path / "out" / "summary.json").read_text())
        first_figs = figures_by_id(first)
        second_figs = figures_by_id(second)
        assert (
            second_figs["metrics.total_count"]["value"]
            == first_figs["metrics.total_count"]["value"]
        )
        assert second_figs["logs.stored_gb"]["value"] == pytest.approx(
            first_figs["logs.stored_gb"]["value"]
        )

    def test_environment_metadata(self, cloudwatch_collect, tmp_path, monkeypatch):
        summary = run_collector(cloudwatch_collect, tmp_path, monkeypatch)
        env = summary["environment"]
        assert env["detected_backend"] == "aws-cloudwatch"
        assert "regions" in env

    def test_evidence_files_written(self, cloudwatch_collect, tmp_path, monkeypatch):
        run_collector(cloudwatch_collect, tmp_path, monkeypatch)
        evidence_dir = tmp_path / "out" / "evidence"
        assert evidence_dir.exists()
        evidence_files = list(evidence_dir.glob("*.json"))
        assert len(evidence_files) >= 5
        manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
        assert len(manifest) >= 5
