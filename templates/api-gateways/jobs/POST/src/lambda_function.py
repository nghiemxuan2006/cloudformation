"""POST /jobs - enqueue a job on SQS; process-job handles it asynchronously."""

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
    if not isinstance(payload.get("type"), str) or not payload["type"]:
        return json_response(400, {"message": "'type' is required"})

    job = {
        "jobId": str(uuid.uuid4()),
        "type": payload["type"],
        "payload": payload.get("payload", {}),
        "requestedAt": datetime.now(timezone.utc).isoformat(),
    }
    res = clients.sqs().send_message(
        QueueUrl=os.environ["JOBS_QUEUE_URL"],
        MessageBody=json.dumps(job),
        MessageAttributes={"type": {"DataType": "String", "StringValue": job["type"]}},
    )

    logger.info("job queued", extra={"jobId": job["jobId"], "messageId": res["MessageId"]})
    return json_response(202, {"jobId": job["jobId"], "status": "QUEUED"})
