# Stop Vinted Hunter (it restarts at the next Windows logon if autostart is installed).
$ErrorActionPreference = "SilentlyContinue"
Stop-ScheduledTask -TaskName "VintedHunter"
Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like "*vinted_hunter*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.MessageBox]::Show("Vinted Hunter est arrêté.", "Vinted Hunter") | Out-Null
