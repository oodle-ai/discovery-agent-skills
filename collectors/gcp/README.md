# GCP Cloud Operations collector

Read-only discovery of GCP observability scale from the same Cloud Monitoring
and Cloud Logging APIs that back the consoles. Auth is via
`gcloud auth print-access-token` (no google-cloud SDK). Raw responses are saved
(redacted) under `evidence/`; `summary.json` follows
[schemas/summary.schema.json](../../schemas/summary.schema.json). Re-derive
`summary.json` offline from saved evidence with `--report-only`.

```bash
uv run collectors/gcp/collect.py --project my-project-1 --lookback 30d \
  --output-dir ./discovery-output/gcp
```

## Figure ↔ API mapping (ground truth)

| Figure | Source API | Derivation |
|---|---|---|
| `metrics.total_count` / `metrics.custom_metrics_count` | `GET /v3/projects/*/metricDescriptors` | count of descriptors; custom = `custom.`/`external.`/`workload.`/`prometheus.googleapis.com/` prefixes |
| `metrics.samples_per_sec` | `timeSeries` `monitoring.googleapis.com/billing/samples_ingested` | sum of DELTA points / lookback seconds |
| `logs.ingest_gb_per_day` | `timeSeries` `logging.googleapis.com/billing/bytes_ingested` | sum of DELTA bytes / lookback days, → GB |
| `logs.stored_gb` | `timeSeries` `.../billing/monthly_bytes_ingested` | latest cumulative value (month-to-date), → GB |
| `traces.spans_per_sec` | `timeSeries` `cloudtrace.googleapis.com/billing/spans_ingested` | sum of DELTA points / lookback seconds |
| `alerts.monitor_count` | `GET /v3/projects/*/alertPolicies` | count of policies |
| `cost.monthly_usd` | — | **unavailable**: GCP has no cost query API; requires the Cloud Billing BigQuery export |

## Monthly usage-by-SKU export

For usage reviews you often want per-signal volume broken out by **calendar
month**. `monthly_usage.py` reconstructs that from the same Cloud Monitoring
`billing/*` timeSeries, reusing this collector's fetch and project selection.
Point it at its **own** output dir so its `summary.json` doesn't collide with
the discovery collector's:

```bash
uv run collectors/gcp/monthly_usage.py --project my-project-1 --months 6 \
  --output-dir ./discovery-output/gcp-monthly --tar
```

It writes `gcp_monthly_usage_by_sku.csv` plus a `summary.json` whose
`inventory.monthly_usage_by_sku` the report generator renders as a **"Monthly
Usage by SKU"** section (picked up by the usual `--summaries
./discovery-output/*/summary.json` glob; no new flags, no scalar figures). The
reconstructed SKUs, summed across the selected projects:

- `metrics` / `samples_ingested`
- `logs` / `bytes_ingested` (→ GB)
- `traces` / `spans_ingested`

**Retention caveat.** Cloud Monitoring retains `billing/*` timeSeries for only
**~6 weeks**, so months older than that return no data and appear **blank — a
retention limit, not zero usage**. The report carries this caveat inline. For a
true multi-month history, GCP usage-by-SKU lives in the Cloud Billing
**BigQuery export**, which this collector does not yet read.

SKU rows with zero/no usage in every month are dropped by default;
`--include-empty` keeps the full matrix. `--report-only` re-derives the CSV +
summary from saved evidence with no API calls.
