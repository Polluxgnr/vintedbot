# Create the desktop shortcuts "Vinted Hunter" (start/restart) and "Arrêter Vinted Hunter".
$root = Split-Path -Parent $PSScriptRoot
$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell

function New-Shortcut($name, $script, $icon) {
    $lnk = $shell.CreateShortcut((Join-Path $desktop "$name.lnk"))
    $lnk.TargetPath = "powershell.exe"
    $lnk.Arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$(Join-Path $PSScriptRoot $script)`""
    $lnk.WorkingDirectory = $root
    $lnk.IconLocation = $icon
    $lnk.Save()
}

New-Shortcut "Vinted Hunter" "demarrer_bot.ps1" "$env:SystemRoot\System32\shell32.dll,137"
New-Shortcut "Arrêter Vinted Hunter" "arreter_bot.ps1" "$env:SystemRoot\System32\shell32.dll,131"
Write-Host "Raccourcis créés sur le bureau : $desktop"
