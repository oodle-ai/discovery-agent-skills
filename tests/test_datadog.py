from __future__ import annotations

import json

import fixtures_datadog as fx
import httpx
import pytest

BASE = "https://api.datadoghq.com"


def mock_datadog(respx_mock, estimated_cost_status=200, hourly_pages=1):
    respx_mock.get(f"{BASE}/api/v1/hosts/totals").respond(json=fx.HOSTS_TOTALS)
    respx_mock.get(f"{BASE}/api/v1/dashboard").respond(json=fx.DASHBOARDS)
    respx_mock.get(f"{BASE}/api/v1/synthetics/tests").respond(json=fx.SYNTHETICS)
    respx_mock.get(f"{BASE}/api/v1/notebooks").respond(json=fx.NOTEBOOKS)
    respx_mock.get(f"{BASE}/api/v1/logs/config/pipelines").respond(json=fx.LOGS_PIPELINES)
    respx_mock.get(f"{BASE}/api/v1/logs/config/indexes").respond(json=fx.LOGS_INDEXES)
    respx_mock.get(f"{BASE}/api/v1/monitor").respond(json=fx.MONITORS)
    respx_mock.get(f"{BASE}/api/v1/slo").respond(json=fx.SLOS)
    respx_mock.get(f"{BASE}/api/v1/usage/summary").respond(json=fx.USAGE_SUMMARY)
    respx_mock.get(f"{BASE}/api/v1/metrics").respond(json=fx.METRICS_LIST)
    def estimated_cost_side_effect(request: httpx.Request) -> httpx.Response:
        # the API rejects filter[start_month]; require the bare param
        if "start_month" not in request.url.params:
            detail = "Must provide exactly one of start_month or start_date"
            return httpx.Response(400, json={"errors": [{"detail": detail}]})
        if estimated_cost_status != 200:
            return httpx.Response(estimated_cost_status, json={"errors": ["Forbidden"]})
        return httpx.Response(200, json=fx.ESTIMATED_COST)

    respx_mock.get(f"{BASE}/api/v2/usage/estimated_cost").mock(
        side_effect=estimated_cost_side_effect
    )

    def hourly_side_effect(request: httpx.Request) -> httpx.Response:
        families = request.url.params.get("filter[product_families]").split(",")
        records = [r for f in families for r in fx.HOURLY_RESPONSES[f]["data"]]
        if hourly_pages == 1:
            return httpx.Response(200, json={"data": records})
        # split records into pages to exercise pagination
        half = len(records) // 2
        if "page[next_record_id]" not in request.url.params:
            return httpx.Response(
                200,
                json={
                    "data": records[:half],
                    "meta": {"pagination": {"next_record_id": "page2"}},
                },
            )
        return httpx.Response(200, json={"data": records[half:]})

    respx_mock.get(f"{BASE}/api/v2/usage/hourly_usage").mock(side_effect=hourly_side_effect)


def run_collector(datadog_collect, tmp_path, monkeypatch, extra_args=None):
    monkeypatch.setenv("DD_API_KEY", "test-api-key")
    monkeypatch.setenv("DD_APP_KEY", "test-app-key")
    argv = ["collect.py", "--output-dir", str(tmp_path / "out"), "--lookback", "2d"]
    argv += extra_args or []
    monkeypatch.setattr("sys.argv", argv)
    rc = datadog_collect.main()
    assert rc == 0
    return json.loads((tmp_path / "out" / "summary.json").read_text())


def figures_by_id(summary):
    return {f["id"]: f for f in summary["figures"]}


class TestDatadogCollector:
    def test_full_run_known_ground_truth(
        self, datadog_collect, tmp_path, monkeypatch, respx_mock, summary_schema
    ):
        mock_datadog(respx_mock)
        summary = run_collector(datadog_collect, tmp_path, monkeypatch)
        jsonschema = pytest.importorskip("jsonschema")
        jsonschema.validate(
            {k: v for k, v in summary.items() if not k.startswith("_")}, summary_schema
        )

        figs = figures_by_id(summary)
        assert figs["hosts.count"]["value"] == 142
        assert figs["metrics.total_count"]["value"] == 3
        # 1 GB/hour seeded -> 24 GB/day exactly
        assert figs["logs.ingest_gb_per_day"]["value"] == pytest.approx(24.0)
        assert figs["traces.ingest_gb_per_day"]["value"] == pytest.approx(12.0)
        assert figs["metrics.custom_metrics_count"]["value"] == pytest.approx(1000)
        assert figs["datadog.rum_sessions_per_day"]["value"] == pytest.approx(2400)
        assert figs["alerts.monitor_count"]["value"] == 7
        assert figs["cost.monthly_usd"]["value"] == pytest.approx(41230.5)
        assert summary["gaps"] == []

        # cost breakdown: per-product "total" rows only (no committed/on_demand
        # double counting), zero-cost products dropped, sorted descending
        breakdown = summary["inventory"]["cost_breakdown"]
        assert [c["product"] for c in breakdown] == ["infra_host", "logs_indexed", "timeseries"]
        assert all(c["charge_type"] == "total" for c in breakdown)
        assert sum(c["monthly_usd"] for c in breakdown) == pytest.approx(41230.5)

        # provenance present on every collected figure
        for fig in summary["figures"]:
            assert fig["method"], fig["id"]
            assert fig["source_api"], fig["id"]
            assert fig["evidence_files"], fig["id"]

    def test_hourly_pagination_followed(
        self, datadog_collect, tmp_path, monkeypatch, respx_mock
    ):
        mock_datadog(respx_mock, hourly_pages=2)
        summary = run_collector(datadog_collect, tmp_path, monkeypatch)
        figs = figures_by_id(summary)
        # if pagination were broken we'd only see half the hours; the seeded
        # rates are constant so per-day values stay 24/12 only when the day
        # divisor matches the record count
        assert figs["logs.ingest_gb_per_day"]["value"] == pytest.approx(24.0)
        evidence = json.loads(
            (tmp_path / "out" / "evidence" / "usage_hourly_logs.json").read_text()
        )
        assert len(evidence["data"]) == fx.HOURS

    def test_estimated_cost_403_becomes_gap(
        self, datadog_collect, tmp_path, monkeypatch, respx_mock
    ):
        mock_datadog(respx_mock, estimated_cost_status=403)
        summary = run_collector(datadog_collect, tmp_path, monkeypatch)
        figs = figures_by_id(summary)
        assert figs["cost.monthly_usd"]["status"] == "unavailable"
        assert "permission_denied" in figs["cost.monthly_usd"]["unavailable_reason"]
        gap = next(g for g in summary["gaps"] if "cost.monthly_usd" in g["figure_ids"])
        assert gap["reason"] == "permission_denied"
        assert gap["remediation"]

    def test_credentials_redacted_in_evidence(
        self, datadog_collect, tmp_path, monkeypatch, respx_mock
    ):
        mock_datadog(respx_mock)
        run_collector(datadog_collect, tmp_path, monkeypatch)
        for f in (tmp_path / "out").rglob("*.json"):
            assert "test-api-key" not in f.read_text(), f
            assert "test-app-key" not in f.read_text(), f

    def test_report_only_recomputes_from_evidence(
        self, datadog_collect, tmp_path, monkeypatch, respx_mock
    ):
        mock_datadog(respx_mock)
        first = run_collector(datadog_collect, tmp_path, monkeypatch)
        # second run: no network access allowed (respx with no routes would fail)
        monkeypatch.setattr(
            "sys.argv",
            ["collect.py", "--output-dir", str(tmp_path / "out"), "--report-only"],
        )
        rc = datadog_collect.main()
        assert rc == 0
        second = json.loads((tmp_path / "out" / "summary.json").read_text())
        assert figures_by_id(second)["logs.ingest_gb_per_day"]["value"] == pytest.approx(
            figures_by_id(first)["logs.ingest_gb_per_day"]["value"]
        )
