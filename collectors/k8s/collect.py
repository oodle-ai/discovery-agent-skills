# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Kubernetes inventory collector.

Measures cluster inventory (nodes, vCPU, memory, workloads) across one or
more kubectl contexts so the report's compute cards are deterministic
figures with evidence, not agent arithmetic or user input.

Examples:
    uv run collectors/k8s/collect.py --output-dir ./discovery-output/k8s
    uv run collectors/k8s/collect.py --context prod-eks --context stg-eks \\
        --output-dir ./discovery-output/k8s
    uv run collectors/k8s/collect.py --all-contexts --output-dir ./discovery-output/k8s
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lib.cli import base_parser  # noqa: E402
from lib.evidence import EvidenceWriter  # noqa: E402
from lib.summary import ExpectedFigure, Figure, SummaryWriter  # noqa: E402

COLLECTOR = "k8s"
VERSION = "1.0.0"
KUBECTL_TIMEOUT_S = 20

EXPECTED = [
    ExpectedFigure("infra.nodes_total", "Kubernetes nodes", "nodes", "infra"),
    ExpectedFigure("infra.vcpu_total", "Cluster vCPU capacity", "vCPU", "infra"),
    ExpectedFigure("infra.memory_gib_total", "Cluster memory capacity", "GiB", "infra"),
    ExpectedFigure("infra.services_total", "Deployed services", "services", "infra"),
]


def kubectl(args: list[str], context: str | None = None) -> tuple[Any, str | None]:
    """Run kubectl, return (parsed JSON or text, error)."""
    cmd = ["kubectl"]
    if context:
        cmd += ["--context", context]
    cmd += args
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=KUBECTL_TIMEOUT_S, check=False
        )
    except FileNotFoundError:
        return None, "kubectl not installed"
    except subprocess.TimeoutExpired:
        return None, f"kubectl timed out after {KUBECTL_TIMEOUT_S}s"
    if out.returncode != 0:
        return None, (out.stderr or out.stdout).strip()[:300]
    text = out.stdout
    if "-o" in args and "json" in args:
        try:
            return json.loads(text), None
        except json.JSONDecodeError as exc:
            return None, f"invalid kubectl JSON: {exc}"
    return text, None


def parse_cpu(v: str) -> float:
    """Kubernetes CPU quantity: '8' cores or '7910m' millicores."""
    v = str(v).strip()
    if v.endswith("m"):
        return float(v[:-1]) / 1000.0
    return float(v)


_MEM_FACTORS = {
    "Ki": 1 / (1024**2),
    "Mi": 1 / 1024,
    "Gi": 1.0,
    "Ti": 1024.0,
    "K": 1e3 / (1024**3),
    "M": 1e6 / (1024**3),
    "G": 1e9 / (1024**3),
    "T": 1e12 / (1024**3),
}


def parse_memory_gib(v: str) -> float:
    """Kubernetes memory quantity to GiB: '32827080Ki', '64Gi', '128974848'."""
    v = str(v).strip()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([A-Za-z]*)", v)
    if not m:
        raise ValueError(f"unparseable memory quantity: {v!r}")
    n, suffix = float(m.group(1)), m.group(2)
    if not suffix:
        return n / (1024**3)  # bytes
    if suffix not in _MEM_FACTORS:
        raise ValueError(f"unknown memory suffix: {v!r}")
    return n * _MEM_FACTORS[suffix]


def safe_name(context: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", context)[:80]


def collect_context(ctx: str, ev: EvidenceWriter) -> tuple[dict[str, Any] | None, str | None]:
    """Inventory one kubectl context. Returns (stats, error)."""
    nodes, err = kubectl(["get", "nodes", "-o", "json"], context=ctx)
    if err:
        return None, err
    ev.write(f"{safe_name(ctx)}_nodes", nodes, source_api=f"kubectl --context {ctx} get nodes")

    stats: dict[str, Any] = {"context": ctx}
    items = nodes.get("items", [])
    stats["nodes"] = len(items)
    vcpu = 0.0
    mem = 0.0
    instance_types: set[str] = set()
    for n in items:
        cap = n.get("status", {}).get("capacity", {})
        try:
            vcpu += parse_cpu(cap.get("cpu", "0"))
            mem += parse_memory_gib(cap.get("memory", "0"))
        except ValueError:
            pass
        itype = n.get("metadata", {}).get("labels", {}).get("node.kubernetes.io/instance-type")
        if itype:
            instance_types.add(itype)
    stats["vcpu"] = round(vcpu, 1)
    stats["memory_gib"] = round(mem, 1)
    stats["instance_types"] = sorted(instance_types)

    deploys, derr = kubectl(["get", "deployments", "-A", "-o", "json"], context=ctx)
    if not derr:
        ev.write(
            f"{safe_name(ctx)}_deployments",
            {"count": len(deploys.get("items", [])),
             "names": [
                 f"{d['metadata']['namespace']}/{d['metadata']['name']}"
                 for d in deploys.get("items", [])
             ]},
            source_api=f"kubectl --context {ctx} get deployments -A",
        )
        stats["deployments"] = len(deploys.get("items", []))
    for kind, args in (
        ("pods", ["get", "pods", "-A", "--no-headers"]),
        ("namespaces", ["get", "namespaces", "--no-headers"]),
    ):
        out, kerr = kubectl(args, context=ctx)
        if not kerr and isinstance(out, str):
            stats[kind] = len([line for line in out.splitlines() if line.strip()])
    return stats, None


def main() -> int:
    parser = base_parser("Kubernetes inventory collector", default_lookback="0d")
    parser.add_argument(
        "--context",
        action="append",
        default=[],
        help="kubectl context to inventory (repeatable; default: current context)",
    )
    parser.add_argument(
        "--all-contexts", action="store_true", help="Inventory every context in kubeconfig"
    )
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    ev = EvidenceWriter(args.output_dir)

    summary = SummaryWriter(
        collector=COLLECTOR,
        collector_version=VERSION,
        expected=EXPECTED,
        target="kubeconfig",
        lookback="n/a",
        args_redacted={"contexts": args.context or "current", "all_contexts": args.all_contexts},
    )
    summary.environment = {
        "detected_backend": "kubernetes",
        "detection_method": "kubectl config / get nodes per context",
    }

    contexts = list(args.context)
    if args.all_contexts:
        out, err = kubectl(["config", "get-contexts", "-o", "name"])
        if err:
            for fid in summary.expected:
                summary.mark_unavailable(
                    fid, "not_configured", f"kubectl unavailable: {err}",
                    remediation="install kubectl and configure kubeconfig",
                )
            summary.write(args.output_dir)
            ev.finalize()
            return 0
        contexts = [c for c in out.splitlines() if c.strip()]
    if not contexts:
        out, err = kubectl(["config", "current-context"])
        if err or not isinstance(out, str) or not out.strip():
            for fid in summary.expected:
                summary.mark_unavailable(
                    fid, "not_configured",
                    f"no kubectl context available: {err or 'empty current-context'}",
                    remediation="pass --context <name> or configure a current context",
                )
            summary.write(args.output_dir)
            ev.finalize()
            return 0
        contexts = [out.strip()]

    per_context: list[dict[str, Any]] = []
    failures: list[tuple[str, str]] = []
    for ctx in contexts:
        print(f"inventorying context {ctx}")
        stats, err = collect_context(ctx, ev)
        if err:
            print(f"  WARN {ctx}: {err}")
            failures.append((ctx, err))
        else:
            per_context.append(stats)

    if not per_context:
        detail = "; ".join(f"{c}: {e}" for c, e in failures) or "no contexts inventoried"
        for fid in summary.expected:
            summary.mark_unavailable(
                fid, "timeout" if "timed out" in detail else "not_configured", detail,
                remediation="check cluster connectivity/credentials for the listed contexts",
            )
        summary.write(args.output_dir)
        ev.finalize()
        return 0

    status = "partial" if failures else "ok"
    notes = (
        "unreachable contexts: " + ", ".join(c for c, _ in failures) if failures else None
    )
    ctx_names = [s["context"] for s in per_context]
    method = f"kubectl get nodes summed across contexts: {', '.join(ctx_names)}"
    evidence = [f"evidence/{safe_name(c)}_nodes.json" for c in ctx_names]

    def total(key: str) -> float:
        return float(sum(s.get(key, 0) for s in per_context))

    summary.add_figure(Figure(
        id="infra.nodes_total", label="Kubernetes nodes", value=total("nodes"), unit="nodes",
        status=status, method=method, source_api="kubectl get nodes -o json",
        evidence_files=evidence, notes=notes,
    ))
    summary.add_figure(Figure(
        id="infra.vcpu_total", label="Cluster vCPU capacity", value=round(total("vcpu"), 1),
        unit="vCPU", status=status,
        method="sum of node .status.capacity.cpu (millicores normalized)",
        source_api="kubectl get nodes -o json", evidence_files=evidence, notes=notes,
    ))
    summary.add_figure(Figure(
        id="infra.memory_gib_total", label="Cluster memory capacity",
        value=round(total("memory_gib"), 1), unit="GiB", status=status,
        method="sum of node .status.capacity.memory converted to GiB",
        source_api="kubectl get nodes -o json", evidence_files=evidence, notes=notes,
    ))
    if any("deployments" in s for s in per_context):
        summary.add_figure(Figure(
            id="infra.services_total", label="Deployed services",
            value=float(sum(s.get("deployments", 0) for s in per_context)), unit="services",
            status=status, method="count of Deployments across all namespaces and contexts",
            source_api="kubectl get deployments -A -o json",
            evidence_files=[f"evidence/{safe_name(c)}_deployments.json" for c in ctx_names],
            notes=notes,
        ))
    else:
        summary.mark_unavailable(
            "infra.services_total", "permission_denied",
            "could not list deployments in any context",
            remediation="grant list permission on deployments cluster-wide",
        )
    if failures:
        summary.add_gap(
            "infra", [e.id for e in EXPECTED], "timeout",
            "; ".join(f"{c}: {e}" for c, e in failures),
            remediation="check connectivity/credentials for the listed contexts; figures "
            "cover the reachable contexts only",
        )
    summary.inventory["contexts"] = per_context
    summary.write(args.output_dir)
    ev.finalize()
    if args.tar:
        ev.tar()
    print(f"done: inventoried {len(per_context)}/{len(contexts)} context(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
