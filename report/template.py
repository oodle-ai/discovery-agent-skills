"""HTML building blocks for the discovery report.

Single self-contained file, no external assets. The executive layer keeps the
look of examples/sample-datadog-report.html; deep-dive and provenance styles are new.
"""

from __future__ import annotations

import html


def esc(s: object) -> str:
    return html.escape(str(s), quote=True)


CSS = """
  :root {
    --primary: #1a1a2e;
    --accent: #4f46e5;
    --bg: #f8fafc;
    --card: #ffffff;
    --text: #1e293b;
    --text-muted: #64748b;
    --border: #e2e8f0;
    --success: #10b981;
    --warning: #f59e0b;
    --danger: #ef4444;
  }
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.6;
  }
  .header { background: var(--primary); color: white; padding: 3rem 2rem; text-align: center; }
  .header h1 { font-size: 2rem; margin-bottom: 0.5rem; }
  .header p { color: #94a3b8; font-size: 1.1rem; }
  .container { max-width: 1100px; margin: 0 auto; padding: 2rem; }
  .summary-grid {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
    gap: 1rem;
    margin: 2rem 0;
  }
  .summary-card {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 1.25rem; text-align: center;
  }
  .summary-card .value { font-size: 1.75rem; font-weight: 700; color: var(--accent); }
  .summary-card .label { color: var(--text-muted); font-size: 0.8rem; margin-top: 0.25rem; }
  .section {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; padding: 1.75rem; margin: 1.25rem 0;
  }
  .section h2 {
    font-size: 1.3rem; margin-bottom: 0.75rem; color: var(--primary);
    border-bottom: 2px solid var(--accent); padding-bottom: 0.5rem;
  }
  .section h3 { font-size: 1.05rem; margin: 1rem 0 0.5rem; color: var(--primary); }
  table { width: 100%; border-collapse: collapse; margin: 0.75rem 0; font-size: 0.9rem; }
  th, td { padding: 0.6rem 0.75rem; text-align: left; border-bottom: 1px solid var(--border); }
  th {
    background: var(--bg); font-weight: 600; color: var(--text-muted);
    text-transform: uppercase; font-size: 0.75rem; letter-spacing: 0.05em;
  }
  .tag {
    display: inline-block; padding: 0.2rem 0.6rem; border-radius: 9999px;
    font-size: 0.75rem; font-weight: 500; margin: 0.125rem;
  }
  .tag-blue { background: #dbeafe; color: #1d4ed8; }
  .tag-green { background: #d1fae5; color: #065f46; }
  .tag-purple { background: #ede9fe; color: #5b21b6; }
  .tag-orange { background: #ffedd5; color: #9a3412; }
  .tag-red { background: #fee2e2; color: #991b1b; }
  .tag-gray { background: #f1f5f9; color: #475569; }
  .pain-point {
    border-left: 4px solid var(--warning); padding: 0.75rem 1.25rem;
    margin: 0.5rem 0; background: #fffbeb; border-radius: 0 8px 8px 0;
  }
  .recommendation {
    border-left: 4px solid var(--success); padding: 0.75rem 1.25rem;
    margin: 0.5rem 0; background: #ecfdf5; border-radius: 0 8px 8px 0;
  }
  .env-badge {
    display: inline-block; padding: 0.2rem 0.6rem; border-radius: 4px;
    font-size: 0.75rem; font-weight: 600; text-transform: uppercase;
  }
  .env-prod { background: #fee2e2; color: #991b1b; }
  .env-staging { background: #fef3c7; color: #92400e; }
  .env-dev { background: #d1fae5; color: #065f46; }
  .env-other { background: #f1f5f9; color: #475569; }
  .footer { text-align: center; padding: 2rem; color: var(--text-muted); font-size: 0.85rem; }
  ul { padding-left: 1.5rem; margin: 0.5rem 0; }
  li { margin: 0.25rem 0; }

  /* status + provenance */
  .status-badge {
    display: inline-block; padding: 0.1rem 0.5rem; border-radius: 4px;
    font-size: 0.7rem; font-weight: 600; text-transform: uppercase;
  }
  .status-ok { background: #d1fae5; color: #065f46; }
  .status-estimated { background: #fef3c7; color: #92400e; }
  .status-partial { background: #ffedd5; color: #9a3412; }
  .status-unavailable { background: #fee2e2; color: #991b1b; }
  .status-reported { background: #f1f5f9; color: #475569; }
  .narrative {
    border-left: 4px solid var(--accent); padding: 0.75rem 1.25rem;
    margin: 0.5rem 0; background: #eef2ff; border-radius: 0 8px 8px 0;
    font-size: 0.95rem;
  }
  details.deep-dive {
    background: var(--card); border: 1px solid var(--border);
    border-radius: 12px; margin: 1rem 0; padding: 0;
  }
  details.deep-dive > summary {
    cursor: pointer; padding: 1rem 1.5rem; font-weight: 600;
    color: var(--primary); font-size: 1.05rem; list-style-position: inside;
  }
  details.deep-dive > .deep-dive-body { padding: 0 1.75rem 1.5rem; }
  .provenance { font-size: 0.8rem; }
  .provenance td { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                   font-size: 0.75rem; vertical-align: top; }
  .provenance td:nth-child(1), .provenance td:nth-child(2) { white-space: nowrap; }
  .table-wrap { overflow-x: auto; }
  .muted { color: var(--text-muted); font-size: 0.85rem; }
  code { background: #f1f5f9; padding: 0.1rem 0.3rem; border-radius: 4px;
         font-size: 0.85em; overflow-wrap: anywhere; }

  /* pure-CSS tooltips (title attributes are unreliable/invisible) */
  [data-tip] { position: relative; cursor: help; }
  [data-tip]:hover::after {
    content: attr(data-tip);
    position: absolute; left: 50%; transform: translateX(-50%);
    bottom: calc(100% + 6px);
    background: var(--primary); color: #f1f5f9;
    padding: 0.45rem 0.65rem; border-radius: 6px;
    font-size: 0.75rem; font-weight: 400; line-height: 1.4;
    white-space: pre-line; width: max-content; max-width: 340px;
    text-align: left; z-index: 10; pointer-events: none;
    box-shadow: 0 4px 12px rgba(0,0,0,0.25);
  }
"""


def page(title: str, subtitle: str, body: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="header">
  <h1>{esc(title)}</h1>
  <p>{esc(subtitle)}</p>
</div>
<div class="container">
{body}
</div>
<div class="footer">
  Generated by the Oodle discovery skill. Every measured figure links to raw API evidence
  (see Provenance). Items marked "reported" were stated by the user and are not verified.
</div>
</body>
</html>
"""


def section(title: str, inner: str, section_id: str | None = None) -> str:
    id_attr = f' id="{esc(section_id)}"' if section_id else ""
    return f'<div class="section"{id_attr}><h2>{esc(title)}</h2>\n{inner}\n</div>'


def card(value: str, label: str, tooltip: str | None = None) -> str:
    tip = f' data-tip="{esc(tooltip)}"' if tooltip else ""
    return (
        f'<div class="summary-card"{tip}><div class="value">{esc(value)}</div>'
        f'<div class="label">{esc(label)}</div></div>'
    )


def status_badge(status: str) -> str:
    return f'<span class="status-badge status-{esc(status)}">{esc(status)}</span>'


def env_badge(kind: str) -> str:
    kind = kind if kind in ("prod", "staging", "dev") else "other"
    return f'<span class="env-badge env-{kind}">{esc(kind)}</span>'


def tag(text: str, color: str = "gray") -> str:
    return f'<span class="tag tag-{esc(color)}">{esc(text)}</span>'


def table(headers: list[str], rows: list[list[str]], css_class: str = "") -> str:
    """Build a table. Cell values are pre-escaped HTML (callers escape data)."""
    cls = f' class="{esc(css_class)}"' if css_class else ""
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "\n".join("<tr>" + "".join(f"<td>{c}</td>" for c in row) + "</tr>" for row in rows)
    return (
        f'<div class="table-wrap"><table{cls}><thead><tr>{head}</tr></thead>'
        f"<tbody>\n{body}\n</tbody></table></div>"
    )


def narrative_block(text: str) -> str:
    return f'<div class="narrative">{esc(text)}</div>'


def deep_dive(title: str, inner: str) -> str:
    return (
        f'<details class="deep-dive"><summary>{esc(title)}</summary>'
        f'<div class="deep-dive-body">{inner}</div></details>'
    )


def human_number(n: float) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.2f}B"
    if n >= 1e6:
        return f"{n / 1e6:.2f}M"
    if n >= 1e3:
        return f"{n / 1e3:.1f}K"
    if n == int(n):
        return f"{int(n):,}"
    return f"{n:,.2f}"


def format_value(value: float | None, unit: str, status: str) -> str:
    if value is None:
        return "—"
    prefix = "~" if status in ("estimated", "partial") else ""
    if unit == "USD":
        return f"{prefix}${human_number(value)}"
    if unit.startswith("GB/"):
        return f"{prefix}{value:,.1f} {unit}"
    if "/" in unit or unit in ("series", "metrics", "hosts", "monitors"):
        return f"{prefix}{human_number(value)} {unit}"
    return f"{prefix}{human_number(value)}{' ' + unit if unit else ''}"
