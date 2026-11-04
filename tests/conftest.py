"""Test helpers.

Every function's entry point is called ``lambda_function.py`` (one per
templates/api-gateways/<path>/<METHOD>/src and templates/lambda-functions/<name>/src),
so they are loaded under unique
module names instead of being imported normally. The shared "common" layer is
put on sys.path exactly like Lambda does with /opt/python.
"""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
API_DIR = ROOT / "templates" / "api-gateways"
FUNCTIONS_DIR = ROOT / "templates" / "lambda-functions"
sys.path.insert(0, str(ROOT / "templates" / "layers" / "common" / "python"))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")


def load_function(rel_path, base=API_DIR):
    """load_function("items/{id}/GET") -> module of its src/lambda_function.py
    load_function("process-job", base=FUNCTIONS_DIR) for non-API functions."""
    path = base / rel_path / "src" / "lambda_function.py"
    name = f"fn_{base.name}_" + rel_path.replace("/", "_").replace("{", "").replace("}", "").lower()
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_clients(monkeypatch):
    """Replace the shared clients cache with mocks the test provides."""
    from common import clients

    store = {}
    monkeypatch.setattr(clients, "_clients", store)
    return store
