# /// script
# requires-python = ">=3.11"
# dependencies = ["jsonschema"]
# ///
"""Merge collector summary.json files + agent context.json into one HTML report.

This script is the only thing that writes the discovery report. Every numeric
figure comes from a collector summary.json (with provenance); everything from
context.json renders as qualitative/"reported". The agent never edits the
output.

Usage:
    uv run report/generate_report.py \\
        --context ./discovery-output/context.json \\
        --summaries ./discovery-output/*/summary.json \\
        --out ./discovery-report.html
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "collectors"))

import template as T  # noqa: E402
from lib.summary import validate_summary  # noqa: E402

SCHEMAS_DIR = REPO_ROOT / "schemas"


# ── loading & validation ─────────────────────────────────────────────────


def jsonschema_validate(doc: dict, schema_file: str, strict: bool) -> list[str]:
    """Validate with jsonschema when available; returns warnings."""
    try:
        import jsonschema
    except ImportError:
        return [f"jsonschema not installed; skipped {schema_file} validation"]
    schema_path = SCHEMAS_DIR / schema_file
    if not schema_path.exists():
        return [f"schema {schema_file} not found; skipped validation"]
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = jsonschema.Draft202012Validator(schema)
    errors = [f"{schema_file}: {e.json_path}: {e.message}" for e in validator.iter_errors(doc)]
    if errors and strict:
        raise SystemExit("validation failed (--strict):\n  " + "\n  ".join(errors))
    return errors


def load_summaries(paths: list[Path], strict: bool) -> list[dict]:
    summaries: list[dict] = []
    seen: set[str] = set()
    for p in paths:
        doc = json.loads(p.read_text(encoding="utf-8"))
        validate_summary(doc)  # dependency-free structural validation, always on
        for w in jsonschema_validate(doc, "summary.schema.json", strict):
            print(f"WARN: {p}: {w}")
        name = doc["collector"]
        if name in seen:
            print(f"WARN: duplicate collector {name!r} ({p}); keeping both, labels may repeat")
        seen.add(name)
        doc["_path"] = str(p)
        summaries.append(doc)
    return summaries


def load_context(path: Path | None, strict: bool) -> dict:
    if path is None:
        return {"schema_version": "1.0"}
    doc = json.loads(path.read_text(encoding="utf-8"))
    if doc.get("schema_version") != "1.0":
        raise SystemExit(
            f"context.json: schema_version must be '1.0' (got {doc.get('schema_version')!r})"
        )
    for w in jsonschema_validate(doc, "context.schema.json", strict):
        print(f"WARN: {path}: {w}")
    return doc


# ── figure aggregation ───────────────────────────────────────────────────


@dataclass
class Agg:
    value: float
    status: str  # "ok" if all ok, else "estimated"
    parts: list[str]  # "<collector>: <formatted> (<status>)"


def collect_figures(summaries: list[dict]) -> dict[str, list[tuple[str, dict]]]:
    out: dict[str, list[tuple[str, dict]]] = {}
    for s in summaries:
        for fig in s["figures"]:
            out.setdefault(fig["id"], []).append((s["collector"], fig))
    return out


def aggregate(figures: dict[str, list[tuple[str, dict]]], figure_id: str) -> Agg | None:
    entries = [
        (c, f)
        for c, f in figures.get(figure_id, [])
        if f["status"] != "unavailable" and f.get("value") is not None
    ]
    if not entries:
        return None
    unit = entries[0][1].get("unit", "")
    total = sum(f["value"] for _, f in entries)
    status = "ok" if all(f["status"] == "ok" for _, f in entries) else "estimated"
    parts = [
        f"{c}: {T.format_value(f['value'], f.get('unit', unit), f['status'])} ({f['status']})"
        for c, f in entries
    ]
    return Agg(value=total, status=status, parts=parts)


# ── sections ─────────────────────────────────────────────────────────────


def exec_cards(figures: dict, ctx: dict) -> str:
    envs = ctx.get("environments", [])
    inv = ctx.get("infra_inventory", {}) or {}
    cards: list[str] = []

    def reported(v: Any, label: str, suffix: str = "") -> str:
        tip = "reported by agent/user — not collector-verified" if v is not None else None
        return T.card(f"{v}{suffix}" if v is not None else "—", label, tooltip=tip)

    cards.append(reported(len(envs) or None, "Environments"))
    cards.append(reported(inv.get("total_services"), "Services"))
    cards.append(reported(inv.get("total_nodes"), "Compute Nodes"))

    def measured(figure_id: str, label: str, unit_hint: str) -> str:
        agg = aggregate(figures, figure_id)
        if agg is None:
            return T.card("—", label, tooltip="no measured figure; see Coverage & Gaps")
        return T.card(
            T.format_value(agg.value, unit_hint, agg.status), label,
            tooltip="; ".join(agg.parts),
        )

    metrics_agg = aggregate(figures, "metrics.samples_per_sec")
    if metrics_agg is not None:
        cards.append(measured("metrics.samples_per_sec", "Metrics Ingestion", "samples/sec"))
    elif aggregate(figures, "metrics.active_series") is not None:
        cards.append(measured("metrics.active_series", "Active Time Series", "series"))
    else:
        cards.append(measured("metrics.custom_metrics_count", "Custom Metrics", "metrics"))
    cards.append(measured("logs.ingest_gb_per_day", "Log Volume", "GB/day"))
    if aggregate(figures, "traces.spans_per_sec") is not None:
        cards.append(measured("traces.spans_per_sec", "Trace Spans", "spans/sec"))
    else:
        cards.append(measured("traces.ingest_gb_per_day", "Trace Volume", "GB/day"))
    cards.append(measured("cost.monthly_usd", "Est. Observability Spend", "USD"))
    cards.append(reported(ctx.get("team_size"), "Team Size"))
    return '<div class="summary-grid">' + "\n".join(cards) + "</div>"


def environments_section(ctx: dict) -> str:
    envs = ctx.get("environments", [])
    if not envs:
        return ""
    rows = []
    for e in envs:
        rows.append([
            f"<strong>{T.esc(e.get('name', ''))}</strong> {T.env_badge(e.get('kind', 'other'))}",
            T.esc(e.get("cloud") or "—"),
            T.esc(e.get("region") or "—"),
            T.esc(e.get("cluster") or "—"),
            T.esc(e.get("nodes") if e.get("nodes") is not None else "—"),
            T.esc(e.get("services") if e.get("services") is not None else "—"),
        ])
    inner = T.table(["Environment", "Cloud", "Region", "Cluster", "Nodes", "Services"], rows)
    inner += '<p class="muted">Reported by discovery agent from kubectl/cloud CLI inventory.</p>'
    nar = ctx.get("narrative", {}).get("environments")
    return T.section("Environments", (T.narrative_block(nar) if nar else "") + inner)


def tech_stack_section(ctx: dict) -> str:
    stack = ctx.get("tech_stack", {}) or {}
    groups = [
        ("Languages", "languages", "blue"),
        ("Databases", "databases", "green"),
        ("Infra / IaC", "infra", "purple"),
        ("Messaging", "messaging", "orange"),
        ("Other", "other", "gray"),
    ]
    parts = []
    for label, key, color in groups:
        items = stack.get(key) or []
        if items:
            tags = " ".join(T.tag(i, color) for i in items)
            parts.append(f"<p><strong>{T.esc(label)}:</strong> {tags}</p>")
    if not parts:
        return ""
    return T.section("Tech Stack", "\n".join(parts))


SIGNAL_FIGURES: dict[str, list[tuple[str, str]]] = {
    "metrics": [
        ("metrics.samples_per_sec", "samples/sec"),
        ("metrics.active_series", "active series"),
        ("metrics.custom_metrics_count", "custom metrics"),
        ("metrics.total_count", "metric names"),
    ],
    "logs": [
        ("logs.ingest_gb_per_day", "GB/day ingested"),
        ("logs.stored_gb", "GB stored"),
        ("logs.retention_days", "days retention"),
    ],
    "traces": [
        ("traces.spans_per_sec", "spans/sec"),
        ("traces.ingest_gb_per_day", "GB/day ingested"),
    ],
    "alerting": [
        ("alerts.monitor_count", "monitors"),
    ],
}


def observability_section(figures: dict, ctx: dict) -> str:
    tools_by_signal: dict[str, list[str]] = {}
    notes_by_signal: dict[str, str] = {}
    for entry in ctx.get("observability_stack", []) or []:
        tools_by_signal.setdefault(entry["signal"], []).extend(entry.get("tools", []))
        if entry.get("notes"):
            notes_by_signal[entry["signal"]] = entry["notes"]
    rows = []
    for signal in ("metrics", "logs", "traces", "alerting"):
        scale_bits = []
        for fid, suffix in SIGNAL_FIGURES[signal]:
            agg = aggregate(figures, fid)
            if agg is not None:
                val = T.format_value(agg.value, "", agg.status).strip()
                scale_bits.append(
                    f'<span title="{T.esc("; ".join(agg.parts))}">{val} {T.esc(suffix)}</span> '
                    + T.status_badge(agg.status)
                )
        tools = tools_by_signal.get(signal, [])
        if not tools and not scale_bits:
            continue
        rows.append([
            T.esc(signal.capitalize()),
            " ".join(T.tag(t, "blue") for t in tools) or "—",
            "<br>".join(scale_bits) or '<span class="muted">no measured figure</span>',
        ])
    if not rows:
        return ""
    inner = T.table(["Signal", "Tools", "Measured Scale (hover for source)"], rows)
    user_bits = [
        f"<li><strong>{T.esc(u['label'])}:</strong> {T.esc(u['value'])} "
        f"{T.status_badge('reported')}</li>"
        for u in ctx.get("user_reported", []) or []
        if u.get("area") in ("metrics", "logs", "traces", "retention", "alerting")
    ]
    if user_bits:
        inner += "<h3>User-reported (not verified)</h3><ul>" + "".join(user_bits) + "</ul>"
    nar = ctx.get("narrative", {}).get("observability")
    return T.section("Observability Stack & Scale", (T.narrative_block(nar) if nar else "") + inner)


def spend_section(summaries: list[dict], ctx: dict) -> str:
    rows = []
    total = 0.0
    any_estimated = False
    found = False
    for s in summaries:
        for fig in s["figures"]:
            if fig["id"] != "cost.monthly_usd" or fig["status"] == "unavailable":
                continue
            if fig.get("value") is None:
                continue
            found = True
            total += fig["value"]
            any_estimated = any_estimated or fig["status"] != "ok"
            rows.append([
                T.esc(s["collector"]),
                T.format_value(fig["value"], "USD", fig["status"]) + "/mo",
                T.status_badge(fig["status"]),
                T.esc(fig.get("method") or ""),
            ])
    for u in ctx.get("user_reported", []) or []:
        if u.get("area") == "cost":
            rows.append([
                T.esc(u["label"]),
                T.esc(u["value"]),
                T.status_badge("reported"),
                T.esc(u.get("notes") or "stated by user; not verified"),
            ])
    if not rows:
        return ""
    inner = T.table(["Source", "Monthly Spend", "Status", "Method"], rows)
    if found:
        marker = "~" if any_estimated else ""
        inner += (
            f"<p><strong>Measured total: {marker}${T.human_number(total)}/mo</strong> "
            '<span class="muted">(observability spend only; user-reported rows '
            "not included)</span></p>"
        )
    nar = ctx.get("narrative", {}).get("costs")
    return T.section(
        "Observability Spend (Estimated Monthly)",
        (T.narrative_block(nar) if nar else "") + inner,
    )


def coverage_section(summaries: list[dict], ctx: dict) -> str:
    parts = []
    cov_rows = []
    for s in summaries:
        figs = s["figures"]
        ok_n = sum(1 for f in figs if f["status"] != "unavailable")
        cov_rows.append([
            T.esc(s["collector"]),
            f"{ok_n}/{len(figs)}",
            T.esc(s.get("run", {}).get("target", "")),
            T.esc(s.get("run", {}).get("lookback", "")),
        ])
    parts.append(T.table(["Collector", "Figures collected", "Target", "Lookback"], cov_rows))

    gap_rows = []
    for s in summaries:
        for g in s.get("gaps", []):
            gap_rows.append([
                T.esc(s["collector"]),
                T.esc(g.get("area", "")),
                "<br>".join(f"<code>{T.esc(i)}</code>" for i in g.get("figure_ids", [])),
                T.tag(g.get("reason", ""), "red"),
                T.esc(g.get("detail", "")),
                T.esc(g.get("remediation") or "—"),
            ])
    for sk in ctx.get("skipped_collectors", []) or []:
        gap_rows.append([
            T.esc(sk["tool"]), "all", "—", T.tag("not_run", "red"), T.esc(sk["reason"]), "—",
        ])
    if gap_rows:
        parts.append("<h3>Gaps</h3>")
        parts.append(T.table(
            ["Collector", "Area", "Figures", "Reason", "Detail", "Remediation"], gap_rows
        ))
    else:
        parts.append('<p class="muted">No gaps — all expected figures were collected.</p>')
    return T.section("Coverage & Gaps", "\n".join(parts), section_id="coverage")


def pain_points_section(ctx: dict) -> str:
    pains = ctx.get("pain_points", []) or []
    if not pains:
        return ""
    parts = []
    for p in pains:
        parts.append(
            f'<div class="pain-point"><strong>{T.esc(p["title"])}</strong><br>'
            f"{T.esc(p['detail'])}</div>"
        )
        if p.get("recommendation"):
            parts.append(f'<div class="recommendation">{T.esc(p["recommendation"])}</div>')
    nar = ctx.get("narrative", {}).get("pain_points")
    return T.section(
        "Observability Pain Points", (T.narrative_block(nar) if nar else "") + "\n".join(parts)
    )


def render_inventory(inv: dict) -> str:
    """Generic renderer for tool-specific inventory facts."""
    parts = []
    scalars = []
    for key, val in inv.items():
        label = key.replace("_", " ").capitalize()
        if isinstance(val, (str, int, float)) or val is None:
            scalars.append((label, val))
        elif isinstance(val, list) and val and isinstance(val[0], dict):
            headers = list(val[0].keys())
            rows = [
                [T.esc(item.get(h) if item.get(h) is not None else "—") for h in headers]
                for item in val[:50]
            ]
            parts.append(f"<h3>{T.esc(label)}</h3>")
            parts.append(T.table([h.replace("_", " ") for h in headers], rows))
            if len(val) > 50:
                parts.append(f'<p class="muted">…and {len(val) - 50} more (see evidence files)</p>')
        elif isinstance(val, list):
            parts.append(
                f"<p><strong>{T.esc(label)}:</strong> "
                + " ".join(T.tag(str(v)) for v in val[:30])
                + "</p>"
            )
        elif isinstance(val, dict):
            rows = [[T.esc(k), T.esc(v)] for k, v in val.items()]
            parts.append(f"<h3>{T.esc(label)}</h3>")
            parts.append(T.table(["Key", "Value"], rows))
    if scalars:
        rows = [[T.esc(k), T.esc(v if v is not None else "—")] for k, v in scalars]
        parts.insert(0, T.table(["Fact", "Value"], rows))
    return "\n".join(parts)


def deep_dive_sections(summaries: list[dict]) -> str:
    parts = ["<h2 style='margin:2rem 0 0.5rem'>Deep Dives</h2>"]
    for s in summaries:
        env = s.get("environment", {}) or {}
        env_bits = ", ".join(
            f"{k}={v}" for k, v in env.items() if v is not None and k != "detection_method"
        )
        inner = []
        if env_bits:
            inner.append(f'<p class="muted">Detected: {T.esc(env_bits)}</p>')
        fig_rows = []
        for f in s["figures"]:
            window = f.get("time_window")
            window_s = f"{window['start']} → {window['end']}" if window else "—"
            fig_rows.append([
                T.esc(f["label"]),
                T.esc(T.format_value(f.get("value"), f.get("unit", ""), f["status"])),
                T.status_badge(f["status"]),
                T.esc(f.get("method") or f.get("unavailable_reason") or ""),
                T.esc(window_s),
            ])
        inner.append(T.table(["Figure", "Value", "Status", "Method / Reason", "Window"], fig_rows))
        if s.get("inventory"):
            inner.append(render_inventory(s["inventory"]))
        parts.append(T.deep_dive(f"{s['collector']} — collected details", "\n".join(inner)))
    return "\n".join(parts)


def provenance_section(summaries: list[dict]) -> str:
    rows = []
    for s in summaries:
        for f in s["figures"]:
            if f["status"] == "unavailable":
                continue
            rows.append([
                f"<code>{T.esc(f['id'])}</code>",
                T.esc(s["collector"]),
                T.esc(f.get("source_api") or "—"),
                T.esc(f.get("query") or "—"),
                "<br>".join(f"<code>{T.esc(e)}</code>" for e in f.get("evidence_files", []))
                or "—",
            ])
    if not rows:
        return ""
    inner = (
        '<p class="muted">Every measured figure below can be re-derived from the raw API '
        "response captured in the listed evidence file (under each collector's output "
        "directory).</p>"
        + T.table(["Figure", "Collector", "Source API", "Query", "Evidence"], rows, "provenance")
    )
    return T.section("Provenance Appendix", inner)


# ── main ─────────────────────────────────────────────────────────────────


def build_report(summaries: list[dict], ctx: dict, title: str, date: str | None = None) -> str:
    figures = collect_figures(summaries)
    company = ctx.get("company")
    today = date or datetime.now(UTC).strftime("%Y-%m-%d")
    subtitle = f"Generated on {today}" + (f" for {company}" if company else "")
    body_parts = [exec_cards(figures, ctx)]
    overview = ctx.get("narrative", {}).get("overview")
    if overview:
        body_parts.append(T.narrative_block(overview))
    body_parts += [
        environments_section(ctx),
        tech_stack_section(ctx),
        observability_section(figures, ctx),
        spend_section(summaries, ctx),
        pain_points_section(ctx),
        coverage_section(summaries, ctx),
        deep_dive_sections(summaries),
        provenance_section(summaries),
    ]
    return T.page(title, subtitle, "\n".join(p for p in body_parts if p))


def print_coverage(summaries: list[dict], ctx: dict) -> None:
    """Plain-text coverage summary for the agent's gap-review phase."""
    print("\n=== coverage summary ===")
    for s in summaries:
        figs = s["figures"]
        ok_n = sum(1 for f in figs if f["status"] != "unavailable")
        print(f"{s['collector']}: {ok_n}/{len(figs)} figures")
        for g in s.get("gaps", []):
            rem = f" | fix: {g.get('remediation')}" if g.get("remediation") else ""
            print(f"  gap [{g['reason']}] {', '.join(g['figure_ids'])}: {g['detail']}{rem}")
    for sk in ctx.get("skipped_collectors", []) or []:
        print(f"{sk['tool']}: skipped — {sk['reason']}")


def main() -> int:
    p = argparse.ArgumentParser(description="Discovery report generator")
    p.add_argument("--summaries", nargs="*", type=Path, default=[],
                   help="summary.json files (one per collector); may be empty in degraded mode")
    p.add_argument("--context", type=Path, default=None, help="context.json from the agent")
    p.add_argument("--out", type=Path, default=Path("discovery-report.html"))
    p.add_argument("--title", default="Infrastructure & Observability Discovery Report")
    p.add_argument("--strict", action="store_true",
                   help="Schema validation failures are fatal instead of warnings")
    p.add_argument("--date", default=None,
                   help="Override the 'Generated on' date (for reproducible output)")
    args = p.parse_args()

    summaries = load_summaries(args.summaries, args.strict)
    ctx = load_context(args.context, args.strict)
    html_doc = build_report(summaries, ctx, args.title, date=args.date)
    args.out.write_text(html_doc, encoding="utf-8")
    print(f"wrote {args.out}")
    print_coverage(summaries, ctx)
    return 0


if __name__ == "__main__":
    sys.exit(main())
