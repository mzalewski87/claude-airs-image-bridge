# Non-elevated entry point: installs deps, builds exe if missing, elevates install_service.ps1 (UAC),
# then sets up the tray icon and Startup shortcut for this user.
param([string]$Action = 'install')
$uninstall = $Action -eq 'uninstall'

if (-not $uninstall) {
    $exePath = Join-Path $PSScriptRoot 'dist\PortkeyImageProxy\PortkeyImageProxy.exe'
    if (-not (Test-Path $exePath)) {
        Write-Host 'Installing Python dependencies and building PortkeyImageProxy.exe...'
        cmd /c "python -m pip install -r `"$PSScriptRoot\requirements.txt`""
        & "$PSScriptRoot\build.ps1"
    }
}

$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$argList = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$PSScriptRoot\install_service.ps1`"", '-UserSid', $sid)
if ($uninstall) { $argList += '-Uninstall' }
Start-Process powershell.exe -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList $argList
Get-Content "$PSScriptRoot\install_result.txt" | Select-String 'INSTALL|service status|stopping|fallback|uninstalled' | ForEach-Object { $_.Line }

$startup = [Environment]::GetFolderPath('Startup')
Remove-Item (Join-Path $startup 'Portkey image proxy.lnk') -ErrorAction SilentlyContinue   # old console autostart
$trayLnk = Join-Path $startup 'Portkey image proxy tray.lnk'
# Stop the running tray and wait, otherwise the new one still sees its single-instance mutex and exits.
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*tray.py*' } | ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force
    Wait-Process -Id $_.ProcessId -Timeout 10 -ErrorAction SilentlyContinue
}
if ($uninstall) {
    Remove-Item $trayLnk -ErrorAction SilentlyContinue
    'tray autostart removed'
    return
}
$pyw = Join-Path (Split-Path (Get-Command python).Source) 'pythonw.exe'
$ws = New-Object -ComObject WScript.Shell
$s = $ws.CreateShortcut($trayLnk)
$s.TargetPath = $pyw
$s.Arguments = "`"$PSScriptRoot\tray.py`""
$s.WorkingDirectory = $PSScriptRoot
$s.Description = 'Tray icon for the Portkey Image Proxy service'
$s.Save()
Start-Process $pyw -ArgumentList "`"$PSScriptRoot\tray.py`"" -WorkingDirectory $PSScriptRoot
"tray autostart: $trayLnk (started)"
