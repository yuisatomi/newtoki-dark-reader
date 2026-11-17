$ErrorActionPreference = 'Stop'
$buildRoot = Join-Path $PSScriptRoot 'build/pc-helper'
$buildPython = Join-Path $buildRoot 'venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $buildPython)) {
  python -m venv (Join-Path $buildRoot 'venv')
  if ($LASTEXITCODE -ne 0) { throw 'Build environment creation failed.' }
}
& $buildPython -I -m pip install -r (Join-Path $PSScriptRoot 'pc_helper/requirements-build.txt')
if ($LASTEXITCODE -ne 0) { throw 'Build dependency installation failed.' }
& $buildPython -I -m PyInstaller --noconfirm --clean --onefile --windowed --noupx --name Reader-ChatGPT-Connect `
  --paths (Join-Path $PSScriptRoot 'server') --hidden-import jwt --collect-submodules cryptography `
  --distpath (Join-Path $PSScriptRoot 'dist') --workpath (Join-Path $buildRoot 'work') `
  --specpath $buildRoot (Join-Path $PSScriptRoot 'pc_helper/reader_connect.py')
if ($LASTEXITCODE -ne 0) { throw 'PC helper build failed.' }
$exe = Join-Path $PSScriptRoot 'dist/Reader-ChatGPT-Connect.exe'
$check = Start-Process -FilePath $exe -ArgumentList '--self-test' -WindowStyle Hidden -PassThru -Wait
if ($check.ExitCode -ne 0) { throw 'Bundled helper self-test failed.' }
Get-FileHash -LiteralPath $exe -Algorithm SHA256
