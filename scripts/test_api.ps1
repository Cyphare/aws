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

    $lines = @()
    $lines += "=== $Label ==="
    $lines += "Request: $Method $Base$Path"
    $args = @("-sS", "-w", "`nHTTP_STATUS:%{http_code}`n", "-X", $Method)
    $tmpFile = $null
    try {
        if ($Body -ne "") {
            $lines += "Payload: $Body"
            $tmpFile = [System.IO.Path]::GetTempFileName()
            $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
            [System.IO.File]::WriteAllText($tmpFile, $Body, $utf8NoBom)
            $args += @("-H", "Content-Type: application/json", "--data-binary", "@$tmpFile")
        }
        $args += "$Base$Path"
        $raw = & curl.exe @args
    }
    finally {
        if ($tmpFile -and (Test-Path $tmpFile)) {
            Remove-Item $tmpFile -Force -ErrorAction SilentlyContinue
        }
    }

    $text = ($raw -join "`n")
    $match = [regex]::Match($text, "HTTP_STATUS:(\d+)")
    if (-not $match.Success) {
        throw "Failed to parse HTTP status for $Label`n$text"
    }
    $status = [int]$match.Groups[1].Value
    $respBody = ($text -replace "`r?`nHTTP_STATUS:\d+\s*$", "").Trim()
    $lines += "Status: $status (expected $ExpectedStatus)"
    $lines += "Response: $respBody"
    $lines += ""

    if ($status -ne $ExpectedStatus) {
        $lines | ForEach-Object { [Console]::WriteLine($_) }
        throw "Assertion failed for $Label : got HTTP $status, expected $ExpectedStatus"
    }

    return [PSCustomObject]@{
        Status = $status
        Body   = $respBody
        Lines  = $lines
    }
}

$allLines = @()

$r1 = Invoke-ApiCheck -Label "1. Health Check (GET /)" -Method "GET" -Path "/" -ExpectedStatus 200
$allLines += $r1.Lines

$r2 = Invoke-ApiCheck -Label "2. Initial List (GET /items)" -Method "GET" -Path "/items" -ExpectedStatus 200
$allLines += $r2.Lines

$r3 = Invoke-ApiCheck -Label "3. Create Item 1 (POST /items)" -Method "POST" -Path "/items" -ExpectedStatus 201 -Body '{"name":"Oscilloscope Tektronix TBS1052C","stock":4,"location":"Lab A - Rack 2"}'
$allLines += $r3.Lines
$id1 = ($r3.Body | ConvertFrom-Json).itemId

$r4 = Invoke-ApiCheck -Label "4. Create Item 2 (POST /items)" -Method "POST" -Path "/items" -ExpectedStatus 201 -Body '{"name":"Digital Multimeter Fluke 117","stock":10,"location":"Lab B - Cabinet 1"}'
$allLines += $r4.Lines
$id2 = ($r4.Body | ConvertFrom-Json).itemId

$r5 = Invoke-ApiCheck -Label "5. Invalid Create Missing Name (POST /items -> 400)" -Method "POST" -Path "/items" -ExpectedStatus 400 -Body '{}'
$allLines += $r5.Lines

$r6 = Invoke-ApiCheck -Label "6. List After Creates (GET /items)" -Method "GET" -Path "/items" -ExpectedStatus 200
$allLines += $r6.Lines

$r7 = Invoke-ApiCheck -Label "7. Update Item 1 (PUT /items/$id1)" -Method "PUT" -Path "/items/$id1" -ExpectedStatus 200 -Body '{"stock":7,"location":"Lab A - Bench 5"}'
$allLines += $r7.Lines

$r8 = Invoke-ApiCheck -Label "8. Update Nonexistent Item (PUT -> 404)" -Method "PUT" -Path "/items/nonexistent-id-000" -ExpectedStatus 404 -Body '{"stock":1}'
$allLines += $r8.Lines

$r9 = Invoke-ApiCheck -Label "9. Delete Item 2 (DELETE /items/$id2)" -Method "DELETE" -Path "/items/$id2" -ExpectedStatus 200
$allLines += $r9.Lines

$r10 = Invoke-ApiCheck -Label "10. Delete Nonexistent Item (DELETE -> 404)" -Method "DELETE" -Path "/items/nonexistent-id-000" -ExpectedStatus 404
$allLines += $r10.Lines

$r11 = Invoke-ApiCheck -Label "11. Final List Persistence Check (GET /items)" -Method "GET" -Path "/items" -ExpectedStatus 200
$allLines += $r11.Lines

$allLines += "ALL 11 LIVE API CHECKS PASSED."
$allLines
