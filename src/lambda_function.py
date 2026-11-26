"""AWS Lambda handler for the Serverless Inventory API (Amazon S3 storage backend)."""

import base64
import json
import os
from typing import Any
from urllib.parse import unquote
import uuid

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - local unit tests inject a mock client
    boto3 = None

    class ClientError(Exception):
        """Fallback ClientError when botocore is unavailable locally."""

        def __init__(self, error_response: dict[str, Any], operation_name: str = ""):
            super().__init__(str(error_response))
            self.response = error_response
            self.operation_name = operation_name


DEFAULT_BUCKET_NAME = "inventory-items-992382537395-us-east-1"
DEFAULT_ITEMS_PREFIX = "items/"
_CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}
_s3_client: Any = None


def get_bucket_name() -> str:
    """Return the configured S3 bucket name."""
    return os.environ.get("BUCKET_NAME", DEFAULT_BUCKET_NAME)


def get_items_prefix() -> str:
    """Return the normalized S3 key prefix ending with '/' if non-empty."""
    prefix = os.environ.get("ITEMS_PREFIX", DEFAULT_ITEMS_PREFIX).lstrip("/")
    if prefix and not prefix.endswith("/"):
        prefix = f"{prefix}/"
    return prefix


def get_s3_client() -> Any:
    """Return a cached boto3 S3 client."""
    global _s3_client
    if _s3_client is None:
        if boto3 is None:
            raise RuntimeError("boto3 is required to connect to Amazon S3")
        _s3_client = boto3.client("s3")
    return _s3_client


def response(status_code: int, body: Any = None) -> dict[str, Any]:
    """Build an HTTP Lambda Function URL / API Gateway v2.0 JSON response."""
    return {
        "statusCode": status_code,
        "headers": dict(_CORS_HEADERS),
        "body": "" if body is None else json.dumps(body),
    }


def extract_method_and_path(event: dict[str, Any]) -> tuple[str, str]:
    """Extract normalized HTTP method and path from Lambda Function URL or API GW event."""
    http_ctx = event.get("requestContext", {}).get("http", {})
    method = (http_ctx.get("method") or event.get("httpMethod") or "").upper()
    raw_path = event.get("rawPath") or http_ctx.get("path") or event.get("path") or "/"
    if len(raw_path) > 1 and raw_path.endswith("/"):
        raw_path = raw_path.rstrip("/") or "/"
    return method, raw_path


def parse_json_body(event: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Parse and validate a JSON object request body."""
    raw_body = event.get("body")
    if raw_body is None or raw_body == "":
        return None, "Request body is required"
    if isinstance(raw_body, dict):
        return raw_body, None
    if event.get("isBase64Encoded"):
        try:
            raw_body = base64.b64decode(raw_body).decode("utf-8")
        except Exception:
            return None, "Invalid base64-encoded request body"
    try:
        parsed = json.loads(raw_body)
    except (TypeError, json.JSONDecodeError):
        return None, "Request body must be valid JSON"
    if not isinstance(parsed, dict):
        return None, "Request body must be a JSON object"
    return parsed, None


def _is_valid_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_valid_item_id(item_id: str) -> bool:
    return bool(item_id) and "/" not in item_id and "\\" not in item_id and ".." not in item_id


def _item_key(item_id: str) -> str:
    return f"{get_items_prefix()}{item_id}.json"


def _is_not_found_error(exc: ClientError) -> bool:
    resp = getattr(exc, "response", {}) or {}
    code = str(resp.get("Error", {}).get("Code", ""))
    status = resp.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in ("NoSuchKey", "404", "NotFound") or status == 404


def _read_item_from_s3(s3: Any, bucket: str, key: str) -> dict[str, Any]:
    obj = s3.get_object(Bucket=bucket, Key=key)
    raw = obj["Body"].read()
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def _write_item_to_s3(s3: Any, bucket: str, key: str, item: dict[str, Any]) -> None:
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(item).encode("utf-8"),
        ContentType="application/json",
    )


def handle_create_item(event: dict[str, Any], s3: Any, bucket: str) -> dict[str, Any]:
    """Handle POST /items."""
    data, err = parse_json_body(event)
    if err or data is None:
        return response(400, {"error": err})

    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        return response(400, {"error": "Field 'name' is required and must be a non-empty string"})

    stock = data.get("stock", 0)
    if stock is None:
        stock = 0
    if not _is_valid_non_negative_int(stock):
        return response(400, {"error": "Field 'stock' must be a non-negative integer"})

    location = data.get("location", "")
    if location is None:
        location = ""
    if not isinstance(location, str):
        return response(400, {"error": "Field 'location' must be a string"})

    item = {
        "itemId": str(uuid.uuid4()),
        "name": name.strip(),
        "stock": stock,
        "location": location.strip(),
    }
    _write_item_to_s3(s3, bucket, _item_key(item["itemId"]), item)
    return response(201, item)


def handle_list_items(s3: Any, bucket: str) -> dict[str, Any]:
    """Handle GET /items with paginated S3 object listing."""
    prefix = get_items_prefix()
    keys: list[str] = []
    list_kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}

    while True:
        res = s3.list_objects_v2(**list_kwargs)
        for obj in res.get("Contents", []):
            key = obj.get("Key", "")
            if key.endswith(".json"):
                keys.append(key)
        if not res.get("IsTruncated"):
            break
        token = res.get("NextContinuationToken")
        if not token:
            break
        list_kwargs["ContinuationToken"] = token

    items: list[dict[str, Any]] = []
    for key in sorted(keys):
        try:
            items.append(_read_item_from_s3(s3, bucket, key))
        except ClientError as exc:
            if _is_not_found_error(exc):
                continue
            raise

    items.sort(key=lambda entry: str(entry.get("itemId", "")))
    return response(200, {"items": items, "count": len(items)})


def handle_get_item(item_id: str, s3: Any, bucket: str) -> dict[str, Any]:
    """Handle GET /items/{id}."""
    try:
        item = _read_item_from_s3(s3, bucket, _item_key(item_id))
    except ClientError as exc:
        if _is_not_found_error(exc):
            return response(404, {"error": "Item not found", "itemId": item_id})
        raise
    return response(200, item)


def handle_update_item(item_id: str, event: dict[str, Any], s3: Any, bucket: str) -> dict[str, Any]:
    """Handle PUT /items/{id}."""
    data, err = parse_json_body(event)
    if err or data is None:
        return response(400, {"error": err})

    updates: dict[str, Any] = {}

    if "name" in data:
        name = data["name"]
        if not isinstance(name, str) or not name.strip():
            return response(400, {"error": "Field 'name' must be a non-empty string"})
        updates["name"] = name.strip()

    if "stock" in data:
        stock = data["stock"]
        if not _is_valid_non_negative_int(stock):
            return response(400, {"error": "Field 'stock' must be a non-negative integer"})
        updates["stock"] = stock

    if "location" in data:
        location = data["location"]
        if not isinstance(location, str):
            return response(400, {"error": "Field 'location' must be a string"})
        updates["location"] = location.strip()

    if not updates:
        return response(
            400,
            {"error": "At least one updatable field ('name', 'stock', 'location') is required"},
        )

    key = _item_key(item_id)
    try:
        item = _read_item_from_s3(s3, bucket, key)
    except ClientError as exc:
        if _is_not_found_error(exc):
            return response(404, {"error": "Item not found", "itemId": item_id})
        raise

    item.update(updates)
    _write_item_to_s3(s3, bucket, key, item)
    return response(200, item)


def handle_delete_item(item_id: str, s3: Any, bucket: str) -> dict[str, Any]:
    """Handle DELETE /items/{id}."""
    key = _item_key(item_id)
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if _is_not_found_error(exc):
            return response(404, {"error": "Item not found", "itemId": item_id})
        raise

    s3.delete_object(Bucket=bucket, Key=key)
    return response(200, {"deleted": True, "itemId": item_id})


def lambda_handler(event: dict[str, Any], context: Any = None) -> dict[str, Any]:
    """Main AWS Lambda entry point for inventory CRUD requests."""
    method, path = extract_method_and_path(event)

    if method == "OPTIONS":
        return response(204, None)

    if method == "GET" and path == "/":
        return response(
            200,
            {
                "service": "Serverless Inventory API",
                "storage": "Amazon S3",
                "status": "ok",
                "endpoints": [
                    "POST /items",
                    "GET /items",
                    "GET /items/{id}",
                    "PUT /items/{id}",
                    "DELETE /items/{id}",
                ],
            },
        )

    s3 = get_s3_client()
    bucket = get_bucket_name()

    if path == "/items":
        if method == "POST":
            return handle_create_item(event, s3, bucket)
        if method == "GET":
            return handle_list_items(s3, bucket)

    if path.startswith("/items/"):
        raw_id = path[len("/items/") :]
        if raw_id and "/" not in raw_id:
            item_id = unquote(raw_id)
            if not _is_valid_item_id(item_id):
                return response(400, {"error": "Invalid itemId"})
            if method == "GET":
                return handle_get_item(item_id, s3, bucket)
            if method == "PUT":
                return handle_update_item(item_id, event, s3, bucket)
            if method == "DELETE":
                return handle_delete_item(item_id, s3, bucket)

    return response(404, {"error": "Route not found", "method": method, "path": path})
