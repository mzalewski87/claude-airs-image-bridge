# Installs/updates the PortkeyImageProxy Windows service. Must run elevated.
# Usage (elevated): install_service.ps1 -UserSid <SID allowed to start/stop the service> [-Uninstall]
param([Parameter(Mandatory)][string]$UserSid, [switch]$Uninstall)
$name = 'PortkeyImageProxy'
$dest = Join-Path $env:ProgramFiles $name
$data = Join-Path $env:ProgramData $name
$result = Join-Path $PSScriptRoot 'install_result.txt'
Start-Transcript -Path $result -Force | Out-Null
try {
    if (Get-Service $name -ErrorAction SilentlyContinue) {
        sc.exe stop $name | Out-Null
        (Get-Service $name).WaitForStatus('Stopped', '00:00:20')
        sc.exe delete $name
        Start-Sleep -Seconds 2
    }
    if ($Uninstall) {
        Remove-Item $dest -Recurse -Force -ErrorAction SilentlyContinue
        'uninstalled'
        return
    }

    # Free port 8787 from the old console proxy (python ... proxy.py), if any.
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -like '*proxy.py*' } | ForEach-Object {
        "stopping old console proxy pid $($_.ProcessId)"
        Stop-Process -Id $_.ProcessId -Force
        Stop-Process -Id $_.ParentProcessId -Force -ErrorAction SilentlyContinue
    }

    robocopy (Join-Path $PSScriptRoot 'dist\PortkeyImageProxy') $dest /MIR /NFL /NDL /NJH /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "robocopy failed ($LASTEXITCODE)" }
    icacls $dest /grant '*S-1-5-32-545:(OI)(CI)M' | Out-Null                                   # Users modify for future updates without UAC
    New-Item -ItemType Directory -Force $data | Out-Null
    icacls $data /grant '*S-1-5-19:(OI)(CI)M' /grant '*S-1-5-32-545:(OI)(CI)RX' | Out-Null   # LocalService modify, Users read

    sc.exe create $name binPath= "`"$dest\$name.exe`"" start= auto obj= 'NT AUTHORITY\LocalService' DisplayName= 'Portkey Image Proxy (Claude Desktop)'
    if ($LASTEXITCODE -ne 0) { throw "sc create failed ($LASTEXITCODE)" }
    sc.exe description $name 'Local proxy 127.0.0.1:8787 between Claude Desktop and Portkey; moves tool_result images for Gemini models. Log: %ProgramData%\PortkeyImageProxy\proxy.log'
    sc.exe failure $name reset= 86400 actions= restart/5000/restart/10000/restart/30000
    sc.exe failureflag $name 1

    # Let the user start/stop/query the service (tray app) without UAC.
    $sddl = (sc.exe sdshow $name | Where-Object { $_ }) -join ''
    $ace = "(A;;CCLCSWRPWPDTLOCRRC;;;$UserSid)"
    if ($sddl -notlike "*$UserSid*") {
        $new = if ($sddl -match 'S:') { $sddl -replace 'S:', "${ace}S:" } else { $sddl + $ace }
        sc.exe sdset $name $new
    }

    sc.exe start $name | Out-Null
    (Get-Service $name).WaitForStatus('Running', '00:00:20')
    Start-Sleep -Seconds 2
    $health = [Text.Encoding]::ASCII.GetString((Invoke-WebRequest -UseBasicParsing http://127.0.0.1:8787/__proxy_health -TimeoutSec 5).Content)
    "service status: $((Get-Service $name).Status), health: $health"
    'INSTALL OK'
} catch {
    "INSTALL FAILED: $_"
    if (-not $Uninstall) {
        # Keep Claude Desktop working: fall back to the hidden console proxy.
        $pyw = Join-Path (Split-Path (Get-Command python).Source) 'pythonw.exe'
        Start-Process $pyw -ArgumentList "`"$PSScriptRoot\proxy.py`"" -WindowStyle Hidden
        'fallback: started pythonw proxy.py'
    }
} finally {
    Stop-Transcript | Out-Null
}
