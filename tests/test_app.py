import json
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "api"))
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import app  # noqa: E402


def _event(method, path, body=None):
    return {"requestContext": {"http": {"method": method}}, "rawPath": path, "body": body}


def test_health():
    res = app.handler(_event("GET", "/health"), None)
    assert res["statusCode"] == 200
    assert json.loads(res["body"])["status"] == "ok"


def test_unknown_route_returns_404():
    res = app.handler(_event("DELETE", "/nope"), None)
    assert res["statusCode"] == 404


def test_create_item_rejects_invalid_json():
    res = app.handler(_event("POST", "/items", "{not json"), None)
    assert res["statusCode"] == 400


def test_create_item_writes_dynamodb_s3_and_efs(tmp_path, monkeypatch):
    table, s3 = MagicMock(), MagicMock()
    monkeypatch.setattr(app, "_clients", {"dynamodb": table, "s3": s3})
    monkeypatch.setattr(app, "EFS_MOUNT_PATH", str(tmp_path))

    res = app.handler(_event("POST", "/items", json.dumps({"name": "book"})), None)

    assert res["statusCode"] == 201
    item = json.loads(res["body"])
    table.put_item.assert_called_once()
    s3.put_object.assert_called_once()
    assert item["id"] in (tmp_path / "audit.log").read_text()


def test_get_item_served_from_cache(monkeypatch):
    cache = MagicMock()
    cache.get.return_value = json.dumps({"id": "abc"})
    table = MagicMock()
    monkeypatch.setattr(app, "_clients", {"cache": cache, "dynamodb": table})

    res = app.handler(_event("GET", "/items/abc"), None)

    assert res["statusCode"] == 200
    assert json.loads(res["body"])["source"] == "cache"
    table.get_item.assert_not_called()


def test_get_item_falls_back_to_dynamodb_and_populates_cache(monkeypatch):
    cache = MagicMock()
    cache.get.return_value = None
    table = MagicMock()
    table.get_item.return_value = {"Item": {"id": "abc"}}
    monkeypatch.setattr(app, "_clients", {"cache": cache, "dynamodb": table})

    res = app.handler(_event("GET", "/items/abc"), None)

    assert json.loads(res["body"])["source"] == "dynamodb"
    cache.setex.assert_called_once()
