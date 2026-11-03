"""GET /items/{id} - read-through cache: Valkey first, then DynamoDB."""

import json
import logging
import os

from common import clients
from common.http import json_response

logger = logging.getLogger()


def handler(event, _context):
    item_id = (event.get("pathParameters") or {}).get("id")
    if not item_id:
        return json_response(400, {"message": "id is required"})

    key = f"item:{item_id}"
    try:
        cached = clients.cache().get(key)
        if cached:
            return json_response(200, {"source": "cache", "item": json.loads(cached)})
    except Exception:  # noqa: BLE001 - the cache is best effort
        logger.warning("cache unavailable, falling back to DynamoDB", exc_info=True)

    item = clients.items_table().get_item(Key={"id": item_id}).get("Item")
    if not item:
        return json_response(404, {"message": "item not found"})

    try:
        ttl = int(os.environ.get("CACHE_TTL_SECONDS", "300"))
        clients.cache().setex(key, ttl, json.dumps(item, default=str))
    except Exception:  # noqa: BLE001
        logger.warning("failed to populate cache", exc_info=True)
    return json_response(200, {"source": "dynamodb", "item": item})
