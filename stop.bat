@echo off
rem ============================================
rem  Screen OCR monitor + driver - stop
rem  Only kills python processes belonging to
rem  THIS project (path / cmdline contains it),
rem  never other apps' python (e.g. ComfyUI).
rem ============================================
set "APPDIR=%~dp0"
powershell -NoProfile -Command "$d=$env:APPDIR; $p = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'pythonw?\.exe' -and $_.CommandLine -match 'main\.py' -and ($_.CommandLine -match [regex]::Escape($d) -or $_.ExecutablePath -match [regex]::Escape($d) -or $_.CommandLine -match 'pythonw(\.exe)?\s+\"?main\.py\"?\s*$') }; if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('Stopped pid=' + $_.ProcessId) }; Write-Host 'Stopped.' } else { Write-Host 'Not running.' }"
exit /b 0
