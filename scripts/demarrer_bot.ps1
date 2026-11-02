# Start (or restart) Vinted Hunter in the background, then open the bot in Telegram.
# Used by the "Vinted Hunter" desktop shortcut. Safe to run any time: only one bot instance can run.
$ErrorActionPreference = "SilentlyContinue"
$root = Split-Path -Parent $PSScriptRoot
$task = Get-ScheduledTask -TaskName "VintedHunter"

if ($task) {
    Stop-ScheduledTask -TaskName "VintedHunter"
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -like "*vinted_hunter*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Start-Sleep -Seconds 2
    Start-ScheduledTask -TaskName "VintedHunter"
} else {
    # No autostart task installed: launch directly, hidden.
    Start-Process -FilePath "pythonw.exe" -ArgumentList "-m", "vinted_hunter" -WorkingDirectory $root -WindowStyle Hidden
}

Start-Sleep -Seconds 4
$log = Join-Path $env:USERPROFILE ".vinted_hunter\bot.log"
$ok = (Test-Path $log) -and ((Get-Item $log).LastWriteTime -gt (Get-Date).AddSeconds(-30))
Add-Type -AssemblyName System.Windows.Forms
if ($ok) {
    Start-Process "https://t.me/vinted_hunter_dealz_bot"
    [System.Windows.Forms.MessageBox]::Show("Vinted Hunter tourne en arrière-plan.`nTelegram s'ouvre sur le bot.", "Vinted Hunter") | Out-Null
} else {
    [System.Windows.Forms.MessageBox]::Show("Le bot n'a pas démarré. Ouvre le journal :`n$log", "Vinted Hunter") | Out-Null
    Start-Process notepad.exe $log
}
