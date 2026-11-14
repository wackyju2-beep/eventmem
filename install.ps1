#Requires -Version 5.1
<#
    eventmem - single-machine installer for Windows.

    Four steps, nothing else:
      1. make a virtualenv under <home>\eventmem\venv and install core (+ the vector arm)
      2. write daemon.cmd and start the daemon on 127.0.0.1:9123
      3. register autostart (scheduled task, or the Startup folder when not elevated)
      4. run tools\selfcheck.py and print what works and what does not

    The DSH bundle is installed from the app: Plugins -> Add plugin -> <repo>\adapters\dsh.
    -Uninstall stops the daemon and removes autostart; the database is kept.
#>
[CmdletBinding()]
param(
    [string]$RepoRoot = '',
    [string]$HomeDir = '',
    [int]$Port = 9123,
    [switch]$NoVector,
    [switch]$NoAutostart,
    [switch]$NoPythonInstall,
    [switch]$RequireToken,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$TaskName = 'eventmem-daemon'

if (-not $RepoRoot) { $RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path }
$RepoRoot = (Resolve-Path $RepoRoot).Path

if (-not $HomeDir) {
    if ($env:HERMES_HOME) { $HomeDir = $env:HERMES_HOME }
    else { $HomeDir = Join-Path $env:USERPROFILE '.hermes' }
}

$DataDir = Join-Path $HomeDir 'eventmem'
$DbPath = Join-Path $DataDir 'events.db'
$Venv = Join-Path $DataDir 'venv'
$PyExe = Join-Path $Venv 'Scripts\python.exe'
$CmdPath = Join-Path $DataDir 'daemon.cmd'
$LogPath = Join-Path $DataDir 'daemon.log'
$TokenPath = Join-Path $DataDir 'daemon.token'
$Url = "http://127.0.0.1:$Port"

# A token file from an earlier run is kept: once the daemon requires a token, dropping it
# would silently reopen every management route on the next login.
$UseToken = (-not $Uninstall) -and ($RequireToken -or (Test-Path $TokenPath))
$script:Token = ''
if ($UseToken) {
    if (-not (Test-Path $DataDir)) { New-Item -ItemType Directory -Force -Path $DataDir | Out-Null }
    if ($RequireToken -or -not (Test-Path $TokenPath)) {
        $bytes = New-Object byte[] 32
        $rng = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
        $token = -join ($bytes | ForEach-Object { $_.ToString('x2') })
        [IO.File]::WriteAllText($TokenPath, $token, [Text.Encoding]::ASCII)
        Write-Host "    generated API token at $TokenPath" -ForegroundColor Green
    }
    $script:Token = ([IO.File]::ReadAllText($TokenPath)).Trim()
}

function Write-Step([string]$Message) { Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Ok([string]$Message) { Write-Host "    $Message" -ForegroundColor Green }
function Write-Warn([string]$Message) { Write-Host "    $Message" -ForegroundColor Yellow }
function Write-Bad([string]$Message) { Write-Host "    $Message" -ForegroundColor Red }

function Find-BasePython {
    $tries = @(
        @{ Exe = 'py'; Args = @('-3.12') },
        @{ Exe = 'py'; Args = @('-3.11') },
        @{ Exe = 'py'; Args = @('-3') },
        @{ Exe = 'python'; Args = @() }
    )
    $code = "import sys; print('%d.%d' % sys.version_info[:2]); print(sys.executable)"
    foreach ($try in $tries) {
        if (-not (Get-Command $try.Exe -ErrorAction SilentlyContinue)) { continue }
        $argv = @($try.Args) + @('-c', $code)
        try { $out = @(& $try.Exe @argv 2>$null) } catch { continue }
        if ($LASTEXITCODE -ne 0 -or $out.Count -lt 2) { continue }
        $version = $null
        try { $version = [version]$out[0].Trim() } catch { continue }
        if ($version -lt [version]'3.11') { continue }
        $exe = $out[1].Trim()
        if ($exe -and (Test-Path $exe)) { return [pscustomobject]@{ Path = $exe; Version = $version } }
    }
    return $null
}

function Install-Python {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return $false }
    Write-Step 'Python 3.12 not found; installing it with winget (a few minutes)'
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & winget install --id Python.Python.3.12 -e --silent --accept-package-agreements --accept-source-agreements }
    finally { $ErrorActionPreference = $old }
    return ($LASTEXITCODE -eq 0)
}

function Get-Health {
    $headers = @{}
    if ($script:Token) { $headers['Authorization'] = "Bearer $($script:Token)" }
    try { return Invoke-RestMethod -Uri "$Url/api/health" -Headers $headers -TimeoutSec 2 -ErrorAction Stop } catch { return $null }
}

# Native tools report failures on stderr, which $ErrorActionPreference='Stop' would turn into
# a terminating error before we can read $LASTEXITCODE. Run them quietly and decide ourselves.
function Invoke-Native([string]$Exe, [string[]]$Arguments, [switch]$Silent) {
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($Silent) { & $Exe @Arguments *> $null } else { & $Exe @Arguments }
    } finally {
        $ErrorActionPreference = $old
    }
    return $LASTEXITCODE
}

function Get-OwnDaemonPids {
    Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.CommandLine -and
            $_.CommandLine.Contains('run.py') -and
            $_.CommandLine.Contains('daemon') -and
            $_.CommandLine.Contains($DbPath)
        } |
        ForEach-Object { $_.ProcessId }
}

Write-Host ''
Write-Host 'eventmem on one machine' -ForegroundColor Cyan
Write-Host "  repo: $RepoRoot"
Write-Host "  home: $HomeDir"
Write-Host ''

if ($Uninstall) {
    Invoke-Native 'schtasks' @('/delete', '/tn', $TaskName, '/f') -Silent | Out-Null
    $lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'eventmem-daemon.lnk'
    if (Test-Path $lnk) { Remove-Item $lnk -Force }
    $pids = Get-OwnDaemonPids
    foreach ($pidValue in $pids) { Stop-Process -Id $pidValue -Force -ErrorAction SilentlyContinue }
    if ($pids) { Write-Ok "stopped daemon (pid $($pids -join ', '))" }
    Write-Ok "autostart removed; data kept in $DataDir"
    exit 0
}

if (-not (Test-Path (Join-Path $RepoRoot 'core\pyproject.toml'))) {
    Write-Bad "this does not look like the eventmem repository: $RepoRoot"
    exit 1
}

$base = Find-BasePython
if (-not $base) {
    if ($NoPythonInstall) {
        Write-Bad 'Python 3.11+ is required: https://www.python.org/downloads/windows/'
        exit 1
    }
    if (-not (Install-Python)) {
        Write-Bad 'could not install Python automatically; install it from https://www.python.org/downloads/windows/'
        exit 1
    }
    $base = Find-BasePython
}
if (-not $base) {
    Write-Bad 'Python is installed but not visible yet; open a new terminal and run the installer again'
    exit 1
}
Write-Ok "python $($base.Version)  $($base.Path)"

New-Item -ItemType Directory -Force -Path $DataDir | Out-Null

if (-not (Test-Path $PyExe)) {
    Write-Step "creating the virtualenv (first run): $Venv"
    $code = Invoke-Native $base.Path @('-m', 'venv', $Venv)
    if ($code -ne 0 -or -not (Test-Path $PyExe)) {
        Write-Bad 'could not create the virtualenv'
        exit 1
    }
}

Write-Step 'installing eventmem-core (the first run downloads packages)'
$spec = Join-Path $RepoRoot 'core'
if (-not $NoVector) { $spec = $spec + '[vector]' }
$code = Invoke-Native $PyExe @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', '-e', $spec)
if ($code -ne 0) {
    Write-Bad 'pip install failed; check the network/proxy and run this script again'
    exit 1
}
$code = Invoke-Native $PyExe @('-m', 'pip', 'install', '--disable-pip-version-check', '--quiet', 'pyyaml')
if ($code -ne 0) {
    Write-Bad 'could not install PyYAML'
    exit 1
}
Write-Ok 'eventmem-core installed'

$runPy = Join-Path $RepoRoot 'adapters\hermes\run.py'
$runLine = '"' + $PyExe + '" "' + $runPy + '" daemon --db "' + $DbPath + '" --host 127.0.0.1 --port ' + $Port
if ($UseToken) {
    $runLine += ' --token-file "' + $TokenPath + '"'
}
$runLine += ' >> "' + $LogPath + '" 2>&1'
$cmdLines = @(
    '@echo off',
    'rem eventmem daemon - generated by install.ps1, re-run the installer to regenerate',
    "set HERMES_HOME=$HomeDir",
    'set PYTHONIOENCODING=utf-8'
)
# The recall block is rendered by the daemon; match the Windows display language.
if ([Globalization.CultureInfo]::CurrentCulture.TwoLetterISOLanguageName -eq 'zh') {
    $cmdLines += 'set EVENTMEM_LANG=zh'
}
$cmdLines += $runLine

# cmd.exe reads a batch file in the OEM code page, so write it that way (a profile path can
# contain non-ASCII). Set-Content has no portable encoding name across 5.1 and 7, hence .NET.
# A batch file that is being executed must not be rewritten, so only replace it while idle.
$oem = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.OEMCodePage)
$cmdBytes = $oem.GetBytes((($cmdLines -join "`r`n") + "`r`n"))
$cmdStale = $true
if (Test-Path $CmdPath) {
    $current = [IO.File]::ReadAllBytes($CmdPath)
    $cmdStale = ($current.Length -ne $cmdBytes.Length)
    if (-not $cmdStale) {
        for ($i = 0; $i -lt $cmdBytes.Length; $i++) {
            if ($current[$i] -ne $cmdBytes[$i]) { $cmdStale = $true; break }
        }
    }
}
if ($cmdStale) {
    if (@(Get-OwnDaemonPids).Count -gt 0) {
        Write-Warn 'daemon.cmd needs an update; stop the daemon (-Uninstall) and run this script again'
    } else {
        [IO.File]::WriteAllBytes($CmdPath, $cmdBytes)
    }
}

$health = Get-Health
if ($health) {
    $running = [string]$health.db
    if ($running -and $running -ne $DbPath) {
        Write-Warn "another eventmem daemon already answers on $Url"
        Write-Warn "  its db: $running"
        Write-Warn "  ours:   $DbPath"
        Write-Warn 'nothing changed; stop that daemon or choose another -Port'
        exit 1
    }
    Write-Ok "daemon already running on $Url"
} else {
    $existing = @(Get-OwnDaemonPids)
    if ($existing.Count -gt 0) {
        Write-Warn "a daemon for this database is already running (pid $($existing -join ', ')) but not on $Url"
        Write-Warn "stop it with this script's -Uninstall, or use the port it listens on"
        exit 1
    }
    Write-Step "starting the daemon on $Url"
    Start-Process -FilePath $CmdPath -WindowStyle Hidden
    for ($i = 0; $i -lt 30; $i++) {
        Start-Sleep -Seconds 1
        $health = Get-Health
        if ($health) { break }
    }
    if (-not $health) {
        Write-Bad "the daemon did not answer within 30 s; last lines of $LogPath"
        if (Test-Path $LogPath) { Get-Content $LogPath -Tail 15 | ForEach-Object { Write-Host "    $_" } }
        exit 1
    }
    Write-Ok "daemon up (schema $($health.schema)) on $Url"
}

if (-not $NoAutostart) {
    $tr = '"' + $CmdPath + '"'
    $created = Invoke-Native 'schtasks' @('/create', '/tn', $TaskName, '/tr', $tr, '/sc', 'onlogon', '/f') -Silent
    if ($created -eq 0) {
        Write-Ok "autostart: scheduled task '$TaskName' (on logon)"
    } else {
        $lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'eventmem-daemon.lnk'
        $shell = New-Object -ComObject WScript.Shell
        $shortcut = $shell.CreateShortcut($lnk)
        $shortcut.TargetPath = $CmdPath
        $shortcut.WorkingDirectory = $DataDir
        $shortcut.WindowStyle = 7
        $shortcut.Description = 'eventmem daemon'
        $shortcut.Save()
        Write-Ok "autostart: $lnk (scheduled tasks need admin, so the Startup folder is used)"
    }
}

$env:HERMES_HOME = $HomeDir
$selfcheck = Join-Path $RepoRoot 'tools\selfcheck.py'
if (Test-Path $selfcheck) {
    Write-Step 'self-check'
    if ($UseToken) {
        & $PyExe $selfcheck --url $Url --db $DbPath --token-file $TokenPath
    } else {
        & $PyExe $selfcheck --url $Url --db $DbPath
    }
}

Write-Host ''
Write-Host 'Next steps' -ForegroundColor Cyan
Write-Host "  1. In DSH: Plugins -> Add plugin, choose $(Join-Path $RepoRoot 'adapters\dsh')"
Write-Host "     then open the plugin's 连接 page and set tokenFile to $TokenPath" -ForegroundColor Yellow
Write-Host "  2. Relation edges need a decision backend: open $Url -> Backends, or Models to download a GGUF (~2.4 GB)"
Write-Host "  3. Semantic recall needs the ONNX encoder in $DataDir\models; without it recall uses FTS5 only"
Write-Host "  log: $LogPath"
Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
