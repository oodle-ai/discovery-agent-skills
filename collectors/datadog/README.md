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

## Figure ↔ API mapping (ground truth)

| Figure | Source API | Derivation |
|---|---|---|
| `hosts.count` | `GET /api/v1/hosts/totals` | `total_active` (hosts seen in ~2h) |
| `metrics.total_count` | `GET /api/v1/metrics?from=<2h ago>` | count of active metric names |
| `metrics.custom_metrics_count` | `GET /api/v2/usage/hourly_usage` family `timeseries` | average of hourly `num_custom_timeseries` over lookback |
| `logs.ingest_gb_per_day` | hourly usage family `logs` | sum of hourly `ingested_events_bytes` / days covered / 1e9 |
| `datadog.logs_indexed_events_per_day` | hourly usage family `logs` | sum of hourly `indexed_events_count` / days covered |
| `traces.ingest_gb_per_day` | hourly usage family `ingested_spans` | sum of hourly `ingested_events_bytes` / days covered / 1e9 |
| `datadog.rum_sessions_per_day` | hourly usage family `rum` | sum of hourly `rum_total_session_count` / days covered |
| `alerts.monitor_count` | `GET /api/v1/monitor` (paginated) | count |
| `cost.monthly_usd` | `GET /api/v2/usage/estimated_cost?view=summary` | `total_cost`, current month to date |

Inventory (deep-dive section): dashboards, notebooks, SLOs, synthetics by
type, monitors by type, log indexes with retention/daily limits, log pipeline
count, billable infra hosts (avg/max from hourly usage).

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
- **Estimated cost added** (`/api/v2/usage/estimated_cost`); a 403 becomes a
  gap with the required permission named, never a guessed number.

## Validation

Run against a known org and compare each figure to **Plan & Usage → Usage**
for the same window. `cost.monthly_usd` must match the month-to-date
estimated cost shown on the billing page.
