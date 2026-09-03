@echo off
rem ============================================
rem  Screen OCR monitor + driver - stop
rem ============================================
powershell -NoProfile -Command "$p = Get-CimInstance Win32_Process | Where-Object { $_.Name -match 'pythonw?\.exe' -and $_.CommandLine -match 'main\.py' }; if ($p) { $p | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }; Write-Host 'Stopped.' } else { Write-Host 'Not running.' }"
exit /b 0
