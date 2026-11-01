# Starts Vinted Hunter automatically in the background when you log in to Windows.
# - no console window (pythonw), restarts by itself if it crashes, logs to %USERPROFILE%\.vinted_hunter\bot.log
# - user-level scheduled task: no admin rights needed
# Remove it with scripts\uninstall_autostart.ps1

$ErrorActionPreference = "Stop"
$TaskName = "VintedHunter"
$Project = Split-Path -Parent $PSScriptRoot

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "Python introuvable. Installe Python 3.11+ depuis python.org puis relance." }
$pythonw = Join-Path (Split-Path $python) "pythonw.exe"
if (-not (Test-Path $pythonw)) { $pythonw = $python }

if (-not (Test-Path (Join-Path $Project ".env"))) { throw "Fichier .env manquant dans $Project (lance start.bat une fois)." }

Write-Host "Installation des dépendances..."
& $python -m pip install -q -r (Join-Path $Project "requirements.txt")

$action = New-ScheduledTaskAction -Execute $pythonw -Argument "-m vinted_hunter" -WorkingDirectory $Project
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Description "Vinted Hunter - bot Telegram de chasse aux bonnes affaires" -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 5
$state = (Get-ScheduledTask -TaskName $TaskName).State
Write-Host ""
Write-Host "OK : Vinted Hunter démarre maintenant automatiquement à chaque ouverture de session (état : $state)."
Write-Host "Journal : $env:USERPROFILE\.vinted_hunter\bot.log"
Write-Host "Pour qu'il tourne en continu, empêche la mise en veille : Paramètres > Système > Alimentation > Veille : Jamais."
