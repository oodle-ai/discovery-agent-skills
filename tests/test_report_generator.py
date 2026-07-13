from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture()
def summary():
    return json.loads((FIXTURES / "datadog_summary.json").read_text())


@pytest.fixture()
def context():
    return json.loads((FIXTURES / "context.json").read_text())


class TestFixturesValid:
    def test_summary_fixture_matches_schema(self, summary, summary_schema):
        jsonschema = pytest.importorskip("jsonschema")
        jsonschema.validate(summary, summary_schema)

    def test_context_fixture_matches_schema(self, context, context_schema):
        jsonschema = pytest.importorskip("jsonschema")
        jsonschema.validate(context, context_schema)


class TestBuildReport:
    def test_measured_figures_rendered_verbatim(self, report_gen, summary, context):
        html = report_gen.build_report([summary], context, "Test Report")
        # collector-measured values appear; the generator does not recompute
        assert "231.4 GB/day" in html
        assert "$41.2K" in html  # cost card, humanized by the generator only
        assert "Est. Observability Spend" in html
        # no account-wide cost vocabulary
        assert "Cloud Spend" not in html

    def test_gaps_and_skipped_collectors_visible(self, report_gen, summary, context):
        html = report_gen.build_report([summary], context, "Test Report")
        assert "Coverage &amp; Gaps" in html or "Coverage & Gaps" in html
        assert "permission_denied" in html
        assert "grant usage_read" in html
        assert "cloudwatch" in html  # skipped collector listed
        assert "aws CLI not configured" in html

    def test_spend_breakdown_rendered(self, report_gen, summary, context):
        html = report_gen.build_report([summary], context, "Test Report")
        # per-product rows under the datadog spend row
        assert "infra_host" in html
        assert "logs_indexed" in html
        assert "$28.0K" in html
        assert "$9.2K" in html

    def test_user_reported_marked_unverified(self, report_gen, summary, context):
        html = report_gen.build_report([summary], context, "Test Report")
        assert "Datadog contract" in html
        assert "$480K/yr committed" in html
        assert "reported" in html

    def test_provenance_appendix_links_evidence(self, report_gen, summary, context):
        html = report_gen.build_report([summary], context, "Test Report")
        assert "Provenance Appendix" in html
        assert "evidence/usage_hourly_logs.json" in html
        assert "GET /api/v2/usage/hourly_usage" in html

    def test_narrative_is_escaped(self, report_gen, summary, context):
        context["narrative"]["overview"] = '<script>alert("xss")</script>'
        html = report_gen.build_report([summary], context, "Test Report")
        assert "<script>alert" not in html
        assert "&lt;script&gt;" in html

    def test_metrics_cards_show_total_and_custom(self, report_gen, summary, context):
        html = report_gen.build_report([summary], context, "Test Report")
        assert "Active Metrics" in html
        assert "1.8K metrics" in html  # metrics.total_count = 1842
        assert "Custom Metrics" in html
        assert "18.2K metrics" in html  # metrics.custom_metrics_count = 18234

    def test_compute_card_prefers_datadog_hosts(self, report_gen, summary, context):
        html = report_gen.build_report([summary], context, "Test Report")
        # hosts.count (142, measured by the Datadog hosts API) outranks the
        # context-reported node count (30)
        assert "Hosts (monitored)" in html
        assert "142 hosts" in html

    def test_degraded_mode_no_summaries(self, report_gen, context):
        html = report_gen.build_report([], context, "Test Report")
        assert "Acme Corp (fictional sample)" in html
        assert "cloudwatch" in html  # skipped collectors still surface
        # cards render placeholders, not invented numbers
        assert "Log Volume" in html

    def test_unavailable_figures_never_show_a_number(self, report_gen, summary, context):
        html = report_gen.build_report([summary], context, "Test Report")
        # the unavailable traces figure must not leak a fabricated value
        assert "Trace Volume" in html or "traces" in html
        fig = next(f for f in summary["figures"] if f["id"] == "traces.ingest_gb_per_day")
        assert fig["status"] == "unavailable"


class TestMonthlyUsageSection:
    def _monthly_summary(self):
        return {
            "schema_version": "1.0",
            "collector": "datadog_monthly",
            "collector_version": "1.0.0",
            "run": {
                "started_at": "2026-07-01T00:00:00Z",
                "finished_at": "2026-07-01T00:01:00Z",
                "target": "https://api.datadoghq.com",
                "lookback": "6 full calendar months (2026-01..2026-06)",
            },
            "figures": [],
            "inventory": {
                "monthly_usage_months": ["May 2026", "Jun 2026"],
                "monthly_usage_by_sku": [
                    {"product_family": "logs", "usage_type": "ingested_events_bytes",
                     "unit": "GB", "aggregation": "sum", "May 2026": "3.000", "Jun 2026": "2.000"},
                    {"product_family": "timeseries", "usage_type": "num_custom_timeseries",
                     "unit": "custom metrics (avg)", "aggregation": "avg",
                     "May 2026": "1500.0", "Jun 2026": ""},
                ],
            },
            "gaps": [],
        }

    def test_section_renders_matrix(self, report_gen, context):
        html = report_gen.build_report([self._monthly_summary()], context, "Test Report")
        assert "Monthly Usage by SKU" in html
        assert "ingested_events_bytes" in html
        assert "May 2026" in html and "Jun 2026" in html
        assert "3.000" in html  # value shown verbatim
        assert "num_custom_timeseries" in html

    def test_matrix_not_duplicated_in_deep_dive(self, report_gen, context):
        html = report_gen.build_report([self._monthly_summary()], context, "Test Report")
        # the SKU matrix appears once (the dedicated section), not again as a
        # generic inventory table in the deep-dive
        assert html.count("num_custom_timeseries") == 1

    def test_section_absent_when_no_monthly_inventory(self, report_gen, summary, context):
        html = report_gen.build_report([summary], context, "Test Report")
        assert "Monthly Usage by SKU" not in html

    def test_per_collector_note_rendered(self, report_gen, context):
        s = self._monthly_summary()
        s["inventory"]["monthly_usage_note"] = "Retention caveat: only ~6 weeks retained."
        html = report_gen.build_report([s], context, "Test Report")
        assert "Retention caveat: only ~6 weeks retained." in html


class TestMainCli:
    def test_end_to_end_strict(self, report_gen, tmp_path, monkeypatch, capsys):
        out = tmp_path / "report.html"
        monkeypatch.setattr(
            "sys.argv",
            [
                "generate_report.py",
                "--summaries",
                str(FIXTURES / "datadog_summary.json"),
                "--context",
                str(FIXTURES / "context.json"),
                "--out",
                str(out),
                "--strict",
            ],
        )
        assert report_gen.main() == 0
        assert out.exists()
        printed = capsys.readouterr().out
        assert "coverage summary" in printed
        assert "datadog: 8/9 figures" in printed
        assert "permission_denied" in printed
