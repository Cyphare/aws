"""AWS Lambda handler for the Serverless Inventory API."""

import base64
from decimal import Decimal
import json
import os
from typing import Any
from urllib.parse import unquote
import uuid

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:  # pragma: no cover - local unit tests inject a mock table
    boto3 = None

    class ClientError(Exception):
        """Fallback ClientError when botocore is unavailable locally."""

        def __init__(self, error_response: dict[str, Any], operation_name: str = ""):
            super().__init__(str(error_response))
            self.response = error_response
            self.operation_name = operation_name


TABLE_NAME = os.environ.get("TABLE_NAME", "InventoryItems")
_CORS_HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
}
_table_instance: Any = None


class DecimalEncoder(json.JSONEncoder):
    """Serialize DynamoDB Decimal values as int or float."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


def get_table() -> Any:
    """Return a cached DynamoDB Table resource for TABLE_NAME."""
    global _table_instance
    if _table_instance is None:
        if boto3 is None:
            raise RuntimeError("boto3 is required to connect to DynamoDB")
        table_name = os.environ.get("TABLE_NAME", TABLE_NAME)
        _table_instance = boto3.resource("dynamodb").Table(table_name)
    return _table_instance


def response(status_code: int, body: Any = None) -> dict[str, Any]:
    """Build an HTTP Lambda/API Gateway v2.0 JSON response."""
    return {
        "statusCode": status_code,
        "headers": dict(_CORS_HEADERS),
        "body": "" if body is None else json.dumps(body, cls=DecimalEncoder),
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


def _is_conditional_failure(exc: ClientError) -> bool:
    err = getattr(exc, "response", {}).get("Error", {})
    return err.get("Code") == "ConditionalCheckFailedException"


def handle_create_item(event: dict[str, Any], table: Any) -> dict[str, Any]:
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
    table.put_item(Item=item)
    return response(201, item)


def handle_list_items(table: Any) -> dict[str, Any]:
    """Handle GET /items with full DynamoDB scan pagination."""
    items: list[dict[str, Any]] = []
    scan_kwargs: dict[str, Any] = {}
    while True:
        res = table.scan(**scan_kwargs)
        items.extend(res.get("Items", []))
        last_key = res.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return response(200, {"items": items, "count": len(items)})


def handle_get_item(item_id: str, table: Any) -> dict[str, Any]:
    """Handle GET /items/{id}."""
    res = table.get_item(Key={"itemId": item_id})
    item = res.get("Item")
    if not item:
        return response(404, {"error": "Item not found", "itemId": item_id})
    return response(200, item)


def handle_update_item(item_id: str, event: dict[str, Any], table: Any) -> dict[str, Any]:
    """Handle PUT /items/{id}."""
    data, err = parse_json_body(event)
    if err or data is None:
        return response(400, {"error": err})

    expr_parts: list[str] = []
    expr_names: dict[str, str] = {}
    expr_values: dict[str, Any] = {}

    if "name" in data:
        name = data["name"]
        if not isinstance(name, str) or not name.strip():
            return response(400, {"error": "Field 'name' must be a non-empty string"})
        expr_parts.append("#name = :name")
        expr_names["#name"] = "name"
        expr_values[":name"] = name.strip()

    if "stock" in data:
        stock = data["stock"]
        if not _is_valid_non_negative_int(stock):
            return response(400, {"error": "Field 'stock' must be a non-negative integer"})
        expr_parts.append("#stock = :stock")
        expr_names["#stock"] = "stock"
        expr_values[":stock"] = stock

    if "location" in data:
        location = data["location"]
        if not isinstance(location, str):
            return response(400, {"error": "Field 'location' must be a string"})
        expr_parts.append("#location = :location")
        expr_names["#location"] = "location"
        expr_values[":location"] = location.strip()

    if not expr_parts:
        return response(
            400,
            {"error": "At least one updatable field ('name', 'stock', 'location') is required"},
        )

    try:
        res = table.update_item(
            Key={"itemId": item_id},
            UpdateExpression="SET " + ", ".join(expr_parts),
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
            ConditionExpression="attribute_exists(itemId)",
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return response(404, {"error": "Item not found", "itemId": item_id})
        raise

    return response(200, res.get("Attributes", {}))


def handle_delete_item(item_id: str, table: Any) -> dict[str, Any]:
    """Handle DELETE /items/{id}."""
    try:
        table.delete_item(
            Key={"itemId": item_id},
            ConditionExpression="attribute_exists(itemId)",
        )
    except ClientError as exc:
        if _is_conditional_failure(exc):
            return response(404, {"error": "Item not found", "itemId": item_id})
        raise

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

    table = get_table()

    if path == "/items":
        if method == "POST":
            return handle_create_item(event, table)
        if method == "GET":
            return handle_list_items(table)

    if path.startswith("/items/"):
        raw_id = path[len("/items/") :]
        if raw_id and "/" not in raw_id:
            item_id = unquote(raw_id)
            if method == "GET":
                return handle_get_item(item_id, table)
            if method == "PUT":
                return handle_update_item(item_id, event, table)
            if method == "DELETE":
                return handle_delete_item(item_id, table)

    return response(404, {"error": "Route not found", "method": method, "path": path})
