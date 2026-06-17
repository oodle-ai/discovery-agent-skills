from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import fixtures_opensearch as fx
import pytest

OS_BASE = "https://os:9200"


def mock_os_direct(respx_mock):
    """Wire up respx routes for a direct OpenSearch connection."""
    respx_mock.get(f"{OS_BASE}/_cluster/health").respond(json=fx.CLUSTER_HEALTH)
    respx_mock.get(f"{OS_BASE}/_cluster/stats").respond(json=fx.CLUSTER_STATS)
    respx_mock.get(f"{OS_BASE}/_nodes/stats/fs,os,process,jvm,indices").respond(
        json=fx.NODES_STATS
    )
    respx_mock.get(f"{OS_BASE}/_nodes/os,jvm").respond(json=fx.NODES_INFO)
    respx_mock.get(f"{OS_BASE}/_cat/indices").respond(json=fx.cat_indices())
    respx_mock.get(f"{OS_BASE}/_all/_settings").respond(json=fx.index_settings())
    respx_mock.get(f"{OS_BASE}/_stats").respond(json=fx.CLUSTER_INDEX_STATS)
    respx_mock.get(f"{OS_BASE}/_snapshot/_all").respond(json=fx.SNAPSHOT_REPOS)
    respx_mock.get(f"{OS_BASE}/_snapshot/s3-backups/_all").respond(
        json=fx.SNAPSHOT_DETAILS[0]["snapshots"]
    )
    respx_mock.get(f"{OS_BASE}/_nodes/usage").respond(json=fx.NODES_USAGE)
    respx_mock.get(f"{OS_BASE}/_cat/plugins").respond(json=fx.CAT_PLUGINS)
    respx_mock.get(f"{OS_BASE}/_plugins/_ism/policies").respond(json=fx.ISM_POLICIES)


def run_collector(os_collect, tmp_path, monkeypatch, extra_args=None):
    monkeypatch.setenv("OS_URL", OS_BASE)
    argv = [
        "collect.py",
        "--output-dir",
        str(tmp_path / "out"),
        "--lookback",
        f"{fx.LOOKBACK_DAYS}d",
    ]
    argv += extra_args or []
    monkeypatch.setattr("sys.argv", argv)
    rc = os_collect.main()
    assert rc == 0
    return json.loads((tmp_path / "out" / "summary.json").read_text())


def figures_by_id(summary):
    return {f["id"]: f for f in summary["figures"]}


class TestOpenSearchCollector:
    def test_full_run_known_ground_truth(
        self, os_collect, tmp_path, monkeypatch, respx_mock, summary_schema
    ):
        mock_os_direct(respx_mock)
        summary = run_collector(os_collect, tmp_path, monkeypatch)

        jsonschema = pytest.importorskip("jsonschema")
        jsonschema.validate(
            {k: v for k, v in summary.items() if not k.startswith("_")}, summary_schema
        )

        figs = figures_by_id(summary)

        # total docs = date indices + OTel trace indices + system index
        assert figs["opensearch.total_docs"]["value"] == fx.GRAND_TOTAL_DOCS

        # store sizes
        assert figs["opensearch.total_store_size_gb"]["value"] == pytest.approx(
            fx.GRAND_TOTAL_STORE / 1e9, rel=0.01
        )
        assert figs["opensearch.primary_store_size_gb"]["value"] == pytest.approx(
            fx.GRAND_TOTAL_PRI / 1e9, rel=0.01
        )

        # data nodes
        assert figs["hosts.count"]["value"] == fx.NUM_DATA_NODES

        # shards
        assert figs["opensearch.total_shards"]["value"] == fx.ACTIVE_SHARDS

        # daily ingestion: 10 indices x 2GB primary / 5 days = 4.0 GB/day
        assert figs["logs.ingest_gb_per_day"]["value"] == pytest.approx(
            fx.INGEST_GB_PER_DAY, rel=0.01
        )
        assert figs["logs.ingest_gb_per_day"]["status"] == "ok"

        # OTel traces: 5 indices x 500MB / 5 days = 0.5 GB/day
        assert figs["traces.ingest_gb_per_day"]["value"] == pytest.approx(
            fx.TRACE_INGEST_GB_PER_DAY, rel=0.01
        )
        assert figs["traces.ingest_gb_per_day"]["status"] == "ok"
        assert figs["traces.spans_per_day"]["value"] == pytest.approx(
            fx.TRACE_SPANS_PER_DAY, rel=0.01
        )

        # no gaps in a clean run
        assert summary["gaps"] == []

        # environment
        assert summary["environment"]["detected_backend"] == "opensearch"
        assert summary["environment"]["version"] == fx.OS_VERSION
        assert summary["environment"]["cluster_name"] == fx.CLUSTER_NAME

        # provenance on every collected figure
        for fig in summary["figures"]:
            assert fig["method"], fig["id"]
            assert fig["source_api"], fig["id"]
            assert fig["evidence_files"], fig["id"]

    def test_inventory_populated(self, os_collect, tmp_path, monkeypatch, respx_mock):
        mock_os_direct(respx_mock)
        summary = run_collector(os_collect, tmp_path, monkeypatch)
        inv = summary["inventory"]

        assert inv["cluster_name"] == fx.CLUSTER_NAME
        assert inv["cluster_status"] == "green"
        assert inv["number_of_nodes"] == fx.NUM_NODES
        assert inv["number_of_data_nodes"] == fx.NUM_DATA_NODES
        assert len(inv["node_fleet"]) == fx.NUM_NODES
        assert inv["node_fleet"][0]["cpu_count"] == 8
        assert inv["node_fleet"][0]["heap_used_percent"] == pytest.approx(50.0)
        assert len(inv["ism_policies"]) == 2
        assert inv["search_stats"]["query_total"] == 150000
        assert inv["search_stats"]["avg_query_latency_ms"] == pytest.approx(5.0)
        assert len(inv["rest_api_usage"]) > 0
        assert inv["snapshot_repositories"] == ["s3-backups"]
        assert inv["otel_traces"]["index_count"] == fx.NUM_TRACE_INDICES
        assert inv["otel_traces"]["total_spans"] == fx.TRACE_TOTAL_DOCS

    def test_index_patterns_grouped(self, os_collect, tmp_path, monkeypatch, respx_mock):
        mock_os_direct(respx_mock)
        summary = run_collector(os_collect, tmp_path, monkeypatch)
        patterns = summary["inventory"]["index_patterns"]
        pattern_names = {p["pattern"] for p in patterns}
        assert "logs-app-*" in pattern_names or any("logs" in p for p in pattern_names)
        assert any(".opensearch" in p["pattern"] for p in patterns)

    def test_daily_ingestion_breakdown(self, os_collect, tmp_path, monkeypatch, respx_mock):
        mock_os_direct(respx_mock)
        summary = run_collector(os_collect, tmp_path, monkeypatch)
        daily = summary["inventory"]["daily_ingestion"]
        assert len(daily) == fx.DAYS_WITH_DATA
        total_gb = sum(d["primary_gb"] for d in daily)
        assert total_gb == pytest.approx(fx.TOTAL_PRI_BYTES / 1e9, rel=0.01)

    def test_cluster_health_403_marks_shards_unavailable(
        self, os_collect, tmp_path, monkeypatch, respx_mock
    ):
        mock_os_direct(respx_mock)
        respx_mock.get(f"{OS_BASE}/_cluster/health").respond(
            status_code=403, json={"error": "forbidden"}
        )
        summary = run_collector(os_collect, tmp_path, monkeypatch)
        figs = figures_by_id(summary)
        assert figs["opensearch.total_shards"]["status"] == "unavailable"
        # hosts.count should fall back to nodes_info
        assert figs["hosts.count"]["value"] == fx.NUM_DATA_NODES

    def test_cat_indices_403_marks_multiple_unavailable(
        self, os_collect, tmp_path, monkeypatch, respx_mock
    ):
        mock_os_direct(respx_mock)
        respx_mock.get(f"{OS_BASE}/_cat/indices").respond(
            status_code=403, json={"error": "forbidden"}
        )
        summary = run_collector(os_collect, tmp_path, monkeypatch)
        figs = figures_by_id(summary)
        assert figs["opensearch.total_docs"]["status"] == "unavailable"
        assert figs["opensearch.total_store_size_gb"]["status"] == "unavailable"
        assert figs["opensearch.primary_store_size_gb"]["status"] == "unavailable"
        assert figs["logs.ingest_gb_per_day"]["status"] == "unavailable"
        assert figs["traces.ingest_gb_per_day"]["status"] == "unavailable"
        assert figs["traces.spans_per_day"]["status"] == "unavailable"

    def test_skip_snapshots(self, os_collect, tmp_path, monkeypatch, respx_mock):
        mock_os_direct(respx_mock)
        summary = run_collector(
            os_collect, tmp_path, monkeypatch, extra_args=["--skip-snapshots"]
        )
        inv = summary["inventory"]
        assert "snapshot_repositories" not in inv

    def test_credentials_redacted_in_evidence(
        self, os_collect, tmp_path, monkeypatch, respx_mock
    ):
        mock_os_direct(respx_mock)
        monkeypatch.setenv("OS_USER", "admin")
        monkeypatch.setenv("OS_PASSWORD", "supersecret")
        run_collector(os_collect, tmp_path, monkeypatch)
        for f in (tmp_path / "out").rglob("*.json"):
            content = f.read_text()
            assert "supersecret" not in content, f

    def test_report_only_recomputes_from_evidence(
        self, os_collect, tmp_path, monkeypatch, respx_mock
    ):
        mock_os_direct(respx_mock)
        first = run_collector(os_collect, tmp_path, monkeypatch)
        monkeypatch.setattr(
            "sys.argv",
            ["collect.py", "--output-dir", str(tmp_path / "out"), "--report-only"],
        )
        rc = os_collect.main()
        assert rc == 0
        second = json.loads((tmp_path / "out" / "summary.json").read_text())
        figs1 = figures_by_id(first)
        figs2 = figures_by_id(second)
        assert (
            figs2["opensearch.total_docs"]["value"]
            == figs1["opensearch.total_docs"]["value"]
        )
        assert figs2["opensearch.total_store_size_gb"]["value"] == pytest.approx(
            figs1["opensearch.total_store_size_gb"]["value"]
        )

    def test_evidence_files_written(self, os_collect, tmp_path, monkeypatch, respx_mock):
        mock_os_direct(respx_mock)
        run_collector(os_collect, tmp_path, monkeypatch)
        evidence_dir = tmp_path / "out" / "evidence"
        assert evidence_dir.exists()
        expected_files = [
            "cluster_health.json",
            "cluster_stats.json",
            "nodes_stats.json",
            "nodes_info.json",
            "cat_indices.json",
            "index_settings.json",
            "cluster_index_stats.json",
            "snapshot_repos.json",
            "nodes_usage.json",
            "cat_plugins.json",
            "ism_policies.json",
        ]
        for name in expected_files:
            assert (evidence_dir / name).exists(), f"missing {name}"
        manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
        assert len(manifest) >= len(expected_files)

    def test_no_date_indices_marks_ingestion_unavailable(
        self, os_collect, tmp_path, monkeypatch, respx_mock
    ):
        mock_os_direct(respx_mock)
        respx_mock.get(f"{OS_BASE}/_cat/indices").respond(
            json=[{
                "index": ".internal_something",
                "health": "green",
                "status": "open",
                "pri": "1",
                "rep": "0",
                "docs.count": "100",
                "docs.deleted": "0",
                "store.size": "1000000",
                "pri.store.size": "1000000",
            }]
        )
        old_ts = int((datetime.now(UTC) - timedelta(days=365)).timestamp() * 1000)
        respx_mock.get(f"{OS_BASE}/_all/_settings").respond(
            json={".internal_something": {"settings": {"index": {"creation_date": str(old_ts)}}}}
        )
        summary = run_collector(os_collect, tmp_path, monkeypatch)
        figs = figures_by_id(summary)
        assert figs["logs.ingest_gb_per_day"]["status"] == "unavailable"
        assert any(
            "logs.ingest_gb_per_day" in g["figure_ids"] for g in summary["gaps"]
        )

    def test_aoss_mode_marks_cluster_figures_unavailable(
        self, os_collect, tmp_path, monkeypatch, respx_mock
    ):
        """AOSS mode: only cat_indices and index_settings are fetched;
        hosts.count and total_shards should be unavailable with version_unsupported."""
        respx_mock.get(f"{OS_BASE}/_cat/indices").respond(json=fx.AOSS_CAT_INDICES)
        old_ts = int((datetime.now(UTC) - timedelta(days=1)).timestamp() * 1000)
        respx_mock.get(f"{OS_BASE}/_all/_settings").respond(
            json={
                "logs-aoss-2026.06.16": {
                    "settings": {"index": {"creation_date": str(old_ts)}}
                }
            },
        )
        monkeypatch.setenv("OS_URL", OS_BASE)
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
        argv = [
            "collect.py",
            "--output-dir",
            str(tmp_path / "out"),
            "--lookback",
            f"{fx.LOOKBACK_DAYS}d",
            "--aws-sigv4",
            "--aws-service",
            "aoss",
        ]
        monkeypatch.setattr("sys.argv", argv)
        rc = os_collect.main()
        assert rc == 0
        summary = json.loads((tmp_path / "out" / "summary.json").read_text())
        figs = figures_by_id(summary)

        assert figs["hosts.count"]["status"] == "unavailable"
        assert "version_unsupported" in figs["hosts.count"]["unavailable_reason"]
        assert figs["opensearch.total_shards"]["status"] == "unavailable"
        assert "version_unsupported" in figs["opensearch.total_shards"]["unavailable_reason"]

        assert summary["environment"]["detected_backend"] == "opensearch_serverless"

        # doc/store figures should still be present from cat_indices
        assert figs["opensearch.total_docs"]["status"] == "ok"
        assert figs["opensearch.total_docs"]["value"] == 500000.0

    def test_ism_policies_in_inventory(self, os_collect, tmp_path, monkeypatch, respx_mock):
        mock_os_direct(respx_mock)
        summary = run_collector(os_collect, tmp_path, monkeypatch)
        inv = summary["inventory"]
        assert "ism_policies" in inv
        policies = inv["ism_policies"]
        assert len(policies) == 2
        names = {p["name"] for p in policies}
        assert "logs-policy" in names
        assert "metrics-policy" in names
        logs_policy = next(p for p in policies if p["name"] == "logs-policy")
        assert "hot" in logs_policy["states"]
        assert logs_policy.get("has_delete") is True

    def test_ism_fallback_to_legacy_endpoint(
        self, os_collect, tmp_path, monkeypatch, respx_mock
    ):
        """When _plugins endpoint fails, ISM should fall back to _opendistro."""
        mock_os_direct(respx_mock)
        respx_mock.get(f"{OS_BASE}/_plugins/_ism/policies").respond(
            status_code=404, json={"error": "not found"}
        )
        respx_mock.get(f"{OS_BASE}/_opendistro/_ism/policies").respond(
            json=fx.ISM_POLICIES
        )
        summary = run_collector(os_collect, tmp_path, monkeypatch)
        inv = summary["inventory"]
        assert "ism_policies" in inv
        assert len(inv["ism_policies"]) == 2

    def test_plugins_in_inventory(self, os_collect, tmp_path, monkeypatch, respx_mock):
        mock_os_direct(respx_mock)
        summary = run_collector(os_collect, tmp_path, monkeypatch)
        inv = summary["inventory"]
        assert "plugins" in inv
        plugin_names = {p["component"] for p in inv["plugins"]}
        assert "opensearch-security" in plugin_names
        assert "opensearch-index-management" in plugin_names
        # should be deduplicated (only unique component+version pairs)
        assert len(inv["plugins"]) == 2

    def test_cs_automated_snapshot_repos_skipped(
        self, os_collect, tmp_path, monkeypatch, respx_mock
    ):
        mock_os_direct(respx_mock)
        respx_mock.get(f"{OS_BASE}/_snapshot/_all").respond(
            json={
                "cs-automated-backup": {"type": "s3", "settings": {}},
                "manual-backups": {"type": "s3", "settings": {}},
            }
        )
        respx_mock.get(f"{OS_BASE}/_snapshot/manual-backups/_all").respond(
            json={"snapshots": [{"snapshot": "snap-1", "state": "SUCCESS"}]}
        )
        summary = run_collector(os_collect, tmp_path, monkeypatch)
        inv = summary["inventory"]
        assert "cs-automated-backup" in inv["snapshot_repositories"]
        assert "manual-backups" in inv["snapshot_repositories"]
        # only manual-backups should have snapshot details
        if "snapshots" in inv:
            repos_with_details = {s["repository"] for s in inv["snapshots"]}
            assert "cs-automated-backup" not in repos_with_details
