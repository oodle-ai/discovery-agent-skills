from __future__ import annotations

import json
import os
import stat

import pytest
from conftest import REPO_ROOT, load_module


@pytest.fixture(scope="session")
def k8s_collect():
    return load_module("k8s_collect", REPO_ROOT / "collectors" / "k8s" / "collect.py")


def node(name: str, itype: str, cpu: str, memory: str) -> dict:
    return {
        "metadata": {"name": name, "labels": {"node.kubernetes.io/instance-type": itype}},
        "status": {"capacity": {"cpu": cpu, "memory": memory}},
    }


NODES = {
    "items": [
        node("n1", "m6i.xlarge", "4", "16384Mi"),
        node("n2", "m6i.2xlarge", "7910m", "32827080Ki"),
    ]
}

DEPLOYMENTS = {
    "items": [
        {"metadata": {"namespace": "default", "name": f"svc-{i}"}} for i in range(5)
    ]
}


@pytest.fixture()
def fake_kubectl(tmp_path, monkeypatch):
    """A kubectl stand-in that serves fixture data for context 'test-ctx'."""
    fixtures = tmp_path / "kfixtures"
    fixtures.mkdir()
    (fixtures / "nodes.json").write_text(json.dumps(NODES))
    (fixtures / "deployments.json").write_text(json.dumps(DEPLOYMENTS))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "kubectl"
    script.write_text(f"""#!/bin/sh
case "$*" in
  "config current-context") echo "test-ctx" ;;
  *"get nodes -o json"*) cat {fixtures}/nodes.json ;;
  *"get deployments -A -o json"*) cat {fixtures}/deployments.json ;;
  *"get pods -A --no-headers"*) printf 'p1\\np2\\np3\\n' ;;
  *"get namespaces --no-headers"*) printf 'default\\nkube-system\\n' ;;
  *) echo "unexpected: $*" >&2; exit 1 ;;
esac
""")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    return bin_dir


def run(k8s_collect, tmp_path, monkeypatch, extra=None):
    argv = ["collect.py", "--output-dir", str(tmp_path / "out")] + (extra or [])
    monkeypatch.setattr("sys.argv", argv)
    assert k8s_collect.main() == 0
    return json.loads((tmp_path / "out" / "summary.json").read_text())


class TestQuantityParsing:
    def test_cpu(self, k8s_collect):
        assert k8s_collect.parse_cpu("8") == 8.0
        assert k8s_collect.parse_cpu("7910m") == pytest.approx(7.91)

    def test_memory(self, k8s_collect):
        assert k8s_collect.parse_memory_gib("64Gi") == 64.0
        assert k8s_collect.parse_memory_gib("16384Mi") == 16.0
        assert k8s_collect.parse_memory_gib("32827080Ki") == pytest.approx(31.3, abs=0.1)
        assert k8s_collect.parse_memory_gib(str(8 * 1024**3)) == 8.0  # bytes


class TestK8sCollector:
    def test_inventory_current_context(self, k8s_collect, tmp_path, monkeypatch, fake_kubectl,
                                       summary_schema):
        summary = run(k8s_collect, tmp_path, monkeypatch)
        jsonschema = pytest.importorskip("jsonschema")
        jsonschema.validate(summary, summary_schema)
        figs = {f["id"]: f for f in summary["figures"]}
        assert figs["infra.nodes_total"]["value"] == 2
        assert figs["infra.vcpu_total"]["value"] == pytest.approx(11.9)
        assert figs["infra.memory_gib_total"]["value"] == pytest.approx(47.3, abs=0.1)
        assert figs["infra.services_total"]["value"] == 5
        assert all(f["status"] == "ok" for f in summary["figures"])
        assert summary["gaps"] == []
        ctx = summary["inventory"]["contexts"][0]
        assert ctx["instance_types"] == ["m6i.2xlarge", "m6i.xlarge"]
        assert ctx["pods"] == 3

    def test_kubectl_missing_yields_gaps(self, k8s_collect, tmp_path, monkeypatch):
        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
        summary = run(k8s_collect, tmp_path, monkeypatch)
        assert all(f["status"] == "unavailable" for f in summary["figures"])
        assert {g["reason"] for g in summary["gaps"]} == {"not_configured"}
        assert all(g["remediation"] for g in summary["gaps"])
