Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$marketplaceRoot = Split-Path -Parent $PSCommandPath

$archivePath = Join-Path `
    $marketplaceRoot `
    "plugins\video-analyzing-private\seed\full-history.zip"

$manifestPath = Join-Path `
    $marketplaceRoot `
    "plugins\video-analyzing-private\seed\snapshot-manifest.json"

if (-not (Test-Path -LiteralPath $archivePath)) {
    throw "找不到数据库快照：$archivePath"
}

if (-not (Test-Path -LiteralPath $manifestPath)) {
    throw "找不到快照清单：$manifestPath"
}

$manifest = Get-Content `
    -LiteralPath $manifestPath `
    -Raw |
    ConvertFrom-Json

$expectedHash = ([string]$manifest.archive_sha256).ToLowerInvariant()

$actualHash = (
    Get-FileHash `
        -LiteralPath $archivePath `
        -Algorithm SHA256
).Hash.ToLowerInvariant()

if ($actualHash -ne $expectedHash) {
    throw @"
数据库快照校验失败。
期望：$expectedHash
实际：$actualHash
禁止继续安装。
"@
}

$codexCommand = Get-Command codex -ErrorAction SilentlyContinue

if (-not $codexCommand) {
    throw "找不到 codex 命令，请先安装或启动 Codex。"
}

Write-Host "数据库快照校验通过：$actualHash"

& codex plugin marketplace add $marketplaceRoot --json

if ($LASTEXITCODE -ne 0) {
    throw @"
Marketplace 注册失败。
如果 private-marketplace 已经注册，请检查原来的注册地址。
"@
}

& codex plugin add `
    "video-analyzing-private@private-marketplace" `
    --json

if ($LASTEXITCODE -ne 0) {
    throw "video-analyzing-private 安装失败。"
}

Write-Host ""
Write-Host "video-analyzing-private 安装成功。"
Write-Host "请重新打开 Codex，或者新建一个任务后使用。"