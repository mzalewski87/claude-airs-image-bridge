# Builds dist\PortkeyImageProxy\PortkeyImageProxy.exe (service host, one-folder build).
# PyInstaller logs to stderr; in PowerShell 5.1 that must not be treated as a terminating error.
Set-Location $PSScriptRoot
cmd /c "python -m PyInstaller --noconfirm --clean --onedir --console --name PortkeyImageProxy --hidden-import win32timezone --distpath dist --workpath build --specpath build service.py 2>&1"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed ($LASTEXITCODE)" }
"built: $PSScriptRoot\dist\PortkeyImageProxy\PortkeyImageProxy.exe"
