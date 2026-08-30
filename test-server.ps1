$ErrorActionPreference = 'Stop'
$testDb = Join-Path $env:TEMP ('reader-sync-' + [guid]::NewGuid() + '.db')
$testPort = Get-Random -Minimum 20000 -Maximum 30000
$legacySetup = 'import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); db.execute("CREATE TABLE progress (kind TEXT NOT NULL, work_id TEXT NOT NULL, episode_id TEXT NOT NULL, position REAL NOT NULL, title TEXT NOT NULL DEFAULT '''', device_id TEXT NOT NULL DEFAULT '''', updated_at INTEGER NOT NULL, PRIMARY KEY (kind, work_id))"); db.close()'
python -c $legacySetup $testDb
if ($LASTEXITCODE -ne 0) { throw 'Legacy database setup failed.' }
$env:SYNC_DB = $testDb
$env:SYNC_TOKEN = 'integration-test-token'
$env:SYNC_ALLOWED_NETWORK = '127.0.0.0/8'
$env:SYNC_PORT = [string]$testPort
$process = Start-Process -FilePath python -ArgumentList (Join-Path $PSScriptRoot 'server\reader_sync.py') -PassThru -WindowStyle Hidden

try {
  $ready = $false
  for ($i = 0; $i -lt 20; $i++) {
    try { Invoke-RestMethod "http://127.0.0.1:$testPort/health" | Out-Null; $ready = $true; break }
    catch { Start-Sleep -Milliseconds 100 }
  }
  if (-not $ready) { throw 'Test server did not start.' }
  $headers = @{ Authorization = 'Bearer integration-test-token' }
  $body = @{ kind='novel'; work_id='60853'; episode_id='6919020'; position=0.42; title='test'; device_id='mobile' } | ConvertTo-Json
  Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:$testPort/v1/progress" -Headers $headers -ContentType 'application/json' -Body $body | Out-Null
  $list = Invoke-RestMethod -Uri "http://127.0.0.1:$testPort/v1/progress" -Headers $headers
  if ($list.progress.Count -ne 1 -or $list.progress[0].episode_id -ne '6919020') { throw 'List endpoint failed.' }
  Invoke-RestMethod -Method Delete -Uri "http://127.0.0.1:$testPort/v1/progress?kind=novel&work_id=60853" -Headers $headers | Out-Null
  $deleted = Invoke-RestMethod -Uri "http://127.0.0.1:$testPort/v1/progress" -Headers $headers
  if ($deleted.progress.Count -ne 1 -or -not $deleted.progress[0].deleted) { throw 'Delete tombstone was not retained.' }
  $target = Invoke-RestMethod -Uri "http://127.0.0.1:$testPort/v1/progress?kind=novel&work_id=60853" -Headers $headers
  if ($null -ne $target.progress) { throw 'Deleted progress remains readable.' }
  Write-Host 'reader sync integration checks passed'
} finally {
  if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force }
  if (Test-Path -LiteralPath $testDb) { Remove-Item -LiteralPath $testDb -Force }
}
