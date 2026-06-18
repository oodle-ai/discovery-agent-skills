# Mimir Collector

Collects scale, configuration, and cost data from a Grafana Mimir cluster
using Mimir's Prometheus-compatible query API and admin endpoints.

## Figure ↔ API Mapping

| Figure ID | Label | API | Query / Endpoint | Notes |
|-----------|-------|-----|------------------|-------|
| `metrics.active_series` | Active time series | `GET /api/v1/query` | `sum(cortex_ingester_active_series)` | Instant query; counts all active series across ingesters |
| `metrics.samples_per_sec` | Ingestion rate (avg) | `GET /api/v1/query_range` | `sum(rate(cortex_distributor_received_samples_total[5m]))` | Average over lookback window; falls back to `cortex_ingest_storage_reader_fetch_records_total` |
| `mimir.ingestion_rate_peak` | Ingestion rate (peak) | `GET /api/v1/query_range` | same as above | Max value from the range query |
| `mimir.query_rate_avg` | Query rate (avg) | `GET /api/v1/query_range` | `sum(rate(cortex_request_duration_seconds_count{route=~".*query.*"}[5m]))` | Average over lookback |
| `mimir.query_rate_peak` | Query rate (peak) | `GET /api/v1/query_range` | same as above | Max value from the range query |
| `mimir.objstore_ops_per_day` | Object store operations | `GET /api/v1/query_range` | `sum(rate(thanos_objstore_bucket_operations_total[5m])) by (operation)` | Per-operation rates × 86400 |
| `mimir.retention_days` | Retention period | `GET /config` | `compactor_blocks_retention_period` parsed from YAML config | |
| `cost.monthly_usd` | Estimated infra cost | derived | storage × $/GB/mo + API ops × $/1k | Status: `estimated`; does not include compute (no kubectl) |

## Inventory

| Key | Source |
|-----|--------|
| `tenant_breakdown` | `sum by (user) (cortex_ingester_active_series)` + `sum by (user) (rate(cortex_distributor_received_samples_total[5m]))` |
| `replication_factor` | parsed from `/config` |
| `estimated_storage_gb` | ingestion rate × bytes/sample × retention |
| `cost_breakdown` | API ops cost + object storage cost |

## Auth

The collector supports:
- **Basic auth**: `--username` + `--password`
- **Bearer token**: `--bearer-token` or `MIMIR_BEARER_TOKEN` env var
- **Tenant header**: `--tenant` or `MIMIR_TENANT` env var (sets `X-Scope-OrgID`)
- **Extra headers**: `--header 'Key: value'` (repeatable)

## Usage

```bash
# Direct connection
uv run collectors/mimir/collect.py \
    --endpoint https://mimir-gateway:8080 \
    --output-dir ./discovery-output/mimir

# With auth
uv run collectors/mimir/collect.py \
    --endpoint https://mimir-gateway:8080 \
    --username admin --password secret \
    --tenant myorg \
    --output-dir ./discovery-output/mimir

# Re-derive summary from cached evidence
uv run collectors/mimir/collect.py \
    --report-only --output-dir ./discovery-output/mimir
```

## Local Testing

```bash
docker compose -f docker/compose.mimir.yml up -d
# Wait ~30s for Prometheus to scrape Mimir metrics
uv run collectors/mimir/collect.py \
    --endpoint http://localhost:8080 \
    --output-dir ./discovery-output/mimir
```

## Audit Notes

Ported from `oodlectl/mimir/collect_mimir_data.py` with these changes:

1. **Dropped `rich` dependency** — plain prints; agent-invoked.
2. **Adopted shared `lib/`** — `HttpClient` (retries, circuit breaker, throttling), `EvidenceWriter` (redaction), `SummaryWriter` (schema-validated output).
3. **PromQL queries preserved** — `cortex_ingester_active_series`, `cortex_distributor_received_samples_total`, `cortex_request_duration_seconds_count`, `thanos_objstore_bucket_operations_total`. Added fallback to `cortex_ingest_storage_reader_fetch_records_total` for Mimir's ingest storage path.
4. **Config parsing** — retention (`compactor_blocks_retention_period`) and replication factor extracted identically from `/config` YAML text.
5. **Cost estimation** — storage + API ops only (status: `estimated`). Original also estimated compute/disk/cross-AZ from kubectl data; that's deferred to when `lib/portforward.py` ships (M5).
6. **kubectl/k8s collection dropped** for now — the original collected pod resources, PVCs, and node AZs. Will be reintroduced as optional `--kube-context`/`--kube-namespace` flags.
7. **Every figure carries provenance** (method, source_api, query, evidence_files) as required by summary.schema.json.
8. **Gap handling** — auth failures, missing config fields, and empty query results all produce explicit gaps with remediation advice.
