"""POST /items - create an item in DynamoDB, copy it to S3, audit to EFS."""

import json
import logging
import os
import uuid
from datetime import datetime, timezone

from common import clients
from common.http import json_response, parse_json_body

logger = logging.getLogger()


def handler(event, _context):
    payload, error = parse_json_body(event)
    if error:
        return error

    item = {
        **payload,
        "id": str(uuid.uuid4()),
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }

    clients.items_table().put_item(Item=item)
    clients.s3().put_object(
        Bucket=os.environ["DATA_BUCKET_NAME"],
        Key=f"items/{item['id']}.json",
        Body=json.dumps(item).encode(),
        ContentType="application/json",
    )
    audit_log = os.path.join(os.environ.get("EFS_MOUNT_PATH", "/mnt/efs"), "audit.log")
    with open(audit_log, "a", encoding="utf-8") as fh:
        fh.write(f"{item['createdAt']} CREATE {item['id']}\n")

    logger.info("item created", extra={"itemId": item["id"]})
    return json_response(201, item)
