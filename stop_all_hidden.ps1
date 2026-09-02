foreach ($name in @('web.pid', 'embed.pid')) {
    $pidFile = Join-Path $PSScriptRoot "runtime\$name"
    if (-not (Test-Path $pidFile)) { continue }
    $pidToStop = Get-Content $pidFile -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pidToStop -match '^\d+$') {
        Stop-Process -Id ([int]$pidToStop) -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $pidFile -Force -ErrorAction SilentlyContinue
}
