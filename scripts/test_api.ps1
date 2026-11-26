param(
    [Parameter(Mandatory = $true)]
    [string]$BaseUrl
)

$ErrorActionPreference = "Stop"
$Base = $BaseUrl.TrimEnd("/")

function Invoke-ApiCheck {
    param(
        [string]$Label,
        [string]$Method,
        [string]$Path,
        [int]$ExpectedStatus,
        [string]$Body = ""
    )

    Write-Output "=== $Label ==="
    Write-Output "Request: $Method $Base$Path"
    $args = @("-sS", "-w", "`nHTTP_STATUS:%{http_code}`n", "-X", $Method)
    if ($Body -ne "") {
        Write-Output "Payload: $Body"
        $args += @("-H", "Content-Type: application/json", "-d", $Body)
    }
    $args += "$Base$Path"

    $raw = & curl.exe @args
    $text = ($raw -join "`n")
    $match = [regex]::Match($text, "HTTP_STATUS:(\d+)")
    if (-not $match.Success) {
        throw "Failed to parse HTTP status for $Label`n$text"
    }
    $status = [int]$match.Groups[1].Value
    $respBody = ($text -replace "`r?`nHTTP_STATUS:\d+\s*$", "").Trim()
    Write-Output "Status: $status (expected $ExpectedStatus)"
    Write-Output "Response: $respBody"
    Write-Output ""

    if ($status -ne $ExpectedStatus) {
        throw "Assertion failed for $Label : got HTTP $status, expected $ExpectedStatus"
    }
    return $respBody
}

Invoke-ApiCheck -Label "1. Health Check (GET /)" -Method "GET" -Path "/" -ExpectedStatus 200 | Out-Null
Invoke-ApiCheck -Label "2. Initial List (GET /items)" -Method "GET" -Path "/items" -ExpectedStatus 200 | Out-Null

$created1Json = Invoke-ApiCheck -Label "3. Create Item 1 (POST /items)" -Method "POST" -Path "/items" -ExpectedStatus 201 -Body '{"name":"Oscilloscope Tektronix TBS1052C","stock":4,"location":"Lab A - Rack 2"}'
$id1 = ($created1Json | ConvertFrom-Json).itemId

$created2Json = Invoke-ApiCheck -Label "4. Create Item 2 (POST /items)" -Method "POST" -Path "/items" -ExpectedStatus 201 -Body '{"name":"Digital Multimeter Fluke 117","stock":10,"location":"Lab B - Cabinet 1"}'
$id2 = ($created2Json | ConvertFrom-Json).itemId

Invoke-ApiCheck -Label "5. Invalid Create Missing Name (POST /items -> 400)" -Method "POST" -Path "/items" -ExpectedStatus 400 -Body '{}' | Out-Null
Invoke-ApiCheck -Label "6. List After Creates (GET /items)" -Method "GET" -Path "/items" -ExpectedStatus 200 | Out-Null
Invoke-ApiCheck -Label "7. Update Item 1 (PUT /items/$id1)" -Method "PUT" -Path "/items/$id1" -ExpectedStatus 200 -Body '{"stock":7,"location":"Lab A - Bench 5"}' | Out-Null
Invoke-ApiCheck -Label "8. Update Nonexistent Item (PUT -> 404)" -Method "PUT" -Path "/items/nonexistent-id-000" -ExpectedStatus 404 -Body '{"stock":1}' | Out-Null
Invoke-ApiCheck -Label "9. Delete Item 2 (DELETE /items/$id2)" -Method "DELETE" -Path "/items/$id2" -ExpectedStatus 200 | Out-Null
Invoke-ApiCheck -Label "10. Delete Nonexistent Item (DELETE -> 404)" -Method "DELETE" -Path "/items/nonexistent-id-000" -ExpectedStatus 404 | Out-Null
Invoke-ApiCheck -Label "11. Final List Persistence Check (GET /items)" -Method "GET" -Path "/items" -ExpectedStatus 200 | Out-Null

Write-Output "ALL 11 LIVE API CHECKS PASSED."
