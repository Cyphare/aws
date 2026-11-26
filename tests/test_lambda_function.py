"""Unit tests for the Serverless Inventory API Lambda handler (S3 backend)."""

import base64
import io
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

import lambda_function


class FakeS3Client:
    """In-memory mock of a boto3 S3 client supporting CRUD and list pagination."""

    def __init__(self, page_size: int = 100):
        self.objects: dict[tuple[str, str], bytes] = {}
        self.page_size = page_size

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str = "") -> dict:
        self.objects[(Bucket, Key)] = Body if isinstance(Body, bytes) else Body.encode("utf-8")
        return {}

    def get_object(self, Bucket: str, Key: str) -> dict:
        data = self.objects.get((Bucket, Key))
        if data is None:
            raise lambda_function.ClientError(
                {
                    "Error": {"Code": "NoSuchKey", "Message": "The specified key does not exist."},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "GetObject",
            )
        return {"Body": io.BytesIO(data)}

    def head_object(self, Bucket: str, Key: str) -> dict:
        if (Bucket, Key) not in self.objects:
            raise lambda_function.ClientError(
                {
                    "Error": {"Code": "404", "Message": "Not Found"},
                    "ResponseMetadata": {"HTTPStatusCode": 404},
                },
                "HeadObject",
            )
        return {"ContentLength": len(self.objects[(Bucket, Key)])}

    def delete_object(self, Bucket: str, Key: str) -> dict:
        self.objects.pop((Bucket, Key), None)
        return {}

    def list_objects_v2(
        self, Bucket: str, Prefix: str = "", ContinuationToken: str | None = None
    ) -> dict:
        matching_keys = sorted(
            key for (bkt, key) in self.objects if bkt == Bucket and key.startswith(Prefix)
        )
        start_idx = int(ContinuationToken) if ContinuationToken is not None else 0
        batch = matching_keys[start_idx : start_idx + self.page_size]
        result: dict = {
            "Contents": [{"Key": key} for key in batch],
            "IsTruncated": start_idx + self.page_size < len(matching_keys),
        }
        if result["IsTruncated"]:
            result["NextContinuationToken"] = str(start_idx + self.page_size)
        return result


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
        raw = json.dumps(body) if isinstance(body, (dict, list)) else body
        if base64_encoded:
            raw = base64.b64encode(raw.encode("utf-8")).decode("utf-8")
        event["body"] = raw
    return event


class LambdaFunctionTestCase(unittest.TestCase):
    def setUp(self):
        self.fake_s3 = FakeS3Client()
        lambda_function._s3_client = self.fake_s3

    def tearDown(self):
        lambda_function._s3_client = None

    def test_health_root(self):
        res = lambda_function.lambda_handler(make_event("GET", "/"))
        self.assertEqual(res["statusCode"], 200)
        payload = json.loads(res["body"])
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["storage"], "Amazon S3")

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

    def test_list_items_empty_and_pagination(self):
        res_empty = lambda_function.lambda_handler(make_event("GET", "/items"))
        self.assertEqual(res_empty["statusCode"], 200)
        self.assertEqual(json.loads(res_empty["body"]), {"items": [], "count": 0})

        self.fake_s3 = FakeS3Client(page_size=1)
        lambda_function._s3_client = self.fake_s3
        bucket = lambda_function.get_bucket_name()
        self.fake_s3.put_object(
            bucket,
            "items/id-1.json",
            json.dumps({"itemId": "id-1", "name": "Multimeter", "stock": 10, "location": "Cab 1"}).encode(),
        )
        self.fake_s3.put_object(
            bucket,
            "items/id-2.json",
            json.dumps({"itemId": "id-2", "name": "Power Supply", "stock": 2, "location": "Rack 3"}).encode(),
        )
        # Non-JSON prefix marker should be ignored
        self.fake_s3.put_object(bucket, "items/", b"")

        res_list = lambda_function.lambda_handler(make_event("GET", "/items"))
        self.assertEqual(res_list["statusCode"], 200)
        body = json.loads(res_list["body"])
        self.assertEqual(body["count"], 2)
        self.assertEqual([item["itemId"] for item in body["items"]], ["id-1", "id-2"])

    def test_get_item_found_and_not_found(self):
        bucket = lambda_function.get_bucket_name()
        self.fake_s3.put_object(
            bucket,
            "items/abc-123.json",
            json.dumps({"itemId": "abc-123", "name": "Logic Analyzer", "stock": 3, "location": "Bench 2"}).encode(),
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

    def test_path_traversal_and_unknown_route(self):
        traversal_res = lambda_function.lambda_handler(make_event("GET", "/items/..secret"))
        self.assertEqual(traversal_res["statusCode"], 400)

        unknown_res = lambda_function.lambda_handler(make_event("GET", "/unknown"))
        self.assertEqual(unknown_res["statusCode"], 404)


if __name__ == "__main__":
    unittest.main()
