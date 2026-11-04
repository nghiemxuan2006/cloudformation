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


# ---- POST /jobs ------------------------------------------------------------
def test_jobs_post_requires_type():
    res = load_function("jobs/POST").handler({"body": json.dumps({"payload": {}})}, None)
    assert res["statusCode"] == 400


def test_jobs_post_sends_message_to_queue(monkeypatch, fake_clients):
    sqs = MagicMock()
    sqs.send_message.return_value = {"MessageId": "m-1"}
    fake_clients["sqs"] = sqs
    monkeypatch.setenv("JOBS_QUEUE_URL", "https://sqs/queue")

    res = load_function("jobs/POST").handler(
        {"body": json.dumps({"type": "report", "payload": {"month": 10}})}, None
    )

    assert res["statusCode"] == 202
    body = _body(res)
    assert body["status"] == "QUEUED"
    sent = sqs.send_message.call_args.kwargs
    assert sent["QueueUrl"] == "https://sqs/queue"
    message = json.loads(sent["MessageBody"])
    assert message["jobId"] == body["jobId"]
    assert message["payload"] == {"month": 10}


# ---- process-job (SQS trigger) ---------------------------------------------
def _sqs_event(*bodies):
    return {"Records": [{"messageId": f"m-{i}", "body": b} for i, b in enumerate(bodies)]}


def test_process_job_writes_result_to_s3(monkeypatch, fake_clients):
    from conftest import FUNCTIONS_DIR

    s3 = MagicMock()
    fake_clients["s3"] = s3
    monkeypatch.setenv("DATA_BUCKET_NAME", "bucket")

    event = _sqs_event(json.dumps({"jobId": "j-1", "type": "report"}))
    res = load_function("process-job", base=FUNCTIONS_DIR).handler(event, None)

    assert res == {"batchItemFailures": []}
    put = s3.put_object.call_args.kwargs
    assert put["Key"] == "jobs/j-1.json"
    assert json.loads(put["Body"])["status"] == "DONE"


def test_process_job_reports_only_failed_records(monkeypatch, fake_clients):
    from conftest import FUNCTIONS_DIR

    fake_clients["s3"] = MagicMock()
    monkeypatch.setenv("DATA_BUCKET_NAME", "bucket")

    event = _sqs_event(json.dumps({"jobId": "ok", "type": "report"}), "{not json", "{}")
    res = load_function("process-job", base=FUNCTIONS_DIR).handler(event, None)

    assert res == {"batchItemFailures": [{"itemIdentifier": "m-1"}, {"itemIdentifier": "m-2"}]}
