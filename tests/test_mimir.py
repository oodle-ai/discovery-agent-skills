from __future__ import annotations

import json

import fixtures_mimir as fx
import pytest
import respx
from httpx import Response

MIMIR_URL = "http://mimir:8080"


def _mock_mimir(mock: respx.MockRouter) -> None:
    """Wire up respx routes for a fully successful Mimir collection."""
    # detect_prom_prefix probes /prometheus first — 404 falls back to /api/v1
    mock.get("/prometheus/api/v1/query").mock(return_value=Response(404))
    mock.get("/api/v1/status/buildinfo").mock(
        return_value=Response(200, json=fx.BUILDINFO)
    )
    mock.get("/config").mock(
        return_value=Response(200, text=fx.CONFIG_TEXT, headers={"content-type": "text/yaml"})
    )
    _q = "/api/v1/query"
    _qr = "/api/v1/query_range"
    mock.get(
        _q, params__contains={"query": "sum(cortex_ingester_active_series)"},
    ).mock(return_value=Response(200, json=fx.ACTIVE_SERIES_SUM))
    mock.get(
        _q,
        params__contains={"query": "sum by (user) (cortex_ingester_active_series)"},
    ).mock(return_value=Response(200, json=fx.ACTIVE_SERIES_BY_USER))
    mock.get(_q).mock(
        return_value=Response(200, json=fx.SAMPLES_RATE_BY_USER)
    )
    mock.get(
        _qr,
        params__contains={"query": (
            "sum(rate(cortex_distributor_received_samples_total[5m]))"
            " or sum(rate(cortex_ingest_storage_reader_fetch"
            "_records_total[5m]))"
        )},
    ).mock(return_value=Response(200, json=fx.INGESTION_RATE_RANGE))
    mock.get(
        _qr,
        params__contains={"query": (
            'sum(rate(cortex_request_duration_seconds_count'
            '{route=~".*query.*"}[5m]))'
        )},
    ).mock(return_value=Response(200, json=fx.QUERY_REQUESTS_RANGE))
    mock.get(
        _qr,
        params__contains={"query": (
            "sum(rate(thanos_objstore_bucket_operations_total[5m]))"
            " by (operation)"
        )},
    ).mock(return_value=Response(200, json=fx.OBJSTORE_OPS_RANGE))


def run_collector(mimir_collect, tmp_path, monkeypatch, extra_args=None):
    argv = [
        "collect.py",
        "--endpoint", MIMIR_URL,
        "--output-dir", str(tmp_path / "out"),
        "--lookback", "7d",
    ]
    argv += extra_args or []
    monkeypatch.setattr("sys.argv", argv)
    with respx.mock(base_url=MIMIR_URL) as mock:
        _mock_mimir(mock)
        rc = mimir_collect.main()
    assert rc == 0
    return json.loads((tmp_path / "out" / "summary.json").read_text())


def figures_by_id(summary):
    return {f["id"]: f for f in summary["figures"]}


class TestMimirCollector:
    def test_full_run_known_ground_truth(
        self, mimir_collect, tmp_path, monkeypatch, summary_schema
    ):
        summary = run_collector(mimir_collect, tmp_path, monkeypatch)
        jsonschema = pytest.importorskip("jsonschema")
        jsonschema.validate(
            {k: v for k, v in summary.items() if not k.startswith("_")},
            summary_schema,
        )

        figs = figures_by_id(summary)
        assert figs["metrics.active_series"]["value"] == fx.ACTIVE_SERIES
        assert figs["metrics.active_series"]["status"] == "ok"

        assert figs["metrics.samples_per_sec"]["value"] == pytest.approx(
            fx.INGESTION_RATE_AVG, rel=0.01
        )
        assert figs["mimir.ingestion_rate_peak"]["value"] == pytest.approx(
            fx.INGESTION_RATE_PEAK, rel=0.01
        )

        assert figs["mimir.query_rate_avg"]["value"] == pytest.approx(
            fx.QUERY_RATE_AVG, rel=0.01
        )
        assert figs["mimir.query_rate_peak"]["value"] == pytest.approx(
            fx.QUERY_RATE_PEAK, rel=0.01
        )

        assert figs["mimir.objstore_ops_per_day"]["value"] == pytest.approx(
            fx.OBJSTORE_OPS_PER_DAY_APPROX, rel=0.01
        )

        assert figs["mimir.retention_days"]["value"] == fx.RETENTION_DAYS

        assert figs["cost.monthly_usd"]["status"] == "estimated"
        assert figs["cost.monthly_usd"]["value"] == pytest.approx(
            fx.TOTAL_COST_MO, rel=0.05
        )

        assert summary["gaps"] == []

        for fig in summary["figures"]:
            if fig["status"] != "unavailable":
                assert fig["method"], fig["id"]
                assert fig["source_api"], fig["id"]
                assert fig["evidence_files"], fig["id"]

    def test_environment_detected(self, mimir_collect, tmp_path, monkeypatch):
        summary = run_collector(mimir_collect, tmp_path, monkeypatch)
        env = summary["environment"]
        assert env["detected_backend"] == "mimir"
        assert env["version"] == fx.VERSION
        assert env["tenancy"] == "multi"

    def test_inventory_tenant_breakdown(self, mimir_collect, tmp_path, monkeypatch):
        summary = run_collector(mimir_collect, tmp_path, monkeypatch)
        inv = summary["inventory"]
        assert "tenant_breakdown" in inv
        tenants = {t["tenant"]: t for t in inv["tenant_breakdown"]}
        assert tenants["tenant-a"]["active_series"] == int(fx.TENANT_A_SERIES)
        assert tenants["tenant-b"]["active_series"] == int(fx.TENANT_B_SERIES)
        assert inv["replication_factor"] == fx.REPLICATION_FACTOR

    def test_evidence_files_written(self, mimir_collect, tmp_path, monkeypatch):
        run_collector(mimir_collect, tmp_path, monkeypatch)
        evidence_dir = tmp_path / "out" / "evidence"
        assert evidence_dir.exists()
        evidence_files = list(evidence_dir.glob("*.json"))
        assert len(evidence_files) >= 7
        manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
        assert len(manifest) >= 7

    def test_report_only_recomputes_from_evidence(
        self, mimir_collect, tmp_path, monkeypatch
    ):
        first = run_collector(mimir_collect, tmp_path, monkeypatch)
        monkeypatch.setattr(
            "sys.argv",
            ["collect.py", "--output-dir", str(tmp_path / "out"), "--report-only"],
        )
        rc = mimir_collect.main()
        assert rc == 0
        second = json.loads((tmp_path / "out" / "summary.json").read_text())
        first_figs = figures_by_id(first)
        second_figs = figures_by_id(second)
        assert (
            second_figs["metrics.active_series"]["value"]
            == first_figs["metrics.active_series"]["value"]
        )
        assert second_figs["metrics.samples_per_sec"]["value"] == pytest.approx(
            first_figs["metrics.samples_per_sec"]["value"], rel=0.01
        )

    def test_auth_failure_produces_gaps(self, mimir_collect, tmp_path, monkeypatch):
        argv = [
            "collect.py",
            "--endpoint", MIMIR_URL,
            "--output-dir", str(tmp_path / "out"),
            "--lookback", "7d",
        ]
        monkeypatch.setattr("sys.argv", argv)
        with respx.mock(base_url=MIMIR_URL) as mock:
            mock.get("/prometheus/api/v1/query").mock(
                return_value=Response(401, json={"error": "Unauthorized"})
            )
            mock.get("/prometheus/api/v1/query_range").mock(
                return_value=Response(401, json={"error": "Unauthorized"})
            )
            mock.get("/api/v1/status/buildinfo").mock(
                return_value=Response(401, json={"error": "Unauthorized"})
            )
            mock.get("/config").mock(
                return_value=Response(401, json={"error": "Unauthorized"})
            )
            mock.get("/api/v1/query").mock(
                return_value=Response(401, json={"error": "Unauthorized"})
            )
            rc = mimir_collect.main()
        assert rc == 0
        summary = json.loads((tmp_path / "out" / "summary.json").read_text())
        figs = figures_by_id(summary)
        for fig_id in [
            "metrics.active_series",
            "metrics.samples_per_sec",
            "mimir.ingestion_rate_peak",
            "mimir.query_rate_avg",
            "mimir.query_rate_peak",
            "mimir.objstore_ops_per_day",
        ]:
            assert figs[fig_id]["status"] == "unavailable", f"{fig_id} should be unavailable"
        assert len(summary["gaps"]) >= 6
        for gap in summary["gaps"]:
            assert gap["reason"] in ("auth_failed", "not_configured"), (
                f"expected auth_failed or not_configured, got {gap['reason']}"
            )

    def test_no_config_retention_gap(self, mimir_collect, tmp_path, monkeypatch):
        argv = [
            "collect.py",
            "--endpoint", MIMIR_URL,
            "--output-dir", str(tmp_path / "out"),
            "--lookback", "7d",
        ]
        monkeypatch.setattr("sys.argv", argv)
        with respx.mock(base_url=MIMIR_URL) as mock:
            _mock_mimir(mock)
            # Override config to return YAML without retention
            mock.get("/config").mock(return_value=Response(
                200,
                text="server:\n  http_listen_port: 8080\n",
                headers={"content-type": "text/yaml"},
            ))
            rc = mimir_collect.main()
        assert rc == 0
        summary = json.loads((tmp_path / "out" / "summary.json").read_text())
        figs = figures_by_id(summary)
        assert figs["mimir.retention_days"]["status"] == "unavailable"
        gap = next(g for g in summary["gaps"] if "mimir.retention_days" in g["figure_ids"])
        assert gap["reason"] == "not_configured"

    def test_endpoint_required_without_report_only(self, mimir_collect, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "sys.argv",
            ["collect.py", "--output-dir", str(tmp_path / "out")],
        )
        rc = mimir_collect.main()
        assert rc == 1

    def test_bucket_size_override(self, mimir_collect, tmp_path, monkeypatch):
        summary = run_collector(
            mimir_collect, tmp_path, monkeypatch,
            extra_args=["--bucket-size-gb", "1000"],
        )
        figs = figures_by_id(summary)
        assert figs["cost.monthly_usd"]["status"] == "estimated"
        assert "bucket-size-gb=1000" in figs["cost.monthly_usd"]["method"]
