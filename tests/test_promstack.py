"""Tests for collectors/promstack/collect.py."""

import json

import fixtures_promstack as fx
import httpx
import pytest
import respx

ENDPOINT = "http://test-prom:9090"
VM_ENDPOINT = "http://test-vm:8428"
THANOS_ENDPOINT = "http://test-thanos:10902"
LOKI_ENDPOINT = "http://test-loki:3100"
TEMPO_ENDPOINT = "http://test-tempo:3200"


def _figs(summary):
    return {f["id"]: f for f in summary["figures"]}


# ---------------------------------------------------------------------------
# Prometheus mocking
# ---------------------------------------------------------------------------

def _mock_prometheus(mock):
    """Wire up respx routes for a Prometheus backend."""
    mock.get("/api/v1/status/buildinfo").mock(
        return_value=httpx.Response(200, json=fx.PROM_BUILDINFO))
    mock.get("/api/v1/status/tsdb").mock(
        return_value=httpx.Response(200, json=fx.PROM_TSDB_STATUS))
    mock.get("/api/v1/status/flags").mock(
        return_value=httpx.Response(200, json=fx.PROM_STATUS_FLAGS))

    def route_queries(request):
        query = request.url.params.get("query", "")
        if "prometheus_tsdb_head_samples_appended_total" in query:
            return httpx.Response(200, json=fx.PROM_SAMPLES_RATE)
        if query == "count(up)":
            return httpx.Response(200, json=fx.PROM_SCRAPE_TARGETS_RESP)
        if "prometheus_build_info" in query:
            return httpx.Response(200, json=fx.PROM_BUILD_INFO_METRIC)
        if "vm_app_version" in query:
            return httpx.Response(200, json=fx.EMPTY_PROBE)
        if "thanos_build_info" in query:
            return httpx.Response(200, json=fx.EMPTY_PROBE)
        if "cortex_build_info" in query:
            return httpx.Response(200, json=fx.EMPTY_PROBE)
        if "loki_distributor_bytes_received_total" in query:
            return httpx.Response(200, json=fx.EMPTY_PROBE)
        if "tempo_distributor_spans_received_total" in query:
            return httpx.Response(200, json=fx.EMPTY_PROBE)
        if "tempo_distributor_bytes_received_total" in query:
            return httpx.Response(200, json=fx.EMPTY_PROBE)
        return httpx.Response(200, json=fx.EMPTY_PROBE)

    mock.get("/api/v1/query").mock(side_effect=route_queries)


def _run(promstack_collect, tmp_path, monkeypatch, mock_fn, extra_args=None):
    """Run collector with mocked HTTP, return parsed summary dict."""
    output_dir = tmp_path / "out"
    argv = [
        "collect.py",
        "--endpoint", ENDPOINT,
        "--output-dir", str(output_dir),
        "--lookback", "7d",
    ]
    if extra_args:
        argv.extend(extra_args)
    monkeypatch.setattr("sys.argv", argv)
    with respx.mock(base_url=ENDPOINT) as mock:
        mock_fn(mock)
        rc = promstack_collect.main()
    assert rc == 0
    return json.loads((output_dir / "summary.json").read_text())


class TestImport:
    def test_module_loads(self, promstack_collect):
        assert hasattr(promstack_collect, "main")
        assert hasattr(promstack_collect, "COLLECTOR")
        assert promstack_collect.COLLECTOR == "promstack"


class TestPrometheus:
    def test_detection(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run(promstack_collect, tmp_path, monkeypatch, _mock_prometheus)
        assert summary["environment"]["detected_backend"] == "prometheus"
        assert summary["environment"]["version"] == fx.PROM_VERSION

    def test_active_series(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run(promstack_collect, tmp_path, monkeypatch, _mock_prometheus)
        figs = _figs(summary)
        assert figs["metrics.active_series"]["value"] == fx.PROM_ACTIVE_SERIES
        assert figs["metrics.active_series"]["status"] == "ok"
        assert figs["metrics.active_series"]["method"] == "TSDB head stats"

    def test_samples_per_sec(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run(promstack_collect, tmp_path, monkeypatch, _mock_prometheus)
        figs = _figs(summary)
        assert figs["metrics.samples_per_sec"]["value"] == pytest.approx(
            fx.PROM_SAMPLES_PER_SEC, rel=0.01)
        assert figs["metrics.samples_per_sec"]["status"] == "ok"

    def test_scrape_targets(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run(promstack_collect, tmp_path, monkeypatch, _mock_prometheus)
        figs = _figs(summary)
        assert figs["promstack.scrape_targets"]["value"] == fx.PROM_SCRAPE_TARGETS

    def test_retention(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run(promstack_collect, tmp_path, monkeypatch, _mock_prometheus)
        figs = _figs(summary)
        assert figs["promstack.retention_days"]["value"] == fx.PROM_RETENTION_DAYS
        assert figs["promstack.retention_days"]["status"] == "ok"

    def test_top_metrics_in_inventory(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run(promstack_collect, tmp_path, monkeypatch, _mock_prometheus)
        inv = summary["inventory"]
        assert "top_metrics_by_series" in inv
        assert inv["top_metrics_by_series"][0]["name"] == "node_cpu_seconds_total"

    def test_loki_tempo_gaps_when_not_configured(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run(promstack_collect, tmp_path, monkeypatch, _mock_prometheus)
        figs = _figs(summary)
        assert figs["logs.ingest_gb_per_day"]["status"] == "unavailable"
        assert figs["traces.spans_per_sec"]["status"] == "unavailable"

    def test_schema_valid(self, promstack_collect, tmp_path, monkeypatch, summary_schema):
        import jsonschema
        summary = _run(promstack_collect, tmp_path, monkeypatch, _mock_prometheus)
        jsonschema.validate(summary, summary_schema)

    def test_evidence_files_written(self, promstack_collect, tmp_path, monkeypatch):
        _run(promstack_collect, tmp_path, monkeypatch, _mock_prometheus)
        evidence_dir = tmp_path / "out" / "evidence"
        assert (evidence_dir / "buildinfo.json").exists()
        assert (evidence_dir / "tsdb_status.json").exists()
        assert (evidence_dir / "status_flags.json").exists()

    def test_report_only(self, promstack_collect, tmp_path, monkeypatch):
        _run(promstack_collect, tmp_path, monkeypatch, _mock_prometheus)
        monkeypatch.setattr("sys.argv", [
            "collect.py", "--output-dir", str(tmp_path / "out"), "--report-only",
        ])
        rc = promstack_collect.main()
        assert rc == 0
        second = json.loads((tmp_path / "out" / "summary.json").read_text())
        figs = _figs(second)
        assert figs["metrics.active_series"]["value"] == fx.PROM_ACTIVE_SERIES


# ---------------------------------------------------------------------------
# VictoriaMetrics mocking
# ---------------------------------------------------------------------------

def _mock_victoriametrics(mock):
    """Wire up respx routes for a VictoriaMetrics backend."""
    mock.get("/api/v1/status/buildinfo").mock(
        return_value=httpx.Response(200, json=fx.VM_BUILDINFO))
    mock.get("/api/v1/status/tsdb").mock(
        return_value=httpx.Response(200, json=fx.VM_TSDB_STATUS))
    mock.get("/flags").mock(
        return_value=httpx.Response(200, text=fx.VM_FLAGS_TEXT))

    def route_queries(request):
        query = request.url.params.get("query", "")
        if "vm_cache_entries" in query:
            return httpx.Response(200, json=fx.VM_ACTIVE_SERIES_RESP)
        if "vm_rows_inserted_total" in query:
            return httpx.Response(200, json=fx.VM_SAMPLES_RATE)
        if query == "count(up)":
            return httpx.Response(200, json=fx.VM_SCRAPE_TARGETS_RESP)
        if "vm_app_version" in query:
            return httpx.Response(200, json=fx.VM_APP_VERSION_PROBE)
        if "loki_distributor_bytes_received_total" in query:
            return httpx.Response(200, json=fx.EMPTY_PROBE)
        if "tempo_distributor" in query:
            return httpx.Response(200, json=fx.EMPTY_PROBE)
        return httpx.Response(200, json=fx.EMPTY_PROBE)

    mock.get("/api/v1/query").mock(side_effect=route_queries)


def _run_vm(promstack_collect, tmp_path, monkeypatch, extra_args=None):
    output_dir = tmp_path / "out"
    argv = [
        "collect.py",
        "--endpoint", VM_ENDPOINT,
        "--output-dir", str(output_dir),
        "--lookback", "7d",
    ]
    if extra_args:
        argv.extend(extra_args)
    monkeypatch.setattr("sys.argv", argv)
    with respx.mock(base_url=VM_ENDPOINT) as mock:
        _mock_victoriametrics(mock)
        rc = promstack_collect.main()
    assert rc == 0
    return json.loads((output_dir / "summary.json").read_text())


class TestVictoriaMetrics:
    def test_detection(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run_vm(promstack_collect, tmp_path, monkeypatch)
        assert summary["environment"]["detected_backend"] == "victoriametrics"
        assert "v1.103.0" in summary["environment"]["version"]

    def test_active_series(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run_vm(promstack_collect, tmp_path, monkeypatch)
        figs = _figs(summary)
        assert figs["metrics.active_series"]["value"] == pytest.approx(
            fx.VM_ACTIVE_SERIES, rel=0.01)
        assert figs["metrics.active_series"]["status"] == "ok"

    def test_samples_per_sec(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run_vm(promstack_collect, tmp_path, monkeypatch)
        figs = _figs(summary)
        assert figs["metrics.samples_per_sec"]["value"] == pytest.approx(
            fx.VM_SAMPLES_PER_SEC, rel=0.01)

    def test_retention(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run_vm(promstack_collect, tmp_path, monkeypatch)
        figs = _figs(summary)
        assert figs["promstack.retention_days"]["value"] == fx.VM_RETENTION_DAYS
        assert figs["promstack.retention_days"]["status"] == "ok"

    def test_top_metrics_inventory(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run_vm(promstack_collect, tmp_path, monkeypatch)
        inv = summary["inventory"]
        assert inv["backend"] == "victoriametrics"
        assert "top_metrics_by_series" in inv

    def test_schema_valid(self, promstack_collect, tmp_path, monkeypatch, summary_schema):
        import jsonschema
        summary = _run_vm(promstack_collect, tmp_path, monkeypatch)
        jsonschema.validate(summary, summary_schema)


# ---------------------------------------------------------------------------
# Thanos mocking
# ---------------------------------------------------------------------------

def _mock_thanos(mock, rf_resp=None):
    """Wire up respx routes for a Thanos backend."""
    mock.get("/api/v1/status/buildinfo").mock(
        return_value=httpx.Response(200, json={"status": "success", "data": {}}))

    def route_queries(request):
        query = request.url.params.get("query", "")
        if "vm_app_version" in query:
            return httpx.Response(200, json=fx.EMPTY_PROBE)
        if "thanos_build_info" in query:
            return httpx.Response(200, json=fx.THANOS_BUILD_INFO_METRIC)
        if "cortex_build_info" in query:
            return httpx.Response(200, json=fx.EMPTY_PROBE)
        if "prometheus_build_info" in query:
            return httpx.Response(200, json=fx.EMPTY_PROBE)
        if query == "sum(prometheus_tsdb_head_series)":
            return httpx.Response(200, json=fx.THANOS_ACTIVE_SERIES_RESP)
        if "prometheus_tsdb_head_samples_appended_total" in query:
            return httpx.Response(200, json=fx.THANOS_SAMPLES_RATE)
        if query == "count(up)":
            return httpx.Response(200, json=fx.THANOS_SCRAPE_TARGETS_RESP)
        if "thanos_receive_replication_factor" in query:
            return httpx.Response(200, json=rf_resp or fx.THANOS_RF_RESP)
        if "loki_distributor" in query or "tempo_distributor" in query:
            return httpx.Response(200, json=fx.EMPTY_PROBE)
        return httpx.Response(200, json=fx.EMPTY_PROBE)

    mock.get("/api/v1/query").mock(side_effect=route_queries)


def _run_thanos(promstack_collect, tmp_path, monkeypatch, rf_resp=None, extra_args=None):
    output_dir = tmp_path / "out"
    argv = [
        "collect.py",
        "--endpoint", THANOS_ENDPOINT,
        "--output-dir", str(output_dir),
        "--lookback", "7d",
    ]
    if extra_args:
        argv.extend(extra_args)
    monkeypatch.setattr("sys.argv", argv)
    with respx.mock(base_url=THANOS_ENDPOINT, assert_all_called=False) as mock:
        _mock_thanos(mock, rf_resp=rf_resp)
        rc = promstack_collect.main()
    assert rc == 0
    return json.loads((output_dir / "summary.json").read_text())


class TestThanos:
    def test_detection(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run_thanos(promstack_collect, tmp_path, monkeypatch)
        assert summary["environment"]["detected_backend"] == "thanos"
        assert summary["environment"]["version"] == fx.THANOS_VERSION

    def test_active_series_sidecar_mode(self, promstack_collect, tmp_path, monkeypatch):
        """Sidecar mode: RF not detected, no division."""
        summary = _run_thanos(promstack_collect, tmp_path, monkeypatch)
        figs = _figs(summary)
        assert figs["metrics.active_series"]["value"] == fx.THANOS_ACTIVE_SERIES_RAW
        assert figs["metrics.active_series"]["status"] == "ok"

    def test_active_series_cli_rf_override(self, promstack_collect, tmp_path, monkeypatch):
        """CLI --replication-factor overrides auto-detect."""
        summary = _run_thanos(promstack_collect, tmp_path, monkeypatch,
                              extra_args=["--replication-factor", "3"])
        figs = _figs(summary)
        expected = fx.THANOS_ACTIVE_SERIES_RAW // 3
        assert figs["metrics.active_series"]["value"] == expected
        assert figs["metrics.active_series"]["status"] == "partial"
        assert "RF=3" in figs["metrics.active_series"]["method"]

    def test_retention_unavailable(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run_thanos(promstack_collect, tmp_path, monkeypatch)
        figs = _figs(summary)
        assert figs["promstack.retention_days"]["status"] == "unavailable"

    def test_top_metrics_gap(self, promstack_collect, tmp_path, monkeypatch):
        """Thanos has no /status/tsdb — top_metrics should not be in inventory."""
        summary = _run_thanos(promstack_collect, tmp_path, monkeypatch)
        assert "top_metrics_by_series" not in summary["inventory"]

    def test_samples_per_sec(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run_thanos(promstack_collect, tmp_path, monkeypatch)
        figs = _figs(summary)
        assert figs["metrics.samples_per_sec"]["value"] == pytest.approx(
            fx.THANOS_SAMPLES_PER_SEC, rel=0.01)

    def test_schema_valid(self, promstack_collect, tmp_path, monkeypatch, summary_schema):
        import jsonschema
        summary = _run_thanos(promstack_collect, tmp_path, monkeypatch)
        jsonschema.validate(summary, summary_schema)


# ---------------------------------------------------------------------------
# Loki mocking (multi-host: ENDPOINT for TSDB + LOKI_ENDPOINT for direct)
# ---------------------------------------------------------------------------

def _run_with_loki(promstack_collect, tmp_path, monkeypatch):
    output_dir = tmp_path / "out"
    argv = [
        "collect.py",
        "--endpoint", ENDPOINT,
        "--loki-endpoint", LOKI_ENDPOINT,
        "--output-dir", str(output_dir),
        "--lookback", "7d",
    ]
    monkeypatch.setattr("sys.argv", argv)

    with respx.mock as mock:
        mock.get(f"{ENDPOINT}/api/v1/status/buildinfo").mock(
            return_value=httpx.Response(200, json=fx.PROM_BUILDINFO))
        mock.get(f"{ENDPOINT}/api/v1/status/tsdb").mock(
            return_value=httpx.Response(200, json=fx.PROM_TSDB_STATUS))
        mock.get(f"{ENDPOINT}/api/v1/status/flags").mock(
            return_value=httpx.Response(200, json=fx.PROM_STATUS_FLAGS))

        def route_main_queries(request):
            query = request.url.params.get("query", "")
            if "prometheus_tsdb_head_samples_appended_total" in query:
                return httpx.Response(200, json=fx.PROM_SAMPLES_RATE)
            if query == "count(up)":
                return httpx.Response(200, json=fx.PROM_SCRAPE_TARGETS_RESP)
            if "prometheus_build_info" in query:
                return httpx.Response(200, json=fx.PROM_BUILD_INFO_METRIC)
            if "loki_distributor_bytes_received_total" in query:
                return httpx.Response(200, json=fx.LOKI_INGEST_RATE_RESP)
            if "tempo_distributor" in query:
                return httpx.Response(200, json=fx.EMPTY_PROBE)
            return httpx.Response(200, json=fx.EMPTY_PROBE)

        mock.get(f"{ENDPOINT}/api/v1/query").mock(side_effect=route_main_queries)

        mock.get(f"{LOKI_ENDPOINT}/loki/api/v1/status/buildinfo").mock(
            return_value=httpx.Response(200, json=fx.LOKI_BUILDINFO))
        mock.get(f"{LOKI_ENDPOINT}/config").mock(
            return_value=httpx.Response(200, text=fx.LOKI_CONFIG_TEXT))
        mock.get(f"{LOKI_ENDPOINT}/metrics").mock(
            return_value=httpx.Response(200, text=""))

        rc = promstack_collect.main()
    assert rc == 0
    return json.loads((output_dir / "summary.json").read_text())


class TestLoki:
    def test_ingest_gb_per_day_from_tsdb(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run_with_loki(promstack_collect, tmp_path, monkeypatch)
        figs = _figs(summary)
        assert figs["logs.ingest_gb_per_day"]["value"] == pytest.approx(
            fx.LOKI_INGEST_GB_PER_DAY, rel=0.01)
        assert figs["logs.ingest_gb_per_day"]["status"] == "ok"

    def test_retention_from_config(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run_with_loki(promstack_collect, tmp_path, monkeypatch)
        figs = _figs(summary)
        assert figs["logs.retention_days"]["value"] == fx.LOKI_RETENTION_DAYS
        assert figs["logs.retention_days"]["status"] == "ok"

    def test_schema_valid(self, promstack_collect, tmp_path, monkeypatch, summary_schema):
        import jsonschema
        summary = _run_with_loki(promstack_collect, tmp_path, monkeypatch)
        jsonschema.validate(summary, summary_schema)


# ---------------------------------------------------------------------------
# Tempo mocking (multi-host: ENDPOINT for TSDB + TEMPO_ENDPOINT for direct)
# ---------------------------------------------------------------------------

def _run_with_tempo(promstack_collect, tmp_path, monkeypatch):
    output_dir = tmp_path / "out"
    argv = [
        "collect.py",
        "--endpoint", ENDPOINT,
        "--tempo-endpoint", TEMPO_ENDPOINT,
        "--output-dir", str(output_dir),
        "--lookback", "7d",
    ]
    monkeypatch.setattr("sys.argv", argv)

    with respx.mock as mock:
        mock.get(f"{ENDPOINT}/api/v1/status/buildinfo").mock(
            return_value=httpx.Response(200, json=fx.PROM_BUILDINFO))
        mock.get(f"{ENDPOINT}/api/v1/status/tsdb").mock(
            return_value=httpx.Response(200, json=fx.PROM_TSDB_STATUS))
        mock.get(f"{ENDPOINT}/api/v1/status/flags").mock(
            return_value=httpx.Response(200, json=fx.PROM_STATUS_FLAGS))

        def route_main_queries(request):
            query = request.url.params.get("query", "")
            if "prometheus_tsdb_head_samples_appended_total" in query:
                return httpx.Response(200, json=fx.PROM_SAMPLES_RATE)
            if query == "count(up)":
                return httpx.Response(200, json=fx.PROM_SCRAPE_TARGETS_RESP)
            if "prometheus_build_info" in query:
                return httpx.Response(200, json=fx.PROM_BUILD_INFO_METRIC)
            if "tempo_distributor_spans_received_total" in query:
                return httpx.Response(200, json=fx.TEMPO_SPANS_RATE_RESP)
            if "tempo_distributor_bytes_received_total" in query:
                return httpx.Response(200, json=fx.TEMPO_INGEST_RATE_RESP)
            if "loki_distributor" in query:
                return httpx.Response(200, json=fx.EMPTY_PROBE)
            return httpx.Response(200, json=fx.EMPTY_PROBE)

        mock.get(f"{ENDPOINT}/api/v1/query").mock(side_effect=route_main_queries)

        mock.get(f"{TEMPO_ENDPOINT}/api/status/buildinfo").mock(
            return_value=httpx.Response(200, json=fx.TEMPO_BUILDINFO))
        mock.get(f"{TEMPO_ENDPOINT}/metrics").mock(
            return_value=httpx.Response(200, text=""))

        rc = promstack_collect.main()
    assert rc == 0
    return json.loads((output_dir / "summary.json").read_text())


class TestTempo:
    def test_spans_per_sec(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run_with_tempo(promstack_collect, tmp_path, monkeypatch)
        figs = _figs(summary)
        assert figs["traces.spans_per_sec"]["value"] == pytest.approx(
            fx.TEMPO_SPANS_PER_SEC, rel=0.01)
        assert figs["traces.spans_per_sec"]["status"] == "ok"

    def test_ingest_gb_per_day(self, promstack_collect, tmp_path, monkeypatch):
        summary = _run_with_tempo(promstack_collect, tmp_path, monkeypatch)
        figs = _figs(summary)
        assert figs["traces.ingest_gb_per_day"]["value"] == pytest.approx(
            fx.TEMPO_INGEST_GB_PER_DAY, rel=0.01)
        assert figs["traces.ingest_gb_per_day"]["status"] == "ok"

    def test_schema_valid(self, promstack_collect, tmp_path, monkeypatch, summary_schema):
        import jsonschema
        summary = _run_with_tempo(promstack_collect, tmp_path, monkeypatch)
        jsonschema.validate(summary, summary_schema)
