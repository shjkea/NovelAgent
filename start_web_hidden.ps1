$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
New-Item -ItemType Directory -Force -Path "$Root\logs", "$Root\runtime" | Out-Null

$existing = Get-NetTCPConnection -LocalPort 7860 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($existing) { exit 0 }

$python = (Get-Command python.exe -ErrorAction Stop).Source
$stdout = "$Root\logs\web_stdout.log"
$stderr = "$Root\logs\web_stderr.log"
$p = Start-Process -FilePath $python -ArgumentList @('app.py') -WorkingDirectory $Root -WindowStyle Hidden -RedirectStandardOutput $stdout -RedirectStandardError $stderr -PassThru
Set-Content -Path "$Root\runtime\web.pid" -Value $p.Id -Encoding ascii
