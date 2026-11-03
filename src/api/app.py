"""API Lambda - demonstrates every dependency in the topology.

Routes (HTTP API payload v2):
  GET  /health        liveness, no downstream calls
  GET  /health/deps   checks Aurora, ElastiCache and EFS
  POST /items         write item -> DynamoDB + S3 + EFS audit log
  GET  /items/{id}    read-through cache: ElastiCache -> DynamoDB
"""

import json
import logging
import os
import ssl
import time
import uuid
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()

ENVIRONMENT = os.environ.get("ENVIRONMENT", "local")
ITEMS_TABLE_NAME = os.environ.get("ITEMS_TABLE_NAME", "")
DATA_BUCKET_NAME = os.environ.get("DATA_BUCKET_NAME", "")
EFS_MOUNT_PATH = os.environ.get("EFS_MOUNT_PATH", "/mnt/efs")
DB_HOST = os.environ.get("DB_HOST", "")
DB_PORT = int(os.environ.get("DB_PORT", "5432"))
DB_NAME = os.environ.get("DB_NAME", "")
DB_SECRET_ARN = os.environ.get("DB_SECRET_ARN", "")
CACHE_HOST = os.environ.get("CACHE_HOST", "")
CACHE_PORT = int(os.environ.get("CACHE_PORT", "6379"))
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "300"))
RDS_CA_BUNDLE = os.path.join(os.path.dirname(__file__), "rds-global-bundle.pem")

# Clients are created lazily and reused across warm invocations.
_clients = {}


def _client(name):
    if name not in _clients:
        if name == "dynamodb":
            _clients[name] = boto3.resource("dynamodb").Table(ITEMS_TABLE_NAME)
        else:
            _clients[name] = boto3.client(name)
    return _clients[name]


def _cache():
    if "cache" not in _clients:
        import redis  # Valkey speaks the Redis protocol

        _clients["cache"] = redis.Redis(
            host=CACHE_HOST,
            port=CACHE_PORT,
            ssl=True,  # TransitEncryptionEnabled=true
            socket_timeout=2,
            socket_connect_timeout=2,
            decode_responses=True,
        )
    return _clients["cache"]


def _db_credentials():
    if "db_secret" not in _clients:
        raw = _client("secretsmanager").get_secret_value(SecretId=DB_SECRET_ARN)
        _clients["db_secret"] = json.loads(raw["SecretString"])
    return _clients["db_secret"]


def _db_query(sql):
    # One connection per call keeps the sample simple; use RDS Proxy in prod.
    import pg8000.native

    creds = _db_credentials()
    try:
        conn = pg8000.native.Connection(
            user=creds["username"],
            password=creds["password"],
            host=DB_HOST,
            port=DB_PORT,
            database=DB_NAME,
            ssl_context=ssl.create_default_context(cafile=RDS_CA_BUNDLE),
            timeout=5,
        )
    except Exception:
        _clients.pop("db_secret", None)  # secret may have been rotated
        raise
    try:
        return conn.run(sql)
    finally:
        conn.close()


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


# --------------------------------------------------------------------------
# Route handlers
# --------------------------------------------------------------------------
def health(_event):
    return _response(200, {"status": "ok", "environment": ENVIRONMENT})


def health_deps(_event):
    checks = {}

    def run(name, fn):
        started = time.perf_counter()
        try:
            fn()
            checks[name] = {"ok": True}
        except Exception as exc:  # noqa: BLE001 - report any failure
            logger.exception("dependency check failed: %s", name)
            checks[name] = {"ok": False, "error": type(exc).__name__}
        checks[name]["ms"] = round((time.perf_counter() - started) * 1000, 1)

    run("aurora", lambda: _db_query("SELECT 1"))
    run("cache", lambda: _cache().ping())
    run("efs", lambda: os.listdir(EFS_MOUNT_PATH))
    healthy = all(c["ok"] for c in checks.values())
    return _response(200 if healthy else 503, {"healthy": healthy, "checks": checks})


def create_item(event):
    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _response(400, {"message": "body must be valid JSON"})
    if not isinstance(payload, dict):
        return _response(400, {"message": "body must be a JSON object"})

    item = {
        **payload,
        "id": str(uuid.uuid4()),
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    _client("dynamodb").put_item(Item=item)
    _client("s3").put_object(
        Bucket=DATA_BUCKET_NAME,
        Key=f"items/{item['id']}.json",
        Body=json.dumps(item).encode(),
        ContentType="application/json",
    )
    with open(os.path.join(EFS_MOUNT_PATH, "audit.log"), "a", encoding="utf-8") as fh:
        fh.write(f"{item['createdAt']} CREATE {item['id']}\n")
    return _response(201, item)


def get_item(_event, item_id):
    key = f"item:{item_id}"
    try:
        cached = _cache().get(key)
        if cached:
            return _response(200, {"source": "cache", "item": json.loads(cached)})
    except Exception:  # noqa: BLE001 - cache is best effort
        logger.warning("cache unavailable, falling back to DynamoDB", exc_info=True)

    item = _client("dynamodb").get_item(Key={"id": item_id}).get("Item")
    if not item:
        return _response(404, {"message": "item not found"})
    try:
        _cache().setex(key, CACHE_TTL_SECONDS, json.dumps(item, default=str))
    except Exception:  # noqa: BLE001
        logger.warning("failed to populate cache", exc_info=True)
    return _response(200, {"source": "dynamodb", "item": item})


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------
def handler(event, _context):
    method = event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("rawPath", "/").rstrip("/") or "/"
    logger.info("request", extra={"method": method, "path": path})

    try:
        if method == "GET" and path == "/health":
            return health(event)
        if method == "GET" and path == "/health/deps":
            return health_deps(event)
        if method == "POST" and path == "/items":
            return create_item(event)
        if method == "GET" and path.startswith("/items/"):
            item_id = path.split("/", 2)[2]
            if item_id and "/" not in item_id:
                return get_item(event, item_id)
        return _response(404, {"message": f"route {method} {path} not found"})
    except Exception:  # noqa: BLE001
        logger.exception("unhandled error")
        return _response(500, {"message": "internal server error"})
