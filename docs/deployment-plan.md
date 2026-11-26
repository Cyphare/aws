# Deployment Plan — Serverless Inventory API

**Author:** Arnold Gavrael Bonardo Situmorang – `24/539797/TK/59872`  
**Track:** Week 12 Individual Project — Track C: Serverless  
**Target AWS Account & Region:** `9923-8253-7395` / `us-east-1` (US East — N. Virginia)

---

## 1. Prerequisites

- Active **AWS Academy Sandbox** session (`us-east-1`) with access to:
  - **Amazon S3**
  - **AWS Lambda** (Python 3.12 + Function URL)
  - **IAM** pre-provisioned **`LabRole`** (`arn:aws:iam::992382537395:role/LabRole`)
  - **Amazon CloudWatch Logs**
- Local tools: `Python 3.12+`, `git`, `curl` (or `scripts/test_api.ps1` on Windows PowerShell).

---

## 2. Sandbox Service Availability & Architectural Adaptation

Before provisioning, each planned service was tested live in AWS Account `9923-8253-7395` (`us-east-1`, federated role `voclabs`):

| Planned Service | Sandbox Status | Verified Finding | Chosen Replacement / Handling |
|---|---|---|---|
| **API Gateway (HTTP API)** | **Denied** (`403`) | `apigateway:PUT` on `arn:aws:apigateway:us-east-1::/apis` denied (*no identity-based policy allows the action*). | **AWS Lambda Function URL** (`AuthType: NONE`) — native HTTPS endpoint with Payload Format 2.0. |
| **Amazon DynamoDB** | **Denied** (`400`) | `dynamodb:CreateTable` on `table/InventoryItems` denied (*no identity-based policy allows the action*). | **Amazon S3** private bucket storing one JSON document per item (`items/{itemId}.json`). |
| **IAM Role Creation** | **Restricted** | `iam:CreateRole` is not granted in the sandbox policy. | Reuse pre-existing **`LabRole`** (`iam:PassRole` allowed). |
| **AWS Lambda + Function URL** | **Available** | Function creation (`Python 3.12`), `.zip` upload, Function URL (`AuthType: NONE`), and HTTPS invoke all succeeded. | Retained as core compute & HTTPS entry point. |
| **Amazon S3** | **Available** | Bucket creation (`inventory-items-992382537395-us-east-1`), SSE-S3, and object CRUD via `LabRole` all succeeded. | Used as serverless object store. |
| **CloudWatch Logs** | **Available** | Automatic log stream creation under `/aws/lambda/inventory-handler`. | Retained for execution logs. |

---

## 3. Reproducible Deployment Steps

### Step A — Create the Private Amazon S3 Bucket (`us-east-1`)

**AWS Console:**
1. Open **Amazon S3 → Create bucket** in `us-east-1`.
2. Bucket name: `inventory-items-992382537395-us-east-1`.
3. Keep **ACLs disabled**, **Block all public access** enabled, and **SSE-S3** default encryption.
4. Click **Create bucket**.

**Equivalent AWS CLI:**
```bash
aws s3api create-bucket \
  --bucket inventory-items-992382537395-us-east-1 \
  --region us-east-1

aws s3api put-public-access-block \
  --bucket inventory-items-992382537395-us-east-1 \
  --public-access-block-configuration \
  "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"
```

---

### Step B — Package the Lambda Deployment Artifact

From the repository root, package `src/lambda_function.py` as `lambda_function.py` at the archive root:

```powershell
python -c "import zipfile; z = zipfile.ZipFile('lambda_function.zip', 'w', zipfile.ZIP_DEFLATED); z.write('src/lambda_function.py', 'lambda_function.py'); z.close()"
```

---

### Step C — Create and Configure AWS Lambda (`inventory-handler`)

**AWS Console:**
1. Open **AWS Lambda → Create function** (`us-east-1`) → **Author from scratch**.
2. Function name: `inventory-handler`
3. Runtime: **Python 3.12** | Architecture: **x86_64**
4. Execution role: **Use an existing role** → **`LabRole`** (`arn:aws:iam::992382537395:role/LabRole`).
5. Under **Additional configurations**:
   - Check **Enable function URL**
   - Auth type: **`NONE`**
   - Invoke mode: **`BUFFERED`**
   - Leave AWS CORS unchecked (CORS headers and `OPTIONS` preflight are returned cleanly by `src/lambda_function.py`).
6. Click **Create function**.
7. On the **Code** tab → **Upload from → .zip file** → upload `lambda_function.zip` → **Save**.
8. On **Configuration → Environment variables → Edit**, add:
   - `BUCKET_NAME` = `inventory-items-992382537395-us-east-1`
   - `ITEMS_PREFIX` = `items/`

**Equivalent AWS CLI (when CLI credentials are configured):**
```bash
aws lambda create-function \
  --function-name inventory-handler \
  --runtime python3.12 \
  --role arn:aws:iam::992382537395:role/LabRole \
  --handler lambda_function.lambda_handler \
  --zip-file fileb://lambda_function.zip \
  --environment "Variables={BUCKET_NAME=inventory-items-992382537395-us-east-1,ITEMS_PREFIX=items/}" \
  --region us-east-1

aws lambda create-function-url-config \
  --function-name inventory-handler \
  --auth-type NONE \
  --invoke-mode BUFFERED \
  --region us-east-1
```

---

## 4. Verification Checklist

1. **Local Unit Tests (Mocked S3):**
   ```powershell
   python -m unittest discover -s tests -v
   ```
2. **AWS Lambda Console Test Event (`list-items`):**
   ```json
   {
     "requestContext": { "http": { "method": "GET", "path": "/items" } },
     "rawPath": "/items"
   }
   ```
   Expected result: `Succeeded`, `statusCode: 200`.
3. **Live End-to-End HTTPS Verification:**
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\scripts\test_api.ps1 `
     -BaseUrl "https://rv2twgddlrfmlyge5ulx6sv7yq0nzuzt.lambda-url.us-east-1.on.aws"
   ```
4. **Persistence Check:** Open S3 bucket `inventory-items-992382537395-us-east-1` → `items/` prefix and verify the updated `<itemId>.json` object exists.

---

## 5. Teardown / Cleanup

1. Empty and delete the S3 bucket:
   ```bash
   aws s3 rm s3://inventory-items-992382537395-us-east-1 --recursive
   aws s3api delete-bucket --bucket inventory-items-992382537395-us-east-1 --region us-east-1
   ```
2. Delete the Lambda Function URL and Lambda function:
   ```bash
   aws lambda delete-function-url-config --function-name inventory-handler --region us-east-1
   aws lambda delete-function --function-name inventory-handler --region us-east-1
   ```
