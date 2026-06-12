"""summary.json writer: the contract between collectors and the report generator.

Rules enforced here (see schemas/summary.schema.json):
- every figure carries provenance (method, source_api, time_window, evidence)
- collectors declare an expected figure set up front; anything not emitted
  by finalize() time becomes status=unavailable + a gap entry, never silence
- the document is validated before it is written; a validation failure is a
  collector bug and raises
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .evidence import utcnow_iso

SCHEMA_VERSION = "1.0"
STATUSES = ("ok", "estimated", "partial", "unavailable")
GAP_REASONS = (
    "permission_denied",
    "auth_failed",
    "endpoint_404",
    "version_unsupported",
    "timeout",
    "not_configured",
    "user_declined",
    "not_collected",
    "api_error",
)
_FIGURE_ID_RE = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+$")


@dataclass
class ExpectedFigure:
    id: str
    label: str
    unit: str
    area: str  # metrics | logs | traces | cost | alerts | hosts | <tool-specific>


@dataclass
class Figure:
    id: str
    label: str
    value: float | None
    unit: str
    status: str
    method: str | None = None
    source_api: str | None = None
    query: str | None = None
    time_window: dict[str, str] | None = None
    evidence_files: list[str] = field(default_factory=list)
    notes: str | None = None
    unavailable_reason: str | None = None
    remediation: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "value": self.value,
            "unit": self.unit,
            "status": self.status,
            "method": self.method,
            "source_api": self.source_api,
            "query": self.query,
            "time_window": self.time_window,
            "evidence_files": self.evidence_files,
            "notes": self.notes,
            "unavailable_reason": self.unavailable_reason,
            "remediation": self.remediation,
        }


class SummaryWriter:
    def __init__(
        self,
        collector: str,
        collector_version: str,
        expected: list[ExpectedFigure],
        target: str,
        lookback: str,
        args_redacted: dict[str, Any] | None = None,
    ) -> None:
        self.collector = collector
        self.collector_version = collector_version
        self.expected = {e.id: e for e in expected}
        self.target = target
        self.lookback = lookback
        self.args_redacted = args_redacted or {}
        self.started_at = utcnow_iso()
        self.environment: dict[str, Any] = {}
        self.inventory: dict[str, Any] = {}
        self._figures: dict[str, Figure] = {}
        self._gaps: list[dict[str, Any]] = []

    # ── recording ────────────────────────────────────────────────

    def add_figure(self, fig: Figure) -> None:
        if not _FIGURE_ID_RE.match(fig.id):
            raise ValueError(f"invalid figure id: {fig.id!r}")
        if fig.status not in STATUSES:
            raise ValueError(f"invalid status {fig.status!r} for {fig.id}")
        if fig.status != "unavailable" and fig.method is None:
            raise ValueError(f"figure {fig.id} with status {fig.status} must state a method")
        if fig.status == "unavailable" and not fig.unavailable_reason:
            raise ValueError(f"unavailable figure {fig.id} must state unavailable_reason")
        if fig.status != "unavailable" and fig.value is None:
            raise ValueError(f"figure {fig.id} with status {fig.status} must carry a value")
        self._figures[fig.id] = fig

    def add_gap(
        self,
        area: str,
        figure_ids: list[str],
        reason: str,
        detail: str,
        remediation: str | None = None,
    ) -> None:
        if reason not in GAP_REASONS:
            raise ValueError(f"invalid gap reason: {reason!r}")
        self._gaps.append(
            {
                "area": area,
                "figure_ids": figure_ids,
                "reason": reason,
                "detail": detail,
                "remediation": remediation,
            }
        )

    def mark_unavailable(
        self,
        figure_id: str,
        reason: str,
        detail: str,
        remediation: str | None = None,
    ) -> None:
        """Record an expected figure as unavailable, with its gap entry."""
        exp = self.expected.get(figure_id)
        label = exp.label if exp else figure_id
        unit = exp.unit if exp else ""
        area = exp.area if exp else figure_id.split(".")[0]
        self.add_figure(
            Figure(
                id=figure_id,
                label=label,
                value=None,
                unit=unit,
                status="unavailable",
                unavailable_reason=f"{reason}: {detail}",
                remediation=remediation,
            )
        )
        self.add_gap(area, [figure_id], reason, detail, remediation)

    # ── output ───────────────────────────────────────────────────

    def _fill_missing_expected(self) -> None:
        for fid in self.expected:
            if fid in self._figures:
                continue
            print(
                f"WARN: expected figure {fid} was never recorded; "
                f"emitting as unavailable (collector bug?)"
            )
            self.mark_unavailable(
                fid,
                "not_collected",
                "collector finished without attempting or recording this figure",
            )

    def to_dict(self) -> dict[str, Any]:
        self._fill_missing_expected()
        return {
            "schema_version": SCHEMA_VERSION,
            "collector": self.collector,
            "collector_version": self.collector_version,
            "run": {
                "started_at": self.started_at,
                "finished_at": utcnow_iso(),
                "target": self.target,
                "lookback": self.lookback,
                "args_redacted": self.args_redacted,
            },
            "environment": self.environment,
            "figures": [f.to_dict() for f in self._figures.values()],
            "inventory": self.inventory,
            "gaps": self._gaps,
        }

    def write(self, output_dir: Path) -> Path:
        doc = self.to_dict()
        validate_summary(doc)
        path = Path(output_dir) / "summary.json"
        path.write_text(json.dumps(doc, indent=2, default=str) + "\n", encoding="utf-8")
        print(f"wrote {path}")
        return path


def validate_summary(doc: dict[str, Any]) -> None:
    """Minimal structural validation (mirrors schemas/summary.schema.json).

    Dependency-free so collectors don't need jsonschema at runtime; CI runs
    the full jsonschema validation in tests.
    """

    def fail(msg: str) -> None:
        raise ValueError(f"summary.json invalid: {msg}")

    for key in ("schema_version", "collector", "collector_version", "run", "figures", "gaps"):
        if key not in doc:
            fail(f"missing key {key!r}")
    if doc["schema_version"] != SCHEMA_VERSION:
        fail(f"schema_version must be {SCHEMA_VERSION!r}")
    run = doc["run"]
    for key in ("started_at", "finished_at", "target", "lookback"):
        if key not in run:
            fail(f"run missing {key!r}")
    seen: set[str] = set()
    for fig in doc["figures"]:
        fid = fig.get("id", "")
        if not _FIGURE_ID_RE.match(fid):
            fail(f"bad figure id {fid!r}")
        if fid in seen:
            fail(f"duplicate figure id {fid!r}")
        seen.add(fid)
        if fig.get("status") not in STATUSES:
            fail(f"bad status for {fid}")
        if fig["status"] != "unavailable":
            if fig.get("value") is None:
                fail(f"{fid}: non-unavailable figure without value")
            if not fig.get("method"):
                fail(f"{fid}: non-unavailable figure without method")
        elif not fig.get("unavailable_reason"):
            fail(f"{fid}: unavailable figure without unavailable_reason")
    for gap in doc["gaps"]:
        if gap.get("reason") not in GAP_REASONS:
            fail(f"bad gap reason {gap.get('reason')!r}")
        for key in ("area", "figure_ids", "detail"):
            if key not in gap:
                fail(f"gap missing {key!r}")
