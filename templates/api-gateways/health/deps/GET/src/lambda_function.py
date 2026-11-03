"""GET /health/deps - check Aurora, ElastiCache and EFS connectivity."""

import logging
import os
import time

from common import clients
from common.http import json_response

logger = logging.getLogger()


def _check(name, fn, results):
    started = time.perf_counter()
    try:
        fn()
        results[name] = {"ok": True}
    except Exception as exc:  # noqa: BLE001 - report any failure
        logger.exception("dependency check failed: %s", name)
        results[name] = {"ok": False, "error": type(exc).__name__}
    results[name]["ms"] = round((time.perf_counter() - started) * 1000, 1)


def handler(_event, _context):
    results = {}
    _check("aurora", lambda: clients.db_query("SELECT 1"), results)
    _check("cache", lambda: clients.cache().ping(), results)
    _check("efs", lambda: os.listdir(os.environ.get("EFS_MOUNT_PATH", "/mnt/efs")), results)

    healthy = all(r["ok"] for r in results.values())
    return json_response(200 if healthy else 503, {"healthy": healthy, "checks": results})
