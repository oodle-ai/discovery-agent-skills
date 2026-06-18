from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import fixtures_elasticsearch as fx
import pytest

ES_BASE = "https://es:9200"


def _merged_settings():
    """Merge creation_date and slowlog settings into one response."""
    base = fx.index_settings()
    for idx_name, body in fx.slowlog_settings().items():
        if idx_name in base:
            base[idx_name]["settings"]["index"]["search"] = (
                body["settings"]["index"]["search"]
            )
        else:
            base[idx_name] = body
    return base


def mock_es_direct(respx_mock):
    """Wire up respx routes for a direct ES connection."""
    respx_mock.get(f"{ES_BASE}/_cluster/health").respond(json=fx.CLUSTER_HEALTH)
    respx_mock.get(f"{ES_BASE}/_cluster/stats").respond(json=fx.CLUSTER_STATS)
    respx_mock.get(f"{ES_BASE}/_nodes/stats/fs,os,process,jvm,indices").respond(
        json=fx.NODES_STATS
    )
    respx_mock.get(f"{ES_BASE}/_nodes/os,jvm").respond(json=fx.NODES_INFO)
    respx_mock.get(f"{ES_BASE}/_cat/indices").respond(json=fx.cat_indices())
    respx_mock.get(f"{ES_BASE}/_all/_settings").respond(json=_merged_settings())
    respx_mock.get(f"{ES_BASE}/_stats").respond(json=fx.cluster_index_stats())
    respx_mock.get(f"{ES_BASE}/_ilm/policy").respond(json=fx.ILM_POLICIES)
    respx_mock.get(f"{ES_BASE}/_snapshot/_all").respond(json=fx.SNAPSHOT_REPOS)
    respx_mock.get(f"{ES_BASE}/_snapshot/s3-backups/_all").respond(
        json=fx.SNAPSHOT_DETAILS[0]["snapshots"]
    )
    respx_mock.get(f"{ES_BASE}/_nodes/usage").respond(json=fx.NODES_USAGE)
    respx_mock.get(f"{ES_BASE}/_cat/indices/.monitoring-es-*").respond(
        json=fx.MONITORING_INDICES
    )


def run_collector(es_collect, tmp_path, monkeypatch, extra_args=None):
    monkeypatch.setenv("ES_URL", ES_BASE)
    argv = [
        "collect.py",
        "--output-dir",
        str(tmp_path / "out"),
        "--lookback",
        f"{fx.LOOKBACK_DAYS}d",
    ]
    argv += extra_args or []
    monkeypatch.setattr("sys.argv", argv)
    rc = es_collect.main()
    assert rc == 0
    return json.loads((tmp_path / "out" / "summary.json").read_text())


def figures_by_id(summary):
    return {f["id"]: f for f in summary["figures"]}


class TestElasticsearchCollector:
    def test_full_run_known_ground_truth(
        self, es_collect, tmp_path, monkeypatch, respx_mock, summary_schema
    ):
        mock_es_direct(respx_mock)
        summary = run_collector(es_collect, tmp_path, monkeypatch)

        jsonschema = pytest.importorskip("jsonschema")
        jsonschema.validate(
            {k: v for k, v in summary.items() if not k.startswith("_")}, summary_schema
        )

        figs = figures_by_id(summary)

        # total docs = date indices + APM indices + system index
        assert figs["elasticsearch.total_docs"]["value"] == fx.GRAND_TOTAL_DOCS

        # store sizes
        assert figs["elasticsearch.total_store_size_gb"]["value"] == pytest.approx(
            fx.GRAND_TOTAL_STORE / 1e9, rel=0.01
        )
        assert figs["elasticsearch.primary_store_size_gb"]["value"] == pytest.approx(
            fx.GRAND_TOTAL_PRI / 1e9, rel=0.01
        )

        # data nodes
        assert figs["hosts.count"]["value"] == fx.NUM_DATA_NODES

        # shards
        assert figs["elasticsearch.total_shards"]["value"] == fx.ACTIVE_SHARDS

        # daily ingestion: 10 indices x 2GB primary / 5 days = 4.0 GB/day
        assert figs["logs.ingest_gb_per_day"]["value"] == pytest.approx(
            fx.INGEST_GB_PER_DAY, rel=0.01
        )
        assert figs["logs.ingest_gb_per_day"]["status"] == "ok"

        # APM traces: 5 indices x 500MB / 5 days = 0.5 GB/day
        assert figs["traces.ingest_gb_per_day"]["value"] == pytest.approx(
            fx.APM_INGEST_GB_PER_DAY, rel=0.01
        )
        assert figs["traces.ingest_gb_per_day"]["status"] == "ok"
        assert figs["traces.spans_per_day"]["value"] == pytest.approx(
            fx.APM_SPANS_PER_DAY, rel=0.01
        )

        # no gaps in a clean run
        assert summary["gaps"] == []

        # environment
        assert summary["environment"]["detected_backend"] == "elasticsearch"
        assert summary["environment"]["version"] == fx.ES_VERSION
        assert summary["environment"]["cluster_name"] == fx.CLUSTER_NAME

        # provenance on every collected figure
        for fig in summary["figures"]:
            assert fig["method"], fig["id"]
            assert fig["source_api"], fig["id"]
            assert fig["evidence_files"], fig["id"]

    def test_inventory_populated(self, es_collect, tmp_path, monkeypatch, respx_mock):
        mock_es_direct(respx_mock)
        summary = run_collector(es_collect, tmp_path, monkeypatch)
        inv = summary["inventory"]

        assert inv["cluster_name"] == fx.CLUSTER_NAME
        assert inv["cluster_status"] == "green"
        assert inv["number_of_nodes"] == fx.NUM_NODES
        assert inv["number_of_data_nodes"] == fx.NUM_DATA_NODES
        assert len(inv["node_fleet"]) == fx.NUM_NODES
        assert inv["node_fleet"][0]["cpu_count"] == 8
        assert inv["node_fleet"][0]["heap_used_percent"] == pytest.approx(50.0)
        assert len(inv["ilm_policies"]) == 2
        assert inv["search_stats"]["query_total"] == 150000
        assert inv["search_stats"]["avg_query_latency_ms"] == pytest.approx(5.0)
        assert len(inv["rest_api_usage"]) > 0
        assert inv["snapshot_repositories"] == ["s3-backups"]
        assert inv["apm_traces"]["index_count"] == fx.NUM_APM_INDICES
        assert inv["apm_traces"]["total_spans"] == fx.APM_TOTAL_DOCS

        # search hotspots (per-index-pattern query stats from /_stats)
        hotspots = inv["search_hotspots"]
        assert len(hotspots) > 0
        patterns = {h["index_pattern"] for h in hotspots}
        assert "logs-app-*" in patterns
        assert "metrics-infra-*" in patterns
        assert all(h["query_total"] > 0 for h in hotspots)
        assert all(h["avg_latency_ms"] is not None for h in hotspots)

        # slow log configuration
        slowlog = inv["slowlog_config"]
        assert len(slowlog) == fx.DAYS_WITH_DATA
        assert all(c["query_warn"] == "10s" for c in slowlog)
        assert all(c["fetch_warn"] == "1s" for c in slowlog)

        # monitoring indices (.monitoring-es-*)
        mon = inv["monitoring_indices"]
        assert len(mon) == 2
        assert mon[0]["index"].startswith(".monitoring-es-")
        assert mon[0]["docs_count"] == 500000
        assert mon[0]["store_size_bytes"] == 250000000

    def test_index_patterns_grouped(self, es_collect, tmp_path, monkeypatch, respx_mock):
        mock_es_direct(respx_mock)
        summary = run_collector(es_collect, tmp_path, monkeypatch)
        patterns = summary["inventory"]["index_patterns"]
        pattern_names = {p["pattern"] for p in patterns}
        # date-based indices should be grouped by pattern
        assert "logs-app-*" in pattern_names or any("logs" in p for p in pattern_names)
        # system index should have its own pattern
        assert any(".kibana" in p["pattern"] for p in patterns)

    def test_daily_ingestion_breakdown(self, es_collect, tmp_path, monkeypatch, respx_mock):
        mock_es_direct(respx_mock)
        summary = run_collector(es_collect, tmp_path, monkeypatch)
        daily = summary["inventory"]["daily_ingestion"]
        assert len(daily) == fx.DAYS_WITH_DATA
        total_gb = sum(d["primary_gb"] for d in daily)
        assert total_gb == pytest.approx(fx.TOTAL_PRI_BYTES / 1e9, rel=0.01)

    def test_cluster_health_403_marks_shards_unavailable(
        self, es_collect, tmp_path, monkeypatch, respx_mock
    ):
        mock_es_direct(respx_mock)
        # override cluster_health to 403
        respx_mock.get(f"{ES_BASE}/_cluster/health").respond(
            status_code=403, json={"error": "forbidden"}
        )
        summary = run_collector(es_collect, tmp_path, monkeypatch)
        figs = figures_by_id(summary)
        assert figs["elasticsearch.total_shards"]["status"] == "unavailable"
        # hosts.count should fall back to nodes_info
        assert figs["hosts.count"]["value"] == fx.NUM_DATA_NODES

    def test_cat_indices_403_marks_multiple_unavailable(
        self, es_collect, tmp_path, monkeypatch, respx_mock
    ):
        mock_es_direct(respx_mock)
        respx_mock.get(f"{ES_BASE}/_cat/indices").respond(
            status_code=403, json={"error": "forbidden"}
        )
        summary = run_collector(es_collect, tmp_path, monkeypatch)
        figs = figures_by_id(summary)
        assert figs["elasticsearch.total_docs"]["status"] == "unavailable"
        assert figs["elasticsearch.total_store_size_gb"]["status"] == "unavailable"
        assert figs["elasticsearch.primary_store_size_gb"]["status"] == "unavailable"
        assert figs["logs.ingest_gb_per_day"]["status"] == "unavailable"
        assert figs["traces.ingest_gb_per_day"]["status"] == "unavailable"
        assert figs["traces.spans_per_day"]["status"] == "unavailable"

    def test_skip_snapshots(self, es_collect, tmp_path, monkeypatch, respx_mock):
        mock_es_direct(respx_mock)
        summary = run_collector(
            es_collect, tmp_path, monkeypatch, extra_args=["--skip-snapshots"]
        )
        inv = summary["inventory"]
        assert "snapshot_repositories" not in inv

    def test_credentials_redacted_in_evidence(
        self, es_collect, tmp_path, monkeypatch, respx_mock
    ):
        mock_es_direct(respx_mock)
        monkeypatch.setenv("ES_USER", "elastic")
        monkeypatch.setenv("ES_PASSWORD", "supersecret")
        run_collector(es_collect, tmp_path, monkeypatch)
        for f in (tmp_path / "out").rglob("*.json"):
            content = f.read_text()
            assert "supersecret" not in content, f

    def test_report_only_recomputes_from_evidence(
        self, es_collect, tmp_path, monkeypatch, respx_mock
    ):
        mock_es_direct(respx_mock)
        first = run_collector(es_collect, tmp_path, monkeypatch)
        # second run: no network (respx with no routes would fail)
        monkeypatch.setattr(
            "sys.argv",
            ["collect.py", "--output-dir", str(tmp_path / "out"), "--report-only"],
        )
        rc = es_collect.main()
        assert rc == 0
        second = json.loads((tmp_path / "out" / "summary.json").read_text())
        figs1 = figures_by_id(first)
        figs2 = figures_by_id(second)
        assert (
            figs2["elasticsearch.total_docs"]["value"]
            == figs1["elasticsearch.total_docs"]["value"]
        )
        assert figs2["elasticsearch.total_store_size_gb"]["value"] == pytest.approx(
            figs1["elasticsearch.total_store_size_gb"]["value"]
        )

    def test_evidence_files_written(self, es_collect, tmp_path, monkeypatch, respx_mock):
        mock_es_direct(respx_mock)
        run_collector(es_collect, tmp_path, monkeypatch)
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
            "ilm_policies.json",
            "snapshot_repos.json",
            "nodes_usage.json",
            "slowlog_settings.json",
            "monitoring_indices.json",
        ]
        for name in expected_files:
            assert (evidence_dir / name).exists(), f"missing {name}"
        manifest = json.loads((tmp_path / "out" / "manifest.json").read_text())
        assert len(manifest) >= len(expected_files)

    def test_no_date_indices_marks_ingestion_unavailable(
        self, es_collect, tmp_path, monkeypatch, respx_mock
    ):
        """When all indices are system indices with no date in name."""
        mock_es_direct(respx_mock)
        # override cat_indices with only system index
        respx_mock.get(f"{ES_BASE}/_cat/indices").respond(
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
        # override settings to have no date-parseable creation_date in lookback window
        old_ts = int((datetime.now(UTC) - timedelta(days=365)).timestamp() * 1000)
        respx_mock.get(f"{ES_BASE}/_all/_settings").respond(
            json={".internal_something": {"settings": {"index": {"creation_date": str(old_ts)}}}}
        )
        summary = run_collector(es_collect, tmp_path, monkeypatch)
        figs = figures_by_id(summary)
        assert figs["logs.ingest_gb_per_day"]["status"] == "unavailable"
        assert any(
            "logs.ingest_gb_per_day" in g["figure_ids"] for g in summary["gaps"]
        )
