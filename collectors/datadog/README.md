# Datadog collector

Collects inventory and usage figures from the Datadog account, using the same
APIs that back Datadog's own **Usage & Cost** pages. All raw responses are
written (redacted) to `evidence/`; every figure in `summary.json` carries the
endpoint, derivation method, and time window that produced it.

## Run

```bash
DD_API_KEY=... DD_APP_KEY=... \
uv run collectors/datadog/collect.py --site us5 --lookback 30d \
  --output-dir ./discovery-output/datadog
```

The application key needs `usage_read` (and `billing_read` for estimated
cost). Re-derive `summary.json` offline from saved evidence with
`--report-only`.

## Monthly usage-by-SKU export

For usage/pricing reviews you often want per-SKU volume broken out by
**calendar month** rather than the discovery report's single-window rates.
`monthly_usage.py` produces that, reusing this collector's paginated v2
hourly-usage fetch. Point it at its **own** output dir (so its `summary.json`
doesn't collide with the discovery collector's):

```bash
DD_API_KEY=... DD_APP_KEY=... \
uv run collectors/datadog/monthly_usage.py --site us5 --months 6 \
  --output-dir ./discovery-output/datadog-monthly --tar
```

It writes two artifacts (plus redacted `evidence/`, and the `--tar` bundle):

- **`datadog_monthly_usage_by_sku.csv`** — one row per `product_family` /
  `usage_type`, one column per month, plus `unit` and `aggregation` columns.
- **`summary.json`** — the same matrix under
  `inventory.monthly_usage_by_sku`. It carries **no scalar figures**; the
  report generator discovers it through the usual
  `--summaries ./discovery-output/*/summary.json` glob and renders a **"Monthly
  Usage by SKU"** section. No new report flags — run it like any collector.

Every figure is self-documenting via its `unit` and `aggregation`:

- `*_bytes` usage types are **summed** over the month and reported in **GB**.
- Host/container gauges (`host_count`, `container_count`, `apm_host_count`, …)
  use the **p99** of the hourly counts. Datadog bills these on a spike-excluded
  high-water mark (it drops the top ~1% of hours), which p99 approximates; a
  plain mean understates the billed figure.
- Custom metrics (`num_custom_timeseries`) are **averaged** — Datadog bills
  custom metrics on the hourly average.
- All other usage types (indexed events, sessions, spans) are **summed**.

SKU rows with **zero/no usage in every month** (e.g. `aws_host_count` on an
account that runs nothing on AWS, or `num_custom_timeseries` at 0) are dropped
by default from both the CSV and the report so you only see SKUs you actually
use. Pass `--include-empty` to keep the full matrix.

Reports the last N *full* calendar months (current partial month excluded).
`--report-only` re-derives the CSV from saved evidence with no API calls. Slow
for long lookbacks — Datadog serves historical hourly windows at ~15s/week; a
6-month pull is a couple of minutes.

## Figure ↔ API mapping (ground truth)

| Figure | Source API | Derivation |
|---|---|---|
| `hosts.count` | `GET /api/v1/hosts/totals` | `total_active` (hosts seen in ~2h) |
| `metrics.total_count` | `GET /api/v1/metrics?from=<2h ago>` | count of active metric names |
| `metrics.custom_metrics_count` | `datadog.estimated_usage.metrics.custom`, else hourly `timeseries` `num_custom_timeseries` | time-average of `sum:datadog.estimated_usage.metrics.custom{*}` (the Usage & Cost dashboard source); falls back to the classic hourly `num_custom_timeseries` |
| `logs.ingest_gb_per_day` | `datadog.estimated_usage.logs.ingested_bytes` → usage/summary `twol_*` → hourly `ingested_events_bytes` | **preference order.** The estimated_usage metric (via `/api/v1/query`, metrics scope) and the usage-summary `twol_ingested_events_bytes` both count **Logging without Limits**; classic `ingested_events_bytes` is **0** for LwL orgs, so it is the last resort |
| `datadog.logs_indexed_events_per_day` | hourly usage family `logs` | sum of hourly `indexed_events_count` / days covered |
| `traces.ingest_gb_per_day` | hourly usage family `ingested_spans` | sum of hourly `ingested_events_bytes` / days covered / 1e9 |
| `datadog.rum_sessions_per_day` | hourly usage family `rum` | sum of hourly `rum_total_session_count` / days covered |
| `alerts.monitor_count` | `GET /api/v1/monitor` (paginated) | count |
| `cost.monthly_usd` | `GET /api/v2/usage/historical_cost?view=summary` | last full month's billed `total_cost` (most stable); falls back to a projection of month-to-date, then the `all.cost` metric (metrics scope, dodges `billing_read`), then usage × public list prices (all `estimated`) |
| `datadog.cost_month_to_date_usd` | `GET /api/v2/usage/estimated_cost?view=summary` | current month `total_cost` (estimated_cost only serves the current month; past months come from historical_cost) |
| `datadog.cost_projected_month_usd` | derived | month-to-date / days elapsed × days in month (`estimated`) |

Inventory (deep-dive section): dashboards, notebooks, SLOs, synthetics by
type, monitors by type, log indexes with retention/daily limits, log pipeline
count, billable infra hosts (avg/max from hourly usage).

### estimated_usage metrics & Logging without Limits

The collector also queries the `datadog.estimated_usage.*` metric namespace via
`GET /api/v1/query` (the same source Datadog's own Usage & Cost dashboards use).
These read with **metrics/timeseries scope** — no `usage_read`/`billing_read` —
and, critically, count **Logging without Limits** ("twol") ingestion. Classic
`ingested_events_bytes` is `0` for Logging-without-Limits orgs (most modern
Datadog log setups), so without this path `logs.ingest_gb_per_day` silently
reads zero for orgs that ingest heavily. `all.cost` provides a measured cost
when the billing API returns 403.

If figures are broadly `permission_denied`, the most common cause is a
`--site` that doesn't match the org's Datadog region (keys for another site are
Forbidden on everything); the report's remediation now calls this out.

## Audit notes vs the original oodlectl script

- **Hourly usage is paginated** via `meta.pagination.next_record_id`. The
  original fetched one page; a 30d window (720 hourly records) was silently
  truncated.
- **Volumes come from the v2 hourly usage API over an exact window**, not
  from month-granularity `usage/summary` fields. The summary API is still
  collected as corroborating evidence (`evidence/usage_summary.json`) but no
  figure is derived from its `*_agg_sum` field variants, whose naming differs
  across accounts.
- **`ingested_spans` family added** — the original only fetched
  `indexed_spans`, which measures indexed (retained) spans, not APM ingestion.
- **Custom metrics** use the `timeseries` family's hourly
  `num_custom_timeseries` gauge (averaged), matching the billing definition,
  instead of summary-field candidates.
- **Cost figures added** (`/api/v2/usage/historical_cost` + `estimated_cost`).
  The headline is the last fully-billed month with its per-product charge
  breakdown (`inventory.cost_breakdown`, product `total` rows only to avoid
  committed/on-demand double counting). If the billing APIs are denied, the
  collector falls back to usage × Datadog public list prices (versioned in
  `LIST_PRICES`, per-component basis recorded in
  `inventory.cost_estimate_components`) with `status: estimated` and an
  explicit gap naming the missing permission — never a silently guessed
  number.

## Validation

Run against a known org and compare each figure to **Plan & Usage → Usage**
for the same window. `cost.monthly_usd` must match the month-to-date
estimated cost shown on the billing page.
