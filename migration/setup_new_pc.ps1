<#
.SYNOPSIS
  Stand up the Varsany automation on a NEW PC so it matches the old one.

.DESCRIPTION
  Run from an ELEVATED Windows PowerShell on the new PC, after
    - the code is at C:\gimpTest (git clone or folder copy), and
    - the bundle made by export_bundle.ps1 is at C:\gimpTest\migration\bundle
  Modes
    -Check    read-only status report, no admin needed (also fine on the old PC, to compare)
    -DryRun   print what would change, change nothing
    (default) apply: folders, .env, template, Python packages, fonts, the daemon service
              (installed but NOT started) and rclone + the B2 sync task (created DISABLED)
    -GoLive   final cutover step: start VarsanyDaemon, enable + start VarsanyB2Sync.
              Only run it after the OLD PC's daemon is stopped AND disabled.
  Every step checks first, so re-running is safe. Secrets are never printed.
  ASCII only on purpose: Windows PowerShell 5.1 reads BOM-less files as ANSI.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File C:\gimpTest\migration\setup_new_pc.ps1 -Check
  powershell -ExecutionPolicy Bypass -File C:\gimpTest\migration\setup_new_pc.ps1
  powershell -ExecutionPolicy Bypass -File C:\gimpTest\migration\setup_new_pc.ps1 -GoLive
#>
[CmdletBinding()]
param(
    [string]$ProjectDir  = 'C:\gimpTest',
    [string]$BundleDir,
    [string]$PythonExe,
    [string]$ServiceUser = ".\$env:USERNAME",
    [string]$DaemonArgs,
    [switch]$Check,
    [switch]$DryRun,
    [switch]$GoLive,
    [switch]$SkipFonts,
    [switch]$SkipService,
    [switch]$SkipB2,
    [switch]$SkipVerify
)

$ErrorActionPreference = 'Stop'

# $PSScriptRoot can be empty inside param() defaults, so resolve the defaults here instead
$ScriptDir = $PSScriptRoot
if (-not $ScriptDir -and $MyInvocation.MyCommand.Path) { $ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $ScriptDir) { $ScriptDir = Join-Path $ProjectDir 'migration' }
if (-not $BundleDir) { $BundleDir = Join-Path $ScriptDir 'bundle' }

$ServiceName = 'VarsanyDaemon'
$TaskName    = 'VarsanyB2Sync'
$NssmExe     = 'C:\nssm\nssm.exe'
$RcloneDir   = 'C:\rclone'
$RcloneExe   = Join-Path $RcloneDir 'rclone.exe'
$Remote      = 'b2varsany'
$Bucket      = 'varsaniautomation'
$SysConf     = 'C:\Windows\system32\config\systemprofile\AppData\Roaming\rclone\rclone.conf'
$NoChange    = $Check.IsPresent -or $DryRun.IsPresent
$script:Results  = New-Object System.Collections.ArrayList
$script:LastExit = 0
$script:Py       = $null
$script:Old      = $null

# ---------------------------------------------------------------- helpers -------------
function Add-Result {
    param([string]$Area, [string]$Status, [string]$Detail = '')
    [void]$script:Results.Add([pscustomobject]@{ Area = $Area; Status = $Status; Detail = $Detail })
    $color = 'Gray'
    switch ($Status) {
        'OK'    { $color = 'Green' }
        'FIXED' { $color = 'Green' }
        'WARN'  { $color = 'Yellow' }
        'FAIL'  { $color = 'Red' }
        'TODO'  { $color = 'Cyan' }
        'DRY'   { $color = 'DarkGray' }
    }
    Write-Host ('[{0,-5}] {1,-22} {2}' -f $Status, $Area, $Detail) -ForegroundColor $color
}

function Invoke-Section([string]$Name, [scriptblock]$Body) {
    try { & $Body } catch { Add-Result $Name 'FAIL' $_.Exception.Message }
}

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    $old = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { $out = & $Exe @Arguments 2>&1 | Out-String; $script:LastExit = $LASTEXITCODE } finally { $ErrorActionPreference = $old }
    return ($out -replace "`0", '').Trim()
}

function Invoke-Nssm([string[]]$NssmArgs) {
    $out = Invoke-Native $NssmExe $NssmArgs
    if ($script:LastExit -ne 0) { throw ("nssm {0} failed: {1}" -f ($NssmArgs[0..1] -join ' '), $out) }
    return $out
}

function Test-Admin {
    return ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
}

function Test-PathSafe([string]$Path) {
    # Test-Path throws "Access is denied" under -ErrorAction Stop for folders a non-admin cannot enter
    try { return [bool](Test-Path -LiteralPath $Path -ErrorAction Stop) } catch { return $false }
}

function Read-EnvFile([string]$Path) {
    $map = @{}
    if (Test-Path -LiteralPath $Path) {
        foreach ($line in Get-Content -LiteralPath $Path) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') { $map[$Matches[1]] = $Matches[2].Trim() }
        }
    }
    return $map
}

function Read-SecretText([string]$Prompt) {
    $sec = Read-Host $Prompt -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) } finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
}

function Resolve-Python {
    if ($PythonExe -and (Test-Path -LiteralPath $PythonExe)) { return $PythonExe }
    $cands = @()
    try { $found = Invoke-Native 'py' @('-3.14', '-c', 'import sys;print(sys.executable)'); if ($script:LastExit -eq 0 -and $found) { $cands += $found.Trim() } } catch { }
    $cands += (Join-Path $env:LOCALAPPDATA 'Python\pythoncore-3.14-64\python.exe')
    $cands += (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python314\python.exe')
    $cands += 'C:\Program Files\Python314\python.exe'
    foreach ($c in $cands) { if ($c -and (Test-Path -LiteralPath $c)) { return $c } }
    return $null
}

# ---------------------------------------------------------------- sections ------------
function Test-Prereqs {
    if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir 'batch_processor.py'))) {
        Add-Result 'Project folder' 'FAIL' "batch_processor.py not found in $ProjectDir - put the code there first (git clone or copy)"
        return $false
    }
    Add-Result 'Project folder' 'OK' $ProjectDir
    if ($ProjectDir -ne 'C:\gimpTest') { Add-Result 'Project path' 'WARN' 'ps_bridge.py and the UXP plugin hardcode C:\gimpTest - use exactly that path' }
    if (-not $NoChange -and -not (Test-Admin)) {
        Add-Result 'Admin rights' 'FAIL' 'run from an elevated PowerShell (right-click > Run as administrator), or use -Check / -DryRun'
        return $false
    }

    $oldFile = Join-Path $BundleDir 'config\old_pc.json'
    if (Test-Path -LiteralPath $oldFile) { try { $script:Old = Get-Content -LiteralPath $oldFile -Raw -Encoding UTF8 | ConvertFrom-Json } catch { } }

    $tz = (Get-TimeZone).Id
    if ($script:Old -and $script:Old.timeZone -and $tz -ne $script:Old.timeZone) {
        Add-Result 'Time zone' 'WARN' ("this PC is '{0}', old PC was '{1}'  (tzutil /s ""{1}"")" -f $tz, $script:Old.timeZone)
    } else { Add-Result 'Time zone' 'OK' $tz }
    return $true
}

function Test-BundleIntegrity {
    if (-not (Test-Path -LiteralPath $BundleDir)) { Add-Result 'Bundle' 'WARN' "not found at $BundleDir (copy it over from the old PC)"; return }
    $sums = Join-Path $BundleDir 'SHA256SUMS.txt'
    if ($SkipVerify -or -not (Test-Path -LiteralPath $sums)) { Add-Result 'Bundle' 'WARN' 'present, integrity NOT verified'; return }
    $root = (Resolve-Path -LiteralPath $BundleDir).Path.TrimEnd('\')
    $bad = @(); $count = 0
    foreach ($line in Get-Content -LiteralPath $sums -Encoding UTF8) {
        if ($line -match '^([0-9A-Fa-f]{64})  (.+)$') {
            $hash = $Matches[1]; $rel = $Matches[2]
            $count++
            $f = Join-Path $root $rel
            if (-not (Test-Path -LiteralPath $f)) { $bad += "missing: $rel"; continue }
            if ((Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash -ne $hash) { $bad += "changed: $rel" }
        }
    }
    if ($bad.Count -eq 0) { Add-Result 'Bundle' 'OK' "$count files verified against SHA256SUMS.txt" }
    else { Add-Result 'Bundle' 'FAIL' ("{0} of {1} files damaged/missing, e.g. {2} - copy the bundle again" -f $bad.Count, $count, ($bad[0..([Math]::Min(2, $bad.Count - 1))] -join '; ')) }
}

function Sync-Python {
    $script:Py = Resolve-Python
    if (-not $script:Py) { Add-Result 'Python 3.14' 'FAIL' 'not found - winget install -e --id Python.Python.3.14  (then re-run)'; return }
    $ver = Invoke-Native $script:Py @('--version')
    if ($ver -match '3\.14') { Add-Result 'Python 3.14' 'OK' "$ver  ($script:Py)" } else { Add-Result 'Python 3.14' 'WARN' "$ver at $script:Py - the pinned packages were built for 3.14" }

    $lock = Join-Path $BundleDir 'config\requirements.lock.txt'
    if (-not (Test-Path -LiteralPath $lock)) { $lock = Join-Path $ProjectDir 'requirements.txt' }
    $have = @{}
    foreach ($l in ((Invoke-Native $script:Py @('-m', 'pip', 'list', '--format=freeze')) -split "`r?`n")) {
        if ($l -match '^([^=\s]+)==(\S+)$') { $have[$Matches[1].ToLower()] = $Matches[2] }
    }
    $todo = @()
    foreach ($l in Get-Content -LiteralPath $lock) {
        $t = $l.Trim()
        if (-not $t -or $t.StartsWith('#')) { continue }
        if ($t -match '^([^=<>!~\s]+)==(\S+)$') {
            $n = $Matches[1].ToLower(); $v = $Matches[2]
            if (-not $have.ContainsKey($n) -or $have[$n] -ne $v) { $todo += $t }
        } else {
            $n = ($t -replace '[<>=!~\s].*$', '').ToLower()
            if (-not $have.ContainsKey($n)) { $todo += $t }
        }
    }
    $src = Split-Path $lock -Leaf
    if ($todo.Count -eq 0) { Add-Result 'Python packages' 'OK' "all match $src"; return }
    if ($Check) { Add-Result 'Python packages' 'WARN' ("{0} missing/different vs {1}, e.g. {2}" -f $todo.Count, $src, ($todo[0..([Math]::Min(2, $todo.Count - 1))] -join ', ')); return }
    if ($DryRun) { Add-Result 'Python packages' 'DRY' "would run: pip install -r $lock ($($todo.Count) to install)"; return }
    $out = Invoke-Native $script:Py @('-m', 'pip', 'install', '--no-warn-script-location', '-r', $lock)
    if ($script:LastExit -ne 0) { Add-Result 'Python packages' 'FAIL' ('pip install failed: ' + (($out -split "`r?`n") | Select-Object -Last 3) -join ' | ') }
    else { Add-Result 'Python packages' 'FIXED' "installed from $src" }
}

function Sync-ProjectFiles {
    $missing = @()
    foreach ($d in 'jobs', 'done', 'error', 'Output', 'Temp\OrderImages', 'template') {
        if (-not (Test-Path -LiteralPath (Join-Path $ProjectDir $d))) { $missing += $d }
    }
    if ($missing.Count -eq 0) { Add-Result 'Folders' 'OK' 'jobs, done, error, Output, Temp\OrderImages, template' }
    elseif ($Check)  { Add-Result 'Folders' 'WARN' ('missing: ' + ($missing -join ', ')) }
    elseif ($DryRun) { Add-Result 'Folders' 'DRY' ('would create: ' + ($missing -join ', ')) }
    else {
        foreach ($d in $missing) { New-Item -ItemType Directory -Force -Path (Join-Path $ProjectDir $d) | Out-Null }
        Add-Result 'Folders' 'FIXED' ('created: ' + ($missing -join ', '))
    }

    $envFile   = Join-Path $ProjectDir '.env'
    $bundleEnv = Join-Path $BundleDir 'config\.env'
    if (-not (Test-Path -LiteralPath $envFile)) {
        if (-not (Test-Path -LiteralPath $bundleEnv)) { Add-Result '.env' 'FAIL' 'no .env here and none in the bundle' }
        elseif ($Check)  { Add-Result '.env' 'WARN' 'missing (the bundle has one)' }
        elseif ($DryRun) { Add-Result '.env' 'DRY' 'would copy it from the bundle' }
        else { Copy-Item -LiteralPath $bundleEnv -Destination $envFile; Add-Result '.env' 'FIXED' 'copied from the bundle' }
    }
    if (Test-Path -LiteralPath $envFile) {
        $map  = Read-EnvFile $envFile
        $need = 'DB_SERVER', 'DB_NAME', 'DB_UID', 'DB_PWD', 'VARSANY_BASE', 'USE_PHOTOSHOP_BRIDGE', 'PS_BRIDGE_DIR', 'PS_TEMPLATES_DIR'
        $lack = @($need | Where-Object { -not $map.ContainsKey($_) -or [string]::IsNullOrWhiteSpace($map[$_]) })
        if ($lack.Count -gt 0) { Add-Result '.env keys' 'FAIL' ('missing/empty: ' + ($lack -join ', ')) }
        elseif ($map['VARSANY_BASE'] -ne $ProjectDir) { Add-Result '.env keys' 'WARN' ("VARSANY_BASE is '{0}', expected '{1}'" -f $map['VARSANY_BASE'], $ProjectDir) }
        else { Add-Result '.env keys' 'OK' 'all required keys present (values not shown)' }
    }

    $tplSrc = Join-Path $BundleDir 'template'
    $tplDst = Join-Path $ProjectDir 'template'
    if (Test-Path -LiteralPath $tplSrc) {
        $srcRoot = (Resolve-Path -LiteralPath $tplSrc).Path.TrimEnd('\')
        $need = @(Get-ChildItem -LiteralPath $srcRoot -Recurse -File | Where-Object { -not (Test-Path -LiteralPath (Join-Path $tplDst $_.FullName.Substring($srcRoot.Length + 1))) })
        if ($need.Count -eq 0) { Add-Result 'template\' 'OK' 'all bundle templates present (the semi-custom jersey PSD)' }
        elseif ($Check)  { Add-Result 'template\' 'WARN' "$($need.Count) file(s) missing" }
        elseif ($DryRun) { Add-Result 'template\' 'DRY' "would copy $($need.Count) file(s)" }
        else {
            New-Item -ItemType Directory -Force -Path $tplDst | Out-Null
            & robocopy $srcRoot $tplDst /E /R:1 /W:1 /NFL /NDL /NJH /NJS /NP | Out-Null
            if ($LASTEXITCODE -ge 8) { throw "robocopy of template\ failed (exit $LASTEXITCODE)" }
            Add-Result 'template\' 'FIXED' "copied $($need.Count) file(s)"
        }
    } else { Add-Result 'template\' 'WARN' 'not in the bundle - semi-custom jersey orders will fail without it' }
}

function Sync-Fonts {
    if ($SkipFonts) { Add-Result 'Fonts' 'SKIP' ''; return }
    $src = Join-Path $BundleDir 'fonts'
    if (-not (Test-Path -LiteralPath $src)) { Add-Result 'Fonts' 'WARN' 'bundle\fonts not found'; return }
    $bundleFonts = @(Get-ChildItem -LiteralPath $src -File | Where-Object { $_.Extension -match '^\.(ttf|otf)$' })
    $have = @{}
    foreach ($d in 'C:\Windows\Fonts', (Join-Path $env:LOCALAPPDATA 'Microsoft\Windows\Fonts')) {
        if (Test-Path -LiteralPath $d) { foreach ($f in Get-ChildItem -LiteralPath $d -File -ErrorAction SilentlyContinue) { $have[$f.Name.ToLower()] = $true } }
    }
    $missing = @($bundleFonts | Where-Object { -not $have.ContainsKey($_.Name.ToLower()) })
    if ($missing.Count -eq 0) { Add-Result 'Fonts' 'OK' "all $($bundleFonts.Count) bundle fonts are installed"; return }
    if ($Check)  { Add-Result 'Fonts' 'WARN' "$($missing.Count) of $($bundleFonts.Count) bundle fonts not installed"; return }
    if ($DryRun) { Add-Result 'Fonts' 'DRY' "would run install_fonts.py for $($missing.Count) missing fonts"; return }
    if (-not $script:Py) { Add-Result 'Fonts' 'FAIL' 'needs Python first'; return }
    $log = Join-Path $ScriptDir 'install_fonts.log'
    $oldEnc = $env:PYTHONIOENCODING; $env:PYTHONIOENCODING = 'utf-8'
    try { $out = Invoke-Native $script:Py @((Join-Path $ProjectDir 'install_fonts.py'), '--source', $src) } finally { $env:PYTHONIOENCODING = $oldEnc }
    Set-Content -LiteralPath $log -Value $out -Encoding UTF8
    $left = @($bundleFonts | Where-Object { -not (Test-Path -LiteralPath (Join-Path 'C:\Windows\Fonts' $_.Name)) -and -not $have.ContainsKey($_.Name.ToLower()) }).Count
    if ($script:LastExit -ne 0 -or $left -gt 0) { Add-Result 'Fonts' 'WARN' "install_fonts.py finished with $left font(s) still missing - see $log" }
    else { Add-Result 'Fonts' 'FIXED' "installed; log: $log  (restart Photoshop so it sees them)" }
}

function Sync-Service {
    if ($SkipService) { Add-Result 'Service' 'SKIP' ''; return }
    $svc = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    $cfg = $null
    if ($script:Old) { $cfg = $script:Old.service }
    $appParams = $DaemonArgs
    if (-not $appParams -and $cfg) { $appParams = [string]$cfg.AppParameters }

    if ($svc) {
        $p = Get-ItemProperty -Path "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName\Parameters" -ErrorAction SilentlyContinue
        $detail = "$($svc.Status), $($svc.StartType); args: $($p.AppParameters)"
        if ($svc.Status -ne 'Running' -and $svc.StartType -eq 'Automatic') { Add-Result 'Service' 'WARN' "$detail   - a reboot would start it; before cutover: sc.exe config $ServiceName start= demand" }
        elseif ($appParams -and $p -and $p.AppParameters -ne $appParams) { Add-Result 'Service' 'WARN' "$detail   (old PC used: $appParams)" }
        else { Add-Result 'Service' 'OK' $detail }
        return
    }
    if (-not $appParams) { Add-Result 'Service' 'FAIL' 'no service settings in the bundle - pass -DaemonArgs "-u C:\gimpTest\batch_processor.py --daemon --date-after YYYY-MM-DD"'; return }
    if ($Check)  { Add-Result 'Service' 'WARN' "not installed (would use: $appParams)"; return }
    if (-not $script:Py) { Add-Result 'Service' 'FAIL' 'needs Python first'; return }
    if ($DryRun) { Add-Result 'Service' 'DRY' "would install $ServiceName as $ServiceUser running $script:Py $appParams (left stopped)"; return }

    if (-not (Test-Path -LiteralPath $NssmExe)) {
        $nssmSrc = Join-Path $BundleDir 'tools\nssm.exe'
        if (-not (Test-Path -LiteralPath $nssmSrc)) { Add-Result 'Service' 'FAIL' 'nssm.exe is neither at C:\nssm nor in the bundle'; return }
        New-Item -ItemType Directory -Force -Path (Split-Path $NssmExe) | Out-Null
        Copy-Item -LiteralPath $nssmSrc -Destination $NssmExe
    }
    $stdout = Join-Path $ProjectDir 'daemon.log'
    if ($cfg -and $cfg.AppStdout) { $stdout = [string]$cfg.AppStdout }
    [void](Invoke-Nssm @('install', $ServiceName, $script:Py))
    [void](Invoke-Nssm @('set', $ServiceName, 'AppParameters', $appParams))
    [void](Invoke-Nssm @('set', $ServiceName, 'AppDirectory', $ProjectDir))
    [void](Invoke-Nssm @('set', $ServiceName, 'AppStdout', $stdout))
    [void](Invoke-Nssm @('set', $ServiceName, 'AppStderr', $stdout))
    [void](Invoke-Nssm @('set', $ServiceName, 'AppExit', 'Default', 'Restart'))
    # manual start until -GoLive: an auto-start service would come up on the next reboot while the old PC still runs
    [void](Invoke-Nssm @('set', $ServiceName, 'Start', 'SERVICE_DEMAND_START'))
    $plain = Read-SecretText "Windows password for $ServiceUser (the daemon service runs as this user, like on the old PC)"
    try { [void](Invoke-Nssm @('set', $ServiceName, 'ObjectName', $ServiceUser, $plain)) } finally { $plain = $null }
    Add-Result 'Service' 'FIXED' "installed as $ServiceUser, manual start, restart-on-exit - NOT started (-GoLive sets auto-start and starts it)"
}

function Sync-B2 {
    if ($SkipB2) { Add-Result 'B2 sync' 'SKIP' ''; return }

    # rclone.exe
    if (Test-Path -LiteralPath $RcloneExe) { Add-Result 'rclone.exe' 'OK' (((Invoke-Native $RcloneExe @('version')) -split "`r?`n")[0]) }
    elseif ($Check)  { Add-Result 'rclone.exe' 'WARN' "missing at $RcloneExe" }
    elseif ($DryRun) { Add-Result 'rclone.exe' 'DRY' "would copy from the bundle (or download) to $RcloneExe" }
    else {
        New-Item -ItemType Directory -Force -Path $RcloneDir | Out-Null
        $bundled = Join-Path $BundleDir 'tools\rclone.exe'
        if (Test-Path -LiteralPath $bundled) { Copy-Item -LiteralPath $bundled -Destination $RcloneExe -Force }
        else {
            $zip = Join-Path $env:TEMP 'rclone.zip'; $tmp = Join-Path $env:TEMP 'rclone-extract'
            Invoke-WebRequest -Uri 'https://downloads.rclone.org/rclone-current-windows-amd64.zip' -OutFile $zip -UseBasicParsing
            if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
            Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force
            Copy-Item -LiteralPath (Get-ChildItem -LiteralPath $tmp -Recurse -Filter rclone.exe | Select-Object -First 1).FullName -Destination $RcloneExe -Force
        }
        Add-Result 'rclone.exe' 'FIXED' $RcloneExe
    }

    # machine PATH (so a plain "rclone" works for every account)
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    if (($machine -split ';') -contains $RcloneDir) { Add-Result 'rclone on PATH' 'OK' 'machine PATH' }
    elseif ($Check)  { Add-Result 'rclone on PATH' 'WARN' "$RcloneDir not on the machine PATH (the scheduled task uses the full path anyway)" }
    elseif ($DryRun) { Add-Result 'rclone on PATH' 'DRY' "would append $RcloneDir to the machine PATH" }
    else { [Environment]::SetEnvironmentVariable('Path', ($machine.TrimEnd(';') + ';' + $RcloneDir), 'Machine'); Add-Result 'rclone on PATH' 'FIXED' 'added to machine PATH (new shells only)' }

    # B2 credentials: the user profile copy is used by this script, the SYSTEM copy by the scheduled task
    $userConf   = Join-Path $env:APPDATA 'rclone\rclone.conf'
    $bundleConf = Join-Path $BundleDir 'config\rclone.conf'
    if (-not (Test-Path -LiteralPath $userConf)) {
        if ($Check)  { Add-Result 'rclone.conf (user)' 'WARN' 'missing' }
        elseif ($DryRun) { Add-Result 'rclone.conf (user)' 'DRY' 'would copy from the bundle, or ask for the B2 keyID + application key' }
        else {
            New-Item -ItemType Directory -Force -Path (Split-Path $userConf) | Out-Null
            if (Test-Path -LiteralPath $bundleConf) { Copy-Item -LiteralPath $bundleConf -Destination $userConf -Force; Add-Result 'rclone.conf (user)' 'FIXED' 'copied from the bundle' }
            else {
                $id  = Read-Host 'Backblaze B2 keyID (starts with 003...)'
                $key = Read-SecretText 'Backblaze B2 applicationKey (input hidden)'
                $text = "[$Remote]`r`ntype = b2`r`naccount = $id`r`nkey = $key`r`n"
                [IO.File]::WriteAllText($userConf, $text, (New-Object Text.UTF8Encoding($false)))
                $key = $null; $text = $null
                Add-Result 'rclone.conf (user)' 'FIXED' 'written from what you typed'
            }
        }
    } else { Add-Result 'rclone.conf (user)' 'OK' $userConf }

    if (Test-PathSafe $SysConf) { Add-Result 'rclone.conf (SYSTEM)' 'OK' 'present' }
    elseif ($Check)  { Add-Result 'rclone.conf (SYSTEM)' 'WARN' 'not found or not readable without admin - the scheduled task runs as SYSTEM and needs it' }
    elseif ($DryRun) { Add-Result 'rclone.conf (SYSTEM)' 'DRY' 'would copy the user config into the SYSTEM profile' }
    elseif (Test-Path -LiteralPath $userConf) {
        New-Item -ItemType Directory -Force -Path (Split-Path $SysConf) | Out-Null
        Copy-Item -LiteralPath $userConf -Destination $SysConf -Force
        Add-Result 'rclone.conf (SYSTEM)' 'FIXED' 'copied into the SYSTEM profile'
    } else { Add-Result 'rclone.conf (SYSTEM)' 'FAIL' 'no user config to copy' }

    # connectivity (lists the bucket's top-level folders only)
    if ((Test-Path -LiteralPath $RcloneExe) -and (Test-Path -LiteralPath $userConf)) {
        $out = Invoke-Native $RcloneExe @('lsd', "${Remote}:$Bucket", '--max-depth', '1', '--contimeout', '10s', '--timeout', '30s')
        if ($script:LastExit -eq 0) { Add-Result 'B2 connection' 'OK' ("bucket '{0}' reachable, {1} top-level folders" -f $Bucket, @(($out -split "`r?`n") | Where-Object { $_ }).Count) }
        else { Add-Result 'B2 connection' 'FAIL' ((($out -split "`r?`n") | Select-Object -Last 2) -join ' | ') }
    }

    # sync_to_b2.bat must call the full rclone path (a bare "rclone" is invisible to SYSTEM)
    $bat = Join-Path $ProjectDir 'sync_to_b2.bat'
    $batOk = (Test-Path -LiteralPath $bat) -and (Select-String -LiteralPath $bat -SimpleMatch $RcloneExe -Quiet)
    if ($batOk) { Add-Result 'sync_to_b2.bat' 'OK' 'calls the full rclone path' }
    elseif ($Check)  { Add-Result 'sync_to_b2.bat' 'WARN' 'missing or still calls a bare "rclone" (stale commit?)' }
    elseif ($DryRun) { Add-Result 'sync_to_b2.bat' 'DRY' 'would rewrite it with the known-good content' }
    else {
        $lines = @(
            '@echo off',
            ':loop',
            ('"{0}" copy "{1}" {2}:{3} --log-file="{4}" --log-level INFO --timeout 30s --contimeout 10s' -f $RcloneExe, (Join-Path $ProjectDir 'Output'), $Remote, $Bucket, (Join-Path $ProjectDir 'rclone_sync.log')),
            'timeout /t 5 /nobreak',
            'goto loop'
        )
        Set-Content -LiteralPath $bat -Value $lines -Encoding ASCII
        Add-Result 'sync_to_b2.bat' 'FIXED' 'rewritten with the known-good content (git will show it as modified)'
    }

    # scheduled task: created DISABLED so nothing uploads until -GoLive
    $q = Invoke-Native 'schtasks.exe' @('/query', '/tn', $TaskName)
    $exists = ($script:LastExit -eq 0) -or ($q -match 'Access is denied')
    if ($exists) { Add-Result "Task $TaskName" 'OK' 'exists' }
    elseif ($Check)  { Add-Result "Task $TaskName" 'WARN' 'not found' }
    elseif ($DryRun) { Add-Result "Task $TaskName" 'DRY' 'would create it (SYSTEM, every 1 min) and leave it DISABLED until -GoLive' }
    else {
        $out = Invoke-Native 'schtasks.exe' @('/create', '/tn', $TaskName, '/tr', $bat, '/sc', 'minute', '/mo', '1', '/ru', 'SYSTEM', '/f')
        if ($script:LastExit -ne 0) { Add-Result "Task $TaskName" 'FAIL' $out }
        else {
            [void](Invoke-Native 'schtasks.exe' @('/change', '/tn', $TaskName, '/disable'))
            Add-Result "Task $TaskName" 'FIXED' 'created, DISABLED until -GoLive'
        }
    }
}

function Test-Database {
    $map = Read-EnvFile (Join-Path $ProjectDir '.env')
    if (-not $map.ContainsKey('DB_SERVER')) { Add-Result 'Database' 'WARN' 'no DB_SERVER in .env yet'; return }
    $dbHost = ($map['DB_SERVER'] -split '[\\,]')[0]
    $tcp = Test-NetConnection -ComputerName $dbHost -Port 1433 -WarningAction SilentlyContinue
    if (-not $tcp.TcpTestSucceeded) {
        $ip = 'unknown'; try { $ip = (Invoke-RestMethod -Uri 'https://api.ipify.org' -TimeoutSec 5) } catch { }
        Add-Result 'Database (TCP)' 'FAIL' "port 1433 unreachable - ask the DB host to allow this PC's public IP ($ip)"
        return
    }
    Add-Result 'Database (TCP)' 'OK' 'port 1433 reachable'
    if (-not $script:Py) { return }
    # single quotes only inside the Python snippet: PowerShell 5.1 mangles embedded double quotes in native args
    $code = "from db import get_connection; r = get_connection().cursor().execute('SELECT 1').fetchone(); print('DB_OK' if r and r[0] == 1 else 'DB_ODD')"
    Push-Location $ProjectDir
    try { $out = Invoke-Native $script:Py @('-c', $code) } finally { Pop-Location }
    if ($out -match 'DB_OK') { Add-Result 'Database (login)' 'OK' 'SELECT 1 succeeded using .env credentials' }
    else { Add-Result 'Database (login)' 'FAIL' ((($out -split "`r?`n") | Select-Object -Last 2) -join ' | ') }
}

function Test-Software {
    $ps = Get-ChildItem 'C:\Program Files\Adobe' -Directory -Filter 'Adobe Photoshop*' -ErrorAction SilentlyContinue | Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName 'Photoshop.exe' } | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($ps) {
        $v = (Get-Item -LiteralPath $ps).VersionInfo.ProductVersion
        if ($v -like '27.*') { Add-Result 'Photoshop' 'OK' "$v ($ps)" } else { Add-Result 'Photoshop' 'WARN' "$v - the old PC runs Photoshop 2026 (27.x)" }
    } else { Add-Result 'Photoshop' 'FAIL' 'not installed - install Photoshop 2026 from Creative Cloud' }

    if (Test-Path -LiteralPath 'C:\Program Files\Adobe\Adobe UXP Developer Tools') { Add-Result 'UXP Developer Tools' 'OK' 'installed' }
    else { Add-Result 'UXP Developer Tools' 'TODO' 'install "UXP Developer Tools" from the Creative Cloud app (needed to load the plugin)' }

    $chrome = @('C:\Program Files\Google\Chrome\Application\chrome.exe', 'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe') | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if ($chrome) { Add-Result 'Chrome' 'OK' 'present (renders the premium colour fonts)' }
    else { Add-Result 'Chrome' 'WARN' 'missing - winget install -e --id Google.Chrome  (premium colour fonts fall back to Edge or grey outlines)' }

    $plugin = Join-Path $ProjectDir 'uxp-plugin\manifest.json'
    if (Test-Path -LiteralPath $plugin) {
        $m = Get-Content -LiteralPath $plugin -Raw | ConvertFrom-Json
        if ($m.manifestVersion -ge 5) { Add-Result 'Plugin manifest' 'OK' "manifestVersion $($m.manifestVersion)" }
        else { Add-Result 'Plugin manifest' 'WARN' "manifestVersion $($m.manifestVersion): stale - the old PC runs the uncommitted v5 manifest; copy the old PC's uxp-plugin folder" }
    }
}

function Invoke-GoLive {
    if (@($script:Results | Where-Object { $_.Status -eq 'FAIL' }).Count -gt 0) { Add-Result 'Go live' 'FAIL' 'fix the FAIL items above first - nothing was started'; return }
    if ($NoChange) { Add-Result 'Go live' 'DRY' 'would start the service and enable the B2 task'; return }
    Write-Host ''
    Write-Host 'Starting the daemon here while the OLD PC is still running it makes both PCs process the same orders.' -ForegroundColor Yellow
    $ans = Read-Host "Type YES to confirm the OLD PC's VarsanyDaemon is STOPPED and set to Disabled"
    if ($ans -cne 'YES') { Add-Result 'Go live' 'FAIL' 'not confirmed - nothing was started'; return }
    [void](Invoke-Nssm @('set', $ServiceName, 'Start', 'SERVICE_AUTO_START'))
    Start-Service -Name $ServiceName
    Add-Result 'Service' 'FIXED' "$ServiceName set to auto-start and started"
    if (-not $SkipB2) {
        [void](Invoke-Native 'schtasks.exe' @('/change', '/tn', $TaskName, '/enable'))
        [void](Invoke-Native 'schtasks.exe' @('/run', '/tn', $TaskName))
        Add-Result "Task $TaskName" 'FIXED' 'enabled and started'
    }
}

# ---------------------------------------------------------------- main ----------------
$mode = 'APPLY'
if ($Check) { $mode = 'CHECK (read-only)' } elseif ($DryRun) { $mode = 'DRY RUN (no changes)' } elseif ($GoLive) { $mode = 'GO LIVE' }
Write-Host ("Varsany new-PC setup - {0} - {1} - {2}" -f $mode, $env:COMPUTERNAME, (Get-Date).ToString('yyyy-MM-dd HH:mm')) -ForegroundColor Cyan

if (Test-Prereqs) {
    if ($GoLive -and -not $Check) {
        Invoke-Section 'Python 3.14'  { Sync-Python }
        Invoke-Section 'Software'     { Test-Software }
        Invoke-Section 'Database'     { Test-Database }
        Invoke-Section 'Go live'      { Invoke-GoLive }
    } else {
        Invoke-Section 'Bundle'       { Test-BundleIntegrity }
        Invoke-Section 'Python'       { Sync-Python }
        Invoke-Section 'Project'      { Sync-ProjectFiles }
        Invoke-Section 'Fonts'        { Sync-Fonts }
        Invoke-Section 'Service'      { Sync-Service }
        Invoke-Section 'B2 sync'      { Sync-B2 }
        Invoke-Section 'Database'     { Test-Database }
        Invoke-Section 'Software'     { Test-Software }
        Add-Result 'Photoshop plugin' 'TODO' 'manual: sign in, enable Developer Mode, load C:\gimpTest\uxp-plugin, open the Varsany Orders panel (MIGRATION.md step B6)'
    }
}

Write-Host ''
$fail = @($script:Results | Where-Object { $_.Status -eq 'FAIL' }).Count
$warn = @($script:Results | Where-Object { $_.Status -eq 'WARN' }).Count
$todo = @($script:Results | Where-Object { $_.Status -eq 'TODO' }).Count
$color = 'Green'
if ($warn -gt 0) { $color = 'Yellow' }
if ($fail -gt 0) { $color = 'Red' }
Write-Host ("Summary: {0} FAIL, {1} WARN, {2} manual TODO" -f $fail, $warn, $todo) -ForegroundColor $color
if ($fail -gt 0) { exit 1 }
