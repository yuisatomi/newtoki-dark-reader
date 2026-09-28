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
  if ($list.progress.Count -ne 1 -or $list.progress[0].episode_id -ne '6919020' -or $list.progress[0].revision -ne 1) { throw 'List endpoint or revision migration failed.' }
  $advanced = @{ kind='novel'; work_id='60853'; episode_id='6919021'; position=0.75; expected_revision=1 } | ConvertTo-Json
  $saved = Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:$testPort/v1/progress" -Headers $headers -ContentType 'application/json' -Body $advanced
  if ($saved.revision -ne 2) { throw 'Conditional episode advance failed.' }
  foreach ($stale in @($body, (@{ kind='novel'; work_id='60853'; episode_id='6919020'; position=0.5; expected_revision=1 } | ConvertTo-Json))) {
    try {
      Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:$testPort/v1/progress" -Headers $headers -ContentType 'application/json' -Body $stale | Out-Null
      throw 'Stale episode write was accepted.'
    } catch {
      if ([int]$_.Exception.Response.StatusCode -ne 409) { throw }
    }
  }
  $olderPosition = @{ kind='novel'; work_id='60853'; episode_id='6919021'; position=0.1 } | ConvertTo-Json
  Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:$testPort/v1/progress" -Headers $headers -ContentType 'application/json' -Body $olderPosition | Out-Null
  $current = Invoke-RestMethod -Uri "http://127.0.0.1:$testPort/v1/progress?kind=novel&work_id=60853" -Headers $headers
  if ($current.progress.episode_id -ne '6919021' -or $current.progress.position -ne 0.75 -or $current.revision -ne 2) { throw 'Legacy write regressed progress.' }
  $manualRewind = @{ kind='novel'; work_id='60853'; episode_id='6919021'; position=0.2; expected_revision=2; allow_rewind=$true } | ConvertTo-Json
  $rewound = Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:$testPort/v1/progress" -Headers $headers -ContentType 'application/json' -Body $manualRewind
  if ($rewound.position -ne 0.2 -or $rewound.revision -ne 3) { throw 'Explicit manual rewind failed.' }
  Invoke-RestMethod -Method Delete -Uri "http://127.0.0.1:$testPort/v1/progress?kind=novel&work_id=60853" -Headers $headers | Out-Null
  $deleted = Invoke-RestMethod -Uri "http://127.0.0.1:$testPort/v1/progress" -Headers $headers
  if ($deleted.progress.Count -ne 1 -or -not $deleted.progress[0].deleted -or $deleted.progress[0].revision -ne 4) { throw 'Delete tombstone was not retained.' }
  $target = Invoke-RestMethod -Uri "http://127.0.0.1:$testPort/v1/progress?kind=novel&work_id=60853" -Headers $headers
  if ($null -ne $target.progress) { throw 'Deleted progress remains readable.' }
  $tombstone = Invoke-RestMethod -Uri "http://127.0.0.1:$testPort/v1/progress?kind=novel&work_id=60853&include_deleted=1" -Headers $headers
  if (-not $tombstone.progress.deleted -or $tombstone.revision -ne 4) { throw 'Deleted revision is unreadable to new clients.' }
  try {
    Invoke-RestMethod -Method Put -Uri "http://127.0.0.1:$testPort/v1/progress" -Headers $headers -ContentType 'application/json' -Body $body | Out-Null
    throw 'Legacy client revived a deleted work.'
  } catch {
    if ([int]$_.Exception.Response.StatusCode -ne 409) { throw }
  }
  Write-Host 'reader sync integration checks passed'
} finally {
  if ($process -and -not $process.HasExited) { Stop-Process -Id $process.Id -Force }
  if (Test-Path -LiteralPath $testDb) { Remove-Item -LiteralPath $testDb -Force }
}
