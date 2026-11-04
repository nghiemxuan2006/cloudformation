"""process-job - triggered by the jobs SQS queue.

Each record is processed on its own. Failed records are returned in
``batchItemFailures`` so SQS retries only them; after JobsMaxReceiveCount
attempts a message moves to the dead-letter queue.
"""

import json
import logging
import os
from datetime import datetime, timezone

from common import clients

logger = logging.getLogger()


def handler(event, _context):
    failures = []
    for record in event.get("Records", []):
        try:
            process(json.loads(record["body"]))
        except Exception:
            logger.exception("job failed", extra={"messageId": record["messageId"]})
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}


def process(job):
    """Business logic goes here. It must be idempotent: SQS delivers at least
    once, so the same job can arrive twice - writing to a key derived from the
    jobId makes a duplicate overwrite instead of duplicate."""
    job_id = job["jobId"]
    result = {
        **job,
        "status": "DONE",
        "processedAt": datetime.now(timezone.utc).isoformat(),
    }
    clients.s3().put_object(
        Bucket=os.environ["DATA_BUCKET_NAME"],
        Key=f"jobs/{job_id}.json",
        Body=json.dumps(result).encode(),
        ContentType="application/json",
    )
    logger.info("job done", extra={"jobId": job_id, "type": job.get("type")})
