"""Mock HTTP responses and ground-truth constants for promstack collector tests."""

# ---------------------------------------------------------------------------
# Prometheus variant
# ---------------------------------------------------------------------------

PROM_VERSION = "2.54.0"
PROM_ACTIVE_SERIES = 2_450_000
PROM_SAMPLES_PER_SEC = 85_000.0
PROM_SCRAPE_TARGETS = 340
PROM_RETENTION_DAYS = 15

PROM_BUILDINFO = {
    "status": "success",
    "data": {
        "version": PROM_VERSION,
        "revision": "abc123def",
        "branch": "HEAD",
        "buildUser": "root",
        "buildDate": "20240815-12:00:00",
        "goVersion": "go1.22.6",
    },
}

PROM_TSDB_STATUS = {
    "status": "success",
    "data": {
        "headStats": {
            "numSeries": PROM_ACTIVE_SERIES,
            "numLabelPairs": 34567,
            "chunkCount": 9800000,
            "minTime": 1718000000000,
            "maxTime": 1718700000000,
        },
        "seriesCountByMetricName": [
            {"name": "node_cpu_seconds_total", "value": 48000},
            {"name": "container_memory_working_set_bytes", "value": 35000},
            {"name": "kube_pod_info", "value": 28000},
            {"name": "node_network_receive_bytes_total", "value": 12000},
            {"name": "prometheus_tsdb_head_series", "value": 1},
        ],
        "labelValueCountByLabelName": [],
        "memoryInBytesByLabelName": [],
        "seriesCountByLabelValuePair": [],
    },
}

PROM_STATUS_FLAGS = {
    "status": "success",
    "data": {
        "storage.tsdb.retention.time": "15d",
        "storage.tsdb.path": "/prometheus",
        "web.listen-address": "0.0.0.0:9090",
        "web.enable-lifecycle": "true",
    },
}


def _instant_vector(value, metric=None):
    return {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [{"metric": metric or {}, "value": [1718700000, str(value)]}],
        },
    }


def _empty_vector():
    return {"status": "success", "data": {"resultType": "vector", "result": []}}


PROM_SAMPLES_RATE = _instant_vector(PROM_SAMPLES_PER_SEC)
PROM_SCRAPE_TARGETS_RESP = _instant_vector(PROM_SCRAPE_TARGETS)
PROM_BUILD_INFO_METRIC = _instant_vector(1, {"version": PROM_VERSION, "branch": "HEAD"})

EMPTY_PROBE = _empty_vector()


# ---------------------------------------------------------------------------
# VictoriaMetrics variant
# ---------------------------------------------------------------------------

VM_VERSION = "VictoriaMetrics-v1.103.0"
VM_ACTIVE_SERIES = 8_200_000
VM_SAMPLES_PER_SEC = 250_000.0
VM_SCRAPE_TARGETS = 1_200
VM_RETENTION_DAYS = 90

VM_BUILDINFO = {
    "status": "success",
    "data": {"version": VM_VERSION},
}

VM_TSDB_STATUS = {
    "status": "success",
    "data": {
        "totalSeries": VM_ACTIVE_SERIES,
        "totalLabelValuePairs": 890000,
        "seriesCountByMetricName": [
            {"name": "vm_rows_inserted_total", "value": 4},
            {"name": "node_cpu_seconds_total", "value": 96000},
            {"name": "container_memory_working_set_bytes", "value": 72000},
        ],
    },
}

VM_FLAGS = {
    "-retentionPeriod": "90d",
    "-storageDataPath": "/victoria-metrics-data",
    "-selfScrapeInterval": "15s",
}

VM_FLAGS_TEXT = """-retentionPeriod="90d"
-storageDataPath="/victoria-metrics-data"
-selfScrapeInterval="15s"
"""

VM_ACTIVE_SERIES_RESP = _instant_vector(VM_ACTIVE_SERIES)
VM_SAMPLES_RATE = _instant_vector(VM_SAMPLES_PER_SEC)
VM_SCRAPE_TARGETS_RESP = _instant_vector(VM_SCRAPE_TARGETS)
VM_APP_VERSION_PROBE = _instant_vector(1, {"version": "v1.103.0", "short_version": "v1.103.0"})


# ---------------------------------------------------------------------------
# Thanos variant
# ---------------------------------------------------------------------------

THANOS_VERSION = "0.36.1"
THANOS_ACTIVE_SERIES_RAW = 5_000_000
THANOS_RF = 1
THANOS_ACTIVE_SERIES = THANOS_ACTIVE_SERIES_RAW // THANOS_RF
THANOS_SAMPLES_PER_SEC = 150_000.0
THANOS_SCRAPE_TARGETS = 800

THANOS_BUILD_INFO_METRIC = _instant_vector(
    1, {"version": THANOS_VERSION, "revision": "abc123"}
)
THANOS_ACTIVE_SERIES_RESP = _instant_vector(THANOS_ACTIVE_SERIES_RAW)
THANOS_SAMPLES_RATE = _instant_vector(THANOS_SAMPLES_PER_SEC)
THANOS_SCRAPE_TARGETS_RESP = _instant_vector(THANOS_SCRAPE_TARGETS)
THANOS_RF_RESP = _empty_vector()  # RF metric not present (sidecar mode)

THANOS_RF3_ACTIVE_SERIES_RAW = 15_000_000  # 5M * RF3
THANOS_RF3_RESP = _instant_vector(3)
THANOS_RF3_ACTIVE_SERIES_RESP = _instant_vector(THANOS_RF3_ACTIVE_SERIES_RAW)


# ---------------------------------------------------------------------------
# Loki variant
# ---------------------------------------------------------------------------

LOKI_INGEST_GB_PER_DAY = 45.2
LOKI_INGEST_BYTES_PER_SEC = LOKI_INGEST_GB_PER_DAY * (1024 ** 3) / 86400
LOKI_RETENTION_DAYS = 30

LOKI_INGEST_RATE_RESP = _instant_vector(LOKI_INGEST_BYTES_PER_SEC)

LOKI_BUILDINFO = {
    "status": "success",
    "data": {"version": "3.0.0", "revision": "xyz789"},
}

LOKI_CONFIG_TEXT = """auth_enabled: false

server:
  http_listen_port: 3100

limits_config:
  retention_period: 720h
  max_query_length: 721h

compactor:
  working_directory: /loki/compactor
  retention_enabled: true
"""


# ---------------------------------------------------------------------------
# Tempo variant
# ---------------------------------------------------------------------------

TEMPO_SPANS_PER_SEC = 12_500.0
TEMPO_INGEST_GB_PER_DAY = 8.7
TEMPO_INGEST_BYTES_PER_SEC = TEMPO_INGEST_GB_PER_DAY * (1024 ** 3) / 86400

TEMPO_SPANS_RATE_RESP = _instant_vector(TEMPO_SPANS_PER_SEC)
TEMPO_INGEST_RATE_RESP = _instant_vector(TEMPO_INGEST_BYTES_PER_SEC)

TEMPO_BUILDINFO = {
    "version": "2.5.0",
    "revision": "abc123",
    "branch": "main",
    "goVersion": "go1.22.0",
}
