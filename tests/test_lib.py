from __future__ import annotations

import json

import pytest
from lib.redact import REDACTED, redact
from lib.summary import ExpectedFigure, Figure, SummaryWriter, validate_summary


class TestRedact:
    def test_sensitive_keys_scrubbed(self):
        doc = {
            "password": "hunter2",
            "api_key": "abc123",
            "DD-API-KEY": "xyz",
            "access_key_id": "AKIA...",
            "s3_secret_access_key": "deadbeef",
            "Authorization": "Bearer tok",
            "normal": "kept",
        }
        out = redact(doc)
        assert out["normal"] == "kept"
        for k in doc:
            if k != "normal":
                assert out[k] == REDACTED, k

    def test_nested_and_lists(self):
        doc = {"storage": {"s3": [{"secret_key": "x", "bucket": "logs"}]}}
        out = redact(doc)
        assert out["storage"]["s3"][0]["secret_key"] == REDACTED
        assert out["storage"]["s3"][0]["bucket"] == "logs"

    def test_url_userinfo(self):
        out = redact({"endpoint": "https://admin:s3cret@mimir.internal:8080/prometheus"})
        assert "s3cret" not in out["endpoint"]
        assert "mimir.internal" in out["endpoint"]

    def test_inline_config_string(self):
        s = "storage:\n  access_key_id: AKIAXXXX\n  password = topsecret\n  region: us-east-1"
        out = redact(s)
        assert "AKIAXXXX" not in out
        assert "topsecret" not in out
        assert "us-east-1" in out


def make_writer(expected=None):
    return SummaryWriter(
        collector="testtool",
        collector_version="0.0.1",
        expected=expected
        if expected is not None
        else [ExpectedFigure("metrics.active_series", "Series", "series", "metrics")],
        target="https://example.test",
        lookback="7d",
    )


class TestSummaryWriter:
    def test_missing_expected_becomes_unavailable_gap(self, capsys):
        w = make_writer()
        doc = w.to_dict()
        figs = {f["id"]: f for f in doc["figures"]}
        assert figs["metrics.active_series"]["status"] == "unavailable"
        assert doc["gaps"][0]["reason"] == "not_collected"
        assert "metrics.active_series" in doc["gaps"][0]["figure_ids"]

    def test_ok_figure_requires_method_and_value(self):
        w = make_writer()
        with pytest.raises(ValueError, match="method"):
            w.add_figure(
                Figure(id="metrics.active_series", label="S", value=1.0, unit="x", status="ok")
            )
        with pytest.raises(ValueError, match="value"):
            w.add_figure(
                Figure(
                    id="metrics.active_series",
                    label="S",
                    value=None,
                    unit="x",
                    status="ok",
                    method="m",
                )
            )

    def test_unavailable_requires_reason(self):
        w = make_writer()
        with pytest.raises(ValueError, match="unavailable_reason"):
            w.add_figure(
                Figure(
                    id="metrics.active_series",
                    label="S",
                    value=None,
                    unit="x",
                    status="unavailable",
                )
            )

    def test_invalid_gap_reason_rejected(self):
        w = make_writer()
        with pytest.raises(ValueError, match="gap reason"):
            w.add_gap("metrics", ["metrics.active_series"], "because", "detail")

    def test_write_validates_and_emits(self, tmp_path):
        w = make_writer()
        w.add_figure(
            Figure(
                id="metrics.active_series",
                label="Series",
                value=100.0,
                unit="series",
                status="ok",
                method="instant query",
                source_api="GET /q",
            )
        )
        path = w.write(tmp_path)
        doc = json.loads(path.read_text())
        validate_summary(doc)
        assert doc["figures"][0]["value"] == 100.0
        assert doc["gaps"] == []

    def test_summary_matches_jsonschema(self, tmp_path, summary_schema):
        jsonschema = pytest.importorskip("jsonschema")
        w = make_writer()
        w.mark_unavailable("metrics.active_series", "permission_denied", "403 from API")
        path = w.write(tmp_path)
        jsonschema.validate(json.loads(path.read_text()), summary_schema)


class TestHttpClient:
    def test_circuit_breaker(self, respx_mock):
        import httpx
        from lib.http import DEAD_AFTER_CONSECUTIVE_FAILURES, HttpClient

        respx_mock.get("https://down.test/x").mock(side_effect=httpx.ConnectError("boom"))
        client = HttpClient("https://down.test")
        for _ in range(DEAD_AFTER_CONSECUTIVE_FAILURES):
            res = client.get_json("/x")
            assert not res.ok
        assert client.dead
        res = client.get_json("/x")
        assert not res.ok and "dead" in res.error

    def test_gap_reasons(self, respx_mock):
        import httpx
        from lib.http import HttpClient

        respx_mock.get("https://api.test/forbidden").mock(return_value=httpx.Response(403))
        respx_mock.get("https://api.test/missing").mock(return_value=httpx.Response(404))
        client = HttpClient("https://api.test")
        assert client.get_json("/forbidden").gap_reason == "permission_denied"
        assert client.get_json("/missing").gap_reason == "endpoint_404"
