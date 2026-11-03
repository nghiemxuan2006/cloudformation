"""Helpers for API Gateway (REST, Lambda proxy integration) responses."""

import json


def json_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }


def parse_json_body(event):
    """Return (payload, error_response). Exactly one of them is None."""
    try:
        payload = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return None, json_response(400, {"message": "body must be valid JSON"})
    if not isinstance(payload, dict):
        return None, json_response(400, {"message": "body must be a JSON object"})
    return payload, None
