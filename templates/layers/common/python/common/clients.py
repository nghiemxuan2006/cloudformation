"""Lazily created clients, reused across warm invocations.

Configuration comes from environment variables set in each function's
template.yml, so a function only needs the variables for what it uses.
"""

import json
import os
import ssl

import boto3

RDS_CA_BUNDLE = os.path.join(os.path.dirname(__file__), "rds-global-bundle.pem")

_clients = {}


def items_table():
    if "items_table" not in _clients:
        _clients["items_table"] = boto3.resource("dynamodb").Table(os.environ["ITEMS_TABLE_NAME"])
    return _clients["items_table"]


def s3():
    if "s3" not in _clients:
        _clients["s3"] = boto3.client("s3")
    return _clients["s3"]


def secrets():
    if "secrets" not in _clients:
        _clients["secrets"] = boto3.client("secretsmanager")
    return _clients["secrets"]


def cache():
    """Valkey (Redis protocol) client - TLS because TransitEncryptionEnabled=true."""
    if "cache" not in _clients:
        import redis

        _clients["cache"] = redis.Redis(
            host=os.environ["CACHE_HOST"],
            port=int(os.environ.get("CACHE_PORT", "6379")),
            ssl=True,
            socket_timeout=2,
            socket_connect_timeout=2,
            decode_responses=True,
        )
    return _clients["cache"]


def _db_credentials():
    if "db_secret" not in _clients:
        raw = secrets().get_secret_value(SecretId=os.environ["DB_SECRET_ARN"])
        _clients["db_secret"] = json.loads(raw["SecretString"])
    return _clients["db_secret"]


def db_query(sql, **params):
    """Run one statement on Aurora. One connection per call keeps the sample
    simple; put RDS Proxy in front of Aurora for production traffic."""
    import pg8000.native

    creds = _db_credentials()
    try:
        conn = pg8000.native.Connection(
            user=creds["username"],
            password=creds["password"],
            host=os.environ["DB_HOST"],
            port=int(os.environ.get("DB_PORT", "5432")),
            database=os.environ["DB_NAME"],
            ssl_context=ssl.create_default_context(cafile=RDS_CA_BUNDLE),
            timeout=5,
        )
    except Exception:
        _clients.pop("db_secret", None)  # the secret may have been rotated
        raise
    try:
        return conn.run(sql, **params)
    finally:
        conn.close()
