# Stops Vinted Hunter and removes its automatic start.
$TaskName = "VintedHunter"
if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Démarrage automatique de Vinted Hunter supprimé."
} else {
    Write-Host "Aucun démarrage automatique installé."
}
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -like "*vinted_hunter*" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host "Bot arrêté (PID $($_.ProcessId))." }
