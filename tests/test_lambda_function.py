"""Unit tests for the Serverless Inventory API Lambda handler."""

import base64
from decimal import Decimal
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import lambda_function


class FakeDynamoDBTable:
    """In-memory mock of a boto3 DynamoDB Table supporting CRUD and conditions."""

    def __init__(self, page_size: int = 100):
        self.items: dict[str, dict] = {}
        self.page_size = page_size

    def put_item(self, Item: dict) -> dict:
        self.items[Item["itemId"]] = dict(Item)
        return {}

    def get_item(self, Key: dict) -> dict:
        item = self.items.get(Key["itemId"])
        return {"Item": dict(item)} if item is not None else {}

    def scan(self, ExclusiveStartKey: dict | None = None) -> dict:
        ordered_keys = sorted(self.items.keys())
        start_idx = 0
        if ExclusiveStartKey is not None:
            start_idx = ordered_keys.index(ExclusiveStartKey["itemId"]) + 1
        batch_keys = ordered_keys[start_idx : start_idx + self.page_size]
        result = {"Items": [dict(self.items[k]) for k in batch_keys]}
        if start_idx + self.page_size < len(ordered_keys):
            result["LastEvaluatedKey"] = {"itemId": batch_keys[-1]}
        return result

    def update_item(
        self,
        Key: dict,
        UpdateExpression: str,
        ExpressionAttributeNames: dict,
        ExpressionAttributeValues: dict,
        ConditionExpression: str = "",
        ReturnValues: str = "NONE",
    ) -> dict:
        item_id = Key["itemId"]
        if ConditionExpression == "attribute_exists(itemId)" and item_id not in self.items:
            raise lambda_function.ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "Not found"}},
                "UpdateItem",
            )
        item = dict(self.items[item_id])
        assignments = UpdateExpression.replace("SET ", "", 1).split(", ")
        for assignment in assignments:
            name_placeholder, value_placeholder = assignment.split(" = ")
            attr_name = ExpressionAttributeNames[name_placeholder]
            item[attr_name] = ExpressionAttributeValues[value_placeholder]
        self.items[item_id] = item
        return {"Attributes": dict(item)} if ReturnValues == "ALL_NEW" else {}

    def delete_item(self, Key: dict, ConditionExpression: str = "") -> dict:
        item_id = Key["itemId"]
        if ConditionExpression == "attribute_exists(itemId)" and item_id not in self.items:
            raise lambda_function.ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "Not found"}},
                "DeleteItem",
            )
        del self.items[item_id]
        return {}


def make_event(method: str, path: str, body=None, base64_encoded: bool = False) -> dict:
    """Build a Payload Format 2.0 event matching Lambda Function URL / HTTP API."""
    event = {
        "version": "2.0",
        "rawPath": path,
        "requestContext": {
            "http": {
                "method": method,
                "path": path,
            }
        },
        "isBase64Encoded": base64_encoded,
    }
    if body is not None:
        if isinstance(body, (dict, list)):
            raw = json.dumps(body)
        else:
            raw = body
        if base64_encoded:
            raw = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
        event["body"] = raw
    return event


class LambdaFunctionTestCase(unittest.TestCase):
    def setUp(self):
        self.fake_table = FakeDynamoDBTable()
        lambda_function._table_instance = self.fake_table

    def tearDown(self):
        lambda_function._table_instance = None

    def test_health_root(self):
        res = lambda_function.lambda_handler(make_event("GET", "/"))
        self.assertEqual(res["statusCode"], 200)
        payload = json.loads(res["body"])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["service"], "Serverless Inventory API")

    def test_options_cors_preflight(self):
        res = lambda_function.lambda_handler(make_event("OPTIONS", "/items"))
        self.assertEqual(res["statusCode"], 204)
        self.assertEqual(res["body"], "")
        self.assertEqual(res["headers"]["Access-Control-Allow-Origin"], "*")

    def test_create_item_success_and_defaults(self):
        res1 = lambda_function.lambda_handler(
            make_event(
                "POST",
                "/items",
                {"name": "Oscilloscope", "stock": 4, "location": "Lab A - Rack 2"},
            )
        )
        self.assertEqual(res1["statusCode"], 201)
        body1 = json.loads(res1["body"])
        self.assertIn("itemId", body1)
        self.assertEqual(body1["name"], "Oscilloscope")
        self.assertEqual(body1["stock"], 4)
        self.assertEqual(body1["location"], "Lab A - Rack 2")

        res2 = lambda_function.lambda_handler(
            make_event("POST", "/items/", {"name": "Soldering Iron"}, base64_encoded=True)
        )
        self.assertEqual(res2["statusCode"], 201)
        body2 = json.loads(res2["body"])
        self.assertEqual(body2["stock"], 0)
        self.assertEqual(body2["location"], "")

    def test_create_item_validation_errors(self):
        bad_payloads = [
            {},
            {"name": "   "},
            {"name": 123},
            {"name": "Probe", "stock": -1},
            {"name": "Probe", "stock": True},
            {"name": "Probe", "stock": "5"},
            {"name": "Probe", "stock": 1.5},
            {"name": "Probe", "location": 42},
        ]
        for payload in bad_payloads:
            res = lambda_function.lambda_handler(make_event("POST", "/items", payload))
            self.assertEqual(res["statusCode"], 400, f"Expected 400 for {payload}")

        invalid_json_res = lambda_function.lambda_handler(
            make_event("POST", "/items", "{bad json")
        )
        self.assertEqual(invalid_json_res["statusCode"], 400)

    def test_list_items_empty_decimal_and_pagination(self):
        res_empty = lambda_function.lambda_handler(make_event("GET", "/items"))
        self.assertEqual(res_empty["statusCode"], 200)
        self.assertEqual(json.loads(res_empty["body"]), {"items": [], "count": 0})

        self.fake_table = FakeDynamoDBTable(page_size=1)
        lambda_function._table_instance = self.fake_table
        self.fake_table.put_item(
            {"itemId": "id-1", "name": "Multimeter", "stock": Decimal("10"), "location": "Cab 1"}
        )
        self.fake_table.put_item(
            {"itemId": "id-2", "name": "Power Supply", "stock": Decimal("2"), "location": "Rack 3"}
        )

        res_list = lambda_function.lambda_handler(make_event("GET", "/items"))
        self.assertEqual(res_list["statusCode"], 200)
        body = json.loads(res_list["body"])
        self.assertEqual(body["count"], 2)
        self.assertEqual(len(body["items"]), 2)
        self.assertIsInstance(body["items"][0]["stock"], int)

    def test_get_item_found_and_not_found(self):
        self.fake_table.put_item(
            {"itemId": "abc-123", "name": "Logic Analyzer", "stock": 3, "location": "Bench 2"}
        )
        ok_res = lambda_function.lambda_handler(make_event("GET", "/items/abc-123"))
        self.assertEqual(ok_res["statusCode"], 200)
        self.assertEqual(json.loads(ok_res["body"])["name"], "Logic Analyzer")

        missing_res = lambda_function.lambda_handler(make_event("GET", "/items/does-not-exist"))
        self.assertEqual(missing_res["statusCode"], 404)

    def test_update_item_success_validation_and_not_found(self):
        create_res = lambda_function.lambda_handler(
            make_event("POST", "/items", {"name": "Oscilloscope", "stock": 4, "location": "Rack 2"})
        )
        item_id = json.loads(create_res["body"])["itemId"]

        upd_res = lambda_function.lambda_handler(
            make_event("PUT", f"/items/{item_id}", {"stock": 7, "location": "Lab A - Bench 5"})
        )
        self.assertEqual(upd_res["statusCode"], 200)
        upd_body = json.loads(upd_res["body"])
        self.assertEqual(upd_body["stock"], 7)
        self.assertEqual(upd_body["location"], "Lab A - Bench 5")
        self.assertEqual(upd_body["name"], "Oscilloscope")

        empty_upd = lambda_function.lambda_handler(make_event("PUT", f"/items/{item_id}", {}))
        self.assertEqual(empty_upd["statusCode"], 400)

        bad_stock = lambda_function.lambda_handler(
            make_event("PUT", f"/items/{item_id}", {"stock": -5})
        )
        self.assertEqual(bad_stock["statusCode"], 400)

        missing_upd = lambda_function.lambda_handler(
            make_event("PUT", "/items/nonexistent", {"stock": 1})
        )
        self.assertEqual(missing_upd["statusCode"], 404)

    def test_delete_item_success_and_not_found(self):
        create_res = lambda_function.lambda_handler(
            make_event("POST", "/items", {"name": "Caliper", "stock": 5, "location": "Drawer 4"})
        )
        item_id = json.loads(create_res["body"])["itemId"]

        del_res = lambda_function.lambda_handler(make_event("DELETE", f"/items/{item_id}"))
        self.assertEqual(del_res["statusCode"], 200)
        self.assertEqual(json.loads(del_res["body"]), {"deleted": True, "itemId": item_id})

        del_again = lambda_function.lambda_handler(make_event("DELETE", f"/items/{item_id}"))
        self.assertEqual(del_again["statusCode"], 404)

    def test_unknown_route(self):
        res = lambda_function.lambda_handler(make_event("GET", "/unknown"))
        self.assertEqual(res["statusCode"], 404)


if __name__ == "__main__":
    unittest.main()
