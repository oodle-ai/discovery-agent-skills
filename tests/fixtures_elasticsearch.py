"""Synthetic Elasticsearch API responses with known ground truth.

All values are fabricated; figures derived from them are asserted exactly in
tests (e.g. 3 data nodes, 150 active shards, 10 GB/day ingestion).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# ── ground-truth constants ──────────────────────────────────────────────

NUM_NODES = 3
NUM_DATA_NODES = 3
ACTIVE_SHARDS = 150
CLUSTER_NAME = "test-cluster"
ES_VERSION = "8.12.0"

# index data: 10 date-based indices over 5 days, each 2 GB primary
NUM_DATE_INDICES = 10
PRI_BYTES_PER_INDEX = 2_000_000_000  # 2 GB
REPLICAS = 1  # store.size = pri * (1 + replicas)
DOCS_PER_INDEX = 1_000_000
DAYS_WITH_DATA = 5
LOOKBACK_DAYS = 7

# derived ground truth
TOTAL_DATE_DOCS = NUM_DATE_INDICES * DOCS_PER_INDEX  # 10M
TOTAL_STORE_BYTES = NUM_DATE_INDICES * PRI_BYTES_PER_INDEX * (1 + REPLICAS)  # 40 GB
TOTAL_PRI_BYTES = NUM_DATE_INDICES * PRI_BYTES_PER_INDEX  # 20 GB
INGEST_GB_PER_DAY = TOTAL_PRI_BYTES / DAYS_WITH_DATA / 1e9  # 4.0 GB/day

# APM trace indices: 5 days, 1 index per day, 500 MB primary each, 500K spans each
NUM_APM_INDICES = 5
APM_PRI_BYTES_PER_INDEX = 500_000_000  # 500 MB
APM_DOCS_PER_INDEX = 500_000
APM_REPLICAS = 1
APM_DAYS_WITH_DATA = 5

APM_TOTAL_DOCS = NUM_APM_INDICES * APM_DOCS_PER_INDEX  # 2.5M spans
APM_TOTAL_STORE = NUM_APM_INDICES * APM_PRI_BYTES_PER_INDEX * (1 + APM_REPLICAS)  # 5 GB
APM_TOTAL_PRI = NUM_APM_INDICES * APM_PRI_BYTES_PER_INDEX  # 2.5 GB
APM_INGEST_GB_PER_DAY = APM_TOTAL_PRI / APM_DAYS_WITH_DATA / 1e9  # 0.5 GB/day
APM_SPANS_PER_DAY = APM_TOTAL_DOCS / APM_DAYS_WITH_DATA  # 500K spans/day

# system index (excluded from ingestion calc, included in totals)
SYSTEM_DOCS = 500
SYSTEM_STORE_BYTES = 50_000_000  # 50 MB
SYSTEM_PRI_BYTES = 25_000_000

GRAND_TOTAL_DOCS = TOTAL_DATE_DOCS + APM_TOTAL_DOCS + SYSTEM_DOCS
GRAND_TOTAL_STORE = TOTAL_STORE_BYTES + APM_TOTAL_STORE + SYSTEM_STORE_BYTES
GRAND_TOTAL_PRI = TOTAL_PRI_BYTES + APM_TOTAL_PRI + SYSTEM_PRI_BYTES


# ── helpers ─────────────────────────────────────────────────────────────


def _recent_dates(days: int = DAYS_WITH_DATA) -> list[str]:
    end = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return [
        (end - timedelta(days=d)).strftime("%Y.%m.%d") for d in range(days, 0, -1)
    ]


def _make_date_indices() -> list[dict]:
    dates = _recent_dates()
    indices = []
    idx_per_day = NUM_DATE_INDICES // DAYS_WITH_DATA
    for day_date in dates:
        for j in range(idx_per_day):
            name = f"logs-app-{day_date}" if j == 0 else f"metrics-infra-{day_date}"
            indices.append({
                "index": name,
                "health": "green",
                "status": "open",
                "pri": "5",
                "rep": str(REPLICAS),
                "docs.count": str(DOCS_PER_INDEX),
                "docs.deleted": "0",
                "store.size": str(PRI_BYTES_PER_INDEX * (1 + REPLICAS)),
                "pri.store.size": str(PRI_BYTES_PER_INDEX),
            })
    return indices


def _make_apm_indices() -> list[dict]:
    dates = _recent_dates(APM_DAYS_WITH_DATA)
    indices = []
    for day_date in dates:
        indices.append({
            "index": f"traces-apm-default-{day_date}",
            "health": "green",
            "status": "open",
            "pri": "2",
            "rep": str(APM_REPLICAS),
            "docs.count": str(APM_DOCS_PER_INDEX),
            "docs.deleted": "0",
            "store.size": str(APM_PRI_BYTES_PER_INDEX * (1 + APM_REPLICAS)),
            "pri.store.size": str(APM_PRI_BYTES_PER_INDEX),
        })
    return indices


def _make_system_index() -> dict:
    return {
        "index": ".kibana_8.12.0_001",
        "health": "green",
        "status": "open",
        "pri": "1",
        "rep": "1",
        "docs.count": str(SYSTEM_DOCS),
        "docs.deleted": "10",
        "store.size": str(SYSTEM_STORE_BYTES),
        "pri.store.size": str(SYSTEM_PRI_BYTES),
    }


# ── API response fixtures ──────────────────────────────────────────────


CLUSTER_HEALTH = {
    "cluster_name": CLUSTER_NAME,
    "status": "green",
    "number_of_nodes": NUM_NODES,
    "number_of_data_nodes": NUM_DATA_NODES,
    "active_primary_shards": 75,
    "active_shards": ACTIVE_SHARDS,
    "unassigned_shards": 0,
    "relocating_shards": 0,
    "initializing_shards": 0,
}

CLUSTER_STATS = {
    "cluster_name": CLUSTER_NAME,
    "cluster_uuid": "test-uuid-123",
    "nodes": {
        "count": {"total": NUM_NODES, "data": NUM_DATA_NODES},
        "versions": [ES_VERSION],
        "os": {"mem": {"total_in_bytes": 48_000_000_000}},
        "fs": {
            "total_in_bytes": 3_000_000_000_000,
            "available_in_bytes": 2_000_000_000_000,
        },
        "jvm": {"mem": {"heap_max_in_bytes": 24_000_000_000}},
    },
}


def _make_node(nid: str, name: str) -> dict:
    return {
        "name": name,
        "roles": ["data", "data_content", "data_hot", "ingest", "master"],
        "os": {
            "mem": {"total_in_bytes": 16_000_000_000, "used_percent": 72},
            "available_processors": 8,
        },
        "jvm": {
            "mem": {
                "heap_max_in_bytes": 8_000_000_000,
                "heap_used_in_bytes": 4_000_000_000,
            },
            "uptime_in_millis": 86_400_000,
            "start_time_in_millis": int(
                (datetime.now(UTC) - timedelta(days=1)).timestamp() * 1000
            ),
        },
        "fs": {
            "total": {
                "total_in_bytes": 1_000_000_000_000,
                "available_in_bytes": 600_000_000_000,
            },
            "data": [
                {
                    "path": "/data/es",
                    "mount": "/data",
                    "type": "ext4",
                    "total_in_bytes": 1_000_000_000_000,
                    "available_in_bytes": 600_000_000_000,
                }
            ],
        },
        "indices": {
            "search": {
                "query_total": 50000,
                "query_time_in_millis": 250000,
                "fetch_total": 30000,
                "fetch_time_in_millis": 90000,
                "scroll_total": 1000,
                "scroll_time_in_millis": 5000,
                "suggest_total": 0,
                "suggest_time_in_millis": 0,
            }
        },
    }


NODES_STATS = {
    "_nodes": {"total": NUM_NODES, "successful": NUM_NODES},
    "nodes": {
        f"node_{i}": _make_node(f"node_{i}", f"es-data-{i}")
        for i in range(NUM_NODES)
    },
}

NODES_INFO = {
    "_nodes": {"total": NUM_NODES, "successful": NUM_NODES},
    "nodes": {
        f"node_{i}": {
            "name": f"es-data-{i}",
            "roles": ["data", "data_content", "data_hot", "ingest", "master"],
            "os": {"available_processors": 8},
            "jvm": {"version": "21.0.1"},
        }
        for i in range(NUM_NODES)
    },
}


def cat_indices() -> list[dict]:
    return _make_date_indices() + _make_apm_indices() + [_make_system_index()]


def index_settings() -> dict:
    """Settings with creation_date for all indices."""
    indices = cat_indices()
    now = datetime.now(UTC)
    settings = {}
    for idx in indices:
        name = idx["index"]
        # creation date = date parsed from name, or now for system index
        date_str = None
        import re
        m = re.search(r"(\d{4})\.(\d{2})\.(\d{2})", name)
        if m:
            date_str = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
            dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC)
        else:
            dt = now - timedelta(days=30)
        ts_ms = int(dt.timestamp() * 1000)
        settings[name] = {
            "settings": {"index": {"creation_date": str(ts_ms)}}
        }
    return settings


def _per_index_search_stats() -> dict:
    """Per-index search stats keyed by index name, for /_stats indices block."""
    dates = _recent_dates()
    indices = {}
    for day_date in dates:
        for name in (f"logs-app-{day_date}", f"metrics-infra-{day_date}"):
            indices[name] = {
                "total": {
                    "search": {
                        "query_total": 10000,
                        "query_time_in_millis": 50000,
                        "fetch_total": 6000,
                        "fetch_time_in_millis": 18000,
                    }
                }
            }
    for day_date in _recent_dates(APM_DAYS_WITH_DATA):
        indices[f"traces-apm-default-{day_date}"] = {
            "total": {
                "search": {
                    "query_total": 2000,
                    "query_time_in_millis": 4000,
                    "fetch_total": 1000,
                    "fetch_time_in_millis": 2000,
                }
            }
        }
    return indices


def cluster_index_stats() -> dict:
    return {
        "_all": {
            "total": {
                "search": {
                    "query_total": 150000,
                    "query_time_in_millis": 750000,
                    "fetch_total": 90000,
                    "fetch_time_in_millis": 270000,
                    "scroll_total": 3000,
                    "scroll_time_in_millis": 15000,
                    "suggest_total": 0,
                    "suggest_time_in_millis": 0,
                }
            }
        },
        "indices": _per_index_search_stats(),
    }

ILM_POLICIES = {
    "logs-policy": {
        "policy": {
            "phases": {
                "hot": {
                    "min_age": "0ms",
                    "actions": {
                        "rollover": {"max_size": "50gb", "max_age": "1d"},
                    },
                },
                "warm": {"min_age": "7d", "actions": {"shrink": {"number_of_shards": 1}}},
                "delete": {"min_age": "30d", "actions": {"delete": {}}},
            }
        }
    },
    "metrics-policy": {
        "policy": {
            "phases": {
                "hot": {
                    "min_age": "0ms",
                    "actions": {"rollover": {"max_age": "7d"}},
                },
                "delete": {"min_age": "90d", "actions": {"delete": {}}},
            }
        }
    },
}

SNAPSHOT_REPOS = {
    "s3-backups": {"type": "s3", "settings": {"bucket": "my-backups"}},
}

SNAPSHOT_DETAILS = [
    {
        "repository": "s3-backups",
        "snapshots": {
            "snapshots": [
                {"snapshot": "daily-2026.06.16", "state": "SUCCESS"},
                {"snapshot": "daily-2026.06.15", "state": "SUCCESS"},
            ]
        },
    }
]

NODES_USAGE = {
    "_nodes": {"total": NUM_NODES, "successful": NUM_NODES},
    "nodes": {
        f"node_{i}": {
            "rest_actions": {
                "nodes:data:read/search[phase/query]": 50000,
                "indices:data/write/bulk[s]": 30000,
                "cluster:monitor/health": 10000,
                "indices:admin/refresh[s]": 5000,
            }
        }
        for i in range(NUM_NODES)
    },
}

KIBANA_SAVED_OBJECTS_DASHBOARD = {
    "total": 3,
    "saved_objects": [
        {"id": f"dash-{i}", "type": "dashboard", "attributes": {"title": f"Dashboard {i}"}}
        for i in range(3)
    ],
}

KIBANA_SAVED_OBJECTS_VISUALIZATION = {
    "total": 5,
    "saved_objects": [
        {"id": f"vis-{i}", "type": "visualization", "attributes": {"title": f"Vis {i}"}}
        for i in range(5)
    ],
}

KIBANA_DATA_VIEWS_LIST = {
    "data_view": [
        {"id": "dv-1", "title": "logs-*", "name": "Logs"},
        {"id": "dv-2", "title": "metrics-*", "name": "Metrics"},
    ]
}

KIBANA_DATA_VIEW_DETAIL_1 = {
    "data_view": {
        "id": "dv-1",
        "title": "logs-*",
        "name": "Logs",
        "timeFieldName": "@timestamp",
    }
}

KIBANA_DATA_VIEW_DETAIL_2 = {
    "data_view": {
        "id": "dv-2",
        "title": "metrics-*",
        "name": "Metrics",
        "timeFieldName": "@timestamp",
    }
}


def slowlog_settings() -> dict:
    """Slow log thresholds configured on one index pattern."""
    dates = _recent_dates()
    settings = {}
    for day_date in dates:
        settings[f"logs-app-{day_date}"] = {
            "settings": {
                "index": {
                    "search": {
                        "slowlog": {
                            "threshold": {
                                "query": {"warn": "10s", "info": "5s"},
                                "fetch": {"warn": "1s"},
                            }
                        }
                    }
                }
            }
        }
    return settings


MONITORING_INDICES = [
    {
        "index": ".monitoring-es-7-2026.06.17",
        "docs.count": "500000",
        "store.size": "250000000",
    },
    {
        "index": ".monitoring-es-7-2026.06.16",
        "docs.count": "480000",
        "store.size": "240000000",
    },
]
