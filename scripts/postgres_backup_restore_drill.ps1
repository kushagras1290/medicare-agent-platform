param(
    [string]$OutputDirectory = "output/backups"
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$outputRoot = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $OutputDirectory))
if (-not $outputRoot.StartsWith($projectRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Backup output must remain inside the project directory"
}
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null

$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$containerBackup = "/tmp/medicare-$stamp.dump"
$hostBackup = Join-Path $outputRoot "medicare-$stamp.dump"
$restoreDatabase = "medicare_restore_drill"
if ($restoreDatabase -ne "medicare_restore_drill") {
    throw "Unexpected restore target"
}

Push-Location $projectRoot
try {
    docker compose exec -T db pg_dump -U medicare -d medicare --format=custom --no-owner --no-acl --file=$containerBackup
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed" }
    $containerId = (docker compose ps -q db).Trim()
    if (-not $containerId) { throw "PostgreSQL container is not running" }
    docker cp "${containerId}:$containerBackup" $hostBackup
    if ($LASTEXITCODE -ne 0) { throw "docker cp failed" }
    docker compose exec -T db dropdb -U medicare --if-exists $restoreDatabase
    docker compose exec -T db createdb -U medicare $restoreDatabase
    docker compose exec -T db pg_restore -U medicare -d $restoreDatabase --no-owner --no-acl $containerBackup
    if ($LASTEXITCODE -ne 0) { throw "pg_restore failed" }
    $sourceCount = (docker compose exec -T db psql -U medicare -d medicare -Atc "SELECT COUNT(*) FROM customers").Trim()
    $restoreCount = (docker compose exec -T db psql -U medicare -d $restoreDatabase -Atc "SELECT COUNT(*) FROM customers").Trim()
    $sourceConsent = (docker compose exec -T db psql -U medicare -d medicare -Atc "SELECT COUNT(*) FROM consent_evidence").Trim()
    $restoreConsent = (docker compose exec -T db psql -U medicare -d $restoreDatabase -Atc "SELECT COUNT(*) FROM consent_evidence").Trim()
    if ($sourceCount -ne $restoreCount -or $sourceConsent -ne $restoreConsent) {
        throw "Restored row counts do not match the source database"
    }
    $hash = (Get-FileHash -Algorithm SHA256 -LiteralPath $hostBackup).Hash.ToLowerInvariant()
    [ordered]@{
        backup = $hostBackup
        sha256 = $hash
        customers = [int]$restoreCount
        consent_evidence = [int]$restoreConsent
        restore_database = $restoreDatabase
        verified = $true
    } | ConvertTo-Json
}
finally {
    docker compose exec -T db dropdb -U medicare --if-exists $restoreDatabase 2>$null
    docker compose exec -T db rm -f $containerBackup 2>$null
    Pop-Location
}
