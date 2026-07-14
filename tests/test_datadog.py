from __future__ import annotations

import json

import fixtures_datadog as fx
import httpx
import pytest

BASE = "https://api.datadoghq.com"


def mock_datadog(respx_mock, estimated_cost_status=200, hourly_pages=1,
                 historical_available=True, query_responses=None):
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
        return httpx.Response(200, json=fx.estimated_cost_response())

    respx_mock.get(f"{BASE}/api/v2/usage/estimated_cost").mock(
        side_effect=estimated_cost_side_effect
    )

    if estimated_cost_status != 200:
        respx_mock.get(f"{BASE}/api/v2/usage/historical_cost").respond(
            status_code=estimated_cost_status, json={"errors": ["Forbidden"]}
        )
    elif historical_available:
        respx_mock.get(f"{BASE}/api/v2/usage/historical_cost").respond(
            json=fx.historical_cost_response()
        )
    else:
        # previous month not finalized yet (or young org): empty data
        respx_mock.get(f"{BASE}/api/v2/usage/historical_cost").respond(
            json={"data": [], "metadata": {}}
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

    # datadog.estimated_usage.* / all.cost metric queries — empty by default so
    # existing tests fall through to the usage-API path; override per-query with
    # query_responses={<substring>: [<series>]}.
    def query_side_effect(request: httpx.Request) -> httpx.Response:
        q = request.url.params.get("query", "")
        for needle, series in (query_responses or {}).items():
            if needle in q:
                return httpx.Response(200, json={"series": series})
        return httpx.Response(200, json={"series": []})

    respx_mock.get(f"{BASE}/api/v1/query").mock(side_effect=query_side_effect)


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
        # headline cost = last full month (stable), with MTD and projection
        # as sub-figures
        assert figs["cost.monthly_usd"]["value"] == pytest.approx(fx.LAST_MONTH_COST)
        assert figs["cost.monthly_usd"]["status"] == "ok"
        assert figs["datadog.cost_month_to_date_usd"]["value"] == pytest.approx(fx.MTD_COST)
        from datetime import UTC, datetime, timedelta
        now = datetime.now(UTC)
        days_in_month = (
            (now.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        ).day
        expected_projection = fx.MTD_COST / max(1, now.day) * days_in_month
        proj = figs["datadog.cost_projected_month_usd"]
        assert proj["value"] == pytest.approx(expected_projection, rel=0.01)
        assert proj["status"] == "estimated"
        assert summary["gaps"] == []

        # cost breakdown: from the last full month, per-product "total" rows
        # only (no committed/on_demand double counting), zero-cost dropped,
        # sorted descending
        breakdown = summary["inventory"]["cost_breakdown"]
        assert [c["product"] for c in breakdown] == ["infra_host", "logs_indexed", "timeseries"]
        assert all(c["charge_type"] == "total" for c in breakdown)
        assert sum(c["monthly_usd"] for c in breakdown) == pytest.approx(fx.LAST_MONTH_COST)

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

    def test_no_finalized_month_headline_is_projection(
        self, datadog_collect, tmp_path, monkeypatch, respx_mock
    ):
        from datetime import UTC, datetime, timedelta
        mock_datadog(respx_mock, historical_available=False)
        summary = run_collector(datadog_collect, tmp_path, monkeypatch)
        figs = figures_by_id(summary)
        cost = figs["cost.monthly_usd"]
        assert cost["status"] == "estimated"
        assert "extrapolation" in cost["method"]
        now = datetime.now(UTC)
        days_in_month = (
            (now.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        ).day
        assert cost["value"] == pytest.approx(
            fx.MTD_COST / max(1, now.day) * days_in_month, rel=0.01
        )
        # MTD still present as its own figure
        assert figs["datadog.cost_month_to_date_usd"]["value"] == pytest.approx(fx.MTD_COST)

    def test_estimated_cost_403_falls_back_to_usage_estimate(
        self, datadog_collect, tmp_path, monkeypatch, respx_mock
    ):
        mock_datadog(respx_mock, estimated_cost_status=403)
        summary = run_collector(datadog_collect, tmp_path, monkeypatch)
        figs = figures_by_id(summary)
        cost = figs["cost.monthly_usd"]
        assert cost["status"] == "estimated"
        assert "list prices" in cost["method"]
        # expected from seeded usage x LIST_PRICES:
        #   hosts: 50 x $18 = 900; custom: 1000 avg - 5000 allocation -> 0
        #   logs: 24 GB/day x 30 x $0.10 = 72
        #   indexed: 1.2M/day x 30 / 1e6 x $1.70 = 61.2
        #   apm: 12 GB/day x 30 x $0.10 = 36; rum: 2400 x 30 / 1000 x $1.5 = 108
        assert cost["value"] == pytest.approx(900 + 72 + 61.2 + 36 + 108)
        comps = summary["inventory"]["cost_estimate_components"]
        assert {c["product"] for c in comps} == {
            "infra_host", "logs_ingest", "logs_indexed", "apm_ingest", "rum"
        }
        assert all(c["basis"] for c in comps)
        # the missing measured number is still an explicit gap
        gap = next(g for g in summary["gaps"] if "cost.monthly_usd" in g["figure_ids"])
        assert gap["reason"] == "permission_denied"
        assert "list prices" in gap["detail"]
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


class TestEstimatedUsageHelpers:
    def test_query_points_drops_nulls(self, datadog_collect):
        pts = datadog_collect.query_points(
            {"series": [{"pointlist": [[1000, 5.0], [2000, None], [3000, 7.0]]}]}
        )
        assert pts == [(1000, 5.0), (3000, 7.0)]
        assert datadog_collect.query_points(None) == []

    def test_query_span_days(self, datadog_collect):
        day_ms = 86_400_000
        assert datadog_collect.query_span_days([(0, 1.0), (day_ms, 1.0)]) == pytest.approx(1.0)
        assert datadog_collect.query_span_days([(0, 1.0)]) == 1.0  # single point floors to 1

    def test_est_scalar_averages_points(self, datadog_collect):
        results = {"k": {"series": [{"pointlist": [[1, 10.0], [2, 20.0]]}]}}
        assert datadog_collect.est_scalar(results, "k") == pytest.approx(15.0)
        assert datadog_collect.est_scalar({}, "k") is None

    def test_summary_logs_twol_gb_per_day(self, datadog_collect):
        from datetime import UTC, datetime
        # 2.8 TB over a 28-day window -> 100 GB/day (Logging without Limits field)
        summ = {"twol_ingested_events_bytes_agg_sum": 2.8e12}
        gb = datadog_collect.summary_logs_ingest_gb_per_day(
            {"usage_summary": summ}, datetime(2026, 7, 1, tzinfo=UTC), 28.0
        )
        assert gb == pytest.approx(100.0)
        # classic-only org (no twol) -> None, so caller falls through
        assert datadog_collect.summary_logs_ingest_gb_per_day(
            {"usage_summary": {"usage": []}}, datetime(2026, 7, 1, tzinfo=UTC), 28.0
        ) is None


class TestEstimatedUsagePath:
    def test_logs_prefers_estimated_usage_over_classic_zero(
        self, datadog_collect, tmp_path, monkeypatch, respx_mock
    ):
        t = 1_700_000_000_000
        mock_datadog(respx_mock, query_responses={
            "estimated_usage.logs.ingested_bytes": [
                {"pointlist": [[t, 1e9], [t + 86_400_000, 2e9]]}  # 3 GB over 1 day
            ],
        })
        summary = run_collector(datadog_collect, tmp_path, monkeypatch)
        logs = figures_by_id(summary)["logs.ingest_gb_per_day"]
        assert logs["value"] == pytest.approx(3.0)  # from estimated_usage, not the 24 classic
        assert "estimated_usage" in logs["method"]

    def test_cost_falls_back_to_all_cost_metric_on_billing_403(
        self, datadog_collect, tmp_path, monkeypatch, respx_mock
    ):
        t = 1_700_000_000_000
        mock_datadog(respx_mock, estimated_cost_status=403, query_responses={
            "all.cost": [{"pointlist": [[t, 3000.0], [t + 3_600_000, 2000.0]]}],
        })
        summary = run_collector(datadog_collect, tmp_path, monkeypatch)
        cost = figures_by_id(summary)["cost.monthly_usd"]
        assert cost["value"] == pytest.approx(5000.0)
        assert cost["status"] == "estimated"
        assert "all.cost" in cost["method"]

    def test_permission_denied_gap_gets_site_hint(
        self, datadog_collect, tmp_path, monkeypatch, respx_mock
    ):
        mock_datadog(respx_mock, estimated_cost_status=403)
        summary = run_collector(datadog_collect, tmp_path, monkeypatch)
        perm = [g for g in summary["gaps"] if g["reason"] == "permission_denied"]
        assert perm, "expected a permission_denied gap"
        assert any("--site=us1" in (g.get("remediation") or "") for g in perm)

    def test_hosts_reports_max_from_estimated_usage(
        self, datadog_collect, tmp_path, monkeypatch, respx_mock
    ):
        # estimated_usage.hosts is primary and reduced by max -> peak, not average,
        # and not the hosts/totals fixture value (142)
        mock_datadog(respx_mock, query_responses={
            "estimated_usage.hosts": [{"pointlist": [[1, 50.0], [2, 80.0], [3, 65.0]]}],
        })
        summary = run_collector(datadog_collect, tmp_path, monkeypatch)
        h = figures_by_id(summary)["hosts.count"]
        assert h["value"] == pytest.approx(80.0)  # max(50,80,65), not avg 65, not 142
        assert "max" in h["method"]

    def test_hosts_falls_back_to_totals_when_no_metric(
        self, datadog_collect, tmp_path, monkeypatch, respx_mock
    ):
        mock_datadog(respx_mock)  # estimated_usage empty -> hosts/totals fallback
        summary = run_collector(datadog_collect, tmp_path, monkeypatch)
        assert figures_by_id(summary)["hosts.count"]["value"] == 142

    def test_traces_prefer_estimated_usage_apm_bytes(
        self, datadog_collect, tmp_path, monkeypatch, respx_mock
    ):
        t = 1_700_000_000_000
        mock_datadog(respx_mock, query_responses={
            "apm.ingested_bytes": [{"pointlist": [[t, 3e9], [t + 86_400_000, 3e9]]}],
        })
        traces = figures_by_id(run_collector(datadog_collect, tmp_path, monkeypatch))[
            "traces.ingest_gb_per_day"]
        assert traces["value"] == pytest.approx(6.0)  # 6 GB over 1 day, not the 12 classic
        assert "estimated_usage" in traces["method"]

    def test_rum_prefers_estimated_usage_sessions(
        self, datadog_collect, tmp_path, monkeypatch, respx_mock
    ):
        t = 1_700_000_000_000
        mock_datadog(respx_mock, query_responses={
            "rum.ingested_sessions": [{"pointlist": [[t, 1000.0], [t + 86_400_000, 1000.0]]}],
        })
        rum = figures_by_id(run_collector(datadog_collect, tmp_path, monkeypatch))[
            "datadog.rum_sessions_per_day"]
        assert rum["value"] == pytest.approx(2000.0)  # 2000 sessions/day
        assert "estimated_usage" in rum["method"]

    def test_custom_metrics_prefers_estimated_usage_over_hourly(
        self, datadog_collect, tmp_path, monkeypatch, respx_mock
    ):
        # hourly num_custom_timeseries is present in the fixture, but estimated_usage
        # is primary, so the figure must come from the metric query (avg 100,300=200)
        mock_datadog(respx_mock, query_responses={
            "estimated_usage.metrics.custom": [{"pointlist": [[1, 100.0], [2, 300.0]]}],
        })
        summary = run_collector(datadog_collect, tmp_path, monkeypatch)
        cm = figures_by_id(summary)["metrics.custom_metrics_count"]
        assert cm["value"] == pytest.approx(200.0)
        assert "estimated_usage" in cm["method"]
