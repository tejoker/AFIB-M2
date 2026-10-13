# AFIB-M2 setup for a Windows PC with a Bloomberg Terminal, for the current user only (no administrator rights):
# Python 3.13, the repository in %USERPROFILE%\AFIB-M2, its .venv with the pinned packages and blpapi (a prebuilt
# package: no Visual Studio needed), then verify_setup.py. Safe to run again: it reuses what is already there.
#
# In PowerShell:
#   [Net.ServicePointManager]::SecurityProtocol = 'Tls12'; irm https://raw.githubusercontent.com/tejoker/AFIB-M2/main/tools/setup_windows.ps1 | iex
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$PythonVersion = "3.13.16"
# Not in Documents: school PCs often redirect it to OneDrive, which would also try to sync the .venv
$Repo = Join-Path $env:USERPROFILE "AFIB-M2"

function Invoke-Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Failed ($LASTEXITCODE): $Exe $($Arguments -join ' ')" }
}

function Find-Python313 {
    $local = Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"
    if (Test-Path $local) { return $local }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try {  # Windows PowerShell 5.1 turns a native command's error output into an exception under "Stop"
            $exe = & py -3.13 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) { return $exe.Trim() }
        } catch { }
    }
    return $null
}

Write-Host "== Python $PythonVersion (this user only)"
$python = Find-Python313
if (-not $python) {
    $installer = Join-Path $env:TEMP "python-$PythonVersion-amd64.exe"
    Invoke-WebRequest "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe" -OutFile $installer
    Start-Process $installer -Wait -ArgumentList @("/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_launcher=1",
                                                   "InstallLauncherAllUsers=0", "Include_test=0")
    $python = Find-Python313
    if (-not $python) { throw "Python $PythonVersion did not install; run the installer by hand from python.org" }
}
Write-Host "Using $python"

Write-Host "== Repository in $Repo"
if (Test-Path (Join-Path $Repo ".git")) {
    Invoke-Checked git @("-C", $Repo, "pull")
} elseif (Get-Command git -ErrorAction SilentlyContinue) {
    Invoke-Checked git @("clone", "https://github.com/tejoker/AFIB-M2.git", $Repo)
} elseif (-not (Test-Path $Repo)) {
    $zip = Join-Path $env:TEMP "AFIB-M2.zip"  # no git on this PC: a copy of the code (update it the same way)
    Invoke-WebRequest "https://github.com/tejoker/AFIB-M2/archive/refs/heads/main.zip" -OutFile $zip
    $unpacked = Join-Path $env:TEMP "AFIB-M2-main"
    if (Test-Path $unpacked) { Remove-Item $unpacked -Recurse -Force }
    Expand-Archive $zip -DestinationPath $env:TEMP -Force
    New-Item -ItemType Directory -Force -Path (Split-Path $Repo) | Out-Null
    Move-Item $unpacked $Repo
} else {
    Write-Host "$Repo exists without git; keeping it as it is"
}
Set-Location $Repo

Write-Host "== Virtual environment and packages (a few minutes)"
if (-not (Test-Path ".venv\Scripts\python.exe")) { Invoke-Checked $python @("-m", "venv", ".venv") }
$venv = Join-Path $Repo ".venv\Scripts\python.exe"
Invoke-Checked $venv @("-m", "pip", "install", "--upgrade", "pip")
Invoke-Checked $venv @("-m", "pip", "install", "-r", "requirements.txt")
Invoke-Checked $venv @("-m", "pip", "install", "-r", "requirements-bloomberg.txt")

Write-Host "== Check"
& $venv verify_setup.py
Write-Host ""
Write-Host "Done. Code in $Repo; run scripts with .\.venv\Scripts\python.exe from that folder."
