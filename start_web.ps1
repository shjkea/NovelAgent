$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root
python app.py
