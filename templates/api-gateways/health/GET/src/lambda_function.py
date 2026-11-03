"""GET /health - liveness probe, no downstream calls."""

import os

from common.http import json_response


def handler(_event, _context):
    return json_response(200, {"status": "ok", "environment": os.environ.get("ENV_TYPE", "local")})
