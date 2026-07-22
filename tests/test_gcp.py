from __future__ import annotations

import json

import fixtures_gcp as fx
import httpx
import pytest

MONITORING_BASE = "https://monitoring.googleapis.com"
LOGGING_BASE = "https://logging.googleapis.com"
PROJECT = fx.PROJECT


def mock_gcp(respx_mock, auth_status=200):
    """Register respx routes for all GCP API endpoints."""

    # metric descriptors
    respx_mock.get(
        f"{MONITORING_BASE}/v3/projects/{PROJECT}/metricDescriptors"
    ).respond(json={"metricDescriptors": list(fx.METRIC_DESCRIPTORS)})

    # timeSeries (billing metrics) — route by metric.type filter
    def timeseries_side_effect(request: httpx.Request) -> httpx.Response:
        if auth_status != 200:
            return httpx.Response(auth_status, json={"error": {"message": "denied"}})
        metric_filter = request.url.params.get("filter", "")
        if "billing/samples_ingested" in metric_filter:
            return httpx.Response(200, json=fx.BILLING_SAMPLES)
        elif "monitoring.googleapis.com/billing/bytes_ingested" in metric_filter:
            return httpx.Response(200, json=fx.METRIC_BILLING_BYTES)
        elif "billing/monthly_bytes_ingested" in metric_filter:
            return httpx.Response(200, json=fx.LOG_BILLING_MONTHLY)
        elif "logging.googleapis.com/billing/bytes_ingested" in metric_filter:
            return httpx.Response(200, json=fx.LOG_BILLING_INGEST)
        elif "billing/spans_ingested" in metric_filter:
            return httpx.Response(200, json=fx.TRACE_BILLING)
        return httpx.Response(200, json={"timeSeries": []})

    respx_mock.get(f"{MONITORING_BASE}/v3/projects/{PROJECT}/timeSeries").mock(
        side_effect=timeseries_side_effect
    )

    # alert policies
    respx_mock.get(
        f"{MONITORING_BASE}/v3/projects/{PROJECT}/alertPolicies"
    ).respond(json={"alertPolicies": list(fx.ALERT_POLICIES)})

    # log buckets
    respx_mock.get(
        f"{LOGGING_BASE}/v2/projects/{PROJECT}/locations/-/buckets"
    ).respond(json={"buckets": list(fx.LOG_BUCKETS)})

    # log sinks
    respx_mock.get(
        f"{LOGGING_BASE}/v2/projects/{PROJECT}/sinks"
    ).respond(json={"sinks": list(fx.LOG_SINKS)})


def run_collector(gcp_collect, tmp_path, monkeypatch, respx_mock, extra_args=None,
                  auth_status=200):
    mock_gcp(respx_mock, auth_status=auth_status)
    argv = [
        "collect.py",
        "--output-dir", str(tmp_path / "out"),
        "--lookback", f"{fx.LOOKBACK_DAYS}d",
        "--project", PROJECT,
    ]
    argv += extra_args or []
    monkeypatch.setattr("sys.argv", argv)
    monkeypatch.setattr(gcp_collect, "gcloud_access_token", lambda: "fake-token")
    rc = gcp_collect.main()
    assert rc == 0
    return json.loads((tmp_path / "out" / "summary.json").read_text())


def figures_by_id(summary):
    return {f["id"]: f for f in summary["figures"]}


class TestGcpCollector:
    def test_full_run_known_ground_truth(
        self, gcp_collect, tmp_path, monkeypatch, respx_mock, summary_schema
    ):
        summary = run_collector(gcp_collect, tmp_path, monkeypatch, respx_mock)
        jsonschema = pytest.importorskip("jsonschema")
        jsonschema.validate(
            {k: v for k, v in summary.items() if not k.startswith("_")}, summary_schema
        )

        figs = figures_by_id(summary)

        # metrics
        assert figs["metrics.total_count"]["value"] == fx.TOTAL_DESCRIPTORS
        assert figs["metrics.custom_metrics_count"]["value"] == fx.CUSTOM_DESCRIPTORS

        # samples/sec: 604800 / (7*86400) = 1.0
        assert figs["metrics.samples_per_sec"]["value"] == pytest.approx(
            fx.TOTAL_SAMPLES / fx.LOOKBACK_S, rel=0.01
        )

        # logs: 70 GB / 7 days = 10.0 GB/day
        assert figs["logs.ingest_gb_per_day"]["value"] == pytest.approx(
            fx.TOTAL_LOG_BYTES / 1e9 / fx.LOOKBACK_DAYS, rel=0.01
        )

        # metric bytes (GMP): 35 GB / 7 days = 5.0 GB/day
        assert figs["metrics.ingest_gb_per_day"]["value"] == pytest.approx(
            fx.TOTAL_METRIC_BYTES / 1e9 / fx.LOOKBACK_DAYS, rel=0.01
        )

        # stored: 150 GB (estimated)
        assert figs["logs.stored_gb"]["value"] == pytest.approx(
            fx.MONTHLY_LOG_BYTES / 1e9, rel=0.01
        )
        assert figs["logs.stored_gb"]["status"] == "estimated"

        # traces: 6048000 / (7*86400) = 10.0 spans/sec
        assert figs["traces.spans_per_sec"]["value"] == pytest.approx(
            fx.TOTAL_TRACE_SPANS / fx.LOOKBACK_S, rel=0.01
        )

        # alerts
        assert figs["alerts.monitor_count"]["value"] == fx.ALERT_POLICIES_COUNT

        # cost is always unavailable (no GCP billing API)
        assert figs["cost.monthly_usd"]["status"] == "unavailable"
        cost_gap = next(
            g for g in summary["gaps"] if "cost.monthly_usd" in g["figure_ids"]
        )
        assert cost_gap["reason"] == "not_configured"
        assert cost_gap["remediation"]

        # only cost should be a gap
        non_cost_gaps = [g for g in summary["gaps"] if "cost.monthly_usd" not in g["figure_ids"]]
        assert non_cost_gaps == []

        # provenance present on every collected figure
        for fig in summary["figures"]:
            if fig["status"] not in ("unavailable",):
                assert fig["method"], fig["id"]
                assert fig["source_api"], fig["id"]
                assert fig["evidence_files"], fig["id"]

    def test_inventory_populated(self, gcp_collect, tmp_path, monkeypatch, respx_mock):
        summary = run_collector(gcp_collect, tmp_path, monkeypatch, respx_mock)
        inv = summary["inventory"]
        assert inv["projects_collected"] == [PROJECT]
        assert "metric_type_breakdown" in inv
        assert "log_buckets" in inv
        assert len(inv["log_buckets"]) == fx.LOG_BUCKETS_COUNT
        assert "log_sinks" in inv
        assert len(inv["log_sinks"]) == fx.LOG_SINKS_COUNT
        assert "alert_policies_by_condition_type" in inv

    def test_auth_failure_gaps_billing_metrics(
        self, gcp_collect, tmp_path, monkeypatch, respx_mock
    ):
        summary = run_collector(
            gcp_collect, tmp_path, monkeypatch, respx_mock, auth_status=403
        )
        figs = figures_by_id(summary)
        # billing metrics should be unavailable due to 403
        assert figs["metrics.samples_per_sec"]["status"] == "unavailable"
        assert figs["logs.ingest_gb_per_day"]["status"] == "unavailable"
        assert figs["logs.stored_gb"]["status"] == "unavailable"
        assert figs["traces.spans_per_sec"]["status"] == "unavailable"
        # metric descriptors and alert policies still work (separate routes)
        assert figs["metrics.total_count"]["status"] == "ok"
        assert figs["alerts.monitor_count"]["status"] == "ok"
        # the failed access preflight is surfaced as a gap, not silent
        gap = next(g for g in summary["gaps"] if g["area"] == "environment")
        assert "access preflight" in gap["detail"]
        assert "test-project-1" in gap["detail"]

    def test_metric_domain_breakdown_isolates_gmp(
        self, gcp_collect, tmp_path, monkeypatch, respx_mock
    ):
        summary = run_collector(gcp_collect, tmp_path, monkeypatch, respx_mock)
        inv = summary["inventory"]
        domains = {d["domain"]: d for d in inv["metric_domains"]}
        assert "prometheus.googleapis.com" in domains  # GMP present
        assert "kubernetes.io" in domains
        # GMP samples isolated: 500000 / (7*86400) samples/sec
        assert inv["gmp_metric_samples_per_sec"] == pytest.approx(
            fx.GMP_SAMPLES / fx.LOOKBACK_S, rel=0.01
        )
        # GMP bills by samples -> 0 bytes
        assert inv["gmp_metric_gb_per_day"] == 0.0

    def test_metric_queries_grouped_by_domain(
        self, gcp_collect, tmp_path, monkeypatch, respx_mock
    ):
        run_collector(gcp_collect, tmp_path, monkeypatch, respx_mock)
        samples_call = next(
            c for c in respx_mock.calls
            if "/timeSeries" in c.request.url.path
            and "samples_ingested" in c.request.url.params.get("filter", "")
            and "aggregation.perSeriesAligner" in c.request.url.params  # skip preflight
            and c.request.url.params.get("aggregation.groupByFields")
        )
        assert samples_call.request.url.params.get(
            "aggregation.groupByFields") == "metric.label.metric_domain"

    def test_billing_queries_are_aggregated_server_side(
        self, gcp_collect, tmp_path, monkeypatch, respx_mock
    ):
        # the DELTA volume metrics must carry server-side aggregation params so
        # busy projects don't exceed Google's response-size limit
        run_collector(gcp_collect, tmp_path, monkeypatch, respx_mock)
        ts_calls = [
            c for c in respx_mock.calls
            if "/timeSeries" in c.request.url.path
            and "samples_ingested" in c.request.url.params.get("filter", "")
        ]
        assert ts_calls, "expected a samples_ingested timeSeries call"
        params = ts_calls[-1].request.url.params
        assert params.get("aggregation.perSeriesAligner") == "ALIGN_DELTA"
        assert params.get("aggregation.crossSeriesReducer") == "REDUCE_SUM"

    def test_report_only_recomputes_from_evidence(
        self, gcp_collect, tmp_path, monkeypatch, respx_mock
    ):
        first = run_collector(gcp_collect, tmp_path, monkeypatch, respx_mock)
        monkeypatch.setattr(
            "sys.argv",
            ["collect.py", "--output-dir", str(tmp_path / "out"), "--report-only"],
        )
        rc = gcp_collect.main()
        assert rc == 0
        second = json.loads((tmp_path / "out" / "summary.json").read_text())
        first_figs = figures_by_id(first)
        second_figs = figures_by_id(second)
        assert (
            second_figs["metrics.total_count"]["value"]
            == first_figs["metrics.total_count"]["value"]
        )
        assert second_figs["logs.ingest_gb_per_day"]["value"] == pytest.approx(
            first_figs["logs.ingest_gb_per_day"]["value"]
        )

    def test_environment_metadata(self, gcp_collect, tmp_path, monkeypatch, respx_mock):
        summary = run_collector(gcp_collect, tmp_path, monkeypatch, respx_mock)
        env = summary["environment"]
        assert env["detected_backend"] == "gcp-cloud-operations"
        assert "projects" in env
        assert PROJECT in env["projects"]

    def test_evidence_files_written(self, gcp_collect, tmp_path, monkeypatch, respx_mock):
        run_collector(gcp_collect, tmp_path, monkeypatch, respx_mock)
        evidence_dir = tmp_path / "out" / "evidence"
        assert evidence_dir.exists()
        evidence_files = list(evidence_dir.glob("*.json"))
        # 10 evidence files: _projects, metric_descriptors, billing_samples,
        # metric_billing_bytes, log_billing_ingest, log_billing_monthly,
        # trace_billing, alert_policies, log_buckets, log_sinks
        assert len(evidence_files) == 10
        manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
        assert len(manifest) == 10

    def test_custom_metric_prefix_breakdown(
        self, gcp_collect, tmp_path, monkeypatch, respx_mock
    ):
        summary = run_collector(gcp_collect, tmp_path, monkeypatch, respx_mock)
        inv = summary["inventory"]
        prefixes = inv.get("custom_metric_prefixes", {})
        assert "custom.googleapis.com" in prefixes
        assert "prometheus.googleapis.com" in prefixes
        total_custom = sum(prefixes.values())
        assert total_custom == fx.CUSTOM_DESCRIPTORS

    def test_token_redacted_in_evidence(
        self, gcp_collect, tmp_path, monkeypatch, respx_mock
    ):
        run_collector(gcp_collect, tmp_path, monkeypatch, respx_mock)
        for f in (tmp_path / "out").rglob("*.json"):
            assert "fake-token" not in f.read_text(), f
