from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "collectors"))
sys.path.insert(0, str(REPO_ROOT / "report"))


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="session")
def datadog_collect():
    return load_module("datadog_collect", REPO_ROOT / "collectors" / "datadog" / "collect.py")


@pytest.fixture(scope="session")
def report_gen():
    return load_module("generate_report", REPO_ROOT / "report" / "generate_report.py")


@pytest.fixture(scope="session")
def summary_schema():
    return json.loads((REPO_ROOT / "schemas" / "summary.schema.json").read_text())


@pytest.fixture(scope="session")
def context_schema():
    return json.loads((REPO_ROOT / "schemas" / "context.schema.json").read_text())


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Collectors throttle politely in production; tests shouldn't wait."""
    import lib.http as lib_http

    monkeypatch.setattr(lib_http.time, "sleep", lambda s: None)
