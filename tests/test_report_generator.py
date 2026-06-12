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
        assert "datadog: 5/6 figures" in printed
        assert "permission_denied" in printed
