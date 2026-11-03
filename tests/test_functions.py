import json
from unittest.mock import MagicMock

from conftest import load_function


def _body(res):
    return json.loads(res["body"])


# ---- GET /health -----------------------------------------------------------
def test_health_get(monkeypatch):
    monkeypatch.setenv("ENV_TYPE", "dev")
    res = load_function("health/GET").handler({}, None)
    assert res["statusCode"] == 200
    assert _body(res) == {"status": "ok", "environment": "dev"}


# ---- GET /health/deps ------------------------------------------------------
def test_health_deps_reports_failures(monkeypatch, tmp_path, fake_clients):
    from common import clients

    fake_clients["cache"] = MagicMock()
    monkeypatch.setattr(clients, "db_query", MagicMock(side_effect=ConnectionError()))
    monkeypatch.setenv("EFS_MOUNT_PATH", str(tmp_path))

    res = load_function("health/deps/GET").handler({}, None)

    body = _body(res)
    assert res["statusCode"] == 503
    assert body["checks"]["aurora"]["ok"] is False
    assert body["checks"]["cache"]["ok"] is True
    assert body["checks"]["efs"]["ok"] is True


# ---- POST /items -----------------------------------------------------------
def test_items_post_rejects_invalid_json():
    res = load_function("items/POST").handler({"body": "{not json"}, None)
    assert res["statusCode"] == 400


def test_items_post_writes_dynamodb_s3_and_efs(monkeypatch, tmp_path, fake_clients):
    table, s3 = MagicMock(), MagicMock()
    fake_clients.update(items_table=table, s3=s3)
    monkeypatch.setenv("DATA_BUCKET_NAME", "bucket")
    monkeypatch.setenv("EFS_MOUNT_PATH", str(tmp_path))

    res = load_function("items/POST").handler({"body": json.dumps({"name": "book"})}, None)

    assert res["statusCode"] == 201
    item = _body(res)
    table.put_item.assert_called_once()
    assert s3.put_object.call_args.kwargs["Key"] == f"items/{item['id']}.json"
    assert item["id"] in (tmp_path / "audit.log").read_text()


# ---- GET /items/{id} -------------------------------------------------------
def test_items_get_served_from_cache(fake_clients):
    cache, table = MagicMock(), MagicMock()
    cache.get.return_value = json.dumps({"id": "abc"})
    fake_clients.update(cache=cache, items_table=table)

    res = load_function("items/{id}/GET").handler({"pathParameters": {"id": "abc"}}, None)

    assert _body(res)["source"] == "cache"
    table.get_item.assert_not_called()


def test_items_get_falls_back_to_dynamodb_and_fills_cache(fake_clients):
    cache, table = MagicMock(), MagicMock()
    cache.get.return_value = None
    table.get_item.return_value = {"Item": {"id": "abc"}}
    fake_clients.update(cache=cache, items_table=table)

    res = load_function("items/{id}/GET").handler({"pathParameters": {"id": "abc"}}, None)

    assert _body(res)["source"] == "dynamodb"
    cache.setex.assert_called_once()


def test_items_get_not_found(fake_clients):
    cache, table = MagicMock(), MagicMock()
    cache.get.return_value = None
    table.get_item.return_value = {}
    fake_clients.update(cache=cache, items_table=table)

    res = load_function("items/{id}/GET").handler({"pathParameters": {"id": "nope"}}, None)

    assert res["statusCode"] == 404
